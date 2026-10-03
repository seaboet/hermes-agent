"""Queue CLI preserves exact addressing, profile isolation and credential secrecy."""

import sys
import types
from contextlib import closing

import pytest

from hermes_cli import queue_cmd
from hermes_state import SessionDB


def test_queue_parser_and_exact_profile_resolution(tmp_path, monkeypatch):
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", hermes_state._IMPORT_DEFAULT_DB_PATH)
    monkeypatch.setitem(sys.modules, "hermes_bootstrap", types.ModuleType("hermes_bootstrap"))
    from hermes_cli.main import _build_cli_parser, _parse_cli_args

    parser, subparsers = _build_cli_parser()
    args = _parse_cli_args(parser, subparsers, ["queue", "--session", "SID", "--message", "TEXT"])
    assert (args.session, args.message, args.func.__name__) == ("SID", "TEXT", "cmd_queue")
    assert "live/UI ID" in subparsers.choices["queue"].format_help()
    for argv in (["queue", "--message", "TEXT"], ["queue", "--session", "SID"]):
        with pytest.raises(SystemExit) as caught:
            parser.parse_args(argv)
        assert caught.value.code == 2

    homes = [tmp_path / "a", tmp_path / "b"]
    for home in homes:
        monkeypatch.setenv("HERMES_HOME", str(home))
        with closing(SessionDB()) as db:
            for sid, title in [(home.name + "-id", "same name"), ("exact-id", "other"),
                               ("name-id", "exact-id"), ("live-title", "1f508780"),
                               ("title-shadow", "duplicate-live")]:
                db.create_session(sid, "desktop")
                db.set_session_title(sid, title)
            # Older/imported stores can lack the unique-title constraint.
            db._conn.execute("DROP INDEX idx_sessions_title_unique")
            for sid in ["duplicate-1", "duplicate-2"]:
                db.create_session(sid, "desktop")
                db._conn.execute("UPDATE sessions SET title = 'duplicate' WHERE id = ?", (sid,))
            db._conn.commit()

    for home in [homes[0], homes[1], homes[0]]:
        monkeypatch.setenv("HERMES_HOME", str(home))
        assert queue_cmd.resolve_queue_session("same name", []) == home.name + "-id"
        owners = [
            {"session_id": home.name + "-id", "metadata": {"live_session_id": "1f508780"}},
            {"session_id": "live-name-owner", "metadata": {"live_session_id": "same name"}},
            {"session_id": "live-2", "metadata": {"live_session_id": "duplicate-live"}},
            {"session_id": "live-3", "metadata": {"live_session_id": "duplicate-live"}},
            {"session_id": "collision-owner", "metadata": {"live_session_id": "exact-id"}},
            {"session_id": "collision-2", "metadata": {"live_session_id": "exact-id"}},
        ]
        assert queue_cmd.resolve_queue_session("exact-id", owners) == "exact-id"
        assert queue_cmd.resolve_queue_session(home.name + "-id", owners) == home.name + "-id"
        assert queue_cmd.resolve_queue_session("1f508780", owners) == home.name + "-id"
        assert queue_cmd.resolve_queue_session("same name", owners) == "live-name-owner"
        with pytest.raises(ValueError, match="Ambiguous.*live-2.*live-3"):
            queue_cmd.resolve_queue_session("duplicate-live", owners)
        for target in ("1f50878", "1F508780"):
            with pytest.raises(ValueError, match="No session"):
                queue_cmd.resolve_queue_session(target, owners)
        for target in ("same", "a-", "missing"):
            with pytest.raises(ValueError, match="No session"):
                queue_cmd.resolve_queue_session(target, [])
        with pytest.raises(ValueError, match="Ambiguous.*duplicate-1.*duplicate-2"):
            queue_cmd.resolve_queue_session("duplicate", [])
    assert queue_cmd.resolve_queue_session("lazy", [{"session_id": "lazy"}]) == "lazy"
    # A registry durable ID wins even when other owners use it as their UI ID.
    assert queue_cmd.resolve_queue_session("lazy", [
        {"session_id": "lazy"},
        {"session_id": "other-1", "metadata": {"live_session_id": "lazy"}},
        {"session_id": "other-2", "metadata": {"live_session_id": "lazy"}},
    ]) == "lazy"


@pytest.mark.parametrize("failure", ["offline", "unsupported", "discovery", "transport", "rpc"])
def test_queue_failures_do_not_leak_credentials(tmp_path, monkeypatch, capsys, failure):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with closing(SessionDB()) as db:
        db.create_session("stored", "desktop")
        db.set_session_title("stored", "name")
    secret = "ws://127.0.0.1:1234/api/ws?token=SECRET-CANARY"
    owners = [] if failure == "offline" else [{"session_id": "stored", "metadata": {
        "live_session_id": None if failure == "unsupported" else "live"}}]
    monkeypatch.setattr(queue_cmd, "active_session_registry_snapshot", lambda **kw: owners)

    def discover(_sid):
        if failure == "discovery":
            raise ValueError(secret)
        return secret

    def submit(url, sid, text):
        assert (url, sid, text) == (secret, "live", "SECOND")
        if failure == "transport":
            raise OSError(secret)
        return {"error": {"code": 4001, "message": secret}}

    monkeypatch.setattr(queue_cmd, "discover_attach_url", discover)
    monkeypatch.setattr(queue_cmd, "submit_queued_prompt", submit)
    assert queue_cmd.queue_message("name", "SECOND") == 1
    out = capsys.readouterr()
    assert not out.out
    assert "SECRET-CANARY" not in out.err and "ws://" not in out.err
    if failure == "offline":
        assert "not live" in out.err and "hermes --resume stored" in out.err

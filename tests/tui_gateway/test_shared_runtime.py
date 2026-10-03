"""The local owner handshake grants attachment only for its own live lease."""

from urllib.parse import urlsplit

import httpx
import pytest
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from hermes_cli.active_sessions import active_session_registry_snapshot
from hermes_cli.shared_session_attach import discover_attach_url
from hermes_state import SessionDB
from tui_gateway import server, shared_runtime


def test_owner_handshake_profile_and_lease_fences(tmp_path, monkeypatch):
    from tests.tui_gateway.test_queued_prompt_persistence import _desktop_session, _busy
    from agent import secret_scope
    from hermes_cli.queue_cmd import queue_message

    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)

    shared_runtime.start_shared_runtime()
    records = []
    try:
        for name in ("a", "b"):
            home = tmp_path / name
            monkeypatch.setenv("HERMES_HOME", str(home))
            db = SessionDB(db_path=home / "state.db")
            sid, key = _desktop_session(monkeypatch, db)
            session = server._sessions[sid]
            session["profile_home"] = str(home)
            with server._session_profile_runtime_scope(session):
                server._ensure_session_db_row(session)
                assert server._ensure_active_session_slot(sid, session) is None
            _busy(session)
            records.append((home, db, sid, key, session["active_session_lease"]))
        for home, db, sid, key, lease in (records[0], records[1], records[0]):
            monkeypatch.setenv("HERMES_HOME", str(home))
            owners = active_session_registry_snapshot(home)
            assert [o["session_id"] for o in owners] == [key]
            assert owners[0]["metadata"]["shared_runtime_url"] == shared_runtime.attach_origin()
            url = discover_attach_url(key)
            assert urlsplit(url).path == "/api/ws"
            with pytest.raises(InvalidStatus):
                with connect(url, proxy=None, origin="https://example.com"):
                    pytest.fail("browser-origin attachment was accepted")
            assert discover_attach_url(records[1 if home == records[0][0] else 0][3]) is None
            params = {"session_id": key, "lease_id": lease.lease_id, "profile_home": str(home.resolve())}
            with httpx.Client(trust_env=False) as client:
                for field in params:
                    response = client.get(shared_runtime.attach_origin() + "/api/session-attach",
                                          params={**params, field: "wrong"})
                    assert response.status_code == 409
                    assert "websocket_url" not in response.text
                for headers in ({"Origin": "https://example.com"}, {"Host": "example.com"}):
                    response = client.get(shared_runtime.attach_origin() + "/api/session-attach",
                                          params=params, headers=headers)
                    assert response.status_code == 403
                server._sessions[sid]["_closing"] = True
                assert client.get(shared_runtime.attach_origin() + "/api/session-attach",
                                  params=params).status_code == 409
                server._sessions[sid].pop("_closing")
            assert queue_message(key, "profile-" + home.name) == 0
            assert all("profile-" + home.name in r["content"] for r in db.get_messages(key))
            assert active_session_registry_snapshot(home)[0]["lease_id"] == lease.lease_id
    finally:
        for home, db, sid, key, lease in records:
            server._sessions.pop(sid, None)
            lease.release()
            db.close()
        shared_runtime.stop_shared_runtime()
    assert shared_runtime.attach_origin() is None


@pytest.mark.parametrize("options", [{}, {"origin": "https://example.com"}])
def test_local_websocket_requires_credential_and_native_origin(options):
    shared_runtime.start_shared_runtime()
    try:
        url = shared_runtime.attach_origin().replace("http:", "ws:") + "/api/ws?token=wrong"
        with pytest.raises(InvalidStatus):
            with connect(url, proxy=None, **options):
                pytest.fail("unauthorized socket was accepted")
    finally:
        shared_runtime.stop_shared_runtime()

from __future__ import annotations

import plistlib
import sys
import textwrap
from pathlib import Path

import pytest

from hermes_cli import mcp_catalog
from hermes_cli.mcp_catalog import CatalogError, _parse_manifest
from hermes_platform.resolver.availability import Availability, availability, version_at_least

BASE = textwrap.dedent("""
    manifest_version: 1
    name: thing-mcp
    description: Fronts the Thing desktop app.
    transport: { type: http, url: "http://127.0.0.1:13508/mcp" }
    auth: { type: none }
""")


def _manifest(tmp_path: Path, extra: str) -> Path:
    p = tmp_path / "thing-mcp" / "manifest.yaml"
    p.parent.mkdir(parents=True)
    p.write_text(BASE + textwrap.dedent(extra), encoding="utf-8")
    return p


def _bundle(tmp_path: Path, version: str) -> Path:
    app = tmp_path / "Applications" / "Thing.app"
    (app / "Contents").mkdir(parents=True)
    with open(app / "Contents" / "Info.plist", "wb") as fh:
        plistlib.dump({"CFBundleShortVersionString": version}, fh)
    return app


def test_app_block_parses_into_one_appdef_per_os(tmp_path):
    entry = _parse_manifest(_manifest(tmp_path, """
        app:
          darwin: { presence: bundle, location: /Applications/Thing.app, version: { kind: plist } }
          win32:
            presence: executable
            location: "%ProgramFiles%/Thing/thing.exe"
            version: { kind: uninstall_registry, display_name_prefix: Thing }
            liveness: { kind: server_json, path: "%LOCALAPPDATA%/Thing/server.json", endpoint_path: /rpc }
        requires: { app: true, min_version: "2.3.0" }
    """))
    assert entry.app is not None and set(entry.app.per_os) == {"darwin", "win32"}
    mac, win = entry.app_for("darwin"), entry.app_for("win32")
    assert mac is not None and win is not None
    assert mac.presence == "bundle" and mac.version_kind == "plist" and mac.liveness_kind == "none"
    assert win.version_arg == "Thing" and win.endpoint_path == "/rpc" and win.liveness_pid_key == "pid"
    assert entry.requires_app is True and entry.min_version == "2.3.0"


@pytest.mark.parametrize("extra, message", [
    ("requires: { app: true }", "no 'app' block"),
    ("app: { linux: { presence: executable, location: /x, version: { kind: plist } } }", "only valid under app.darwin"),
    ("app: { win32: { presence: executable, location: /x, version: { kind: uninstall_registry } } }", "display_name_prefix"),
    ("app: { freebsd: { presence: executable, location: /x } }", "unknown OS keys"),
    ("app: { win32: { presence: executable, location: /x } }\nrequires: { min_version: '1.0' }", "needs requires.app"),
    ("app: { win32: { presence: executable, location: /x } }\nrequires: { app: true, min_version: latest }", "dotted numeric"),
    ("suggest: { keywords: [thing], requires_app: true }", "moved to top-level 'requires"),
    ("app: { win32: { presence: executable, location: 'C:/x/y.exe' } }\nrequires: { app: true, min_version: '1.0' }", "needs a version source"),
    ("app: { darwin: { presence: bundle, location: ../Up/Thing.app } }", "must be absolute"),
    ("app: { darwin: { presence: bundle, location: Relative/Thing.app } }", "must be absolute"),
    ("app: { darwin: { presence: bundle, location: 'https://example.test/Thing.app' } }", "must be absolute"),
    ("app: { win32: { presence: executable, location: 'Thing/thing.exe' } }", "must be absolute"),
])
def test_invalid_blocks_name_the_rule(tmp_path, extra, message):
    with pytest.raises(CatalogError, match=message):
        _parse_manifest(_manifest(tmp_path, extra))


def test_every_shipped_manifest_still_parses():
    entries = mcp_catalog.list_catalog()
    assert entries and not mcp_catalog.catalog_diagnostics()
    for entry in entries:
        assert isinstance(entry.requires_app, bool)
        if entry.requires_app:
            assert entry.app is not None


def test_availability_no_requirements_does_no_io(tmp_path):
    entry = _parse_manifest(_manifest(tmp_path, ""))
    result = availability(entry, os_family="freebsd")
    assert result.state == "no_requirements" and result.offerable


def test_availability_unsupported_os_when_no_block_for_host(tmp_path):
    entry = _parse_manifest(_manifest(tmp_path, """
        app: { win32: { presence: executable, location: /x } }
        requires: { app: true }
    """))
    assert availability(entry, os_family="darwin").state == "unsupported_os"


def test_availability_missing_then_present_then_version_gate(tmp_path, monkeypatch):
    entry = _parse_manifest(_manifest(tmp_path, f"""
        app: {{ darwin: {{ presence: bundle, location: {tmp_path / 'Applications' / 'Thing.app'}, version: {{ kind: plist }} }} }}
        requires: {{ app: true, min_version: "2.3.0" }}
    """))
    missing = availability(entry, os_family="darwin")
    assert missing.state == "missing_app" and str(tmp_path) in (missing.path or "")
    _bundle(tmp_path, "2.2.9")
    old = availability(entry, os_family="darwin")
    assert old.state == "version_too_old" and old.version == "2.2.9" and old.min_version == "2.3.0"
    with open(tmp_path / "Applications" / "Thing.app" / "Contents" / "Info.plist", "wb") as fh:
        plistlib.dump({"CFBundleShortVersionString": "2.3.0"}, fh)
    ok = availability(entry, os_family="darwin")
    assert ok.state == "available" and ok.version == "2.3.0" and ok.offerable


def test_availability_is_not_a_boolean():
    with pytest.raises(TypeError):
        bool(Availability("available"))


@pytest.mark.parametrize("found, minimum, expected", [
    ("2.3.0", "2.3.0", True), ("2.3.0.12594", "2.3.0", True), ("2.2.9", "2.3.0", False),
    ("11.0.9.535", "11", True), ("1.10", "1.9", True), ("10.96.30.42", "10.96.31", False),
])
def test_version_at_least(found, minimum, expected):
    assert version_at_least(found, minimum) is expected


def test_catalog_payload_carries_availability_and_no_detection_fields(tmp_path):
    entry = _parse_manifest(_manifest(tmp_path, """
        app: { win32: { presence: executable, location: /x } }
        requires: { app: true }
        suggest: { keywords: [thing], applications: [Thing] }
    """))
    payload = mcp_catalog.catalog_entry_payload(entry, installed=False, enabled=False)
    assert payload["requires_app"] is True
    assert payload["availability"]["state"] == ("missing_app" if sys.platform == "win32" else "unsupported_os")
    assert "detected_apps" not in payload and "requires_app" not in payload["suggest"]
    assert payload["required_env"] == [] and payload["requires"] == []


def test_check_fn_gate_matches_the_endpoint_not_the_name(tmp_path, monkeypatch):
    from hermes_cli import mcp_config
    from tools import mcp_tool_handlers

    entry = _parse_manifest(_manifest(tmp_path, """
        app: { win32: { presence: executable, location: 'C:/definitely/absent.exe' } }
        requires: { app: true }
    """))
    monkeypatch.setattr(mcp_catalog, "get_entry", lambda name: entry if name == "thing-mcp" else None)
    servers = {"thing-mcp": {"transport": "http", "url": "http://127.0.0.1:13508/mcp"}}
    monkeypatch.setattr(mcp_config, "_get_mcp_servers", lambda config=None: servers)
    gate = mcp_tool_handlers._catalog_app_offerable("thing-mcp")
    assert gate is False and type(gate) is bool
    # Same name, the user's own endpoint: the manifest's gate does not apply.
    servers["thing-mcp"] = {"transport": "http", "url": "http://127.0.0.1:9999/other"}
    assert mcp_tool_handlers._catalog_app_offerable("thing-mcp") is True
    assert mcp_tool_handlers._catalog_app_offerable("hand-added-server") is True


def test_list_catalog_reparses_only_when_a_manifest_changes(tmp_path, monkeypatch):
    root = tmp_path / "optional-mcps"
    _manifest(root, "")
    monkeypatch.setattr(mcp_catalog, "_catalog_root", lambda: root)
    mcp_catalog._CATALOG_CACHE = None
    calls = []
    real = mcp_catalog._parse_manifest
    monkeypatch.setattr(mcp_catalog, "_parse_manifest", lambda p: calls.append(p) or real(p))
    assert len(mcp_catalog.list_catalog()) == 1 and len(calls) == 1
    assert len(mcp_catalog.list_catalog()) == 1 and len(calls) == 1
    manifest = root / "thing-mcp" / "manifest.yaml"
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\npost_install: changed\n", encoding="utf-8")
    assert mcp_catalog.list_catalog()[0].post_install == "changed" and len(calls) == 2
    mcp_catalog._CATALOG_CACHE = None


def test_skill_requires_apps_gate(tmp_path, monkeypatch):
    from agent import skill_utils

    entry = _parse_manifest(_manifest(tmp_path, f"""
        app: {{ darwin: {{ presence: bundle, location: {tmp_path / 'Applications' / 'Thing.app'} }} }}
        requires: {{ app: true }}
    """))
    monkeypatch.setattr(mcp_catalog, "get_entry", lambda name: entry if name == "thing-mcp" else None)
    assert skill_utils.skill_matches_apps({}) is True
    assert skill_utils.skill_matches_apps({"requires_apps": ["unknown"]}) is False
    if sys.platform == "darwin":
        assert skill_utils.skill_matches_apps({"requires_apps": ["thing-mcp"]}) is False
        _bundle(tmp_path, "1.0")
        assert skill_utils.skill_matches_apps({"requires_apps": ["thing-mcp"]}) is True

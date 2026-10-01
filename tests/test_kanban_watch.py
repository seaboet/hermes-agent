"""Behavior checks for the standalone, read-only Kanban terminal watcher."""

import importlib.util
import io
import os
from pathlib import Path
import sqlite3
import subprocess
import sys


SCRIPT = Path(__file__).resolve().parents[1] / "hermes-kanban-watch.py"


def load_watcher():
    spec = importlib.util.spec_from_file_location("hermes_kanban_watch", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_status_and_assignee_transitions_are_brief():
    watcher = load_watcher()
    old = {("alpha", "t_1"): {"id": "t_1", "status": "ready", "assignee": None},
           ("beta", "t_2"): {"id": "t_2", "status": "running", "assignee": "kite"}}
    new = {("alpha", "t_1"): {"id": "t_1", "status": "running", "assignee": "developer"},
           ("beta", "t_2"): {"id": "t_2", "status": "done", "assignee": "kite"}}
    lines = watcher.changes(old, new, {"alpha", "beta"})
    assert any("ready → running" in line for line in lines)
    assert any("running → done" in line for line in lines)
    assert any("unassigned → developer" in line for line in lines)
    assert watcher.changes(old, {}, {"alpha"}) == ["➖ t_1 · disappeared · alpha"]


def test_board_connection_closes_after_each_read(tmp_path, monkeypatch):
    watcher = load_watcher()
    db_path = tmp_path / "board.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE tasks (id TEXT, title TEXT, status TEXT, assignee TEXT, "
                     "priority INTEGER, completed_at INTEGER, created_at INTEGER)")
    original_connect = sqlite3.connect
    closed = []

    class TrackedConnection:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.conn.__exit__(*args)

        def close(self):
            closed.append(True)
            self.conn.close()

    monkeypatch.setattr(watcher.sqlite3, "connect", lambda *args, **kwargs:
                        TrackedConnection(original_connect(*args, **kwargs)))
    assert watcher.read_board(str(db_path)) == []
    assert closed == [True]


def test_watch_redraw_stays_on_alternate_screen(monkeypatch):
    watcher = load_watcher()

    class Terminal(io.StringIO):
        def isatty(self):
            return True

    terminal = Terminal()
    monkeypatch.setattr(watcher.sys, "stdout", terminal)
    monkeypatch.setattr(watcher, "fetch", lambda *_: (["alpha"], {"alpha": []}, {}))
    monkeypatch.setattr(watcher.shutil, "get_terminal_size", lambda **_: os.terminal_size((52, 24)))

    def interrupt(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(watcher.time, "sleep", interrupt)
    assert watcher.main(["--interval", "0.1"]) == 0
    output = terminal.getvalue()
    assert output.startswith("\033[?1049h\033[?25l")
    assert output.endswith("\033[?25h\033[?1049l")
    assert "Ctrl+C quit\n\033[?25h" not in output

    redirected = io.StringIO()
    monkeypatch.setattr(watcher.sys, "stdout", redirected)
    assert watcher.main([]) == 0
    assert redirected.getvalue().count("HERMES KANBAN WATCH") == 1
    assert "\033[" not in redirected.getvalue()


def test_real_process_lists_multiple_boards_and_isolates_board_failure(tmp_path):
    command_log = tmp_path / "commands"
    db_path = tmp_path / "alpha.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE tasks (id TEXT, title TEXT, status TEXT, assignee TEXT, "
                     "priority INTEGER, completed_at INTEGER, created_at INTEGER)")
        conn.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?)",
                     ("t_1", "A long task title", "running", "developer", 0, None, 1))
    hermes = tmp_path / "hermes"
    hermes.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "with Path(os.environ['COMMAND_LOG']).open('a') as log: log.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:] == ['kanban', 'boards', 'list', '--json']:\n"
        " print(json.dumps([{'slug':'alpha','archived':False,'db_path':os.environ['ALPHA_DB']},"
        "{'slug':'beta','archived':False,'db_path':os.environ['BETA_DB']},"
        "{'slug':'old','archived':True,'db_path':os.environ['BETA_DB']}]))\n"
    )
    hermes.chmod(0o755)
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ.get("PATH", ""),
               COMMAND_LOG=str(command_log), ALPHA_DB=str(db_path), BETA_DB=str(tmp_path / "missing.db"),
               COLUMNS="52")
    result = subprocess.run([sys.executable, str(SCRIPT), "--once", "--no-color"],
                            capture_output=True, text=True, env=env, check=True)
    assert "2 boards" in result.stdout
    assert "alpha" in result.stdout and "A long task title" in result.stdout
    assert "beta · unable to open database file · retrying" in result.stdout
    commands = command_log.read_text().splitlines()
    assert commands == ["kanban boards list --json"]
    watcher = load_watcher()
    for line in result.stdout.splitlines():
        assert watcher.width(line) <= 52

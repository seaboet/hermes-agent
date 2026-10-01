#!/usr/bin/env python3
"""Small, read-only terminal view of all active Hermes Kanban boards."""

from __future__ import annotations

import argparse
from collections import deque
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback
import unicodedata


STATUS = {
    "running": ("🚀", "RUNNING", "\033[32m"),
    "ready": ("🟡", "READY", "\033[33m"),
    "blocked": ("🔴", "BLOCKED", "\033[31m"),
    "review": ("🟣", "REVIEW", "\033[35m"),
    "triage": ("🧭", "TRIAGE", "\033[36m"),
    "todo": ("📋", "TODO", "\033[36m"),
    "scheduled": ("🗓️", "SCHEDULED", "\033[36m"),
    "done": ("✅", "RECENTLY DONE", "\033[32m"),
}
ASSIGNEE_ICONS = {"developer": "👨‍💻", "kite": "🪁", "vera": "🧪", "theo": "🧠", "operator": "🧑‍💻"}
CONTROL = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))|[\x00-\x1f\x7f]")


def clean(value: object) -> str:
    return CONTROL.sub(" ", str(value or "")).strip()


def width(value: str) -> int:
    return sum(0 if unicodedata.combining(c) or c in "\ufe0f\u200d" else
               2 if unicodedata.east_asian_width(c) in "WF" or ord(c) >= 0x1F000 else 1
               for c in value)


def fit(value: str, columns: int) -> str:
    if columns <= 0:
        return ""
    if width(value) <= columns:
        return value
    used = 0
    out = []
    for char in value:
        char_width = width(char)
        if used + char_width > columns - 1:
            break
        out.append(char)
        used += char_width
    return "".join(out).rstrip() + "…"


def cli_json(command: list[str], debug: bool = False) -> list[dict]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=30)
        if result.returncode:
            raise RuntimeError(f"command failed (exit {result.returncode})")
        data = json.loads(result.stdout)
        if not isinstance(data, list):
            raise ValueError("unexpected JSON shape")
        return data
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError, RuntimeError):
        if debug:
            traceback.print_exc()
        raise


def read_board(db_path: str) -> list[dict]:
    """Read the canonical DB named by the board registry without opening it for writes."""
    uri = Path(db_path).as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=1)) as conn:
        conn.row_factory = sqlite3.Row
        conn.text_factory = lambda data: data.decode("utf-8", errors="replace")
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            "SELECT id, title, status, assignee, priority, completed_at "
            "FROM tasks WHERE status != 'archived' ORDER BY priority, created_at"
        ).fetchall()
        return [dict(row) for row in rows]


def fetch(only_boards: list[str], debug: bool = False) -> tuple[list[str], dict[str, list[dict]], dict[str, str]]:
    boards = cli_json(["hermes", "kanban", "boards", "list", "--json"], debug)
    slugs = [b["slug"] for b in boards if not b.get("archived") and
             (not only_boards or b["slug"] in only_boards)]
    if only_boards:
        missing = set(only_boards) - set(slugs)
        if missing:
            raise ValueError("unknown or archived board: " + ", ".join(sorted(missing)))
    tasks: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}
    for board in boards:
        slug = board["slug"]
        if slug not in slugs:
            continue
        try:
            tasks[slug] = read_board(board["db_path"])
        except (OSError, sqlite3.Error, ValueError) as exc:
            if debug:
                traceback.print_exc()
            errors[slug] = clean(str(exc)) or type(exc).__name__
    return slugs, tasks, errors


def snapshot(tasks: dict[str, list[dict]]) -> dict[tuple[str, str], dict]:
    return {(board, str(task["id"])): task for board, rows in tasks.items() for task in rows}


def changes(previous: dict[tuple[str, str], dict], current: dict[tuple[str, str], dict],
            successful_boards: set[str]) -> list[str]:
    lines = []
    for key, task in current.items():
        board, task_id = key
        old = previous.get(key)
        if old is None:
            lines.append(f"➕ {task_id} · new · {board}")
            continue
        if old.get("status") != task.get("status"):
            mark = "✅" if task.get("status") == "done" else "🔄"
            lines.append(f"{mark} {task_id} · {old.get('status')} → {task.get('status')}")
        if old.get("assignee") != task.get("assignee"):
            lines.append(f"👤 {task_id} · {old.get('assignee') or 'unassigned'} → {task.get('assignee') or 'unassigned'}")
    for (board, task_id) in previous.keys() - current.keys():
        if board in successful_boards:
            lines.append(f"➖ {task_id} · disappeared · {board}")
    return [clean(line) for line in lines]


def render(slugs: list[str], tasks: dict[str, list[dict]], errors: dict[str, str],
           recent: deque[str], done_limit: int, columns: int, height: int, interval: float,
           color: bool) -> str:
    columns = max(columns, 20)
    compact = columns < 70
    all_tasks = [(board, task) for board in slugs for task in tasks.get(board, [])]
    active = sum(task.get("status") not in ("done", "archived") for _, task in all_tasks)
    heading = fit(" 🛰️  HERMES KANBAN WATCH", columns - 2)
    summary = fit(f" {datetime.now():%m-%d %H:%M:%S}  ·  {len(slugs)} boards  ·  {active} active", columns - 2)
    lines = ["╭" + "─" * (columns - 2) + "╮",
             "│" + heading + " " * (columns - 2 - width(heading)) + "│",
             "│" + summary + " " * (columns - 2 - width(summary)) + "│",
             "╰" + "─" * (columns - 2) + "╯"]
    for board in slugs:
        if board in errors:
            lines.append(fit(f"⚠️  {board} · {errors[board]} · retrying…", columns))
    if errors:
        lines.append("")

    def section(status: str, rows: list[tuple[str, dict]]) -> None:
        if not rows:
            return
        icon, label, tint = STATUS.get(status, ("•", status.upper(), "\033[36m"))
        heading = fit(f"{icon} {label} · {len(rows)}", columns)
        lines.append(f"{tint}{heading}\033[0m" if color else heading)
        if not compact:
            lines.append("")
        for board, task in rows:
            task_id = clean(task.get("id"))
            title = clean(task.get("title"))
            assignee = clean(task.get("assignee")) or "unassigned"
            who = f"{ASSIGNEE_ICONS.get(assignee, '🤖')} {assignee}" if assignee != "unassigned" else "○ unassigned"
            priority = task.get("priority")
            priority_text = f"  ·  P{priority}" if priority not in (None, 0) else ""
            if compact:
                lines.extend([fit(f"  {board} · {title}", columns),
                              fit(f"    {who} · {task_id}{priority_text}", columns)])
            else:
                lines.extend([fit(f"  {board} · {title}{priority_text}", columns),
                              fit(f"    {who} · {task_id}", columns), ""])

    ordered = ["running", "ready", "blocked", "review", "triage", "todo", "scheduled"]
    extra = sorted({task.get("status", "unknown") for _, task in all_tasks} - set(ordered) - {"done", "archived"})
    for status in ordered + extra:
        section(status, [(board, task) for board, task in all_tasks if task.get("status") == status])
    done = [(board, task) for board, task in all_tasks if task.get("status") == "done"]
    done.sort(key=lambda pair: pair[1].get("completed_at") or 0, reverse=True)
    if compact:
        reserved = 2 + (1 + len(recent) if recent else 0)
        done_limit = min(done_limit, max(0, (height - len(lines) - reserved - 1) // 2))
    section("done", done[:done_limit])
    if recent:
        lines.append(fit("✨ CHANGES", columns))
        if not compact:
            lines.append("")
        lines.extend(fit(f"  {line}", columns) for line in recent)
        if not compact:
            lines.append("")
    if not active and not done and not errors:
        lines.extend(["  💤 No tasks to show", ""])
    lines.extend(["─" * columns, fit(f"↻ refresh {interval:g}s  ·  Ctrl+C quit", columns)])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Watch all active Hermes Kanban boards in your terminal")
    parser.add_argument("--interval", type=float, default=2, help="seconds between refreshes (default: 2)")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--done-limit", type=int, default=5)
    parser.add_argument("--board", action="append", default=[], metavar="SLUG")
    parser.add_argument("--all", action="store_true", help="show all active boards (the default)")
    parser.add_argument("--once", action="store_true", help="print one frame and exit")
    parser.add_argument("--debug", action="store_true", help="print error tracebacks to stderr")
    args = parser.parse_args(argv)
    if args.interval <= 0 or args.done_limit < 0:
        parser.error("--interval must be positive and --done-limit cannot be negative")
    tty = sys.stdout.isatty()
    watching = tty and not args.once
    previous: dict[tuple[str, str], dict] = {}
    recent: deque[tuple[str, int]] = deque(maxlen=4)
    first = True
    failure = None
    try:
        if watching:
            sys.stdout.write("\033[?1049h\033[?25l")
            sys.stdout.flush()
        while True:
            started = time.monotonic()
            try:
                slugs, tasks, errors = fetch(args.board, args.debug)
            except Exception as exc:
                failure = exc
                break
            current = snapshot(tasks)
            recent = deque(((line, cycles - 1) for line, cycles in recent if cycles > 1), maxlen=4)
            if not first:
                for line in changes(previous, current, set(tasks) | (set(previous_board for previous_board, _ in previous) - set(slugs))):
                    recent.append((line, 3))
            previous = {key: value for key, value in previous.items() if key[0] in errors}
            previous.update(current)
            first = False
            size = shutil.get_terminal_size(fallback=(80, 24))
            frame = render(slugs, tasks, errors, deque((line for line, _ in recent)), args.done_limit,
                           size.columns, size.lines, args.interval,
                           tty and not args.no_color)
            if watching:
                sys.stdout.write("\033[H\033[2J")
            sys.stdout.write(frame.rstrip("\n") if watching else frame)
            sys.stdout.flush()
            if not watching:
                break
            time.sleep(max(0, args.interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        pass
    finally:
        if watching:
            sys.stdout.write("\033[?25h\033[?1049l")
            sys.stdout.flush()
    if failure is not None:
        print("💥 Hermes CLI unavailable\n   command: hermes kanban boards list --json\n   " + clean(failure), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

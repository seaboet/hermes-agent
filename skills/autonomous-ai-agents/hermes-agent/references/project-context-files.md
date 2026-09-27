# Project Context Files

Hermes injects project-level instructions into the system prompt by reading context files from the working directory. The discovery order is **first non-empty context type wins** — one type is loaded per session, but that type may contribute more than one file.

| File (in priority order) | Discovery | Use when |
|---|---|---|
| `.hermes.md` / `HERMES.md` | Nearest non-empty match walking parents up to the git root | You want Hermes-specific project rules |
| `AGENTS.override.md` / `AGENTS.md` / `agents.md` | In a Git repo, merge the git-root-to-cwd directory chain; outside a repo, cwd only. First non-empty name wins per directory. | You want portable project instructions, with optional local overrides |
| `CLAUDE.md` / `claude.md` | Cwd only | Same as AGENTS.md, Claude-flavored |
| `.cursorrules` / `.cursor/rules/*.mdc` | Cwd only | Migrating from Cursor |

`SOUL.md` (in `$HERMES_HOME`) is independent and always loaded when present — it sets the agent's identity, not project rules.

### Pick the right one

- **Use `.hermes.md`** when you want Hermes-specific behavior that lives above the cwd (root + subtree), or when you want rules to inherit from a parent directory. The parent walk stops at the git root, so a home-level `.hermes.md` won't leak into every project (a git repo's root is the boundary).
- **Use `AGENTS.md`** when the same project will also be worked on by other agents (Codex, Claude Code, OpenCode). In a Git repository, Hermes merges files from the git root through the working directory; deeper files take precedence. A non-empty `AGENTS.override.md` wins over `AGENTS.md` in the same directory. Other tools may discover these files differently. Hermes also discovers nested context when tools access subdirectories during the session.
- **Don't put project rules in `~/.hermes/AGENTS.md`** (or any other home-level location). When Hermes runs with that directory as cwd, the file loads — but only for that one directory. For cross-project context, use `SOUL.md` (in `$HERMES_HOME`, identity-only) or install a skill via `hermes skills install`.

### Size and truncation

At startup, `context_file_max_chars` takes precedence when configured; otherwise the limit scales with the model context window, from 20,000 to 500,000 characters. Oversized files are head/tail truncated. Progressively discovered subdirectory hints have a separate per-file ceiling in `agent/subdirectory_hints.py`, not the startup limit. Keep rules concise and move reusable procedures into skills.

### Security

All context files pass through the threat-pattern scanner before reaching the system prompt. Patterns matching prompt injection or promptware are replaced with a `[BLOCKED: ...]` placeholder. This means an `AGENTS.md` containing obvious injection attempts won't reach the model — the scanner blocks the content, not the file, so the rest of the file still loads.

### Disable for one session

`hermes --ignore-rules` skips auto-injection of all project context files (`.hermes.md`, `AGENTS.md`, `CLAUDE.md`, `.cursorrules`) **and** `SOUL.md` identity, plus user config, plugins, and MCP servers. Use it to isolate whether a problem is your setup or Hermes itself.

### Example: a small `.hermes.md`

```markdown
# My Project

Hermes: when working in this repo, follow these rules.

## Build
- Always run `make test` before declaring a change done.
- Use `uv run` for Python, not `pip install`.

## Style
- Prefer `pathlib.Path` over `os.path`.
- No `print()` in production code — use the `logger`.
```

That file at `/home/me/projects/myrepo/.hermes.md` is auto-loaded when Hermes runs in any subdirectory of `/home/me/projects/myrepo`, but not when it runs in `/home/me/other-project`.

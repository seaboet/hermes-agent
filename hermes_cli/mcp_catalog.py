"""MCP catalog — curated, Nous-approved MCP servers shipped with the repo.

Entries are added only by merging a PR into hermes-agent; presence in ``optional-mcps/`` = Nous
approval (no community tier, no other trust signals). Manifests pin transport details.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import yaml

from hermes_constants import get_hermes_home, get_optional_mcps_dir
from hermes_cli._subprocess_compat import noninteractive_git_env
from hermes_cli.colors import Colors, color
from hermes_cli.config import load_config, save_config, get_env_value, save_env_value
from hermes_cli.cli_output import prompt as _prompt_input

_MANIFEST_VERSION = 1

if TYPE_CHECKING:
    from hermes_platform.resolver.app import AppDef

# Substituted at install time inside `transport.command` / `transport.args`.
_INSTALL_DIR_VAR = "${INSTALL_DIR}"


@dataclass
class EnvVarSpec:
    name: str
    prompt: str
    required: bool = True
    secret: bool = True
    default: str = ""


@dataclass
class AuthSpec:
    type: str  # "api_key" | "oauth" | "none"
    env: List[EnvVarSpec] = field(default_factory=list)
    provider: Optional[str] = None  # OAuth-specific (third-party provider like Google)
    scopes: List[str] = field(default_factory=list)
    env_var: Optional[str] = None
    # Pre-registered OAuth client block copied verbatim to ``mcp_servers.<name>.oauth`` (vendors
    # without Dynamic Client Registration). Secrets stay ``${VAR}`` references declared in ``env``.
    oauth: Dict[str, Any] = field(default_factory=dict)


# ``auth.oauth`` keys a manifest may pin; everything else is a user-side tuning knob.
_MANIFEST_OAUTH_KEYS = frozenset({"client_id", "client_secret", "redirect_host", "redirect_port", "scope"})
_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class TransportSpec:
    type: str  # "stdio" | "http"
    command: Optional[str] = None
    args: List[str] = field(default_factory=list)
    url: Optional[str] = None
    version: Optional[str] = None  # informational, pinned
    # Static env for the stdio subprocess (telemetry opt-outs, mode flags). NOT for secrets — those
    # go through auth.env so they are prompted for and land in ~/.hermes/.env.
    env: Dict[str, str] = field(default_factory=dict)


@dataclass
class InstallSpec:
    """Optional bootstrap step (git clone + dep install)."""
    type: str  # "git"
    url: str
    ref: str  # commit/tag/branch — pinned, never floats
    bootstrap: List[str] = field(default_factory=list)


@dataclass
class ToolsSpec:
    """Manifest-side tool-selection hints (see _apply_tool_selection()).

    ``default_enabled``: pre-checked in the install checklist / applied directly on probe failure;
    None => all pre-checked (no filter written on failure). ``default_excluded``: exclude-mode
    counterpart written to ``tools.exclude`` — everything NOT matching stays enabled, including tools
    the server adds later (for huge OpenAPI-derived surfaces). Mutually exclusive.
    """

    default_enabled: Optional[List[str]] = None
    default_excluded: Optional[List[str]] = None


@dataclass
class SuggestSpec:
    """Composer-suggestion metadata (desktop "brand pill" triggers).

    GitHub is intentionally NOT in the catalog and must not be suggested here: its hosted MCP needs a
    per-host OAuth app (generic DCR 404s) and the bundled github/* skills are far more capable.
    """

    keywords: List[str] = field(default_factory=list)  # lowercase whole-word/phrase triggers
    hosts: List[str] = field(default_factory=list)  # hostname suffixes ("atlassian.net")
    applications: List[str] = field(default_factory=list)  # reviewed local app labels/aliases
    examples: List[str] = field(default_factory=list)  # capability examples, not executable instructions


@dataclass
class AppSpec:
    """Where the application this MCP fronts lives, one ``AppDef`` per OS it is detectable on."""

    per_os: Dict[str, "AppDef"] = field(default_factory=dict)

    def for_os(self, os_family: str) -> Optional["AppDef"]:
        return self.per_os.get(os_family)


@dataclass
class RequiresSpec:
    """What the MCP needs before it is offered. Separate from ``app`` on purpose: one is data, one is policy."""

    app: bool = False
    min_version: Optional[str] = None


@dataclass
class CatalogEntry:
    name: str
    description: str
    source: str
    transport: TransportSpec
    auth: AuthSpec
    tools: ToolsSpec = field(default_factory=ToolsSpec)
    install: Optional[InstallSpec] = None
    post_install: str = ""
    suggest: Optional[SuggestSpec] = None
    app: Optional[AppSpec] = None
    requires: RequiresSpec = field(default_factory=RequiresSpec)
    manifest_path: Path = field(default_factory=Path)

    # The slice ``hermes_platform.resolver.availability`` reads.
    @property
    def requires_app(self) -> bool:
        return self.requires.app

    @property
    def min_version(self) -> Optional[str]:
        return self.requires.min_version

    def app_for(self, os_family: str) -> Optional["AppDef"]:
        return self.app.for_os(os_family) if self.app else None


class CatalogError(Exception):
    """Manifest parse/validation failure or install error."""


def _catalog_root() -> Path:
    """The optional-mcps/ dir: env-var override / packaged location, else the source checkout's."""
    return get_optional_mcps_dir(Path(__file__).parent.parent / "optional-mcps")


def _parse_env_spec(raw: Any) -> EnvVarSpec:
    if not isinstance(raw, dict):
        raise CatalogError(f"env entry must be a mapping, got {type(raw).__name__}")
    name = raw.get("name") or ""
    if not name or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        raise CatalogError(f"invalid env var name: {name!r}")
    return EnvVarSpec(
        name=name, prompt=raw.get("prompt") or name, required=bool(raw.get("required", True)),
        secret=bool(raw.get("secret", True)), default=str(raw.get("default") or ""))


def _require_mapping(path: Path, key: str, raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise CatalogError(f"{path}: '{key}' must be a mapping")
    return raw


def _require_list(path: Path, field: str, raw: Any) -> list:
    if not isinstance(raw, list):
        raise CatalogError(f"{path}: {field} must be a list")
    return raw


def _require_str_list(path: Path, field: str, raw: Any, *, non_empty: bool = False) -> None:
    ok = isinstance(raw, list) and all(isinstance(t, str) and (t.strip() if non_empty else True) for t in raw)
    if not ok:
        kind = "non-empty strings" if non_empty else "strings"
        raise CatalogError(f"{path}: {field} must be a list of {kind}")


def _parse_transport(path: Path, raw: Any) -> TransportSpec:
    transport_raw = _require_mapping(path, "transport", raw or {})
    t_type = transport_raw.get("type")
    if t_type not in ("stdio", "http"):
        raise CatalogError(f"{path}: transport.type must be 'stdio' or 'http'")
    args = _require_list(path, "transport.args", transport_raw.get("args") or [])
    env_raw = transport_raw.get("env") or {}
    if not isinstance(env_raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env_raw.items()
    ):
        raise CatalogError(f"{path}: transport.env must be a mapping of string to string")
    transport = TransportSpec(
        type=t_type, command=transport_raw.get("command"), args=[str(a) for a in args],
        url=transport_raw.get("url"), version=transport_raw.get("version"), env=dict(env_raw))
    if t_type == "stdio" and not transport.command:
        raise CatalogError(f"{path}: stdio transport requires 'command'")
    if t_type == "http" and not transport.url:
        raise CatalogError(f"{path}: http transport requires 'url'")
    return transport


def _parse_auth(path: Path, raw: Any, name: str, http: bool) -> AuthSpec:
    auth_raw = _require_mapping(path, "auth", raw or {"type": "none"})
    a_type = auth_raw.get("type") or "none"
    if a_type not in ("api_key", "oauth", "none"):
        raise CatalogError(f"{path}: auth.type must be 'api_key'|'oauth'|'none'")
    env_list = [_parse_env_spec(e) for e in _require_list(path, "auth.env", auth_raw.get("env") or [])]
    if http and a_type == "api_key":
        # _build_server_config emits an Authorization header referencing ${MCP_<NAME>_API_KEY}, but
        # install_entry only persists the env vars DECLARED in auth.env. Enforce the naming contract
        # here, or a manifest declaring e.g. N8N_API_KEY would send a literal-placeholder header (401).
        from hermes_cli.mcp_config import _env_key_for_server

        _required_key = _env_key_for_server(name)
        if all(spec.name != _required_key for spec in env_list):
            raise CatalogError(
                f"{path}: http + api_key auth requires auth.env to declare "
                f"'{_required_key}' (the key the Authorization header references)"
            )
    oauth_raw = auth_raw.get("oauth") or {}
    if oauth_raw and a_type != "oauth":
        raise CatalogError(f"{path}: auth.oauth is only valid with auth.type 'oauth'")
    oauth = _require_mapping(path, "auth.oauth", oauth_raw)
    unknown = sorted(set(oauth) - _MANIFEST_OAUTH_KEYS)
    if unknown or not all(isinstance(v, (str, int)) and not isinstance(v, bool) for v in oauth.values()):
        raise CatalogError(
            f"{path}: auth.oauth allows string/int values for {sorted(_MANIFEST_OAUTH_KEYS)} only"
            + (f" (unknown: {unknown})" if unknown else "")
        )
    # Same contract as api_key headers: install_entry persists only DECLARED env vars, so an
    # undeclared ``${VAR}`` would reach the OAuth flow as a literal placeholder (invalid_client).
    declared = {spec.name for spec in env_list}
    undeclared = sorted({ref for v in oauth.values() if isinstance(v, str) for ref in _ENV_REF_RE.findall(v)} - declared)
    if undeclared:
        raise CatalogError(f"{path}: auth.oauth references env vars not declared in auth.env: {undeclared}")
    return AuthSpec(
        type=a_type, env=env_list, provider=auth_raw.get("provider"),
        scopes=list(auth_raw.get("scopes") or []), env_var=auth_raw.get("env_var"), oauth=dict(oauth))


def _parse_tools(path: Path, raw: Any) -> ToolsSpec:
    tools_raw = _require_mapping(path, "tools", raw or {})
    default_enabled = tools_raw.get("default_enabled")
    default_excluded = tools_raw.get("default_excluded")
    for key, val in (("default_enabled", default_enabled), ("default_excluded", default_excluded)):
        if val is not None:
            _require_str_list(path, f"tools.{key}", val)
    if default_enabled is not None and default_excluded is not None:
        raise CatalogError(f"{path}: tools.default_enabled and tools.default_excluded are mutually exclusive")
    return ToolsSpec(default_enabled=default_enabled, default_excluded=default_excluded)


def _parse_suggest(path: Path, suggest_raw: Any) -> Optional[SuggestSpec]:
    if suggest_raw is None:
        return None
    _require_mapping(path, "suggest", suggest_raw)
    kw_raw = suggest_raw.get("keywords") or []
    hosts_raw = suggest_raw.get("hosts") or []
    _require_str_list(path, "suggest.keywords", kw_raw, non_empty=True)
    _require_str_list(path, "suggest.hosts", hosts_raw, non_empty=True)
    try:
        applications = validate_applications(suggest_raw.get("applications", []))
    except ValueError as exc:
        raise CatalogError(f"{path}: {exc}") from exc
    examples = suggest_raw.get("examples", [])
    _require_str_list(path, "suggest.examples", examples, non_empty=True)
    if len(examples) > 6 or any(len(e) > 240 or not e.isprintable() for e in examples):
        raise CatalogError(f"{path}: suggest.examples allows at most 6 single-line examples of 240 characters")
    if "requires_app" in suggest_raw:
        raise CatalogError(f"{path}: suggest.requires_app moved to top-level 'requires: {{app: true}}'")
    if not kw_raw and not hosts_raw and not applications:
        raise CatalogError(f"{path}: 'suggest' requires at least one keyword, host or application")
    # Matching is case-insensitive whole-word / host-suffix: store lowercase so UIs needn't re-normalize.
    return SuggestSpec(
        keywords=[k.strip().lower() for k in kw_raw],
        hosts=[h.strip().lower().lstrip(".") for h in hosts_raw],
        applications=applications, examples=examples)


_MAX_APPLICATIONS = 16
_APP_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._+-]{0,79}")
_APP_OS_FAMILIES = ("win32", "darwin", "linux")
_APP_PRESENCE = ("executable", "bundle")
_APP_VERSION_KINDS = {"pe_resource": "win32", "uninstall_registry": "win32", "plist": "darwin", "none": None}
_APP_LIVENESS_KINDS = ("server_json", "none")
_VERSION_RE = re.compile(r"^\d+(\.\d+)*$")


def validate_applications(labels: object) -> list[str]:
    """Accept bounded display labels, never paths, commands or regexes."""
    if not isinstance(labels, list) or len(labels) > _MAX_APPLICATIONS:
        raise ValueError("suggest.applications must be a list of at most 16 app labels")
    for label in labels:
        if (not isinstance(label, str) or not _APP_LABEL.fullmatch(label)
                or label != label.strip() or ".." in label or " --" in label):
            raise ValueError("suggest.applications must contain safe app labels (1-80 characters)")
    return list(labels)


def _location_is_rooted(location: str, osf: str) -> bool:
    if ".." in location.replace("\\", "/").split("/") or "://" in location:
        return False
    if location.startswith(("~", "%", "$")):
        return True
    if osf == "win32":
        return bool(re.match(r"^[A-Za-z]:[\\/]", location))
    return location.startswith("/")


def _parse_app_os(path: Path, name: str, osf: str, raw: Any) -> "AppDef":
    from hermes_platform.resolver.app import AppDef

    _require_mapping(path, f"app.{osf}", raw)
    presence = raw.get("presence")
    if presence not in _APP_PRESENCE:
        raise CatalogError(f"{path}: app.{osf}.presence must be one of {_APP_PRESENCE}")
    location = raw.get("location")
    if not isinstance(location, str) or not location.strip():
        raise CatalogError(f"{path}: app.{osf}.location is required")
    if not _location_is_rooted(location.strip(), osf):
        raise CatalogError(
            f"{path}: app.{osf}.location must be absolute or start with ~ / %VAR% / $VAR, without '..' or a URL scheme")
    version = raw.get("version") or {"kind": "none"}
    _require_mapping(path, f"app.{osf}.version", version)
    vkind = version.get("kind", "none")
    if vkind not in _APP_VERSION_KINDS:
        raise CatalogError(f"{path}: app.{osf}.version.kind must be one of {sorted(_APP_VERSION_KINDS)}")
    only_on = _APP_VERSION_KINDS[vkind]
    if only_on and only_on != osf:
        raise CatalogError(f"{path}: app.{osf}.version.kind {vkind!r} is only valid under app.{only_on}")
    varg = str(version.get("display_name_prefix") or "")
    if vkind == "uninstall_registry" and not varg:
        raise CatalogError(f"{path}: app.{osf}.version.display_name_prefix is required for uninstall_registry")
    liveness = raw.get("liveness") or {"kind": "none"}
    _require_mapping(path, f"app.{osf}.liveness", liveness)
    lkind = liveness.get("kind", "none")
    if lkind not in _APP_LIVENESS_KINDS:
        raise CatalogError(f"{path}: app.{osf}.liveness.kind must be one of {_APP_LIVENESS_KINDS}")
    lpath = str(liveness.get("path") or "")
    if lkind == "server_json" and not lpath:
        raise CatalogError(f"{path}: app.{osf}.liveness.path is required for server_json")
    return AppDef(
        app_id=name, os_family=osf, presence=presence, location=location.strip(),
        version_kind=vkind, version_arg=varg,
        liveness_kind=lkind, liveness_path=lpath,
        liveness_pid_key=str(liveness.get("pid_key") or "pid"),
        liveness_url_key=str(liveness.get("url_key") or "http"),
        liveness_token_key=str(liveness.get("token_key") or "token"),
        endpoint_path=str(liveness.get("endpoint_path") or "/mcp"),
    )


def _parse_app(path: Path, name: str, raw: Any) -> Optional[AppSpec]:
    if raw is None:
        return None
    _require_mapping(path, "app", raw)
    unknown = set(raw) - set(_APP_OS_FAMILIES)
    if unknown:
        raise CatalogError(f"{path}: app has unknown OS keys {sorted(unknown)}; use {_APP_OS_FAMILIES}")
    if not raw:
        raise CatalogError(f"{path}: app needs at least one OS block")
    return AppSpec(per_os={osf: _parse_app_os(path, name, osf, raw[osf]) for osf in raw})


def _parse_requires(path: Path, raw: Any, app: Optional[AppSpec]) -> RequiresSpec:
    if raw is None:
        return RequiresSpec()
    _require_mapping(path, "requires", raw)
    unknown = set(raw) - {"app", "min_version"}
    if unknown:
        raise CatalogError(f"{path}: requires has unknown keys {sorted(unknown)}")
    needs_app = raw.get("app", False)
    if not isinstance(needs_app, bool):
        raise CatalogError(f"{path}: requires.app must be a boolean")
    if needs_app and app is None:
        raise CatalogError(f"{path}: requires.app is true but the manifest has no 'app' block")
    min_version = raw.get("min_version")
    if min_version is not None:
        if not isinstance(min_version, str) or not _VERSION_RE.match(min_version):
            raise CatalogError(f"{path}: requires.min_version must be a dotted numeric string")
        if not needs_app:
            raise CatalogError(f"{path}: requires.min_version needs requires.app: true")
        assert app is not None
        unversioned = sorted(osf for osf, d in app.per_os.items() if d.version_kind == "none")
        if unversioned:
            raise CatalogError(
                f"{path}: requires.min_version needs a version source under app.{unversioned[0]} (kind is none)")
    return RequiresSpec(app=needs_app, min_version=min_version)


def _parse_install(path: Path, install_raw: Any) -> Optional[InstallSpec]:
    if install_raw is None:
        return None
    _require_mapping(path, "install", install_raw)
    i_type = install_raw.get("type")
    if i_type != "git":
        raise CatalogError(f"{path}: install.type must be 'git' (got {i_type!r})")
    url, ref = install_raw.get("url") or "", install_raw.get("ref") or ""
    if not url or not ref:
        raise CatalogError(f"{path}: install.url and install.ref are required")
    bootstrap = _require_list(path, "install.bootstrap", install_raw.get("bootstrap") or [])
    return InstallSpec(type=i_type, url=url, ref=ref, bootstrap=[str(c) for c in bootstrap])


def _parse_manifest(path: Path) -> CatalogEntry:
    """Read and validate a manifest.yaml. Raise CatalogError on any problem."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as exc:
        raise CatalogError(f"failed to read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CatalogError(f"{path}: manifest must be a mapping")

    mv = data.get("manifest_version")
    if mv != _MANIFEST_VERSION:
        raise CatalogError(
            f"{path}: manifest_version {mv!r} unsupported "
            f"(this Hermes understands version {_MANIFEST_VERSION})"
        )
    name = data.get("name") or ""
    if not name or not re.match(r"^[A-Za-z0-9_-]+$", name):
        raise CatalogError(f"{path}: invalid or missing 'name'")
    description = str(data.get("description") or "").strip()
    if not description:
        raise CatalogError(f"{path}: 'description' required")

    # Validation order (transport, auth, tools, suggest, install, app, requires) determines which error surfaces.
    transport = _parse_transport(path, data.get("transport"))
    auth = _parse_auth(path, data.get("auth"), name, transport.type == "http")
    tools = _parse_tools(path, data.get("tools"))
    suggest = _parse_suggest(path, data.get("suggest"))
    install = _parse_install(path, data.get("install"))
    app = _parse_app(path, name, data.get("app"))
    requires = _parse_requires(path, data.get("requires"), app)
    return CatalogEntry(
        name=name, description=description, source=str(data.get("source") or "").strip(),
        transport=transport, auth=auth, tools=tools, install=install,
        post_install=str(data.get("post_install") or ""), suggest=suggest,
        app=app, requires=requires, manifest_path=path,
    )


def catalog_entry_payload(entry: CatalogEntry, *, installed: bool, enabled: bool) -> Dict[str, Any]:
    """The one wire projection of a catalog entry; RPC ``mcp.catalog`` is its only caller."""
    from hermes_platform.resolver.availability import availability

    transport, install, auth = entry.transport, entry.install, entry.auth
    return {
        "name": entry.name,
        "description": entry.description,
        "source": entry.source,
        "transport": transport.type,
        "auth_type": auth.type,
        # ``requires`` (env key names) is what the bots dialogs read; ``required_env`` is the full
        # prompt shape the connection card and the MCP tab read. Values never travel.
        "requires": [e.name for e in auth.env],
        "required_env": [
            {"name": e.name, "prompt": e.prompt, "required": e.required, "secret": e.secret, "default": e.default}
            for e in auth.env
        ],
        "command": transport.command,
        "args": list(transport.args or []),
        "url": transport.url,
        "install_url": install.url if install else None,
        "install_ref": install.ref if install else None,
        "bootstrap": list(install.bootstrap) if install else [],
        "default_enabled": list(entry.tools.default_enabled) if entry.tools.default_enabled is not None else None,
        "post_install": entry.post_install or "",
        "suggest": {
            "keywords": list(entry.suggest.keywords), "hosts": list(entry.suggest.hosts),
            "applications": list(entry.suggest.applications), "examples": list(entry.suggest.examples),
        } if entry.suggest else None,
        "requires_app": entry.requires.app,
        "min_version": entry.requires.min_version,
        "availability": availability(entry).as_dict(),
        "needs_install": install is not None,
        "installed": installed,
        "enabled": enabled,
    }


# Populated by list_catalog(); inspected by the picker / catalog UIs so the user gets actionable
# feedback instead of a silently-shorter list.
_CATALOG_DIAGNOSTICS: List[tuple] = []


_CATALOG_CACHE: "tuple[tuple, List[CatalogEntry], List[tuple]] | None" = None


def _catalog_signature(root: Path) -> tuple:
    """(root, (name, mtime_ns, size) per manifest): the catalog changes only when a file does."""
    sig = []
    for child in sorted(root.iterdir()):
        manifest = child / "manifest.yaml"
        try:
            st = manifest.stat()
        except OSError:
            continue
        sig.append((child.name, st.st_mtime_ns, st.st_size))
    return (str(root), tuple(sig))


def list_catalog() -> List[CatalogEntry]:
    """Return all valid catalog entries, sorted by name.

    Invalid manifests are skipped silently (CI catches them); future ``manifest_version`` ones are
    skipped too but surfaced via :func:`catalog_diagnostics` so UIs can say "update Hermes".
    Parsed entries are reused until a manifest's mtime or size changes: the registry's per-server
    ``check_fn`` and the catalog RPC both call this on hot paths.
    """
    global _CATALOG_CACHE
    root = _catalog_root()
    if not root.exists():
        return []
    sig = _catalog_signature(root)
    if _CATALOG_CACHE is not None and _CATALOG_CACHE[0] == sig:
        _CATALOG_DIAGNOSTICS[:] = _CATALOG_CACHE[2]
        return list(_CATALOG_CACHE[1])
    entries: List[CatalogEntry] = []
    diagnostics: List[tuple] = []
    for child in sorted(root.iterdir()):
        manifest = child / "manifest.yaml"
        if not manifest.is_file():
            continue
        try:
            entries.append(_parse_manifest(manifest))
        except CatalogError as exc:
            msg = str(exc)
            future = "manifest_version" in msg and "unsupported" in msg
            diagnostics.append((child.name, "future_manifest" if future else "invalid", msg))
    _CATALOG_DIAGNOSTICS[:] = diagnostics
    _CATALOG_CACHE = (sig, entries, diagnostics)
    return list(entries)



def catalog_diagnostics() -> List[tuple]:
    """``(entry_name, kind, message)`` tuples from the most recent :func:`list_catalog` call;
    ``kind`` is ``future_manifest`` (newer than this Hermes) or ``invalid`` (malformed)."""
    return list(_CATALOG_DIAGNOSTICS)


def get_entry(name: str) -> Optional[CatalogEntry]:
    """Look up a single entry by name. ``official/<name>`` prefix accepted."""
    if name.startswith("official/"):
        name = name[len("official/"):]
    return next((e for e in list_catalog() if e.name == name), None)


def installed_servers() -> Dict[str, dict]:
    """Return current ``mcp_servers`` block from config.yaml."""
    from hermes_cli.mcp_config import _get_mcp_servers

    return _get_mcp_servers()


def is_installed(name: str) -> bool:
    return name in installed_servers()


def server_enabled(cfg: dict) -> bool:
    """Interpret a server block's ``enabled`` flag (bools, and yes/true/1 strings)."""
    enabled = cfg.get("enabled", True)
    if isinstance(enabled, str):
        return enabled.lower() in {"true", "1", "yes"}
    return bool(enabled)


def is_enabled(name: str) -> bool:
    cfg = installed_servers().get(name)
    return bool(cfg) and server_enabled(cfg)


def remove_server(name: str) -> bool:
    """Drop ``mcp_servers.<name>`` from config.yaml (pruning an empty block). True if it existed."""
    from hermes_cli.mcp_config import _remove_mcp_server

    return _remove_mcp_server(name)


def _say(msg: str, colour: str = Colors.GREEN) -> None:
    print(color(msg, colour))


def _install_root() -> Path:
    """Where git-bootstrapped MCPs are cloned. Per-user, profile-aware."""
    root = get_hermes_home() / "mcp-installs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _run_bootstrap(cwd: Path, commands: List[str]) -> None:
    """Execute bootstrap commands in *cwd*. Raise CatalogError on first failure."""
    for cmd in commands:
        _say(f"  $ {cmd}", Colors.DIM)
        rc = subprocess.run(cmd, cwd=str(cwd), shell=True).returncode
        if rc != 0:
            raise CatalogError(f"bootstrap step failed (exit {rc}): {cmd}")


def _do_git_install(entry: CatalogEntry) -> Path:
    """Clone the entry's repo into ``~/.hermes/mcp-installs/<name>`` and run bootstrap. Returns the dir."""
    assert entry.install is not None and entry.install.type == "git"
    install = entry.install
    dest = _install_root() / entry.name

    git = shutil.which("git")
    if not git:
        raise CatalogError("git is required to install this MCP but was not found on PATH")
    if dest.exists():
        # Fresh checkout each install — the manifest ref is the source of truth.
        _say(f"  Removing existing install at {dest}", Colors.DIM)
        shutil.rmtree(dest)
    _say(f"  Cloning {install.url} ({install.ref}) → {dest}", Colors.CYAN)

    # `git clone --branch` only accepts branches/tags, NOT commit SHAs; detect SHA-shaped refs
    # upfront so the fast path doesn't always fail noisily before the full-clone fallback.
    is_sha_ref = bool(re.fullmatch(r"[0-9a-f]{7,40}", install.ref))
    # Never hang on a credential prompt: installs run from CLI/dashboard flows nobody can answer.
    from hermes_cli.git_credentials import run_git_with_credential_fallback

    def _git(*args: str) -> int:
        result = run_git_with_credential_fallback(
            [git, *args], install.url, env=noninteractive_git_env(),
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0 and (result.stderr or "").strip():
            _say(result.stderr.strip(), Colors.DIM)
        return result.returncode

    if not is_sha_ref and _git("clone", "--depth", "1", "--branch", install.ref, install.url, str(dest)) != 0:
        # Branch/tag form failed (e.g. ref deleted upstream): fall through to full-clone path.
        if dest.exists():
            shutil.rmtree(dest)
        is_sha_ref = True
    if is_sha_ref:
        if _git("clone", install.url, str(dest)) != 0:
            raise CatalogError(f"git clone failed for {install.url}")
        if _git("-C", str(dest), "checkout", install.ref) != 0:
            raise CatalogError(f"git checkout {install.ref} failed")

    if install.bootstrap:
        _run_bootstrap(dest, install.bootstrap)
    return dest


def _expand_install_dir(value: str, install_dir: Optional[Path]) -> str:
    if _INSTALL_DIR_VAR not in value:
        return value
    if install_dir is None:
        raise CatalogError(f"manifest references {_INSTALL_DIR_VAR} but no install block exists")
    return value.replace(_INSTALL_DIR_VAR, str(install_dir))


def _prompt_env_vars(specs: List[EnvVarSpec]) -> Dict[str, str]:
    """Prompt for each env spec; secrets and non-secrets alike go to ~/.hermes/.env."""
    collected: Dict[str, str] = {}
    for spec in specs:
        existing = get_env_value(spec.name)
        if existing:
            _say(f"  ✓ {spec.name} already set in .env")
            collected[spec.name] = existing
            continue
        value = _prompt_input(spec.prompt, default=spec.default or None, password=spec.secret)
        if value:
            save_env_value(spec.name, value)
            collected[spec.name] = value
        elif spec.required:
            raise CatalogError(f"{spec.name} is required but no value was provided")
    return collected


def _build_server_config(entry: CatalogEntry, install_dir: Optional[Path]) -> dict:
    """Translate a manifest into the ``mcp_servers.<name>`` block format used by hermes_cli/mcp_config.py."""
    cfg: dict = {}
    t = entry.transport
    if t.type == "stdio":
        cfg["command"] = _expand_install_dir(t.command or "", install_dir)
        if t.args:
            cfg["args"] = [_expand_install_dir(a, install_dir) for a in t.args]
        if t.env:
            cfg["env"] = dict(t.env)
    elif t.type == "http":
        cfg["url"] = t.url
        if entry.auth.type == "oauth":
            cfg["auth"] = "oauth"
            if entry.auth.oauth:
                cfg["oauth"] = dict(entry.auth.oauth)
        elif entry.auth.type == "api_key":
            from hermes_cli.mcp_config import _bearer_auth_headers

            cfg["headers"] = _bearer_auth_headers(entry.name)
    return cfg


def _read_prior_tool_list(name: str, key: str) -> Optional[List[str]]:
    """The user's prior ``tools.<key>`` (``include``/``exclude``) for *name*, if well-formed.

    Read BEFORE a reinstall overwrites the entry: a prior include list pre-checks the checklist and a
    user-edited exclude list survives instead of being clobbered by the manifest's ``default_excluded``.
    """
    tools_cfg = (installed_servers().get(name) or {}).get("tools") or {}
    if not isinstance(tools_cfg, dict):
        return None
    value = tools_cfg.get(key)
    ok = isinstance(value, list) and all(isinstance(t, str) for t in value)
    return list(value) if ok else None


def _probe_tools(name: str) -> Optional[List[tuple]]:
    """Connect to a freshly-configured MCP and list its tools.

    ``(tool_name, description)`` tuples on success, ``None`` on any failure (unreachable, OAuth not
    yet completed, ...). Failures are swallowed here; :func:`_apply_tool_selection` handles them.
    """
    server_cfg = installed_servers().get(name)
    if not server_cfg:
        return None
    try:
        from hermes_cli.mcp_config import _probe_single_server  # lazy: keep this module cheap

        tools = _probe_single_server(name, server_cfg)
        return list(tools) if tools is not None else []
    except Exception as exc:
        _say(f"  Probe failed: {exc}", Colors.YELLOW)
        return None


def _write_tools_filter(name: str, mode: str, values: Optional[List[str]]) -> None:
    """Persist ``mcp_servers.<name>.tools.<mode>`` (``include``/``exclude``), clearing the other
    mode; ``values=None`` drops the whole tools block (no filter)."""
    cfg = load_config()
    servers = cfg.setdefault("mcp_servers", {})
    server_entry = servers.get(name) or {}
    if values is None:
        server_entry.pop("tools", None)
    else:
        tools_block = server_entry.get("tools") or {}
        if not isinstance(tools_block, dict):
            tools_block = {}
        tools_block[mode] = list(values)
        tools_block.pop("exclude" if mode == "include" else "include", None)
        server_entry["tools"] = tools_block
    servers[name] = server_entry
    cfg["mcp_servers"] = servers
    save_config(cfg)


def _apply_tool_selection(
    entry: CatalogEntry,
    *,
    prior_selection: Optional[List[str]],
    prior_exclude: Optional[List[str]] = None) -> None:
    """Probe the server and let the user pick which tools to enable.

    Probe-success: curses checklist; pre-check priority *prior_selection* (reinstall) > manifest
    ``tools.default_enabled`` > all; all-on clears any filter. Probe-fail: keep the prior filter,
    else apply ``default_enabled``, else no filter; point the user at ``hermes mcp configure``.
    """
    print()
    name = entry.name
    configure_hint = f"`hermes mcp configure {name}`"

    # Exclude-mode manifests never probe: the curated exclude list (names or globs) is written as-is
    # and everything else stays enabled, including tools the server adds later. A prior include
    # selection falls through to the checklist; a prior user-edited exclude list is kept verbatim.
    if entry.tools.default_excluded and prior_selection is None:
        edit_hint = f"Edit mcp_servers.{name}.tools.exclude in config.yaml or run {configure_hint} to change."
        if prior_exclude is not None:
            _write_tools_filter(name, "exclude", prior_exclude)
            _say(f"  Kept your existing exclude list ({len(prior_exclude)} entries). {edit_hint}")
            return
        _write_tools_filter(name, "exclude", entry.tools.default_excluded)
        _say(
            f"  Applied manifest exclude list ({len(entry.tools.default_excluded)} entries); "
            f"everything else stays enabled. {edit_hint}"
        )
        return

    _say(f"  Probing '{name}' for available tools...", Colors.CYAN)
    probed = _probe_tools(name)

    # Probe failure. Order matters: a reinstall must keep the user's previous filter intact (common
    # for OAuth entries — the entry rewrite precedes first auth, so the server is unreachable here).
    if probed is None:
        manifest_default = entry.tools.default_enabled
        refine_hint = f"Run {configure_hint} after the server is reachable to refine."
        if prior_selection is not None:
            mode, values = "include", prior_selection
            msg = f"Kept your previous tool selection ({len(prior_selection)} tools). {refine_hint}"
        elif prior_exclude is not None:
            mode, values = "exclude", prior_exclude
            msg = f"Kept your existing exclude list ({len(prior_exclude)} entries)."
        elif manifest_default:
            mode, values = "include", manifest_default
            msg = f"Applied manifest default ({len(manifest_default)} tools). {refine_hint}"
        else:
            mode, values = "include", None
            msg = (
                "installed with no tool filter (all tools enabled when "
                f"reachable). Run {configure_hint} after first connect to prune."
            )
        _write_tools_filter(name, mode, values)
        sep = ";" if values is None else "."
        _say(f"  Couldn't probe server{sep} {msg}", Colors.YELLOW)
        return

    if not probed:
        # Keep a prior explicit selection: "no tools today" must not widen ``include: []`` to
        # "all tools" the next time the server does advertise some.
        _write_tools_filter(name, "include", prior_selection)
        _say("  Server reported no tools.", Colors.YELLOW)
        return

    tool_names = [t[0] for t in probed]

    # Non-TTY: skip the checklist; same priority as the interactive pre-check.
    import sys as _sys
    if not _sys.stdin.isatty():
        preferred = prior_selection if prior_selection is not None else (entry.tools.default_enabled or None)
        _write_tools_filter(
            name, "include", None if preferred is None else [n for n in preferred if n in tool_names]
        )
        return

    # A prior ``include: []`` (user chose zero tools) outranks manifest defaults, like the non-TTY path.
    preferred = prior_selection if prior_selection is not None else (entry.tools.default_enabled or tool_names)
    pre_set = {n for n in preferred if n in tool_names}
    pre_indices = {i for i, n in enumerate(tool_names) if n in pre_set}
    _say(f"  Found {len(probed)} tool(s). Pre-checked: {len(pre_indices)}.")

    from hermes_cli.curses_ui import curses_checklist

    labels = [f"{n}  —  {(d[:60] + '...') if len(d) > 60 else d}" for n, d in probed]
    chosen_indices = curses_checklist(
        f"Select tools for '{name}' (SPACE toggle, ENTER confirm)", labels, pre_indices)
    if not chosen_indices:
        # Everything unchecked: write an empty include so the server is installed but contributes
        # nothing until reconfigured.
        _write_tools_filter(name, "include", [])
        _say(f"  No tools selected. Run {configure_hint} to change.", Colors.YELLOW)
        return
    if len(chosen_indices) == len(probed):
        # Clear the filter: tools the server adds later are auto-enabled too. To pin the current set,
        # re-run `hermes mcp configure <name>` and unselect a tool (switches to include-mode).
        _write_tools_filter(name, "include", None)
        _say(
            f"  ✓ All {len(probed)} tools enabled (no filter — new tools "
            "the server adds later will be auto-enabled)."
        )
        return
    chosen_names = [tool_names[i] for i in sorted(chosen_indices)]
    _write_tools_filter(name, "include", chosen_names)
    _say(f"  ✓ {len(chosen_names)}/{len(probed)} tools enabled.")


def card_install_config(entry: CatalogEntry) -> dict:
    """The ``mcp_servers.<name>`` block a connection card installs, built in memory.

    Same block :func:`install_entry` writes, minus everything a terminal owns: no prompts, no
    probe, no checklist. The caller saves it only once the server accepted the connection. Tool
    filter priority matches :func:`_apply_tool_selection`: a prior user selection survives a
    reinstall, else the manifest's curated filter, else none.
    """
    install_dir = _do_git_install(entry) if entry.install is not None else None
    cfg = _build_server_config(entry, install_dir)
    cfg["enabled"] = True
    prior_include = _read_prior_tool_list(entry.name, "include")
    prior_exclude = _read_prior_tool_list(entry.name, "exclude")
    if prior_include is not None:
        cfg["tools"] = {"include": prior_include}
    elif prior_exclude is not None:
        cfg["tools"] = {"exclude": prior_exclude}
    elif entry.tools.default_excluded:
        cfg["tools"] = {"exclude": list(entry.tools.default_excluded)}
    elif entry.tools.default_enabled:
        cfg["tools"] = {"include": list(entry.tools.default_enabled)}
    return cfg


def install_entry(entry: CatalogEntry, *, enable: bool = True) -> None:
    """Install a catalog entry end-to-end.

    Order: git clone + bootstrap (if any); credential prompts (``auth.env``) to .env; write
    ``mcp_servers.<name>`` (with the ``auth: oauth`` marker and any pre-registered ``oauth`` block); probe + tool checklist (falling back per
    :func:`_apply_tool_selection`); print post_install notes.
    """
    print()
    _say(f"  Installing MCP '{entry.name}'", Colors.CYAN + Colors.BOLD)
    if entry.description:
        _say(f"  {entry.description}", Colors.DIM)
    if entry.source:
        _say(f"  Source: {entry.source}", Colors.DIM)
    print()

    install_dir = _do_git_install(entry) if entry.install is not None else None

    if entry.auth.env:
        print()
        _say("  Configure credentials:", Colors.CYAN)
        _prompt_env_vars(entry.auth.env)
    if entry.auth.type == "oauth" and entry.auth.provider:
        # Provider-mediated OAuth relies on the existing `hermes auth <provider>` flow; surface
        # guidance rather than auto-running it to keep install decoupled from provider-auth lifecycle.
        _say(
            f"  This MCP uses {entry.auth.provider} OAuth. Run "
            f"`hermes auth {entry.auth.provider}` if you have not "
            "already authenticated.",
            Colors.YELLOW)
    elif entry.auth.type == "oauth":
        client = "your pre-registered OAuth client" if entry.auth.oauth.get("client_id") else "native OAuth 2.1"
        _say(
            f"  This MCP uses {client}; tokens will be acquired "
            "on first connection (browser flow).",
            Colors.DIM)

    # Read prior user selection BEFORE overwriting the entry so a reinstall preserves it.
    prior_selection = _read_prior_tool_list(entry.name, "include")
    prior_exclude = _read_prior_tool_list(entry.name, "exclude")

    server_cfg = _build_server_config(entry, install_dir)
    server_cfg["enabled"] = enable

    from hermes_cli.mcp_config import _save_mcp_server

    if not _save_mcp_server(entry.name, server_cfg):
        raise CatalogError(f"catalog entry '{entry.name}' rejected: suspicious command/args configuration")

    _apply_tool_selection(entry, prior_selection=prior_selection, prior_exclude=prior_exclude)

    print()
    _say(
        f"  ✓ Installed '{entry.name}' "
        f"({'enabled' if enable else 'disabled'}). "
        f"Start a new Hermes session to load its tools."
    )
    if entry.post_install:
        print()
        for line in entry.post_install.strip().splitlines():
            _say(f"  {line}", Colors.DIM)
    print()


def uninstall_entry(name: str, *, purge_install_dir: bool = True) -> bool:
    """Remove a catalog-installed MCP from config and (optionally) its clone dir. True if anything removed."""
    removed = remove_server(name)
    if purge_install_dir:
        clone = _install_root() / name
        if clone.exists():
            shutil.rmtree(clone)
            removed = True
    return removed

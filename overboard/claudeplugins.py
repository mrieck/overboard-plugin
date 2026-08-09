"""Claude Code plugin inventory + actions.

Reads Claude Code's own on-disk registry (read-only — mutations always go
through the `claude plugin` CLI, never by writing these files):

- ~/.claude/plugins/installed_plugins.json  (version 2) — the authoritative
  install list: {"plugins": {"name@marketplace": [{scope, projectPath,
  installPath, version, installedAt}, ...]}}
- ~/.claude/plugins/known_marketplaces.json — marketplace key -> source.
- ~/.claude/settings.json plus each project's/workspace's
  .claude/settings{,.local}.json — the enable/disable overlay
  ("enabledPlugins" is an object map {"name@marketplace": true}).

CLI actions (install/uninstall/enable/disable/marketplace add) run as
subprocesses on a single background worker so concurrent invocations never
race Claude's registry writes. This is deliberate subprocess use, not
inference — the key-free rule bans external inference APIs, not shelling out
(git, herdr, and terminals already work this way).

Everything degrades: a corrupt or missing registry file yields a partial
inventory with a notice, never an exception.
"""

from __future__ import annotations

import json
import queue as _queue
import shutil
import subprocess
import threading
import time as _time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

from . import store

CLAUDE_DIR = Path.home() / ".claude"
PLUGINS_DIR = CLAUDE_DIR / "plugins"
INSTALLED_PATH = PLUGINS_DIR / "installed_plugins.json"
MARKETPLACES_PATH = PLUGINS_DIR / "known_marketplaces.json"
USER_SETTINGS_PATH = CLAUDE_DIR / "settings.json"
INSTALLED_VERSION = 2  # the only format this parser understands

POPULAR_URL_DEFAULT = "https://plugmyplugin.com/api/plugins?sort=stars&limit=60"
POPULAR_TTL_SECS = 6 * 3600
JOB_TIMEOUT_SECS = 120
JOBS_CAP = 50
VALID_ACTIONS = ("install", "uninstall", "enable", "disable")
VALID_SCOPES = ("user", "project", "local")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load_json(path: Path) -> dict:
    """Defensive read: missing/corrupt file -> {} (mirrors store.load_* style)."""
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


# ---- claude CLI location ---------------------------------------------------
def claude_binary() -> "str | None":
    found = shutil.which("claude")
    if found:
        return found
    for candidate in (Path.home() / ".local/bin/claude",
                      Path("/opt/homebrew/bin/claude"),
                      Path("/usr/local/bin/claude")):
        if candidate.is_file():
            return str(candidate)
    return None


# ---- inventory -------------------------------------------------------------
def _enabled_plugins(settings_path: Path) -> dict:
    """The enabledPlugins map from one settings file: {"name@mkt": bool}."""
    enabled = _load_json(settings_path).get("enabledPlugins")
    return enabled if isinstance(enabled, dict) else {}


def _scan_dir(path: str) -> "list[tuple[str, dict]]":
    """(scope-name, enabledPlugins) pairs for a project/workspace directory."""
    base = Path(path).expanduser() / ".claude"
    out = []
    for scope, fname in (("project", "settings.json"), ("local", "settings.local.json")):
        enabled = _enabled_plugins(base / fname)
        if enabled:
            out.append((scope, enabled))
    return out


def inventory(project_paths: "dict[str, str] | None" = None,
              workspaces: "list[dict] | None" = None) -> dict:
    """The full plugin picture: what's installed (per Claude's registry), and
    where each plugin is enabled — user scope, per project, per workspace."""
    notices: list = []

    installed_raw = _load_json(INSTALLED_PATH)
    if installed_raw and installed_raw.get("version") != INSTALLED_VERSION:
        # Unknown future format — fall back to the settings-only view rather
        # than mis-parse an internal file we don't own.
        notices.append(f"installed_plugins.json has unknown version "
                       f"{installed_raw.get('version')!r}; inventory is partial")
        installed_raw = {}
    installed = installed_raw.get("plugins") if isinstance(
        installed_raw.get("plugins"), dict) else {}

    marketplaces = {}
    for key, entry in (_load_json(MARKETPLACES_PATH) or {}).items():
        if not isinstance(entry, dict):
            continue
        source = entry.get("source") if isinstance(entry.get("source"), dict) else {}
        mkt = {"source": source,
               "install_location": entry.get("installLocation"),
               "auto_update": bool(entry.get("autoUpdate", False))}
        # Directory-source marketplaces point at absolute local paths — flag
        # ones whose path is gone so workspaces referencing them can warn.
        if source.get("source") == "directory":
            p = source.get("path")
            mkt["missing"] = not (p and Path(p).expanduser().is_dir())
        marketplaces[key] = mkt

    plugins: dict = {}

    def entry(plugin_id: str) -> dict:
        if plugin_id not in plugins:
            name, _, mkt = plugin_id.partition("@")
            plugins[plugin_id] = {"name": name, "marketplace": mkt or None,
                                  "installs": [], "enabled_user": False,
                                  "enabled_in": [], "disabled_in": []}
        return plugins[plugin_id]

    for plugin_id, installs in installed.items():
        if not isinstance(installs, list):
            continue
        for inst in installs:
            if not isinstance(inst, dict):
                continue
            pp = inst.get("projectPath")
            entry(plugin_id)["installs"].append({
                "scope": inst.get("scope"),
                "project_path": pp,
                # A project/local install whose directory is gone (e.g. a
                # deleted workspace) — show it as stale, don't hide it.
                "missing": bool(pp) and not Path(pp).expanduser().is_dir(),
                "install_path": inst.get("installPath"),
                "version": inst.get("version"),
                "installed_at": inst.get("installedAt"),
            })

    for plugin_id, on in _enabled_plugins(USER_SETTINGS_PATH).items():
        entry(plugin_id)["enabled_user"] = bool(on)

    def overlay(kind: str, name: str, path: str, workspace_id: "str | None") -> None:
        for scope, enabled in _scan_dir(path):
            for plugin_id, on in enabled.items():
                where = {"kind": kind, "name": name, "path": path,
                         "scope": scope, "workspace_id": workspace_id}
                key = "enabled_in" if on else "disabled_in"
                entry(plugin_id)[key].append(where)

    for slug, path in (project_paths or {}).items():
        overlay("project", slug, path, None)
    for ws in (workspaces or []):
        overlay("workspace", f"{ws.get('project')}/{ws.get('task')}",
                ws.get("path") or "", ws.get("id"))

    orphans = [pid for pid, p in plugins.items()
               if not p["installs"] and (p["enabled_user"] or p["enabled_in"])]

    return {"marketplaces": marketplaces, "plugins": plugins, "orphans": orphans,
            "notices": notices, "scanned_at": _now_iso()}


# ---- CLI actions (single-worker job queue) ---------------------------------
_jobs_lock = threading.Lock()
_jobs: list = []          # newest first, capped at JOBS_CAP
_pending: "_queue.Queue" = _queue.Queue()
_worker_started = False


def _ensure_worker() -> None:
    global _worker_started
    with _jobs_lock:
        if _worker_started:
            return
        _worker_started = True
    threading.Thread(target=_work, daemon=True, name="ob-plugin-jobs").start()


def _work() -> None:
    while True:
        job = _pending.get()
        with _jobs_lock:
            job["state"] = "running"
            job["started_at"] = _now_iso()
        try:
            proc = subprocess.run(job["argv"], cwd=job.get("cwd") or None,
                                  capture_output=True, text=True,
                                  timeout=JOB_TIMEOUT_SECS)
            output = (proc.stdout or "") + (proc.stderr or "")
            ok = proc.returncode == 0
        except subprocess.TimeoutExpired:
            output, ok = f"timed out after {JOB_TIMEOUT_SECS}s", False
        except OSError as e:
            output, ok = str(e), False
        with _jobs_lock:
            job["state"] = "done" if ok else "failed"
            job["output"] = output.strip()[-4000:]
            job["ended_at"] = _now_iso()


def _enqueue(action: str, plugin: "str | None", argv: list, cwd: "str | None") -> dict:
    job = {"id": uuid.uuid4().hex, "action": action, "plugin": plugin,
           "cwd": cwd, "argv": argv, "state": "queued", "output": None,
           "created_at": _now_iso(), "started_at": None, "ended_at": None}
    with _jobs_lock:
        _jobs.insert(0, job)
        del _jobs[JOBS_CAP:]
    _ensure_worker()
    _pending.put(job)
    return {k: v for k, v in job.items() if k != "argv"}


def jobs_view() -> list:
    with _jobs_lock:
        return [{k: v for k, v in j.items() if k != "argv"} for j in _jobs]


def plugin_action(action: str, plugin_id: str, scope: str = "user",
                  cwd: "str | None" = None) -> dict:
    """Run `claude plugin <action> <id> --scope <scope>` (cwd picks the project
    for project/local scope). Returns the queued job record."""
    if action not in VALID_ACTIONS:
        raise ValueError(f"unknown plugin action: {action}")
    if scope not in VALID_SCOPES:
        raise ValueError(f"unknown scope: {scope}")
    if not plugin_id or any(c.isspace() for c in plugin_id):
        raise ValueError(f"not a plugin id: {plugin_id!r}")
    binary = claude_binary()
    if not binary:
        raise ValueError("claude CLI not found on this machine")
    if scope in ("project", "local") and not (cwd and Path(cwd).expanduser().is_dir()):
        raise ValueError(f"{scope} scope needs an existing directory, got: {cwd!r}")
    argv = [binary, "plugin", action, plugin_id, "--scope", scope]
    return _enqueue(action, plugin_id, argv, cwd)


def add_marketplace(source: str) -> dict:
    """`claude plugin marketplace add <source>` — owner/repo, git URL, or path."""
    if not source or any(c.isspace() for c in source):
        raise ValueError(f"not a marketplace source: {source!r}")
    binary = claude_binary()
    if not binary:
        raise ValueError("claude CLI not found on this machine")
    return _enqueue("marketplace_add", source,
                    [binary, "plugin", "marketplace", "add", source], None)


# ---- popular plugins (plugmyplugin.com) ------------------------------------
def popular_url() -> str:
    # Overridable for local API development; a public GET, no key involved.
    return store.load_credentials().get("plugmyplugin_url") or POPULAR_URL_DEFAULT


def fetch_popular(force: bool = False) -> dict:
    """Cached popular-plugins payload from plugmyplugin.com. 6h disk cache;
    on network failure the stale cache is returned (marked stale) — the
    dashboard must render without the network."""
    cached = _load_json(store.POPULAR_PLUGINS_PATH)
    fetched = _parse_ts(cached.get("fetched_at"))
    if not force and cached.get("payload") is not None and fetched is not None \
            and _time.time() - fetched < POPULAR_TTL_SECS:
        return {"fetched_at": cached["fetched_at"], "stale": False,
                "payload": cached["payload"]}
    try:
        req = urllib.request.Request(popular_url(),
                                     headers={"User-Agent": "overboard-dashboard"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError, OSError) as e:
        return {"fetched_at": cached.get("fetched_at"), "stale": True,
                "error": str(e), "payload": cached.get("payload")}
    fresh = {"fetched_at": _now_iso(), "payload": payload}
    store._atomic_write(store.POPULAR_PLUGINS_PATH, fresh)
    return {"fetched_at": fresh["fetched_at"], "stale": False, "payload": payload}


def _parse_ts(iso: "str | None") -> "float | None":
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return None

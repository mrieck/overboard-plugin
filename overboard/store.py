"""Config, .env, and state-cache load/save. No GTK imports here."""

import json
import os
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
ENV_PATH = PKG_DIR.parent / ".env"
CONFIG_PATH = PKG_DIR / "projects.json"
STATE_DIR = Path.home() / ".cache" / "overboard"
STATE_PATH = STATE_DIR / "state.json"
# AI content produced by the /overboard agent (Max sub). A SEPARATE file from
# state.json so the agent's MCP server and the dashboard never write the same
# file — the dashboard reads this and never writes it; the agent writes only it.
AI_PATH = STATE_DIR / "ai.json"
# Provider credentials (Bitbucket + GitHub), managed by the dashboard Settings
# panel. Machine-local and outside the plugin dir so it survives a reinstall.
CREDENTIALS_PATH = STATE_DIR / "credentials.json"

SCHEMA_VERSION = 3


class ConfigError(Exception):
    pass


def _parse_env(path: Path) -> dict:
    """Tiny stdlib .env parser (KEY=VALUE lines; # comments; optional quotes)."""
    values: dict[str, str] = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                values[k.strip()] = v
    except OSError:
        pass
    return values


def load_env() -> dict:
    env = _parse_env(ENV_PATH)
    missing = [k for k in ("ATLASSIAN_EMAIL", "BITBUCKET_API_TOKEN") if not env.get(k)]
    if missing:
        raise ConfigError(f"{ENV_PATH} is missing {', '.join(missing)}")
    # No Anthropic key needed: the /overboard agent (Max sub) produces all AI
    # content. Only the read-only Bitbucket token is required (for commits).
    return env


# ---- provider credentials + sources ---------------------------------------
def load_credentials() -> dict:
    try:
        with open(CREDENTIALS_PATH) as f:
            cred = json.load(f)
        return cred if isinstance(cred, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_credentials(cred: dict) -> None:
    _atomic_write(CREDENTIALS_PATH, cred)
    try:
        os.chmod(CREDENTIALS_PATH, 0o600)  # tokens live here — keep it private
    except OSError:
        pass


def load_sources(config: dict | None = None) -> list[dict]:
    """Enabled, credential-complete sources for the refresh loop:
      [{"provider":"bitbucket","workspace":...,"email":...,"token":...},
       {"provider":"github","workspace":None,"token":...}]
    Never raises — a missing/incomplete source is simply omitted, so the
    dashboard can boot with no credentials and onboard via the Settings panel."""
    cred = load_credentials()
    bb = dict(cred.get("bitbucket") or {})

    # Legacy migration: no credentials file yet, but the old .env + projects.json
    # workspace exist — synthesize a Bitbucket source so existing installs keep
    # working untouched.
    if not cred:
        env = _parse_env(ENV_PATH)
        if env.get("ATLASSIAN_EMAIL") and env.get("BITBUCKET_API_TOKEN"):
            ws = (config or {}).get("workspace")
            if ws is None:
                try:
                    ws = load_config().get("workspace")
                except ConfigError:
                    ws = None
            bb = {"enabled": True, "workspace": ws,
                  "email": env["ATLASSIAN_EMAIL"], "token": env["BITBUCKET_API_TOKEN"]}

    sources: list[dict] = []
    if bb.get("enabled") and bb.get("workspace") and bb.get("email") and bb.get("token"):
        sources.append({"provider": "bitbucket", "workspace": bb["workspace"],
                        "email": bb["email"], "token": bb["token"]})
    gh = dict(cred.get("github") or {})
    if gh.get("enabled") and gh.get("token"):
        sources.append({"provider": "github", "workspace": None, "token": gh["token"]})
    return sources


def load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            config = json.load(f)
    except FileNotFoundError:
        raise ConfigError(f"Config not found: {CONFIG_PATH}")
    except json.JSONDecodeError as e:
        raise ConfigError(f"{CONFIG_PATH} is not valid JSON: {e}")

    # 'workspace' is no longer required here — it moved into the per-provider
    # credentials (Settings panel). It's still read as a legacy fallback.
    # Projects are auto-discovered from the workspace by recent activity, so
    # 'projects' is optional. If present it's validated as a manual override.
    projects = config.get("projects")
    if projects is not None:
        if not isinstance(projects, list):
            raise ConfigError(f"{CONFIG_PATH}: 'projects' must be a list")
        for p in projects:
            if not p.get("name") or not isinstance(p.get("repos"), list):
                raise ConfigError(f"{CONFIG_PATH}: each project needs 'name' and 'repos'")
            for r in p["repos"]:
                if not r.get("slug") or not r.get("branch"):
                    raise ConfigError(
                        f"{CONFIG_PATH}: project {p['name']!r} has a repo without slug/branch"
                    )
    config.setdefault("refresh_interval_minutes", 60)
    config.setdefault("commit_window_days", 30)
    return config


def fresh_state() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "last_refresh": None,
        "last_refresh_ok": False,
        "last_error": None,
        "unseen_activity": False,
        "projects": {},
        "project_order": [],
        "discovery": {},
        # Local clone paths, keyed by machine node name then repo slug, so
        # different computers don't clobber each other's paths.
        "local_links": {},
        # Static + AI analysis per repo slug, cached on the repo's local HEAD.
        "analysis": {},
        # Content-hash keys of review items the user has dismissed ("OK"'d), so
        # they don't reappear across refreshes. Dashboard-owned.
        "dismissed_reviews": [],
    }


def load_state() -> dict:
    try:
        with open(STATE_PATH) as f:
            state = json.load(f)
        if state.get("schema_version") != SCHEMA_VERSION:
            return fresh_state()
        return state
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return fresh_state()


def save_state(state: dict) -> None:
    _atomic_write(STATE_PATH, state)


# ---- agent-owned AI content (ai.json) -------------------------------------
def fresh_ai() -> dict:
    # All agent-owned, keyed by name (summaries/digests) or repo slug
    # (architecture/prompts/setup/snippets). Written only via the MCP set_*
    # tools; the dashboard overlays these onto the static analysis at read time.
    return {
        "summaries": {},
        "digests": {},
        "architecture": {},
        "prompts": {},
        "setup": {},
        "snippets": {},
    }


def load_ai() -> dict:
    try:
        with open(AI_PATH) as f:
            ai = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return fresh_ai()
    base = fresh_ai()
    base.update({k: v for k, v in ai.items() if k in base and isinstance(v, dict)})
    return base


def save_ai(ai: dict) -> None:
    _atomic_write(AI_PATH, ai)


def _atomic_write(path: Path, data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)

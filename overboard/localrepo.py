"""Discover local git clones and map them to Bitbucket workspace/slug.

Per-machine: the same repo lives at different paths (or not at all) on
different computers, so the discovered map is stored in state keyed by the
machine's node name. No network — this only reads local `.git/config` files.
"""

import os
import platform
from pathlib import Path

# Common places developers keep their clones. Missing roots are skipped, and the
# walk never descends into a repo or noise dirs, so listing several is cheap. Users
# can override with `local_roots` in Settings / projects.json.
DEFAULT_ROOTS = [
    "~/Sites", "~/projects", "~/Projects", "~/dev", "~/Dev", "~/code", "~/Code",
    "~/work", "~/Work", "~/src", "~/repos", "~/git", "~/Documents/GitHub",
]

# Map an Overboard provider to the host its clones use, so discovery can tell a
# GitHub clone from a Bitbucket one under the same local root.
PROVIDER_HOSTS = {"bitbucket": "bitbucket.org", "github": "github.com"}

# Directories never worth walking into when hunting for repos.
_SKIP_DIRS = {
    "node_modules", "venv", ".venv", "__pycache__", "dist", "build",
    "site-packages", "vendor", "target", ".cache", "Library",
}


def machine_key() -> str:
    return platform.node() or "unknown"


def parse_remote(url: str) -> tuple[str, str, str] | None:
    """Extract (host, workspace, slug) from a git remote URL. Handles scp-style
    (git@github.com:ws/slug.git) and URL forms (https://user@bitbucket.org/ws/
    slug.git, ssh://git@host/ws/slug). host is "" if the URL carries no host."""
    url = url.strip()
    if url.endswith(".git"):
        url = url[:-4]
    # Normalize scp-style `git@host:ws/slug` to a slash-separated path.
    if "://" not in url and "@" in url and ":" in url:
        url = url.replace(":", "/", 1)
    if "://" in url:
        url = url.split("://", 1)[1]
    parts = [p for p in url.split("/") if p]
    if len(parts) < 2:
        return None
    host = parts[0].split("@")[-1].lower() if len(parts) >= 3 else ""
    return host, parts[-2], parts[-1]


def _matchers_from_sources(sources: list[dict]) -> list[tuple[str, str | None]]:
    """(host, workspace-or-None) rules from the configured sources. GitHub uses
    workspace=None (owner unknown up front) → any github.com clone matches."""
    out = []
    for s in sources or []:
        host = PROVIDER_HOSTS.get(s.get("provider"))
        if not host:
            continue
        ws = s.get("workspace")
        out.append((host, ws.lower() if ws else None))
    return out


def _matches(host: str, ws: str, matchers: list[tuple[str, str | None]]) -> bool:
    for mhost, mws in matchers:
        if mhost and mhost != host:
            continue
        if mws is not None and mws != ws.lower():
            continue
        return True
    return False


def git_remote_url(repo_dir: Path) -> str | None:
    """Read the origin remote URL from `<repo>/.git/config` (falls back to
    any remote if there's no origin)."""
    cfg = repo_dir / ".git" / "config"
    try:
        text = cfg.read_text()
    except OSError:
        return None
    urls: dict[str, str] = {}
    current: str | None = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[remote "):
            current = s.split('"')[1] if '"' in s else None
        elif s.startswith("["):
            current = None
        elif current and s.startswith("url") and "=" in s:
            urls[current] = s.split("=", 1)[1].strip()
    return urls.get("origin") or next(iter(urls.values()), None)


def discover(roots: list[str], matchers: list[tuple[str, str | None]]) -> dict[str, str]:
    """Walk `roots`, return {slug: absolute_path} for every git clone whose
    remote matches one of `matchers` ((host, workspace) rules from the sources).
    Stops descending once a repo is found, and prunes hidden/noise directories."""
    found: dict[str, str] = {}
    if not matchers:
        return found
    for root in roots:
        base = Path(os.path.expanduser(root))
        if not base.exists():
            continue
        for dirpath, dirnames, _filenames in os.walk(base):
            p = Path(dirpath)
            if (p / ".git").is_dir():
                dirnames[:] = []  # a repo — don't descend into it
                url = git_remote_url(p)
                if not url:
                    continue
                parsed = parse_remote(url)
                if not parsed:
                    continue
                host, ws, slug = parsed
                if _matches(host, ws, matchers):
                    found.setdefault(slug, str(p))
            else:
                dirnames[:] = [
                    d for d in dirnames
                    if d not in _SKIP_DIRS and not d.startswith(".")
                ]
    return found


def links_for_machine(state: dict) -> dict[str, str]:
    return state.get("local_links", {}).get(machine_key(), {})


def resolved_roots(config: dict | None = None, extra: list | None = None) -> list[str]:
    """Where to hunt for clones: any user-set roots FIRST — projects.json
    `local_roots`, the machine-local ones saved in Settings (credentials.json), and
    any `extra` — then the common defaults, deduped and order-preserving. Missing
    roots are skipped by `discover`, so listing several is cheap."""
    from . import store  # lazy: store has no localrepo dependency
    machine_local = store.load_credentials().get("local_roots") or []
    roots: list[str] = []
    for src in ((config or {}).get("local_roots") or [], extra or [], machine_local, DEFAULT_ROOTS):
        for r in src:
            r = str(r).strip()
            if r and r not in roots:
                roots.append(r)
    return roots


def update_state_links(state: dict, config: dict, sources: list[dict] | None = None,
                       extra_roots: list | None = None) -> dict[str, str]:
    """Rescan (across all configured sources) and store the discovered map under
    this machine's key. Returns the map for this machine."""
    roots = resolved_roots(config, extra_roots)
    links = discover(roots, _matchers_from_sources(sources or []))
    state.setdefault("local_links", {})[machine_key()] = links
    return links


if __name__ == "__main__":
    from . import store

    config = store.load_config()
    sources = store.load_sources(config)
    roots = config.get("local_roots") or DEFAULT_ROOTS
    links = discover(roots, _matchers_from_sources(sources))
    print(f"machine: {machine_key()}  roots: {roots}")
    print(f"{len(links)} local clone(s) across {len(sources)} source(s):")
    for slug, path in sorted(links.items()):
        print(f"  {slug:30s} {path}")

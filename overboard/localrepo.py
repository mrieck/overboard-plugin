"""Discover local git clones and map them to Bitbucket workspace/slug.

Per-machine: the same repo lives at different paths (or not at all) on
different computers, so the discovered map is stored in state keyed by the
machine's node name. No network — this only reads local `.git/config` files.
"""

import os
import platform
from pathlib import Path

DEFAULT_ROOTS = ["~/Sites"]

# Directories never worth walking into when hunting for repos.
_SKIP_DIRS = {
    "node_modules", "venv", ".venv", "__pycache__", "dist", "build",
    "site-packages", "vendor", "target", ".cache", "Library",
}


def machine_key() -> str:
    return platform.node() or "unknown"


def parse_workspace_slug(url: str) -> tuple[str, str] | None:
    """Extract (workspace, slug) from a git remote URL. Handles both
    scp-style (git@bitbucket.org:ws/slug.git) and URL forms
    (https://user@bitbucket.org/ws/slug.git, ssh://git@host/ws/slug)."""
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
    return parts[-2], parts[-1]


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


def discover(roots: list[str], workspace: str | None = None) -> dict[str, str]:
    """Walk `roots`, return {slug: absolute_path} for every git clone whose
    remote is in `workspace` (all workspaces if None). Stops descending once a
    repo is found, and prunes hidden/noise directories."""
    found: dict[str, str] = {}
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
                parsed = parse_workspace_slug(url)
                if not parsed:
                    continue
                ws, slug = parsed
                if workspace and ws.lower() != workspace.lower():
                    continue
                found.setdefault(slug, str(p))
            else:
                dirnames[:] = [
                    d for d in dirnames
                    if d not in _SKIP_DIRS and not d.startswith(".")
                ]
    return found


def links_for_machine(state: dict) -> dict[str, str]:
    return state.get("local_links", {}).get(machine_key(), {})


def update_state_links(state: dict, config: dict) -> dict[str, str]:
    """Rescan and store the discovered map under this machine's key. Returns
    the map for this machine."""
    roots = config.get("local_roots") or DEFAULT_ROOTS
    links = discover(roots, config.get("workspace"))
    state.setdefault("local_links", {})[machine_key()] = links
    return links


if __name__ == "__main__":
    from . import store

    config = store.load_config()
    roots = config.get("local_roots") or DEFAULT_ROOTS
    links = discover(roots, config.get("workspace"))
    print(f"machine: {machine_key()}  roots: {roots}")
    print(f"{len(links)} local clone(s) in workspace {config.get('workspace')!r}:")
    for slug, path in sorted(links.items()):
        print(f"  {slug:30s} {path}")

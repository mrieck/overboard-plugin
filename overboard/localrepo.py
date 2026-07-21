"""Discover local git clones and map them to Bitbucket workspace/slug.

Per-machine: the same repo lives at different paths (or not at all) on
different computers, so the discovered map is stored in state keyed by the
machine's node name. Discovery is offline (it only reads local `.git/config`
files); only `sync_status` talks to the network (one `git ls-remote` per call).
"""

from __future__ import annotations

import os
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path

# Common places developers keep their clones. Missing roots are skipped, and the
# walk never descends into a repo or noise dirs, so listing several is cheap. Users
# can override with `local_roots` in Settings / projects.json.
DEFAULT_ROOTS = [
    "~/projects", "~/Projects", "~/code", "~/Code", "~/dev", "~/Dev", "~/src",
    "~/repos", "~/git", "~/work", "~/Work", "~/workspace", "~/Developer",
    "~/Documents/GitHub", "~/Sites",
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
    workspace=None (owner unknown up front) → any github.com clone matches. A
    localgit source emits the wildcard (None, None) → any clone matches, so
    local-only mode tracks every repo under the roots."""
    out = []
    for s in sources or []:
        if s.get("provider") == "localgit":
            out.append((None, None))  # match any clone
            continue
        host = PROVIDER_HOSTS.get(s.get("provider"))
        if not host:
            continue
        ws = s.get("workspace")
        out.append((host, ws.lower() if ws else None))
    return out


def _has_localgit(sources: list[dict] | None) -> bool:
    return any((s or {}).get("provider") == "localgit" for s in (sources or []))


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


# Never let a credential prompt hang the background sync thread.
_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -oBatchMode=yes"}


def _git(repo_dir: Path, *args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo_dir, capture_output=True, text=True,
        timeout=timeout, env={**os.environ, **_GIT_ENV},
    )


def sync_status(repo_dir: Path) -> dict:
    """Compare a clone's current branch against its remote counterpart.

    Read-only — `git ls-remote` asks origin for the branch tip without
    fetching, so the clone is never mutated. Returns:
      status  "ok" (local == remote, clean) | "ahead" (unpushed commits and/or
              uncommitted tracked edits) | "behind" (remote has commits we
              don't — includes diverged) | "unknown" (detached / no remote /
              network or auth failure)
      plus branch, dirty, ahead (count or None), detail, checked_at.
    Untracked files are ignored throughout.
    """
    from .analysis import git_head  # lazy: analysis imports are heavier

    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def result(status: str, detail: str = "", branch: str = "",
               dirty: bool = False, ahead: int | None = None) -> dict:
        return {"status": status, "branch": branch, "dirty": dirty,
                "ahead": ahead, "detail": detail, "checked_at": checked_at}

    try:
        branch_p = _git(repo_dir, "symbolic-ref", "--short", "-q", "HEAD")
        branch = branch_p.stdout.strip()
        if branch_p.returncode != 0 or not branch:
            return result("unknown", "detached HEAD")

        dirty = bool(_git(repo_dir, "status", "--porcelain",
                          "--untracked-files=no").stdout.strip())
        head = git_head(repo_dir)
        if not head:
            return result("unknown", "could not resolve HEAD", branch, dirty)

        remote_p = _git(repo_dir, "ls-remote", "origin", f"refs/heads/{branch}")
        if remote_p.returncode != 0:
            err = (remote_p.stderr or "").strip().splitlines()
            return result("unknown", err[-1] if err else "ls-remote failed", branch, dirty)
        remote_sha = remote_p.stdout.split()[0] if remote_p.stdout.split() else None
    except (subprocess.TimeoutExpired, OSError) as e:
        return result("unknown", f"git failed: {e}")

    if not remote_sha:  # branch never pushed — everything local is unpushed
        return result("ahead", "branch not on origin", branch, dirty)
    if remote_sha == head:
        if dirty:
            return result("ahead", "uncommitted changes", branch, True)
        return result("ok", "", branch)

    try:
        # Remote tip is an ancestor we already have → strictly ahead.
        if _git(repo_dir, "merge-base", "--is-ancestor", remote_sha, head).returncode == 0:
            count_p = _git(repo_dir, "rev-list", "--count", f"{remote_sha}..{head}")
            ahead = int(count_p.stdout.strip()) if count_p.returncode == 0 else None
            parts = [f"{ahead or 'some'} commit{'' if ahead == 1 else 's'} not pushed"]
            if dirty:
                parts.append("uncommitted changes")
            return result("ahead", " · ".join(parts), branch, dirty, ahead)
    except (subprocess.TimeoutExpired, OSError):
        pass

    # Remote tip is unknown locally or not an ancestor: there is remote work
    # we haven't pulled (possibly diverged). Red wins; note local work too.
    parts = ["origin has new commits"]
    try:
        # If we have the remote object and HEAD isn't part of it, we've diverged.
        if (_git(repo_dir, "cat-file", "-e", remote_sha).returncode == 0
                and _git(repo_dir, "merge-base", "--is-ancestor", head, remote_sha).returncode != 0):
            parts.append("unpushed local commits")
    except (subprocess.TimeoutExpired, OSError):
        pass
    if dirty:
        parts.append("uncommitted changes")
    return result("behind", " · ".join(parts), branch, dirty)


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


def _localgit_slug(p: Path, seen: dict[str, str]) -> str:
    """A stable slug for a local clone: its remote slug when it has one (so a
    repo also tracked via GitHub/Bitbucket lines up on the same slug), else the
    repo directory name. Collisions against a *different* path are disambiguated
    with the parent folder so two `webapp` dirs don't clobber each other."""
    url = git_remote_url(p)
    parsed = parse_remote(url) if url else None
    slug = parsed[2] if parsed else p.name
    if slug in seen and seen[slug] != str(p):
        base = f"{p.parent.name}__{slug}"
        alt, i = base, 2
        while alt in seen and seen[alt] != str(p):
            alt = f"{base}_{i}"
            i += 1
        slug = alt
    return slug


def discover_localgit(roots: list[str]) -> list[dict]:
    """Every git clone under `roots`, as [{slug, path, branch}] — no remote or
    configured source required. Powers local-only tracking (commits read via
    `git log`). Same pruned walk as `discover`, stopping at the first `.git`."""
    seen: dict[str, str] = {}
    out: list[dict] = []
    for root in roots:
        base = Path(os.path.expanduser(root))
        if not base.exists():
            continue
        for dirpath, dirnames, _filenames in os.walk(base):
            p = Path(dirpath)
            if (p / ".git").is_dir():
                dirnames[:] = []  # a repo — don't descend into it
                slug = _localgit_slug(p, seen)
                seen[slug] = str(p)
                try:
                    bp = _git(p, "symbolic-ref", "--short", "-q", "HEAD")
                    branch = bp.stdout.strip() if bp.returncode == 0 else ""
                except (subprocess.TimeoutExpired, OSError):
                    branch = ""
                out.append({"slug": slug, "path": str(p), "branch": branch})
            else:
                dirnames[:] = [
                    d for d in dirnames
                    if d not in _SKIP_DIRS and not d.startswith(".")
                ]
    return out


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
    this machine's key. Returns the map for this machine. When local-only mode is
    on, every clone under the roots is linked (remote-parsed slug when present,
    else the dir name) so badges/analysis work for repos with no API source."""
    roots = resolved_roots(config, extra_roots)
    if _has_localgit(sources):
        links = {d["slug"]: d["path"] for d in discover_localgit(roots)}
    else:
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

"""Local-git provider — read commits/diffs straight from a local clone.

Stdlib only (subprocess `git`). Mirrors bitbucket.py / github.py so
providers.py can dispatch uniformly, but there is NO network session and NO
API: the clone's filesystem PATH travels on the repo/source dict. This is what
lets Overboard track a project with zero API keys — commit history comes from
`git log`, diffs from `git diff`.

Repos aren't enumerated here (a local folder can't list "your repos"); local
clones are discovered by localrepo.discover_localgit and injected into the
refresh by app._inject_localgit_repos.
"""

import subprocess

from overboard import localrepo
from overboard.errors import ProviderError

# Unit + record separators keep a commit subject (which may contain newlines or
# any punctuation) from ever colliding with the field/record delimiters.
_US = "\x1f"  # between fields within one commit
_RS = "\x1e"  # between commits
_FORMAT = f"%H{_US}%aI{_US}%an{_US}%s{_RS}"


class LocalGitError(ProviderError):
    pass


class _Session:
    """Marker only — localgit has no network auth; the path lives on the repo."""
    provider = "localgit"


def make_session(source: dict | None = None) -> _Session:
    return _Session()


def list_active_repos(session, cutoff) -> list[dict]:
    """localgit can't enumerate repos from an API — discovery injects them
    (see localrepo.discover_localgit / app._inject_localgit_repos). Kept so the
    provider interface stays uniform."""
    return []


def _run(path, *args) -> subprocess.CompletedProcess:
    try:
        return localrepo._git(path, *args)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise LocalGitError(str(path), f"git failed: {e}") from e


def fetch_recent_commits(path, branch, pagelen: int = 30) -> list[dict]:
    """Up to `pagelen` recent commits on `branch` (or current HEAD), newest
    first, as [{"hash", "date", "message", "author"}]. Reads local git only.
    An empty repo (no commits yet) returns []."""
    n = min(max(int(pagelen or 30), 1), 200)
    args = ["log", "--no-color", f"-n{n}", "--date-order", f"--pretty=format:{_FORMAT}"]
    if branch:
        args.append(branch)
    p = _run(path, *args)
    if p.returncode != 0:
        err = (p.stderr or "")
        # A brand-new repo with no commits isn't an error — just nothing to show.
        if "does not have any commits" in err or "unknown revision" in err:
            return []
        lines = err.strip().splitlines()
        raise LocalGitError(str(path), lines[-1] if lines else "git log failed")
    out = []
    for rec in p.stdout.split(_RS):
        rec = rec.strip("\n")
        if not rec:
            continue
        parts = rec.split(_US)
        if len(parts) < 4:
            continue
        out.append({
            "hash": parts[0],
            "date": parts[1],
            "message": parts[3].strip(),
            "author": parts[2],
        })
    return out


def fetch_diff(path, base: str, head: str) -> dict:
    """Unified diff for `base..head` from local git, as
    {"patch", "files": [{"path", "status", "patch"}], "truncated"}. Per-file
    patch is left empty (the aggregate patch is what callers consume), matching
    bitbucket.fetch_diff."""
    p = _run(path, "diff", "--no-color", base, head)
    if p.returncode != 0:
        lines = (p.stderr or "").strip().splitlines()
        raise LocalGitError(str(path), lines[-1] if lines else "git diff failed")
    files = []
    ns = _run(path, "diff", "--name-status", base, head)
    if ns.returncode == 0:
        for line in ns.stdout.splitlines():
            bits = line.split("\t")
            if len(bits) >= 2:
                files.append({"path": bits[-1], "status": bits[0][:1], "patch": ""})
    return {"patch": p.stdout or "", "files": files, "truncated": False}


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("usage: python -m overboard.localgit <repo-path> [branch]")
        sys.exit(2)
    path = sys.argv[1]
    branch = sys.argv[2] if len(sys.argv) > 2 else None
    try:
        commits = fetch_recent_commits(path, branch)
    except ProviderError as e:
        print(f"error: {e}")
        sys.exit(1)
    print(f"{len(commits)} commit(s) from {path} [{branch or 'HEAD'}]:")
    for c in commits:
        first = c["message"].splitlines()[0] if c["message"] else "(no message)"
        print(f"  {c['hash'][:8]}  {c['date'][:10]}  {first[:90]}")

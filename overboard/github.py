"""GitHub API client — stdlib only (urllib). Mirrors bitbucket.py's interface.

Auth is a personal access token (classic with `repo` scope, or fine-grained with
repository contents+metadata read). Unlike Bitbucket there's no email, and
GitHub paginates via the `Link` response header rather than a `next` body field.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from overboard.errors import AuthError, ProviderError

API = "https://api.github.com"
_TIMEOUT = 20


class GithubError(ProviderError):
    pass


class _Session:
    def __init__(self, token: str):
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "overboard",
        }


def make_session(token: str) -> _Session:
    return _Session(token)


_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


def _next_link(link_header: str | None) -> str | None:
    m = _NEXT_RE.search(link_header or "")
    return m.group(1) if m else None


def _get(session: _Session, url: str, params: dict | None, who: str):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=session.headers)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = resp.read()
            link = resp.headers.get("Link", "")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise AuthError(who, "GitHub auth failed (token invalid or missing scope)") from e
        if e.code == 403:
            raise GithubError(who, "GitHub forbidden or rate-limited") from e
        if e.code == 404:
            raise GithubError(who, "not found (repo or branch)") from e
        raise GithubError(who, f"HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise GithubError(who, f"network error: {e}") from e
    try:
        return json.loads(data), link
    except ValueError as e:
        raise GithubError(who, f"bad JSON response: {e}") from e


def list_active_repos(session: _Session, cutoff: "datetime", per_page: int = 100) -> list[dict]:
    """Every repo the token's user owns (public + private) pushed to since
    `cutoff`, as [{"slug", "workspace", "branch", "updated_on"}], most recently
    active first. GitHub sorts by pushed desc, so we stop once we cross the
    cutoff — same shape as bitbucket.list_active_repos."""
    url = f"{API}/user/repos"
    params: dict | None = {
        "affiliation": "owner",
        "visibility": "all",
        "sort": "pushed",
        "direction": "desc",
        "per_page": per_page,
    }
    repos: list[dict] = []
    while url:
        body, link = _get(session, url, params, "user/repos")
        params = None  # the `next` link already carries the query string
        stop = False
        for r in body:
            pushed = r.get("pushed_at") or ""
            dt = _parse_iso(pushed)
            if dt is not None and dt < cutoff:
                stop = True
                break
            repos.append({
                "slug": r.get("name", ""),
                "workspace": (r.get("owner") or {}).get("login", ""),
                "branch": r.get("default_branch") or "main",
                "updated_on": pushed,
            })
        if stop:
            break
        url = _next_link(link)
    return repos


def fetch_recent_commits(
    session: _Session, owner: str, repo: str, branch: str, pagelen: int = 30
) -> list[dict]:
    """Up to `pagelen` most recent commits on `branch`, newest first, as
    [{"hash", "date", "message", "author"}]. One request."""
    url = f"{API}/repos/{owner}/{repo}/commits"
    body, _ = _get(session, url, {"sha": branch, "per_page": min(max(pagelen, 1), 100)}, repo)
    out = []
    for c in body:
        commit = c.get("commit") or {}
        author = commit.get("author") or {}
        out.append({
            "hash": c.get("sha", ""),
            "date": author.get("date", ""),
            "message": (commit.get("message") or "").strip(),
            "author": author.get("name", ""),
        })
    return out


def fetch_diff(session: _Session, owner: str, repo: str, base: str, head: str) -> dict:
    """Unified diff for `base...head` via the compare API, as
    {"patch", "files": [{"path", "status", "patch"}], "truncated"}. GitHub caps
    the compare file list at 300 files; a changed file whose `patch` is omitted
    (too large / binary) also marks the result truncated. One request."""
    url = f"{API}/repos/{owner}/{repo}/compare/{base}...{head}"
    body, _ = _get(session, url, {"per_page": 100}, repo)
    files = body.get("files") or []
    out_files, chunks, truncated = [], [], False
    for f in files:
        path = f.get("filename", "")
        patch = f.get("patch")
        if patch:
            chunks.append(f"diff --git a/{path} b/{path}\n{patch}")
        elif f.get("changes"):
            truncated = True  # changed file whose patch GitHub omitted
        out_files.append({"path": path, "status": f.get("status", ""), "patch": patch or ""})
    if len(out_files) >= 300:
        truncated = True
    return {"patch": "\n".join(chunks), "files": out_files, "truncated": truncated}


def _parse_iso(iso: str):
    # GitHub timestamps look like 2026-07-10T12:00:00Z.
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None


if __name__ == "__main__":
    import sys

    from overboard import store

    if len(sys.argv) != 4:
        print("usage: python -m overboard.github <owner> <repo> <branch>")
        sys.exit(2)
    owner, repo, branch = sys.argv[1], sys.argv[2], sys.argv[3]
    cred = store.load_credentials().get("github") or {}
    token = cred.get("token") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("no GitHub token (Settings panel, credentials.json, or $GITHUB_TOKEN)")
        sys.exit(1)
    session = make_session(token)
    try:
        commits = fetch_recent_commits(session, owner, repo, branch)
    except ProviderError as e:
        print(f"error: {e}")
        sys.exit(1)
    print(f"{len(commits)} commits from {owner}/{repo}/{branch}:")
    for c in commits:
        first = c["message"].splitlines()[0] if c["message"] else "(no message)"
        print(f"  {c['hash'][:8]}  {c['date'][:10]}  {first[:90]}")

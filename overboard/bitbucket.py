"""Bitbucket Cloud API client — stdlib only (urllib). Fetch recent commits."""

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

API_BASE = "https://api.bitbucket.org/2.0"
FIELDS = "values.hash,values.date,values.message,values.author.raw"
_TIMEOUT = 20


class BitbucketError(Exception):
    def __init__(self, slug: str, reason: str):
        self.slug = slug
        self.reason = reason
        super().__init__(f"{slug}: {reason}")


class AuthError(BitbucketError):
    pass


class _Session:
    """Minimal auth holder — the stdlib stand-in for requests.Session."""

    def __init__(self, email: str, token: str):
        raw = f"{email}:{token}".encode()
        self.auth_header = "Basic " + base64.b64encode(raw).decode()


def make_session(email: str, token: str) -> _Session:
    return _Session(email, token)


def _get(session: _Session, url: str, params: dict | None, who: str) -> dict:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, headers={"Authorization": session.auth_header, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise AuthError(who, "Bitbucket auth failed (token invalid or expired)") from e
        if e.code == 404:
            raise BitbucketError(who, "not found (repo or branch)") from e
        raise BitbucketError(who, f"HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise BitbucketError(who, f"network error: {e}") from e
    try:
        return json.loads(data)
    except ValueError as e:
        raise BitbucketError(who, f"bad JSON response: {e}") from e


def fetch_recent_commits(
    session: _Session, workspace: str, slug: str, branch: str, pagelen: int = 30
) -> list[dict]:
    """Return up to `pagelen` most recent commits as
    [{"hash", "date", "message", "author"}], newest first. One request."""
    url = f"{API_BASE}/repositories/{workspace}/{slug}/commits/{branch}"
    body = _get(session, url, {"pagelen": pagelen, "fields": FIELDS}, slug)
    return [
        {
            "hash": v.get("hash", ""),
            "date": v.get("date", ""),
            "message": v.get("message", "").strip(),
            "author": v.get("author", {}).get("raw", ""),
        }
        for v in body.get("values", [])
    ]


def list_active_repos(
    session: _Session, workspace: str, cutoff: "datetime", pagelen: int = 100
) -> list[dict]:
    """Return every repo in the workspace pushed to since `cutoff`, as
    [{"slug", "branch", "updated_on"}], most recently active first. Bitbucket
    sorts by -updated_on, so we stop paging once we cross the cutoff."""
    url = f"{API_BASE}/repositories/{workspace}"
    params: dict | None = {
        "sort": "-updated_on",
        "pagelen": pagelen,
        "fields": "values.slug,values.updated_on,values.mainbranch.name,next",
    }
    repos: list[dict] = []
    while url:
        body = _get(session, url, params, workspace)
        stop = False
        for v in body.get("values", []):
            updated = v.get("updated_on", "")
            dt = _parse_iso(updated)
            if dt is not None and dt < cutoff:
                stop = True
                break
            repos.append({
                "slug": v.get("slug", ""),
                "branch": (v.get("mainbranch") or {}).get("name") or "main",
                "updated_on": updated,
            })
        if stop:
            break
        url = body.get("next")
        params = None  # `next` already carries the query string
    return repos


def _parse_iso(iso: str):
    try:
        return datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None


if __name__ == "__main__":
    import sys

    from overboard import store

    if len(sys.argv) != 3:
        print("usage: python -m overboard.bitbucket <slug> <branch>")
        sys.exit(2)
    slug, branch = sys.argv[1], sys.argv[2]
    env = store.load_env()
    config = store.load_config()
    session = make_session(env["ATLASSIAN_EMAIL"], env["BITBUCKET_API_TOKEN"])
    try:
        commits = fetch_recent_commits(session, config["workspace"], slug, branch)
    except BitbucketError as e:
        print(f"error: {e}")
        sys.exit(1)
    print(f"{len(commits)} commits from {slug}/{branch}:")
    for c in commits:
        first_line = c["message"].splitlines()[0] if c["message"] else "(no message)"
        print(f"  {c['hash'][:8]}  {c['date'][:10]}  {first_line[:90]}")

"""Provider dispatch — one interface over Bitbucket + GitHub.

A `source` is a credential-bearing config dict from `store.load_sources()`:
  {"provider": "bitbucket", "workspace": "...", "email": "...", "token": "..."}
  {"provider": "github",    "workspace": None,  "token": "..."}

A `repo` is a normalized dict produced by `active_repos()`:
  {"provider", "workspace", "slug", "branch", "updated_on"}

Both clients raise the shared errors from errors.py (AuthError / ProviderError),
so callers can handle failures per-source without knowing which provider it was.
"""

from datetime import datetime

from overboard import bitbucket, github, localgit
from overboard.errors import AuthError, ProviderError  # re-exported

__all__ = ["AuthError", "ProviderError", "make_session", "active_repos", "commits", "diff", "PROVIDERS"]

PROVIDERS = ("bitbucket", "github", "localgit")


def make_session(source: dict):
    p = source["provider"]
    if p == "bitbucket":
        return bitbucket.make_session(source["email"], source["token"])
    if p == "github":
        return github.make_session(source["token"])
    if p == "localgit":
        return localgit.make_session(source)
    raise ProviderError(p, "unknown provider")


def active_repos(source: dict, session, cutoff: "datetime") -> list[dict]:
    """Repos active since `cutoff`, normalized with their provider + workspace."""
    p = source["provider"]
    if p == "bitbucket":
        raw = bitbucket.list_active_repos(session, source["workspace"], cutoff)
    elif p == "github":
        raw = github.list_active_repos(session, cutoff)
    elif p == "localgit":
        # A local folder can't enumerate "your repos" — discovery injects them
        # into the refresh instead (localrepo.discover_localgit).
        raw = localgit.list_active_repos(session, cutoff)
    else:
        raise ProviderError(p, "unknown provider")
    return [
        {
            "provider": p,
            "workspace": r.get("workspace") or source.get("workspace") or "",
            "slug": r["slug"],
            # The provider reports the repo's real default branch (GitHub
            # default_branch / Bitbucket mainbranch), so master/develop/trunk are
            # honored; "main" is only a last resort if the API omits it entirely.
            "branch": r.get("branch") or "main",
            "updated_on": r.get("updated_on", ""),
            # Local filesystem path for localgit repos (None for API providers).
            "path": r.get("path"),
        }
        for r in raw
    ]


def commits(repo: dict, session, pagelen: int = 30) -> list[dict]:
    """Recent commits for one normalized repo, via its own provider."""
    p = repo["provider"]
    if p == "bitbucket":
        return bitbucket.fetch_recent_commits(
            session, repo["workspace"], repo["slug"], repo["branch"], pagelen
        )
    if p == "github":
        return github.fetch_recent_commits(
            session, repo["workspace"], repo["slug"], repo["branch"], pagelen
        )
    if p == "localgit":
        return localgit.fetch_recent_commits(repo["path"], repo.get("branch"), pagelen)
    raise ProviderError(p, "unknown provider")


def diff(repo: dict, session, base: str, head: str) -> dict:
    """Unified diff between `base` and `head` for one normalized repo, via its
    own provider. Returns {"patch", "files", "truncated"}."""
    p = repo["provider"]
    if p == "bitbucket":
        return bitbucket.fetch_diff(session, repo["workspace"], repo["slug"], base, head)
    if p == "github":
        return github.fetch_diff(session, repo["workspace"], repo["slug"], base, head)
    if p == "localgit":
        return localgit.fetch_diff(repo["path"], base, head)
    raise ProviderError(p, "unknown provider")

"""Overboard — dashboard + headless entry point.

Run in the panel:      python -m overboard.app        (from the parent directory)
Headless refresh:      python -m overboard.app --once
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from overboard import analysis, debuglog, diagram, events, localgit, localrepo, manager, providers, store

APP_TITLE = "Overboard"
ICON_NORMAL = "applications-development"
ICON_ACTIVITY = "starred"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_date(iso: str) -> datetime | None:
    try:
        # 3.9/3.10 fromisoformat can't parse a trailing "Z"
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _logical_day_key(dt: datetime, start_hour: int = 5) -> str:
    """Local calendar date under a day that begins at `start_hour` (default 5am):
    a commit before 5am local counts toward the previous day. Shifting back by
    `start_hour` and taking the date does exactly that."""
    return (dt.astimezone() - timedelta(hours=start_hour)).strftime("%Y-%m-%d")


def _updated_at(repo: dict) -> datetime:
    """Parsed activity time for cross-provider comparison. Bitbucket and GitHub
    format timestamps differently (`…+00:00` vs `…Z`), so compare real dates,
    not raw strings."""
    return _parse_date(repo.get("updated_on", "")) or _EPOCH


# Moved to store.slug_signature so mcp_server/manager can compute it without
# importing the heavy app module. Kept as a local alias for existing call sites.
_slug_signature = store.slug_signature


def _days_until(date_str: str):
    """Whole days from today (local) to an ISO date, or None. Negative = overdue."""
    try:
        d = date.fromisoformat(date_str)
    except (ValueError, TypeError):
        return None
    return (d - datetime.now().astimezone().date()).days


def _review_key(project: str, text: str) -> str:
    """Stable content key for a review item, so a dismissed item stays dismissed
    across refreshes (and re-appears only if its text actually changes)."""
    return hashlib.sha1(f"{project}\0{text}".encode()).hexdigest()


def _prefix_group(slugs: list[str]) -> dict[str, list[str]]:
    """Fallback grouping when AI is unavailable: cluster by the name stem
    before the first '-'. A repo with no stem-mates keeps its FULL slug as the
    group name — a lone `demo-video-maker` must not appear as a mysterious
    "demo" (a truncated name reads as an unknown project until the assistant
    groups it properly)."""
    by_stem: dict[str, list[str]] = {}
    for s in slugs:
        by_stem.setdefault(s.split("-", 1)[0], []).append(s)
    return {(members[0] if len(members) == 1 else stem): members
            for stem, members in by_stem.items()}


def _gather_active_repos(sources, sessions, cutoff) -> tuple[dict, list[str]]:
    """Merge repos active since `cutoff` across every configured source. Returns
    (repo_meta, source_errors) where repo_meta is {slug: {provider, workspace,
    slug, branch, updated_on}}. A source that fails to list is recorded in
    source_errors but doesn't stop the others."""
    meta: dict[str, dict] = {}
    errors: list[str] = []
    for src in sources:
        session = sessions.get(src["provider"])
        if session is None:
            continue
        try:
            repos = providers.active_repos(src, session, cutoff)
        except providers.AuthError:
            errors.append(f"{src['provider']}: auth failed")
            continue
        except providers.ProviderError as e:
            errors.append(f"{src['provider']}: {e.reason}")
            continue
        for r in repos:
            slug = r["slug"]
            prev = meta.get(slug)
            # Same repo on Bitbucket + GitHub (e.g. a project you moved to GitHub):
            # keep the most recently active origin, and remember the other so the
            # UI can show it lives on both.
            if prev is None:
                meta[slug] = r
            elif _updated_at(r) > _updated_at(prev):
                r.setdefault("also_providers", []).extend(
                    [prev["provider"], *prev.get("also_providers", [])])
                meta[slug] = r
            else:
                prev.setdefault("also_providers", []).append(r["provider"])
    return meta, errors


def _inject_localgit_repos(repo_meta: dict, config: dict, cutoff=None) -> tuple:
    """Merge locally-discovered clones (local-only mode) into `repo_meta`. A slug
    not already seen from an API source is added as a provider:'localgit' repo
    (commits read from local git); a slug already present from Bitbucket/GitHub is
    left as-is (that stays the commit source, so commits pushed from other
    machines still count) and just records the local path + 'localgit' in
    also_providers (hybrid). With `hide_idle_local` (Settings, default on) a clone
    whose head commit predates `cutoff` is skipped — the localgit mirror of how
    the API providers already drop repos quiet past the window. Clones with no
    parseable head date (fresh/empty repos being set up) are always kept.
    Returns (injected, hidden_idle) counts."""
    injected = hidden = 0
    hide_idle = bool(config.get("hide_idle_local")) and cutoff is not None
    for d in localrepo.discover_localgit(localrepo.resolved_roots(config)):
        slug, path, branch = d["slug"], d["path"], d.get("branch") or ""
        prev = repo_meta.get(slug)
        if prev is None:
            # Bound updated_on to the head commit date so ordering/cutoff work.
            try:
                head = localgit.fetch_recent_commits(path, branch, 1)
            except providers.ProviderError:
                head = []
            if hide_idle:
                head_dt = _parse_date(head[0]["date"]) if head else None
                if head_dt is not None and head_dt < cutoff:
                    hidden += 1
                    continue
            repo_meta[slug] = {
                "provider": "localgit", "workspace": "", "slug": slug,
                "branch": branch or "main", "path": path,
                "updated_on": head[0]["date"] if head else "",
            }
            injected += 1
        else:
            prev["path"] = path
            if prev.get("provider") != "localgit":
                aps = prev.setdefault("also_providers", [])
                if "localgit" not in aps:
                    aps.append("localgit")
    return injected, hidden


def _resolve_grouping(slugs: list[str], old_state: dict) -> dict:
    """Decide how the live repo `slugs` cluster into projects, and each project's
    display name. AI-driven (assistant-owned ai.json), with a deterministic
    prefix-stem fallback. Returns {key: {"display", "members", "source"}} where
    source is "agent" (AI-decided) or "prefix" (heuristic, shown dimmed).

    Three tiers:
      1. exact  — ai.json grouping covers the whole live set → use it as-is.
      2. partial — repos added/removed since the grouping → keep AI groups for the
                   slugs they still cover, prefix-group only the leftovers.
      3. bootstrap — no AI grouping yet → prefix-group everything (dimmed) until
                     the assistant's next pass sets one.
    An offline refresh with no ai.json falls back to the last discovery cache."""
    live = set(slugs)
    ai_groups = (store.load_ai().get("grouping") or {}).get("groups") or {}

    eff: dict = {}
    covered: set = set()
    for key, entry in ai_groups.items():
        members = [s for s in (entry.get("repos") or []) if s in live and s not in covered]
        if not members:
            continue
        eff[key] = {"display": entry.get("display") or key, "members": members, "source": "agent"}
        covered.update(members)

    leftover = [s for s in slugs if s not in covered]
    if not ai_groups and leftover:
        # No AI grouping at all — reuse the last discovery cache if the live set is
        # unchanged (keeps the board stable when offline), else prefix-group.
        cache = old_state.get("discovery") or {}
        if cache.get("signature") == store.slug_signature(slugs) and cache.get("groups"):
            fallback = {k: [s for s in v if s in live] for k, v in cache["groups"].items()}
        else:
            fallback = _prefix_group(leftover)
    else:
        fallback = _prefix_group(leftover)

    for key, members in fallback.items():
        members = [s for s in members if s in live and s not in covered]
        if not members:
            continue
        if key in eff:
            eff[key]["members"].extend(members)
        else:
            eff[key] = {"display": key, "members": members, "source": "prefix"}
        covered.update(members)
    return eff


def resolve_projects(config: dict, repo_meta: dict, old_state: dict) -> tuple[list[dict], dict]:
    """Group merged repos into projects. Grouping is AI-driven (assistant-owned
    ai.json), with a deterministic prefix-stem fallback — see `_resolve_grouping`.
    Returns (projects, discovery_cache); each project carries a stable `name`
    (identity key), a human `display`, its repos, and `grouping_source`."""
    updated_of = {slug: r.get("updated_on", "") for slug, r in repo_meta.items()}
    slugs = list(repo_meta)
    signature = _slug_signature(slugs)

    eff = _resolve_grouping(slugs, old_state)

    projects = []
    for key, g in eff.items():
        repos_in = [dict(repo_meta[s]) for s in g["members"] if s in repo_meta]
        if repos_in:
            latest = max(updated_of[r["slug"]] for r in repos_in)
            projects.append({
                "name": key,
                "display": g["display"],
                "grouping_source": g["source"],
                "repos": repos_in,
                "_latest": latest,
            })
    projects.sort(key=lambda p: p["_latest"], reverse=True)
    for p in projects:
        del p["_latest"]

    groups = {p["name"]: [r["slug"] for r in p["repos"]] for p in projects}
    return projects, {"signature": signature, "groups": groups}


def perform_refresh(config: dict, sources: list[dict], old_state: dict) -> dict:
    """Fetch commits across all sources (Bitbucket + GitHub), compute activity.
    Safe on a worker thread; it only READS ai.json (for grouping) and never
    writes it — the assistant owns ai.json, this owns state.json. Per-repo and
    per-source failures are recorded, not raised; one bad source never blanks
    another."""
    new_state = store.fresh_state()
    new_state["unseen_activity"] = old_state.get("unseen_activity", False)
    new_state["local_links"] = old_state.get("local_links", {})
    new_state["analysis"] = old_state.get("analysis", {})
    new_state["dismissed_reviews"] = old_state.get("dismissed_reviews", [])
    new_state["hidden_work_reviews"] = old_state.get("hidden_work_reviews", [])
    new_state["excluded_repos"] = old_state.get("excluded_repos", [])
    new_state["excluded_projects"] = old_state.get("excluded_projects", [])

    window_days = config["commit_window_days"]
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    # The activity grid is window-independent: 5 calendar week rows (Mon-Sun,
    # newest on top), which reach back at most 34 days. Daily counts always
    # cover that span, even when commit_window_days is shorter.
    grid_cutoff = datetime.now(timezone.utc) - timedelta(days=35)
    start_hour = config.get("day_start_hour", 5)
    today_key = _logical_day_key(datetime.now(timezone.utc), start_hour)

    new_state["last_refresh"] = _now_iso()
    if not sources:
        new_state["last_refresh_ok"] = True
        new_state["last_error"] = "No sources configured — add a token in Settings ⚙"
        new_state["project_order"] = []
        return new_state

    sessions: dict = {}
    for src in sources:
        try:
            sessions[src["provider"]] = providers.make_session(src)
        except providers.ProviderError:
            pass

    with debuglog.timer(f"refresh: list active repos ({len(sources)} source(s))"):
        repo_meta, source_errors = _gather_active_repos(sources, sessions, cutoff)
    if any(s.get("provider") == "localgit" for s in sources):
        with debuglog.timer("refresh: discover local clones (localgit)"):
            n, n_idle = _inject_localgit_repos(repo_meta, config, cutoff)
        debuglog.log(f"refresh: localgit injected {n} local-only repo(s), hid {n_idle} idle")
    # Drop excluded repos before grouping, so state/projects, the view, and every
    # MCP tool downstream are all clean. Keyed by bare slug: after the dedup/merge
    # above, an API repo and its local clone are one record — excluding a slug
    # hides that repo everywhere (undo in Settings).
    for slug in new_state["excluded_repos"]:
        repo_meta.pop(slug, None)
    projects, discovery = resolve_projects(config, repo_meta, old_state)
    # Drop excluded projects (and all their repos) after grouping, so a single
    # project-level exclusion hides every repo regardless of how many there are.
    excluded_projs = set(new_state["excluded_projects"])
    if excluded_projs:
        projects = [p for p in projects if p["name"] not in excluded_projs]
    new_state["discovery"] = discovery
    n_repos = sum(len(p["repos"]) for p in projects)
    debuglog.log(f"refresh: {len(projects)} project(s), {n_repos} repo(s) — fetching commits per repo")

    for project in projects:
        name = project["name"]
        old_repos = old_state.get("projects", {}).get(name, {}).get("repos", {})

        repos_state: dict = {}
        daily_counts: dict[str, int] = {}
        commits_today = 0
        latest: datetime | None = None

        for repo in project["repos"]:
            slug, branch = repo["slug"], repo["branch"]
            provider, workspace = repo["provider"], repo["workspace"]
            old_repo = old_repos.get(slug, {})
            base = {"provider": provider, "workspace": workspace, "branch": branch,
                    "also_providers": sorted(set(repo.get("also_providers") or []))}
            if repo.get("path"):
                base["path"] = repo["path"]  # localgit needs it to read commits/diffs
            session = sessions.get(provider)
            if session is None:
                repos_state[slug] = {**old_repo, **base, "fetch_error": "provider not configured"}
                continue
            try:
                with debuglog.timer(f"refresh: fetch commits {slug} ({provider})"):
                    commits = providers.commits(repo, session)
            except providers.AuthError:
                repos_state[slug] = {**old_repo, **base, "fetch_error": "auth failed"}
                continue
            except providers.ProviderError as e:
                repos_state[slug] = {**old_repo, **base, "fetch_error": e.reason}
                continue

            head = commits[0]["hash"] if commits else None
            head_date = commits[0]["date"] if commits else old_repo.get("last_date")
            repos_state[slug] = {**base, "head": head, "last_date": head_date, "fetch_error": None}
            if head and head != old_repo.get("head"):
                new_state["unseen_activity"] = True

            # Idle time comes from the head commit even when it's outside the
            # window, so a long-dormant project shows an age rather than "no data".
            head_dt = _parse_date(head_date) if head_date else None
            if head_dt and (latest is None or head_dt > latest):
                latest = head_dt

            for c in commits:
                d = _parse_date(c["date"])
                if d is None or d < grid_cutoff:
                    continue
                key = _logical_day_key(d, start_hour)
                daily_counts[key] = daily_counts.get(key, 0) + 1
                if key == today_key:
                    commits_today += 1

        days_idle = (datetime.now(timezone.utc) - latest).days if latest else None

        head_sig = "|".join(f"{s}:{r.get('head')}" for s, r in sorted(repos_state.items()))
        new_state["projects"][name] = {
            "display": project.get("display", name),
            "grouping_source": project.get("grouping_source", "prefix"),
            "commits_today": commits_today,
            "days_idle": days_idle,
            "daily_counts": daily_counts,
            "head_sig": head_sig,
            "latest_commit_date": latest.isoformat(timespec="seconds") if latest else None,
            "repos": repos_state,
        }

    # Every configured source failed to even list its repos → treat as offline
    # and keep the previously known projects rather than showing a blank board.
    if source_errors and not repo_meta:
        new_state["last_refresh_ok"] = False
        new_state["last_error"] = "; ".join(source_errors[:3])
        for name, proj in old_state.get("projects", {}).items():
            new_state["projects"].setdefault(name, proj)
        new_state["project_order"] = old_state.get("project_order") or list(new_state["projects"])
        return new_state

    # Order by the actual latest commit date (what the chips/day-grids show),
    # newest first — not the provider's repo `updated_on`, which bumps on
    # non-commit events and made the order disagree with the visible recency.
    # Projects with no dated commit sink to the bottom.
    new_state["project_order"] = sorted(
        (p["name"] for p in projects),
        key=lambda n: new_state["projects"].get(n, {}).get("latest_commit_date") or "",
        reverse=True,
    )
    fetch_errors = [
        f"{slug}: {r['fetch_error']}"
        for proj in new_state["projects"].values()
        for slug, r in proj["repos"].items()
        if r.get("fetch_error")
    ]
    errors = source_errors + fetch_errors
    new_state["last_refresh_ok"] = True
    if errors:
        new_state["last_error"] = "; ".join(errors[:3])
    return new_state


def tooltip_text(state: dict) -> str:
    if not state.get("last_refresh_ok"):
        when = _parse_date(state.get("last_refresh") or "")
        stamp = when.astimezone().strftime("%H:%M") if when else "never"
        return f"{APP_TITLE} — offline, showing cached data ({stamp})"
    active = [
        (name, p["commits_today"])
        for name, p in state.get("projects", {}).items()
        if p.get("commits_today")
    ]
    if not active:
        return f"{APP_TITLE} — no commits today"
    total = sum(n for _, n in active)
    return (
        f"{APP_TITLE} — {total} new commit{'s' if total != 1 else ''} today "
        f"across {len(active)} project{'s' if len(active) != 1 else ''}"
    )


def run_once() -> int:
    config = store.load_config()
    sources = store.load_sources(config)
    if not sources:
        print("No sources configured. Add a Bitbucket/GitHub token in the "
              "dashboard Settings, or create ~/.cache/overboard/credentials.json.")
        return 1
    state = perform_refresh(config, sources, store.load_state())
    store.save_state(state)
    ai_sum = store.load_ai().get("summaries", {})

    for name in state.get("project_order", []):
        p = state["projects"].get(name)
        if p is None:
            continue
        if p["commits_today"]:
            chip = f"{p['commits_today']} commit(s) today"
        elif p["days_idle"] is not None:
            chip = f"{p['days_idle']} day(s) ago"
        else:
            chip = "no data"
        window_total = sum((p.get("daily_counts") or {}).values())
        print(f"● {name}  [{chip}]  ({window_total} commits in window)")
        summary = (ai_sum.get(name) or {}).get("text") or "(no summary yet — run /overboard)"
        print(f"  {summary}")
        errors = {s: r["fetch_error"] for s, r in p["repos"].items() if r.get("fetch_error")}
        if errors:
            print(f"  ⚠ fetch errors: {errors}")
    if state.get("last_error"):
        print(f"\nlast_error: {state['last_error']}")
    print(f"\nrefresh ok: {state['last_refresh_ok']}  state: {store.STATE_PATH}")
    return 0 if state["last_refresh_ok"] else 1


def _tildify(path: str) -> str:
    """Show an absolute path under $HOME as ~/… for a friendlier UI."""
    home = str(Path.home())
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def _decode_claude_dir(name: str) -> str | None:
    """Decode a ~/.claude/projects dir name back to a real working directory.

    Claude Code encodes the absolute cwd by replacing '/' with '-' (a leading
    '/' becomes a leading '-'). That's lossy — a directory whose real name
    contains '-' (e.g. `overboard-plugin`) is indistinguishable from a
    separator. So we rebuild left-to-right against the filesystem, taking the
    LONGEST '-'-joined run that is a real directory at each level. Returns the
    resolved absolute path, or None if it can't be resolved to an existing dir."""
    if not name.startswith("-"):
        return None
    segs = [s for s in name.split("-") if s]
    if not segs:
        return None
    cur = Path("/")
    i = 0
    while i < len(segs):
        best = None
        acc = ""
        for j in range(i, len(segs)):
            acc = segs[j] if j == i else acc + "-" + segs[j]
            if (cur / acc).is_dir():
                best = (j, acc)
        if best is None:
            return None
        cur = cur / best[1]
        i = best[0] + 1
    return str(cur)


def _shallow_repo_count(root: str) -> int:
    """How many git clones sit directly under `root` (root itself + immediate
    children). One level only — cheap, so scanning many candidate roots is fast."""
    base = Path(root)
    n = 1 if (base / ".git").is_dir() else 0
    try:
        for child in base.iterdir():
            if child.is_dir() and (child / ".git").is_dir():
                n += 1
    except OSError:
        pass
    return n


def _detect_candidate_roots() -> list[dict]:
    """Candidate clone roots (with a repo count each) for onboarding: the parents
    of Claude Code's known working dirs (~/.claude/projects) plus the common
    defaults and any already-saved roots. Only existing roots with ≥1 clone are
    returned, most repos first, capped for a clean UI."""
    roots: set[str] = set()
    claude = Path.home() / ".claude" / "projects"
    try:
        entries = list(claude.iterdir())
    except OSError:
        entries = []
    for d in entries:
        wd = _decode_claude_dir(d.name)
        if wd:
            roots.add(str(Path(wd).parent))  # the folder that CONTAINS the clone
    for r in localrepo.DEFAULT_ROOTS:
        p = Path(os.path.expanduser(r))
        if p.is_dir():
            roots.add(str(p))
    for r in (store.load_credentials().get("local_roots") or []):
        p = Path(os.path.expanduser(r))
        if p.is_dir():
            roots.add(str(p))
    out = [{"root": _tildify(r), "repo_count": _shallow_repo_count(r)} for r in roots]
    out = [c for c in out if c["repo_count"] > 0]
    out.sort(key=lambda c: c["repo_count"], reverse=True)
    return out[:12]


class Api:
    """JS bridge for the pywebview UI. Every method returns JSON-serializable
    data and runs on pywebview's worker thread, so blocking calls (network, AI)
    are fine — the JS side awaits them."""

    def __init__(self, config: dict):
        self.config = config
        self.sources = store.load_sources(config)
        self.state = store.load_state()
        self._refreshing = False
        self._lock = threading.Lock()
        self._analysis_lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._window = None
        # First run on this machine: discover local clones so badges work
        # immediately, before any manual rescan.
        if not localrepo.links_for_machine(self.state):
            try:
                localrepo.update_state_links(self.state, self.config, self.sources)
                store.save_state(self.state)
            except Exception:  # discovery must never block startup
                pass
        # Static analysis is free (no API), so pre-warm it in the background so
        # every project's details are ready without the user asking.
        self._kick_analyses()
        self._kick_sync()
        self._kick_event_compact()

    # ---- read -----------------------------------------------------------
    def get_view(self) -> dict:
        return self._build_view()

    def _build_view(self) -> dict:
        state = self.state
        ai = store.load_ai()
        ai_sum, ai_dig = ai.get("summaries", {}), ai.get("digests", {})
        ai_work = ai.get("work_reviews", {})
        links = localrepo.links_for_machine(state)
        sync_map = state.get("local_sync", {}).get(localrepo.machine_key(), {})
        analyzed = state.get("analysis", {})
        activity = state.get("activity", {})
        dismissed = set(state.get("dismissed_reviews", []))
        hidden_work = set(state.get("hidden_work_reviews", []))
        all_ctx = store.load_context()
        projects = []
        for name in state.get("project_order", []):
            p = state.get("projects", {}).get(name)
            if p is None:
                continue
            slugs = list(p.get("repos", {}))
            repos = [
                {
                    "slug": slug,
                    "branch": r.get("branch"),
                    "provider": r.get("provider"),
                    "also_providers": [x for x in (r.get("also_providers") or []) if x != r.get("provider")],
                    "local_path": links.get(slug),
                    "sync": sync_map.get(slug),
                    "fetch_error": r.get("fetch_error"),
                    "has_analysis": slug in analyzed,
                }
                for slug, r in p.get("repos", {}).items()
            ]
            # Live activity feed + self-reported flags (from events).
            feed, flags = [], []
            for slug in slugs:
                a = activity.get(slug)
                if not a:
                    continue
                for e in a.get("recent", []):
                    feed.append({**e, "repo": slug})
                    if e.get("type") == "flag" and e.get("note"):
                        flags.append(e["note"])
            feed.sort(key=lambda e: e.get("ts", 0), reverse=True)
            # AI content is overlaid from the agent-owned ai.json. Human-flagged
            # items come first, then the agent's digest review items. Anything
            # the user has dismissed ("OK") is filtered out.
            dig = ai_dig.get(name) or {}
            review = [
                r for r in (flags + list(dig.get("review", [])))
                if _review_key(name, r) not in dismissed
            ]
            # Compact scheduled-launch summary for the sidebar (full detail is
            # fetched via get_context when a project is selected).
            proj_ctx = all_ctx.get(name) or {}
            active = proj_ctx.get("active_launch")
            launch = None
            if active:
                launch = {
                    "type": active.get("type", ""),
                    "title": active.get("title", ""),
                    "target_date": active.get("target_date", ""),
                    "days_until": _days_until(active.get("target_date", "")),
                }
            projects.append({
                "name": name,
                "display": proj_ctx.get("display") or p.get("display") or name,
                "named_by_cto": bool(proj_ctx.get("display")),
                "grouping_source": p.get("grouping_source") or "prefix",
                "summary": (ai_sum.get(name) or {}).get("text"),
                "commits_today": p.get("commits_today") or 0,
                "days_idle": p.get("days_idle"),
                "daily_counts": p.get("daily_counts") or {},
                "repos": repos,
                "activity": feed[:25],
                "review": review[:6],
                "pm_narrative": dig.get("narrative", ""),
                "work_reviews": [
                    u for u in (ai_work.get(name) or {}).get("items", [])
                    if u.get("id") not in hidden_work
                ],
                "launch": launch,
                "status": proj_ctx.get("status", ""),
            })
        return {
            "projects": projects,
            "last_refresh": state.get("last_refresh"),
            "last_refresh_ok": state.get("last_refresh_ok"),
            "last_error": state.get("last_error"),
            "agent_has_run": bool(ai_sum or ai_dig or ai.get("architecture")),
            "has_sources": bool(self.sources),
            "refreshing": self._refreshing,
            "machine": localrepo.machine_key(),
            "window_days": self.config.get("commit_window_days", 30),
            "day_start_hour": self.config.get("day_start_hour", 5),
        }

    def _overlay_ai(self, slug: str, result: dict) -> dict:
        """Overlay the agent-owned panels (ai.json) onto a static analysis
        result. Architecture prose + Mermaid, real prompts (which replace the
        noisy static guesses — those stay only as a dim fallback), the universal
        data shape (replaces the SQL/Prisma/Django-only static scan), setup/run
        instructions, and key code snippets."""
        ai = store.load_ai()
        result = dict(result)

        arch = ai.get("architecture", {}).get(slug) or {}
        if arch.get("text"):
            result["architecture"] = arch["text"]
        if arch.get("mermaid"):
            result["diagrams"] = {**result.get("diagrams", {}), "architecture": arch["mermaid"]}

        pr = ai.get("prompts", {}).get(slug) or {}
        if pr.get("items"):
            result["prompts"] = pr["items"]
            result["prompts_source"] = "agent"
        else:
            result["prompts_source"] = "static"  # keyword guesses; show dimmed

        ds = ai.get("data_shape", {}).get(slug) or {}
        if ds.get("items"):
            # Map the stack-agnostic agent items onto the shape the frontend/ER
            # diagram already expect ({name, kind, source, columns}).
            mapped = [{
                "name": it.get("name", ""),
                "kind": it.get("kind", "") or "model",
                "source": it.get("file", "") + (f":{it['line']}" if it.get("line") else ""),
                "columns": it.get("fields", []),
            } for it in ds["items"]]
            result["db"] = mapped
            result["diagrams"] = {**result.get("diagrams", {}), "er": diagram.er_diagram(mapped)}
            result["data_shape_source"] = "agent"
        else:
            result["data_shape_source"] = "static"  # SQL/Prisma/Django guesses; dimmed

        result["setup"] = (ai.get("setup", {}).get(slug) or {}).get("text", "")
        result["snippets"] = (ai.get("snippets", {}).get(slug) or {}).get("items", [])
        return result

    def get_analysis(self, slug: str):
        cached = self.state.get("analysis", {}).get(slug)
        return self._overlay_ai(slug, cached) if cached else None

    # ---- actions --------------------------------------------------------
    def refresh(self) -> dict:
        with self._lock:
            if self._refreshing:
                return self._build_view()
            self._refreshing = True
        try:
            with debuglog.timer("refresh: perform_refresh (provider API calls)"):
                new_state = perform_refresh(self.config, self.sources, self.state)
            self.state = new_state
            manager.update_activity(self.state)  # fold in live activity
            store.save_state(self.state)
        except Exception as e:  # never let the UI hang on a failed refresh
            self.state["last_refresh_ok"] = False
            self.state["last_error"] = f"refresh failed: {e}"
        finally:
            self._refreshing = False
        # New commits may have moved a clone's HEAD — re-run analysis in the
        # background so details stay current without a button.
        self._kick_analyses()
        self._kick_sync()
        self._kick_event_compact()
        return self._build_view()

    def tick(self) -> dict:
        """Cheap periodic poll: fold in new activity events (event-gated digest,
        no Bitbucket network). Safe to call on a short interval."""
        try:
            if manager.update_activity(self.state):
                store.save_state(self.state)
        except Exception as e:
            self.state["last_error"] = f"activity update failed: {e}"
        return self._build_view()

    def rescan_local(self) -> dict:
        localrepo.update_state_links(self.state, self.config, self.sources)
        store.save_state(self.state)
        return self._build_view()

    # ---- settings (credentials) -----------------------------------------
    def get_settings(self) -> dict:
        """Current provider config with tokens masked — never returns secrets."""
        cred = store.load_credentials()
        # Read fresh so the modal shows effective values even if another writer
        # (or an older session) changed credentials since startup.
        fresh_config = store.load_config()
        bb = dict(cred.get("bitbucket") or {})
        gh = dict(cred.get("github") or {})
        # Reflect a legacy (.env-derived) Bitbucket source even before it's been
        # saved into credentials.json.
        if not cred:
            for s in self.sources:
                if s["provider"] == "bitbucket":
                    bb = {"enabled": True, "workspace": s["workspace"],
                          "email": s["email"], "token": "*"}
        return {
            "bitbucket": {
                "enabled": bool(bb.get("enabled")),
                "workspace": bb.get("workspace") or "",
                "email": bb.get("email") or "",
                "token_set": bool(bb.get("token")),
            },
            "github": {"enabled": bool(gh.get("enabled")), "token_set": bool(gh.get("token"))},
            # Local-only tracking (read git log from local clones, no API key).
            "localgit": {"enabled": bool((cred.get("localgit") or {}).get("enabled"))},
            "has_any_source": bool(self.sources),
            # Machine-local extra folders to hunt for clones (on top of the common
            # defaults). Shown so the user can add non-standard locations.
            "local_roots": list(cred.get("local_roots") or []),
            # Tracking knobs (credentials.json overrides overlaid by load_config).
            "commit_window_days": fresh_config.get("commit_window_days", 30),
            "hide_idle_local": bool(fresh_config.get("hide_idle_local", True)),
            # Repo slugs the CTO excluded from the board (manage/undo here).
            "excluded_repos": sorted(self.state.get("excluded_repos") or []),
            # Project names the CTO excluded from the board (manage/undo here).
            "excluded_projects": sorted(self.state.get("excluded_projects") or []),
        }

    def detect_roots(self) -> dict:
        """Candidate project-root folders (with a repo count each) for the
        onboarding wizard's 'where do your clones live' step. Read-only."""
        return {"candidates": _detect_candidate_roots()}

    def save_settings(self, bitbucket: dict | None = None, github: dict | None = None,
                      localgit: dict | None = None, local_roots: list | None = None,
                      commit_window_days=None, hide_idle_local=None) -> dict:
        """Write credentials.json (a blank token keeps the existing one),
        rebuild sources, rediscover local clones, and refresh."""
        cred = store.load_credentials()
        if not cred:  # migrate legacy .env into the file before editing
            for s in self.sources:
                if s["provider"] == "bitbucket":
                    cred["bitbucket"] = {"enabled": True, "workspace": s["workspace"],
                                         "email": s["email"], "token": s["token"]}
        bb = cred.setdefault("bitbucket", {})
        if bitbucket is not None:
            bb["enabled"] = bool(bitbucket.get("enabled"))
            if "workspace" in bitbucket:
                bb["workspace"] = (bitbucket.get("workspace") or "").strip()
            if "email" in bitbucket:
                bb["email"] = (bitbucket.get("email") or "").strip()
            tok = (bitbucket.get("token") or "").strip()
            if tok:
                bb["token"] = tok
        gh = cred.setdefault("github", {})
        if github is not None:
            gh["enabled"] = bool(github.get("enabled"))
            tok = (github.get("token") or "").strip()
            if tok:
                gh["token"] = tok
        lg = cred.setdefault("localgit", {})
        if localgit is not None:
            lg["enabled"] = bool(localgit.get("enabled"))
        if local_roots is not None:
            cred["local_roots"] = [str(r).strip() for r in local_roots if str(r).strip()]
        if commit_window_days is not None:
            try:
                cred["commit_window_days"] = max(7, min(365, int(commit_window_days)))
            except (TypeError, ValueError):
                pass
        if hide_idle_local is not None:
            cred["hide_idle_local"] = bool(hide_idle_local)
        store.save_credentials(cred)
        # Api.config is loaded once at startup — reload so the new window/idle
        # knobs (overlaid from credentials by load_config) apply to the refresh
        # below, not just the next process.
        self.config = store.load_config()
        self.sources = store.load_sources(self.config)
        try:
            # resolved_roots reads the just-saved machine-local roots from credentials.
            localrepo.update_state_links(self.state, self.config, self.sources)
        except Exception:
            pass
        return self.refresh()

    def dismiss_review(self, project: str, text: str) -> dict:
        """Mark a review item as handled ("OK") so it stops showing."""
        key = _review_key(project, text)
        lst = self.state.setdefault("dismissed_reviews", [])
        if key not in lst:
            lst.append(key)
            store.save_state(self.state)
        return self._build_view()

    def hide_work_review(self, project: str, id: str) -> dict:
        """Hide a recent-work card (✕). Ids are server-assigned in ai.json, so
        prune the hidden list against the live cards to keep it from growing."""
        lst = self.state.setdefault("hidden_work_reviews", [])
        if id not in lst:
            lst.append(id)
        live = {u.get("id") for wr in store.load_ai().get("work_reviews", {}).values()
                for u in wr.get("items", [])}
        self.state["hidden_work_reviews"] = [k for k in lst if k in live][-300:]
        store.save_state(self.state)
        return self._build_view()

    def exclude_repo(self, slug: str) -> dict:
        """Exclude a repo from the board (hide ✕ on its badge; undo in Settings).
        Instant: the slug is pruned from the in-memory projects too, no refetch.
        Per-project aggregates (daily_counts, days_idle) stay stale until the
        next refresh — they have no per-repo breakdown in state; self-heals."""
        lst = self.state.setdefault("excluded_repos", [])
        if slug not in lst:
            lst.append(slug)
        for name in list(self.state.get("projects") or {}):
            proj = self.state["projects"][name]
            if slug in (proj.get("repos") or {}):
                proj["repos"].pop(slug, None)
                if not proj["repos"]:
                    del self.state["projects"][name]
                    self.state["project_order"] = [
                        p for p in self.state.get("project_order", []) if p != name]
        store.save_state(self.state)
        return self._build_view()

    def include_repo(self, slug: str) -> dict:
        """Undo an exclusion (Settings). The repo was dropped from state, so a
        full refresh is needed to fetch it back."""
        lst = self.state.setdefault("excluded_repos", [])
        if slug in lst:
            lst.remove(slug)
            store.save_state(self.state)
        return self.refresh()

    def exclude_project(self, project: str) -> dict:
        """Exclude an entire project (and all its repos) from the board.
        Instant: the project is pruned from in-memory state, no refetch needed."""
        lst = self.state.setdefault("excluded_projects", [])
        if project not in lst:
            lst.append(project)
        self.state.get("projects", {}).pop(project, None)
        order = self.state.get("project_order") or []
        self.state["project_order"] = [p for p in order if p != project]
        store.save_state(self.state)
        return self._build_view()

    def include_project(self, project: str) -> dict:
        """Undo a project exclusion (Settings). Triggers a full refresh so the
        project's repos are fetched back from source."""
        lst = self.state.setdefault("excluded_projects", [])
        if project in lst:
            lst.remove(project)
            store.save_state(self.state)
        return self.refresh()

    # ---- CTO context: launches + vision (context.json, CTO-owned) -------
    def _context_view(self, project: str) -> dict:
        ctx = store.load_context().get(project) or {}
        active = ctx.get("active_launch")
        if active:
            active = {**active, "days_until": _days_until(active.get("target_date", ""))}
        return {
            "project": project,
            "active_launch": active,
            "past_launches": ctx.get("past_launches", []),
            "vision": ctx.get("vision", ""),
            "status": ctx.get("status", ""),
            "display": ctx.get("display", ""),
        }

    def _mutate_context(self, project: str, fn) -> dict:
        all_ctx = store.load_context()
        entry = all_ctx.get(project) or {"active_launch": None, "past_launches": [], "vision": ""}
        fn(entry)
        entry["updated_at"] = _now_iso()
        all_ctx[project] = entry
        store.save_context(all_ctx)
        return self._context_view(project)

    def get_context(self, project: str) -> dict:
        return self._context_view(project)

    def set_active_launch(self, project: str, type: str = "", title: str = "",
                          action: str = "", target_date: str = "", goals: str = "") -> dict:
        def fn(entry):
            if entry.get("active_launch"):
                return  # one active launch at a time — use update instead
            entry["active_launch"] = {
                "id": uuid.uuid4().hex[:12],
                "type": (type or "").strip()[:60],
                "title": (title or "").strip()[:160],
                "action": (action or "").strip()[:60],
                "target_date": (target_date or "").strip()[:20],
                "goals": (goals or "").strip()[:2000],
                "created_at": _now_iso(),
                "history": [],
            }
        return self._mutate_context(project, fn)

    def update_active_launch(self, project: str, type=None, title=None,
                             action=None, target_date=None, goals=None) -> dict:
        def fn(entry):
            a = entry.get("active_launch")
            if not a:
                return
            if type is not None:
                a["type"] = type.strip()[:60]
            if title is not None:
                a["title"] = title.strip()[:160]
            if action is not None:
                a["action"] = action.strip()[:60]
            if target_date is not None:
                a["target_date"] = target_date.strip()[:20]
            if goals is not None:
                a["goals"] = goals.strip()[:2000]
        return self._mutate_context(project, fn)

    def pushback_launch(self, project: str, new_date: str, reason: str = "") -> dict:
        def fn(entry):
            a = entry.get("active_launch")
            if not a:
                return
            a.setdefault("history", []).append({
                "from": a.get("target_date", ""),
                "to": (new_date or "").strip()[:20],
                "reason": (reason or "").strip()[:500],
                "at": _now_iso(),
            })
            a["target_date"] = (new_date or "").strip()[:20]
        return self._mutate_context(project, fn)

    def complete_launch(self, project: str, status: str = "shipped") -> dict:
        def fn(entry):
            a = entry.get("active_launch")
            if not a:
                return
            done = {**a, "status": "cancelled" if status == "cancelled" else "shipped",
                    "completed_at": _now_iso()}
            entry.setdefault("past_launches", []).insert(0, done)
            entry["active_launch"] = None
        return self._mutate_context(project, fn)

    def save_vision(self, project: str, text: str = "") -> dict:
        return self._mutate_context(project, lambda e: e.__setitem__("vision", (text or "").strip()[:8000]))

    def set_project_status(self, project: str, status: str = "") -> dict:
        """A CTO-set standing status ("Shipped", "On Hold", …). Shown in the
        sidebar in place of the launch line; empty clears it."""
        return self._mutate_context(project, lambda e: e.__setitem__("status", (status or "").strip()[:40]))

    def rename_project(self, project: str, display: str = "") -> dict:
        """A CTO-set display name that overrides the grouping's name everywhere.
        Empty clears the override (back to the grouping/prefix name)."""
        return self._mutate_context(project, lambda e: e.__setitem__("display", (display or "").strip()[:80]))

    def analyze(self, slug: str) -> dict:
        """Run analysis for one repo's local clone, reusing the cached result
        when the clone's HEAD is unchanged."""
        path = localrepo.links_for_machine(self.state).get(slug)
        if not path:
            return {"error": f"no local clone for {slug} on this machine"}
        head = analysis.git_head(Path(path))
        cached = self.state.get("analysis", {}).get(slug)
        if (cached and head and cached.get("head") == head
                and cached.get("analyzer_version") == analysis._ANALYZER_VERSION):
            debuglog.log(f"analyze[{slug}]: cache hit (HEAD unchanged) — no work")
            return self._overlay_ai(slug, cached)
        try:
            with debuglog.timer(f"analyze[{slug}]: on-demand static analysis (cache miss)"):
                result = analysis.analyze_repo(path, slug)  # static only — no API
        except Exception as e:
            return {"error": f"analysis failed: {e}"}
        self.state.setdefault("analysis", {})[slug] = result
        store.save_state(self.state)
        return self._overlay_ai(slug, result)

    def _kick_analyses(self) -> None:
        """Spawn a background pass that analyzes every local clone whose cache is
        missing or stale (HEAD moved / analyzer bumped). Static-only, no API, so
        it's safe to run unattended; the frontend just reads the cached result."""
        threading.Thread(target=self._ensure_analyses, daemon=True).start()

    def _kick_event_compact(self) -> None:
        """Trim events.jsonl (30-day retention + size cap) in the background.
        maybe_compact() self-guards (daily interval, size trigger, lock) so
        kicking on every refresh is free."""
        threading.Thread(target=events.maybe_compact, daemon=True).start()

    def _ensure_analyses(self) -> None:
        if not self._analysis_lock.acquire(blocking=False):
            debuglog.log("analysis: pass already running — skipping")
            return  # one analyzer at a time is enough
        try:
            links = localrepo.links_for_machine(self.state)
            excluded = set(self.state.get("excluded_repos") or [])
            active = {s for p in self.state.get("projects", {}).values()
                      for s in (p.get("repos") or {})}
            changed = 0
            skipped = 0
            for slug, path in links.items():
                if slug in excluded or slug not in active:
                    continue  # no analysis work for repos hidden from the board
                try:
                    head = analysis.git_head(Path(path))
                except Exception:
                    continue
                cached = self.state.get("analysis", {}).get(slug)
                if (cached and head and cached.get("head") == head
                        and cached.get("analyzer_version") == analysis._ANALYZER_VERSION):
                    skipped += 1
                    continue  # up to date
                try:
                    with debuglog.timer(f"analysis: analyze {slug}"):
                        result = analysis.analyze_repo(path, slug)  # static only
                except Exception:
                    continue
                self.state.setdefault("analysis", {})[slug] = result
                changed += 1
            debuglog.log(f"analysis: background pass done — {changed} analyzed, {skipped} up-to-date")
            if changed:
                store.save_state(self.state)
        finally:
            self._analysis_lock.release()

    def _kick_sync(self) -> None:
        """Spawn a background pass that checks each local clone against its
        remote (green in sync / yellow unpushed / red unpulled). One
        `git ls-remote` per clone, so it runs only on startup and Refresh —
        never on the cheap tick poll."""
        threading.Thread(target=self._ensure_sync, daemon=True).start()

    def _ensure_sync(self) -> None:
        if not self._sync_lock.acquire(blocking=False):
            return  # one sync checker at a time is enough
        try:
            links = localrepo.links_for_machine(self.state)
            excluded = set(self.state.get("excluded_repos") or [])
            active = {s for p in self.state.get("projects", {}).values()
                      for s in (p.get("repos") or {})}
            synced = self.state.setdefault("local_sync", {}).setdefault(
                localrepo.machine_key(), {})
            changed = False
            for slug, path in links.items():
                if slug in excluded or slug not in active:
                    continue  # no ls-remote for repos hidden from the board
                try:
                    synced[slug] = localrepo.sync_status(Path(path))
                    changed = True
                except Exception:  # a broken clone must never kill the pass
                    continue
            # Drop entries for clones that no longer exist on this machine.
            for slug in list(synced):
                if slug not in links:
                    del synced[slug]
                    changed = True
            if changed:
                store.save_state(self.state)
        finally:
            self._sync_lock.release()

    def open_terminal(self, path: str) -> bool:
        if not path or not Path(path).is_dir():
            return False
        return _open_terminal(path)


def _make_handler(api: "Api"):
    from http.server import BaseHTTPRequestHandler

    web_dir = Path(__file__).resolve().parent / "web"
    ALLOWED = {"get_view", "refresh", "tick", "rescan_local", "analyze",
               "get_analysis", "open_terminal", "dismiss_review", "hide_work_review",
               "exclude_repo", "include_repo", "exclude_project", "include_project",
               "get_settings", "save_settings", "detect_roots",
               "get_context", "set_active_launch", "update_active_launch",
               "pushback_launch", "complete_launch", "save_vision",
               "set_project_status", "rename_project"}
    CONTENT_TYPES = {
        ".html": "text/html", ".js": "text/javascript", ".css": "text/css",
        ".json": "application/json", ".svg": "image/svg+xml",
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep stdout/stderr quiet
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _file(self, rel):
            f = (web_dir / rel).resolve()
            if not (f == web_dir or web_dir in f.parents) or not f.is_file():
                self.send_error(404)
                return
            data = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPES.get(f.suffix, "application/octet-stream"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/":
                path = "/index.html"
            if path == "/api/view":
                self._json(api.get_view())
                return
            self._file(path.lstrip("/"))

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if not path.startswith("/api/"):
                self._json({"error": "not found"}, 404)
                return
            method = path[len("/api/"):]
            if method not in ALLOWED:
                self._json({"error": f"forbidden: {method}"}, 403)
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                args = json.loads(raw) if raw else {}
            except ValueError:
                args = {}
            try:
                result = getattr(api, method)(**args)
            except Exception as e:
                self._json({"error": str(e)}, 500)
                return
            self._json(result)

    return Handler


def _open_terminal(path: str) -> bool:
    """Open a terminal emulator with its working directory at `path`. Returns
    True if something was launched. macOS is the primary target (prefers iTerm,
    falls back to Terminal); on Linux the first available emulator wins."""
    if sys.platform == "darwin":
        app = "iTerm" if os.path.isdir("/Applications/iTerm.app") else "Terminal"
        cmd = ["open", "-a", app, path]
    else:
        # (binary, extra args) tried in order; the first on PATH is used.
        candidates = [
            ("gnome-terminal", ["--working-directory=" + path]),
            ("konsole", ["--workdir", path]),
            ("xfce4-terminal", ["--working-directory=" + path]),
            ("tilix", ["--working-directory=" + path]),
            ("terminator", ["--working-directory=" + path]),
            ("kitty", ["--directory", path]),
            ("alacritty", ["--working-directory", path]),
            ("x-terminal-emulator", ["--working-directory=" + path]),  # last resort
            ("xterm", ["-e", "bash", "-c", f"cd {shlex.quote(path)}; exec bash"]),
        ]
        cmd = None
        for binary, args in candidates:
            if shutil.which(binary):
                cmd = [binary, *args]
                break
        if cmd is None:
            return False
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def _open_url(url: str) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform.startswith("linux"):
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            import webbrowser
            webbrowser.open(url)
    except OSError:
        pass


def run_dashboard(config: dict, port: int, prefer_window: bool) -> int:
    """Serve the dashboard over stdlib HTTP (zero deps). Opens a native
    pywebview window if that package happens to be installed and wanted, else
    the default browser. Boots even with no credentials — onboard via Settings."""
    from http.server import ThreadingHTTPServer

    api = Api(config)
    url = f"http://localhost:{port}"
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(api))
    except OSError:
        _open_url(url)  # already running — just point a browser at it
        print(f"Overboard dashboard already running at {url}")
        return 0

    if prefer_window:
        try:
            import webview  # optional native window
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            webview.create_window(APP_TITLE, url=url, width=560, height=820, min_size=(400, 480))
            webview.start()
            return 0
        except ImportError:
            pass

    _open_url(url)
    print(f"Overboard dashboard: {url}  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def _port_from_argv(default: int = 8787) -> int:
    if "--port" in sys.argv:
        i = sys.argv.index("--port")
        if i + 1 < len(sys.argv):
            try:
                return int(sys.argv[i + 1])
            except ValueError:
                pass
    return default


def main() -> int:
    if "--once" in sys.argv:
        return run_once()
    try:
        config = store.load_config()
    except store.ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    # No credential check here — the dashboard boots regardless and lets you add
    # a Bitbucket/GitHub token in the Settings panel.
    # --serve: headless server (spawned by the MCP launch_dashboard tool) —
    # opens a browser tab. Default (direct run): native window if pywebview is
    # installed, otherwise browser.
    prefer_window = "--serve" not in sys.argv
    return run_dashboard(config, _port_from_argv(), prefer_window)


if __name__ == "__main__":
    sys.exit(main())

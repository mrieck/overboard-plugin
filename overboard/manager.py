"""Turn raw activity events into (a) the dashboard's recent feed and (b) the
agent's to-do list. NO API — the /overboard agent (Max sub) produces all the
prose; this module only does deterministic bookkeeping."""

from datetime import date, datetime

from overboard import events, store

STOP_TYPES = ("Stop", "SubagentStop")
_RECENT_KEEP = 25

# A quiet project with a launch this close (or already overdue) still belongs on
# the to-do list — a deadline is signal even when no code moved.
LAUNCH_HORIZON_DAYS = 7


def _launch_status(launch: dict) -> tuple[str | None, int | None]:
    """('overdue'|'due_soon'|None, days_until) for an active launch. Only the
    near/overdue window is actionable; a launch weeks out returns (None, days)."""
    du = _days_until((launch or {}).get("target_date", ""))
    if du is None:
        return None, None
    if du < 0:
        return "overdue", du
    if du <= LAUNCH_HORIZON_DAYS:
        return "due_soon", du
    return None, du


def _slugs_flagged_since(since_ts: float) -> set[str]:
    """Repo slugs the assistant has already flagged since `since_ts` — used to
    keep a near/overdue-launch nudge to at most once a day instead of every loop."""
    return {
        e.get("repo_hint")
        for e in events.read_events(since_ts=since_ts)
        if e.get("type") == "flag" and e.get("repo_hint")
    }


def _days_until(date_str: str):
    try:
        return (date.fromisoformat(date_str) - datetime.now().astimezone().date()).days
    except (ValueError, TypeError):
        return None


def update_activity(state: dict, env: dict | None = None) -> bool:
    """Refresh each repo's recent-activity feed from the event log. Returns True
    if anything changed (so the caller knows whether to save)."""
    grouped = events.events_by_repo(state)
    if not grouped:
        return False
    activity = state.setdefault("activity", {})
    changed = False
    for slug, evs in grouped.items():
        evs.sort(key=lambda e: e.get("ts", 0))
        rec = activity.setdefault(slug, {"recent": [], "last_event_ts": 0})
        newest = evs[-1].get("ts", 0)
        if newest != rec.get("last_event_ts"):
            changed = True
        rec["recent"] = evs[-_RECENT_KEEP:]
        rec["last_event_ts"] = newest
    return changed


def pending_work(state: dict, ai: dict) -> list[dict]:
    """The agent's to-do list. A project needs a fresh **summary** when its
    commit HEAD signature changed (or none exists), and a fresh **digest** when
    there are new Stop/SubagentStop events since the last digest. Returns only
    projects that need something, newest-activity first."""
    grouped = events.events_by_repo(state)
    summaries = ai.get("summaries", {})
    digests = ai.get("digests", {})
    work_reviews = ai.get("work_reviews", {})
    context = store.load_context()

    # Anti-nag basis: a launch nudge only re-fires once the calendar day rolls
    # over, so a still-open deadline doesn't spam the loop every 30 minutes.
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    flagged_today = _slugs_flagged_since(midnight.timestamp())

    out = []
    for name, p in state.get("projects", {}).items():
        slugs = list(p.get("repos", {}))
        stops = [
            e for s in slugs for e in grouped.get(s, [])
            if e.get("type") in STOP_TYPES
        ]
        newest_stop = max((e.get("ts", 0) for e in stops), default=0)
        head_sig = p.get("head_sig", "")

        need_summary = (name not in summaries) or (
            head_sig and head_sig != summaries.get(name, {}).get("head_sig")
        )
        digest_basis = digests.get(name, {}).get("basis", {})
        need_digest = newest_stop > digest_basis.get("stop_ts", 0)

        # Recent-work review: new stops or moved heads since the last recorded
        # basis. First ever review only fires when the project shows signs of
        # life, so it never triggers a scan-all-history pass on a quiet board.
        review = work_reviews.get(name) or {}
        review_basis = review.get("basis") or {}
        basis_heads = review_basis.get("heads") or {}
        heads = {s: (p.get("repos", {}).get(s) or {}).get("head") for s in slugs}
        if review_basis:
            need_review = newest_stop > review_basis.get("stop_ts", 0) or any(
                heads[s] and heads[s] != basis_heads.get(s) for s in slugs
            )
        else:
            need_review = bool(newest_stop or any((p.get("daily_counts") or {}).values()))

        # A near/overdue launch surfaces the project even when no code moved —
        # but only once a day (suppressed if it's already been flagged today).
        launch = (context.get(name) or {}).get("active_launch")
        launch_status, launch_days = _launch_status(launch) if launch else (None, None)
        need_launch = bool(launch_status) and not any(s in flagged_today for s in slugs)

        if not (need_summary or need_digest or need_review or need_launch):
            continue
        reasons = []
        if need_summary:
            reasons.append("new commits" if name in summaries else "no summary yet")
        if need_digest:
            reasons.append("new finished work")
        if need_review:
            reasons.append("recent work to review")
        if need_launch:
            reasons.append(f"launch {launch_status}"
                           + (f" ({abs(launch_days)}d)" if launch_days is not None else ""))
        item = {
            "project": name,
            "repos": slugs,
            "need_summary": bool(need_summary),
            "need_digest": bool(need_digest),
            "need_review": bool(need_review),
            "need_launch": need_launch,
            "review_since": {s: basis_heads.get(s) for s in slugs},
            "recent_review_titles": [
                u.get("title", "") for u in review.get("items", [])[:3]
            ],
            "reasons": reasons,
            "newest_stop_ts": newest_stop,
            "head_sig": head_sig,
        }
        # Inline the active launch so the agent can ground its prose without a
        # get_project_context round-trip (that tool still has goals/history/vision).
        if launch:
            item["launch"] = {
                "type": launch.get("type", ""),
                "title": launch.get("title", ""),
                "target_date": launch.get("target_date", ""),
                "days_until": launch_days,
                "status": launch_status,  # 'overdue' | 'due_soon' | None (further out)
            }
        out.append(item)
    # Overdue launches first (a quiet-but-late project must not sink below busy
    # ones), then newest activity.
    out.sort(
        key=lambda w: ((w.get("launch") or {}).get("status") == "overdue", w["newest_stop_ts"]),
        reverse=True,
    )

    # Grouping to-do: if the live repo set isn't covered by the assistant's last
    # grouping (new/removed repos, or none set yet), ask it to (re)group. Until it
    # does, resolve_projects shows the prefix-stem fallback (dimmed).
    grouping_item = _grouping_todo(state, ai)
    if grouping_item:
        out.insert(0, grouping_item)
    return out


def _grouping_todo(state: dict, ai: dict) -> dict | None:
    live = sorted({s for p in state.get("projects", {}).values() for s in p.get("repos", {})})
    if not live:
        return None
    grouping = ai.get("grouping") or {}
    if grouping.get("signature") == store.slug_signature(live):
        return None
    grouped = {s for g in (grouping.get("groups") or {}).values() for s in g.get("repos", [])}
    return {
        "kind": "grouping",
        "need_grouping": True,
        "reason": "repos not covered by current grouping" if grouped else "no grouping set yet",
        "all_repos": live,
        "ungrouped": [s for s in live if s not in grouped],
    }

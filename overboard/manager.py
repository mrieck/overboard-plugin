"""Turn raw activity events into (a) the dashboard's recent feed and (b) the
agent's to-do list. NO API — the /overboard agent (Max sub) produces all the
prose; this module only does deterministic bookkeeping."""

from overboard import events

STOP_TYPES = ("Stop", "SubagentStop")
_RECENT_KEEP = 25


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

        if not (need_summary or need_digest):
            continue
        reasons = []
        if need_summary:
            reasons.append("new commits" if name in summaries else "no summary yet")
        if need_digest:
            reasons.append("new finished work")
        out.append({
            "project": name,
            "repos": slugs,
            "need_summary": bool(need_summary),
            "need_digest": bool(need_digest),
            "reasons": reasons,
            "newest_stop_ts": newest_stop,
            "head_sig": head_sig,
        })
    out.sort(key=lambda w: w["newest_stop_ts"], reverse=True)
    return out

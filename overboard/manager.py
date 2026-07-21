"""Turn raw activity events into (a) the dashboard's recent feed and (b) the
agent's to-do list. NO API — the /overboard agent (Max sub) produces all the
prose; this module only does deterministic bookkeeping."""

from __future__ import annotations

from datetime import date, datetime, timezone

from overboard import events, localrepo, store

STOP_TYPES = ("Stop", "SubagentStop")
_RECENT_KEEP = 25

# A quiet project with a launch this close (or already overdue) still belongs on
# the to-do list — a deadline is signal even when no code moved.
LAUNCH_HORIZON_DAYS = 7

# Heavy-AI throttling: at most HEAVY_BUDGET projects get "heavy" work (panel
# extraction via repo-analyst, a first-ever work review) per cooldown window,
# so a fresh install trickles in over a few passes instead of burning the CTO's
# subscription on every repo at once. Slots are inferred from ai.json write
# timestamps — the get tool never writes anything.
HEAVY_BUDGET = 2
HEAVY_COOLDOWN_SECS = 30 * 60
# A moved HEAD only re-fires panel analysis once the panels are this old, so an
# actively-developed project isn't re-analyzed on every loop pass.
PANEL_STALE_AFTER_SECS = 24 * 3600
PANEL_KEYS = ("architecture", "prompts", "setup", "snippets", "data_shape")


def _parse_iso(ts) -> datetime | None:
    """Aware datetime from an ai.json 'at' stamp, or None. Tolerates a 'Z'
    suffix and naive stamps (assumed UTC) defensively."""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _panels_state(slug: str, repo: dict, ai: dict) -> str:
    """'missing' | 'stale' | 'ok' for one local repo's assistant panels."""
    entries = [e for k in PANEL_KEYS
               for e in [(ai.get(k) or {}).get(slug)] if isinstance(e, dict)]
    if not entries:
        return "missing"
    cur = (repo or {}).get("head")
    heads = [e.get("head") for e in entries if e.get("head")]
    if not cur or not heads:
        return "ok"  # pre-upgrade entries carry no head — stay anchored
    if all(h == cur for h in heads):
        return "ok"
    ats = [d for d in (_parse_iso(e.get("at", "")) for e in entries) if d]
    newest = max(ats) if ats else None
    now = datetime.now(timezone.utc)
    if newest and (now - newest).total_seconds() < PANEL_STALE_AFTER_SECS:
        return "ok"  # head moved, but panels are fresh enough
    return "stale"


def _heavy_slot_holders(state: dict, ai: dict) -> set:
    """Projects whose heavy output (panels, or a real work-review card) was
    written within HEAVY_COOLDOWN_SECS — they hold a budget slot, so repeated
    get_pending_work calls in one pass can't drain the deferred backlog."""
    now = datetime.now(timezone.utc)

    def recent(iso):
        dt = _parse_iso(iso)
        # A future stamp (clock skew) counts as recent — conservative.
        return dt is not None and (now - dt).total_seconds() < HEAVY_COOLDOWN_SECS

    slug_to_project = {s: name for name, p in state.get("projects", {}).items()
                       for s in p.get("repos", {})}
    holders = set()
    for key in PANEL_KEYS:
        for slug, entry in (ai.get(key) or {}).items():
            if (isinstance(entry, dict) and recent(entry.get("at", ""))
                    and slug in slug_to_project):
                holders.add(slug_to_project[slug])
    for project, entry in (ai.get("work_reviews") or {}).items():
        items = (entry or {}).get("items") or []
        # items[0] is newest; the entry-level 'at' is also stamped by a
        # units=[] basis-advance, which must NOT burn a slot.
        if items and recent(items[0].get("at", "")):
            holders.add(project)
    return holders


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


def pending_work(state: dict, ai: dict) -> dict:
    """The agent's to-do list. A project needs a fresh **summary** when its
    commit HEAD signature changed (or none exists), a fresh **digest** when
    there are new Stop/SubagentStop events since the last digest, and fresh
    **panels** when a local repo has no agent panels yet (or its recorded HEAD
    went stale). Returns ``{"items", "first_run", "heavy_budget", "deferred",
    "notice"}`` — heavy work (panels + first-ever reviews) is granted to at
    most HEAVY_BUDGET projects per HEAVY_COOLDOWN_SECS window; the rest keep
    only their cheap flags and are marked ``deferred_heavy``. Items are
    newest-activity first."""
    grouped = events.events_by_repo(state)
    summaries = ai.get("summaries", {})
    digests = ai.get("digests", {})
    work_reviews = ai.get("work_reviews", {})
    context = store.load_context()
    local = localrepo.links_for_machine(state)

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
        first_review = bool(need_review and not review_basis)

        # Panels: the server (not skill prose) decides which local repos still
        # need the repo-analyst treatment, anchored on the HEAD each panel was
        # written at.
        panels = {s: _panels_state(s, p.get("repos", {}).get(s) or {}, ai)
                  for s in slugs if s in local}
        need_panels = any(v != "ok" for v in panels.values())

        # A near/overdue launch surfaces the project even when no code moved —
        # but only once a day (suppressed if it's already been flagged today).
        launch = (context.get(name) or {}).get("active_launch")
        launch_status, launch_days = _launch_status(launch) if launch else (None, None)
        need_launch = bool(launch_status) and not any(s in flagged_today for s in slugs)

        if not (need_summary or need_digest or need_review or need_launch or need_panels):
            continue
        reasons = []
        if need_summary:
            reasons.append("new commits" if name in summaries else "no summary yet")
        if need_digest:
            reasons.append("new finished work")
        if need_review:
            reasons.append("recent work to review")
        if need_panels:
            reasons.append("panels missing" if "missing" in panels.values() else "panels stale")
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
            "need_panels": need_panels,
            "panel_repos": [s for s, v in panels.items() if v != "ok"],
            "first_review": first_review,
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

    # Heavy-work budget: grant panels + first-ever reviews to at most
    # HEAVY_BUDGET projects per cooldown window. Projects that already hold a
    # slot (recent heavy writes) keep their grant for free so partial work can
    # finish; everyone else queues. Deterministic order → re-calling this tool
    # mid-pass grants the same projects, never more.
    first_run = not (ai.get("summaries") or ai.get("work_reviews"))
    holders = _heavy_slot_holders(state, ai)
    slots = max(0, HEAVY_BUDGET - len(holders))
    kept, deferred = [], []
    for w in out:
        heavy = w["need_panels"] or w["first_review"]
        if heavy and w["project"] not in holders:
            if slots > 0:
                slots -= 1
            else:
                deferred.append(w["project"])
                w["need_panels"], w["panel_repos"] = False, []
                if w["first_review"]:
                    w["need_review"] = False
                w["deferred_heavy"] = True
                w["reasons"] = [r for r in w["reasons"]
                                if not r.startswith("panels")
                                and not (w["first_review"] and r == "recent work to review")]
                w["reasons"].append("in-depth analysis deferred (heavy-work budget)")
        if (w["need_summary"] or w["need_digest"] or w["need_review"]
                or w["need_launch"] or w["need_panels"]):
            kept.append(w)
    out = kept

    # Grouping to-do: if the live repo set isn't covered by the assistant's last
    # grouping (new/removed repos, or none set yet), ask it to (re)group. Until it
    # does, resolve_projects shows the prefix-stem fallback (dimmed).
    grouping_item = _grouping_todo(state, ai)
    if grouping_item:
        out.insert(0, grouping_item)

    notice = ""
    if deferred:
        if first_run:
            notice = (
                "First run: write quick summaries for all listed projects, but do "
                "in-depth analysis (panels + work review) only where granted — "
                "%d project(s) deferred to future passes. Tell the CTO the "
                "dashboard fills in over the next few passes." % len(deferred))
        else:
            notice = (
                "In-depth analysis deferred for %d project(s) this pass "
                "(budget %d per %d min); they'll be picked up on later passes."
                % (len(deferred), HEAVY_BUDGET, HEAVY_COOLDOWN_SECS // 60))
    return {"items": out, "first_run": first_run, "heavy_budget": HEAVY_BUDGET,
            "deferred": deferred, "notice": notice}


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

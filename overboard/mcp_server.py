#!/usr/bin/env python3
"""Overboard MCP server — the /overboard agent's hands.

Pure stdlib, zero third-party deps, so it runs under any `python3` (Linux or
macOS) directly from wherever Claude Code copied the plugin — no venv, no
PYTHONPATH, no machine-specific paths. It speaks the MCP stdio protocol
(newline-delimited JSON-RPC) by hand.

The agent (Max sub) is the brain: it READS deterministic inputs through these
tools, thinks itself (no Anthropic API), and WRITES results back to the
agent-owned ai.json / append-only event log — never the dashboard-owned
state.json.

Run: python3 <plugin>/overboard/mcp_server.py
"""

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import date, datetime, timezone

# Make the plugin's `overboard` package importable no matter the cwd or how
# we're launched (this file lives at <root>/overboard/mcp_server.py).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from overboard import analysis, events, localrepo, manager, providers, store  # noqa: E402

STOP_TYPES = ("Stop", "SubagentStop")
DASHBOARD_PORT = 8787


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clip_note(text, limit=500):
    """Clip to `limit` chars at a word boundary (no mid-word chop) + ellipsis.
    Returns (clipped, original_len, was_trimmed)."""
    s = (text or "").strip()
    if len(s) <= limit:
        return s, len(s), False
    cut = s[:limit].rstrip()
    sp = cut.rfind(" ")
    if sp > limit * 0.6:            # only back up if we don't lose too much
        cut = cut[:sp].rstrip()
    return cut + "…", len(s), True


def _known_slugs() -> dict:
    return localrepo.links_for_machine(store.load_state())


def _resolve_to_slug(state: dict, name: str):
    if name in _known_slugs():
        return name
    proj = state.get("projects", {}).get(name)
    if proj:
        repos = list(proj.get("repos", {}))
        if repos:
            return repos[0]
    return None


def _newest_stop_ts(state: dict, project: str) -> float:
    grouped = events.events_by_repo(state)
    slugs = list(state.get("projects", {}).get(project, {}).get("repos", {}))
    ts = [e.get("ts", 0) for s in slugs for e in grouped.get(s, [])
          if e.get("type") in STOP_TYPES]
    return max(ts, default=0)


# ============================ tool handlers ================================
def get_pending_work():
    return manager.pending_work(store.load_state(), store.load_ai())


def list_projects():
    return [{"slug": s, "path": p} for s, p in sorted(_known_slugs().items())]


def get_commits(slug: str, limit: int = 20):
    state, config = store.load_state(), store.load_config()
    repo = None
    for p in state.get("projects", {}).values():
        r = p.get("repos", {}).get(slug)
        if r:
            repo = {
                "provider": r.get("provider") or "bitbucket",
                "workspace": r.get("workspace"),
                "slug": slug,
                "branch": r.get("branch"),
            }
            break
    if not repo or not repo["branch"]:
        return {"error": f"unknown repo {slug!r} (call list_projects)"}
    src = next((s for s in store.load_sources(config) if s["provider"] == repo["provider"]), None)
    if not src:
        return {"error": f"{repo['provider']} source not configured for {slug!r}"}
    repo["workspace"] = repo["workspace"] or src.get("workspace") or config.get("workspace")
    try:
        session = providers.make_session(src)
        commits = providers.commits(repo, session, pagelen=min(max(limit, 1), 50))
    except providers.ProviderError as e:
        return {"error": str(e)}
    return commits[:limit]


_DIFF_CAP = 200_000  # ~200 KB of patch text is plenty for a sprint delta


def get_recent_diff(slug: str, since: str = None):
    state, config = store.load_state(), store.load_config()
    repo = None
    for p in state.get("projects", {}).values():
        r = p.get("repos", {}).get(slug)
        if r:
            repo = {
                "provider": r.get("provider") or "bitbucket",
                "workspace": r.get("workspace"),
                "slug": slug,
                "branch": r.get("branch"),
                "head": r.get("head"),
            }
            break
    if not repo or not repo["branch"]:
        return {"error": f"unknown repo {slug!r} (call list_projects)", "source": "messages"}
    head = repo.get("head")
    if not head:
        return {"error": f"no known head for {slug!r} (refresh first)", "source": "messages"}
    src = next((s for s in store.load_sources(config) if s["provider"] == repo["provider"]), None)
    if not src:
        return {"error": f"{repo['provider']} source not configured for {slug!r}", "source": "messages"}
    repo["workspace"] = repo["workspace"] or src.get("workspace") or config.get("workspace")
    try:
        session = providers.make_session(src)
        base = (since or "").strip()
        if not base:
            # First-ever review: bound the base so we never diff all of history.
            recent = providers.commits(repo, session, pagelen=30)
            base = recent[min(len(recent) - 1, 20)]["hash"] if recent else head
        if base == head:
            return {"source": "diffs", "slug": slug, "base": base, "head": head,
                    "patch": "", "files": [], "truncated": False}
        result = providers.diff(repo, session, base, head)
    except providers.ProviderError as e:
        return {"error": str(e), "source": "messages"}
    patch = result.get("patch") or ""
    truncated = bool(result.get("truncated"))
    if len(patch) > _DIFF_CAP:
        patch, truncated = patch[:_DIFF_CAP], True
    return {"source": "diffs", "slug": slug, "base": base, "head": head,
            "patch": patch, "files": result.get("files") or [], "truncated": truncated}


def get_repo_analysis(slug: str):
    path = _known_slugs().get(slug)
    if not path:
        return {"error": f"no local clone for {slug!r} on this machine"}
    return analysis.analyze_repo(path, slug)


def _days_until(date_str: str):
    try:
        return (date.fromisoformat(date_str) - datetime.now().astimezone().date()).days
    except (ValueError, TypeError):
        return None


def get_project_context(project: str):
    ctx = store.load_context().get(project) or {}
    active = ctx.get("active_launch")
    if active:
        active = {**active, "days_until": _days_until(active.get("target_date", ""))}
    return {
        "active_launch": active,
        "past_launches": ctx.get("past_launches", []),
        "vision": ctx.get("vision", ""),
    }


def get_project_events(project: str, limit: int = 20):
    state = store.load_state()
    grouped = events.events_by_repo(state)
    slug = _resolve_to_slug(state, project) or project
    evs = grouped.get(slug, []) or grouped.get(project, [])
    return evs[-max(1, min(limit, 100)):]


def set_project_summary(project: str, text: str):
    state = store.load_state()
    p = state.get("projects", {}).get(project)
    if not p:
        return f"unknown project {project!r} (see get_pending_work)"
    ai = store.load_ai()
    ai.setdefault("summaries", {})[project] = {
        "text": text.strip()[:2000], "at": _now_iso(), "head_sig": p.get("head_sig", ""),
    }
    store.save_ai(ai)
    return f"summary set for {project}"


def record_digest(project: str, narrative: str, review=None):
    state = store.load_state()
    p = state.get("projects", {}).get(project)
    if not p:
        return f"unknown project {project!r} (see get_pending_work)"
    items, long_items = [], 0
    for r in (review or []):
        s = str(r).strip()
        if not s:
            continue
        clipped, _orig, trimmed = _clip_note(s)
        long_items += trimmed
        items.append(clipped)
    items = items[:6]
    ai = store.load_ai()
    ai.setdefault("digests", {})[project] = {
        "narrative": (narrative or "").strip()[:600],
        "review": items,
        "at": _now_iso(),
        "basis": {"stop_ts": _newest_stop_ts(state, project), "head_sig": p.get("head_sig", "")},
    }
    store.save_ai(ai)
    msg = f"digest recorded for {project}"
    if long_items:
        msg += (f" — {long_items} review item(s) ran long and were trimmed; "
                f"keep each to one line")
    return msg


_WORK_KINDS = {"model", "endpoint", "mcp-tool", "command", "config", "other"}


def record_work_review(project: str, units=None, reviewed_heads=None):
    state = store.load_state()
    p = state.get("projects", {}).get(project)
    if not p:
        return f"unknown project {project!r} (see get_pending_work)"
    slugs = set(p.get("repos", {}))

    clean = []
    for u in (units or [])[:3]:
        if not isinstance(u, dict):
            continue
        decisions = []
        for d in (u.get("decisions") or []):
            if not isinstance(d, dict) or not str(d.get("text", "")).strip():
                continue
            line = d.get("line")
            decisions.append({
                "text": str(d.get("text", "")).strip()[:300],
                "file": str(d.get("file", ""))[:300],
                "line": line if isinstance(line, int) else None,
            })
        surface = []
        for s in (u.get("surface") or []):
            if not isinstance(s, dict) or not str(s.get("name", "")).strip():
                continue
            kind = str(s.get("kind", "")).strip().lower()
            line = s.get("line")
            surface.append({
                "kind": kind if kind in _WORK_KINDS else "other",
                "name": str(s.get("name", "")).strip()[:120],
                "detail": str(s.get("detail", ""))[:300],
                "file": str(s.get("file", ""))[:300],
                "line": line if isinstance(line, int) else None,
            })
        snippets = []
        for sn in (u.get("snippets") or []):
            if not isinstance(sn, dict):
                continue
            line = sn.get("line")
            snippets.append({
                "title": str(sn.get("title", ""))[:160],
                "file": str(sn.get("file", ""))[:300],
                "line": line if isinstance(line, int) else None,
                "code": str(sn.get("code", ""))[:2000],
                "note": str(sn.get("note", ""))[:400],
            })
        period = u.get("period") if isinstance(u.get("period"), dict) else {}
        clean.append({
            # Server-assigned id — the dashboard's hide (✕) keys off it, so it
            # must be unique and survive title edits.
            "id": uuid.uuid4().hex[:12],
            "title": str(u.get("title", "")).strip()[:120],
            "summary": str(u.get("summary", "")).strip()[:700],
            "repos": [str(s) for s in (u.get("repos") or []) if str(s) in slugs][:10],
            "source": "messages" if u.get("source") == "messages" else "diffs",
            "period": {"from": str(period.get("from", ""))[:32],
                       "to": str(period.get("to", ""))[:32]},
            "commits": [str(c)[:12] for c in (u.get("commits") or [])][:40],
            "decisions": decisions[:8],
            "surface": surface[:20],
            "snippets": snippets[:5],
            "mermaid": str(u.get("mermaid") or "").strip()[:4000],
            "at": _now_iso(),
        })

    # The basis records what was ACTUALLY reviewed: provider heads by default,
    # overridden by reviewed_heads (the clone's local HEAD the subagent diffed).
    # A clone behind the remote leaves need_review true and self-heals on pull.
    heads = {s: r.get("head") for s, r in p.get("repos", {}).items() if r.get("head")}
    if isinstance(reviewed_heads, dict):
        heads.update({str(s): str(h) for s, h in reviewed_heads.items()
                      if str(s) in slugs and h})

    ai = store.load_ai()
    entry = ai.setdefault("work_reviews", {}).setdefault(project, {})
    entry["items"] = (clean + list(entry.get("items", [])))[:12]
    entry["basis"] = {"stop_ts": _newest_stop_ts(state, project), "heads": heads}
    entry["at"] = _now_iso()
    store.save_ai(ai)
    return f"{len(clean)} work unit(s) recorded for {project}"


def set_architecture(slug: str, text: str, mermaid: str = ""):
    if slug not in _known_slugs():
        return f"unknown repo {slug!r} (call list_projects)"
    ai = store.load_ai()
    ai.setdefault("architecture", {})[slug] = {
        "text": (text or "").strip()[:2000],
        "mermaid": (mermaid or "").strip()[:4000],
        "at": _now_iso(),
    }
    store.save_ai(ai)
    return f"architecture set for {slug}"


def set_prompts(slug: str, items=None):
    if slug not in _known_slugs():
        return f"unknown repo {slug!r} (call list_projects)"
    clean = []
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        line = it.get("line")
        clean.append({
            "name": str(it.get("name", ""))[:120],
            "text": str(it.get("text", ""))[:4000],
            "file": str(it.get("file", ""))[:300],
            "line": line if isinstance(line, int) else None,
            "dynamic": bool(it.get("dynamic")),
            "note": str(it.get("note", ""))[:400],
        })
    ai = store.load_ai()
    ai.setdefault("prompts", {})[slug] = {"items": clean[:40], "at": _now_iso()}
    store.save_ai(ai)
    return f"{len(clean)} prompt(s) set for {slug}"


def set_setup(slug: str, text: str):
    if slug not in _known_slugs():
        return f"unknown repo {slug!r} (call list_projects)"
    ai = store.load_ai()
    ai.setdefault("setup", {})[slug] = {"text": (text or "").strip()[:4000], "at": _now_iso()}
    store.save_ai(ai)
    return f"setup set for {slug}"


def set_snippets(slug: str, items=None):
    if slug not in _known_slugs():
        return f"unknown repo {slug!r} (call list_projects)"
    clean = []
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        line = it.get("line")
        clean.append({
            "title": str(it.get("title", ""))[:160],
            "file": str(it.get("file", ""))[:300],
            "line": line if isinstance(line, int) else None,
            "code": str(it.get("code", ""))[:4000],
            "note": str(it.get("note", ""))[:400],
        })
    ai = store.load_ai()
    ai.setdefault("snippets", {})[slug] = {"items": clean[:20], "at": _now_iso()}
    store.save_ai(ai)
    return f"{len(clean)} snippet(s) set for {slug}"


def set_data_shape(slug: str, items=None):
    if slug not in _known_slugs():
        return f"unknown repo {slug!r} (call list_projects)"
    clean = []
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        line = it.get("line")
        fields = it.get("fields")
        clean.append({
            "name": str(it.get("name", ""))[:160],
            "kind": str(it.get("kind", ""))[:40],
            "fields": [str(f)[:160] for f in fields][:60] if isinstance(fields, list) else [],
            "file": str(it.get("file", ""))[:300],
            "line": line if isinstance(line, int) else None,
            "note": str(it.get("note", ""))[:400],
        })
    ai = store.load_ai()
    ai.setdefault("data_shape", {})[slug] = {"items": clean[:60], "at": _now_iso()}
    store.save_ai(ai)
    return f"{len(clean)} data-shape item(s) set for {slug}"


def _all_tracked_slugs(state: dict) -> set:
    """Every repo slug Overboard is tracking (union across projects) — the set
    a full grouping is expected to cover, whether or not it's cloned locally."""
    return {s for p in state.get("projects", {}).values() for s in p.get("repos", {})}


def set_grouping(groups=None):
    """Set how repos cluster into projects and each project's display name.
    groups: list of {key, display, repos:[slug]}. `key` is a stable identity
    (reuse it across re-groups; never rename it — rename via `display` only)."""
    state = store.load_state()
    live = _all_tracked_slugs(state)
    clean: dict = {}
    seen: set = set()
    for g in (groups or []):
        if not isinstance(g, dict):
            continue
        key = str(g.get("key") or "").strip()[:80]
        if not key or key in clean:
            continue
        repos = [str(s).strip() for s in (g.get("repos") or [])]
        repos = [s for s in repos if s in live and s not in seen]
        if not repos:
            continue
        seen.update(repos)
        clean[key] = {"display": (str(g.get("display") or key).strip()[:120]), "repos": repos}
    ai = store.load_ai()
    # Signature over the FULL live set, so resolve_projects only treats this as an
    # exact match when the grouping actually covers every tracked repo.
    ai["grouping"] = {"signature": store.slug_signature(live), "groups": clean, "at": _now_iso()}
    store.save_ai(ai)
    missed = len(live) - len(seen)
    tail = f"; {missed} repo(s) left ungrouped" if missed else ""
    return f"grouping set: {len(clean)} project(s) over {len(seen)} repo(s){tail}"


def flag_for_review(project: str, note: str):
    slug = _resolve_to_slug(store.load_state(), project)
    if not slug:
        return f"unknown project {project!r} (call get_pending_work / list_projects)"
    clipped, orig, trimmed = _clip_note(note)
    events.append_event({"ts": time.time(), "type": "flag", "repo_hint": slug, "note": clipped})
    msg = f"flagged for review on {project}"
    if trimmed:
        msg += (f" — note was {orig} chars and was trimmed to fit; "
                f"flags should be one line, so shorten it next time")
    return msg


def record_status(project: str, note: str):
    slug = _resolve_to_slug(store.load_state(), project)
    if not slug:
        return f"unknown project {project!r} (call get_pending_work / list_projects)"
    events.append_event({"ts": time.time(), "type": "status", "repo_hint": slug, "note": _clip_note(note)[0]})
    return f"status recorded for {project}"


def launch_dashboard():
    """Start the Overboard dashboard (a local web server) if not already up, and
    return its URL. Open this in a browser to see everything."""
    app_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")
    url = f"http://localhost:{DASHBOARD_PORT}"
    try:
        subprocess.Popen(
            [sys.executable, app_py, "--serve", "--port", str(DASHBOARD_PORT)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:
        return {"error": f"could not launch dashboard: {e}", "url": url}
    return {"url": url, "note": "dashboard starting; open the URL in a browser"}


# ============================ MCP tool table ===============================
_S = {"type": "string"}
_I = {"type": "integer"}
_OBJ_ARR = {"type": "array", "items": {"type": "object"}}
TOOLS = [
    ("get_pending_work", "Your to-do list. Each entry is a project needing attention, with need_summary (commits changed), need_digest (new finished work), and/or need_review (work landed since the last recent-work review — see record_work_review; review_since maps each repo slug to the last-reviewed hash or null, and recent_review_titles lists prior card titles for theme-name consistency), plus its repo slugs. Entries carry `launch` {type, title, target_date, days_until} when the CTO has an active launch — use it to ground your prose; call get_project_context only when you need the full plan (goals, push-back history, vision). May also include a grouping item {kind:'grouping', need_grouping:true, all_repos:[...], ungrouped:[...]} — handle it FIRST by calling set_grouping so projects are named right. Start every update pass here; if empty, nothing to do.", {}, [], get_pending_work),
    ("list_projects", "Repo slugs Overboard tracks on this machine + local paths. Use the slug for get_repo_analysis / get_commits / set_architecture.", {}, [], list_projects),
    ("get_commits", "Recent Bitbucket commits for a repo (includes work pushed from other machines). Basis for a commit-status summary.", {"slug": _S, "limit": _I}, ["slug"], get_commits),
    ("get_recent_diff", "Real diff for a repo with NO local clone on THIS machine (e.g. a Mac-only iOS app seen from the Linux/DO box). Fetches the unified diff for review_since..head straight from the provider (GitHub compare / Bitbucket diff) so you can build a real source:'diffs' recent-work card instead of a messages-only one. slug is a repo slug; since = review_since[slug] (the last-reviewed hash; omit for a bounded first review). Returns {source:'diffs', base, head, patch, files, truncated} — review the patch yourself and call record_work_review with reviewed_heads={slug: head}. If it returns an 'error' (no creds / force-push / provider down), fall back to get_commits with source:'messages'. When a local clone DOES exist, prefer the work-reviewer subagent instead.", {"slug": _S, "since": _S}, ["slug"], get_recent_diff),
    ("get_repo_analysis", "Static analysis of a repo's local clone: prompts, DB-schema shape, file structure, and manifest_digest (turn it into an architecture write-up via set_architecture). No AI is run — that's your job.", {"slug": _S}, ["slug"], get_repo_analysis),
    ("get_project_events", "Recent activity events for a project or repo (Stop/tool/flag/status), most recent last — raw material for a digest.", {"project": _S, "limit": _I}, ["project"], get_project_events),
    ("get_project_context", "The CTO's plans for a project (READ-ONLY, CTO-owned): active launch/milestone (type, title, action, target_date, days_until, goals, push-back history), past launches, and the vision/direction text. Use it to sharpen summaries and to flag when a launch is near and its goals look unmet, or the plan has slipped.", {"project": _S}, ["project"], get_project_context),
    ("set_project_summary", "Set a project's commit-status summary (1-2 sentences: what was worked on and where it stands). project is a group name from get_pending_work.", {"project": _S, "text": _S}, ["project", "text"], set_project_summary),
    ("record_digest", "Record the live status digest: narrative (one sentence: what the team is doing now) and review (0-4 items the CTO must ACT ON, gotchas in how it works, or assumptions the Claude made — NOT a summary of features built, which the CTO already knows). Call after reading the project's new events.", {"project": _S, "narrative": _S, "review": {"type": "array", "items": _S}}, ["project", "narrative"], record_digest),
    ("record_work_review", "Record 1-3 'recent work' cards for a project — the delta layer shown newest-first on the dashboard (what just landed, vs the snapshot panels). Each unit: {title, summary, repos:[slug], source ('diffs' when built from real local git diffs, 'messages' when only commit messages were available), period:{from,to}, commits:[short-hash], decisions:[{text,file,line}] (choices/assumptions baked in), surface:[{kind: model|endpoint|mcp-tool|command|config|other, name, detail, file, line}] (new core surface), snippets:[{title,file,line,code,note}] (key NEW code), mermaid (optional flow)}. Ids are server-assigned. reviewed_heads: {slug: full-hash} — the clone HEAD each diff actually ran against (defaults to provider heads); pass it so a stale clone re-fires need_review until pulled. units=[] is legal and just advances the basis (stops fired but nothing material changed) so need_review clears.", {"project": _S, "units": _OBJ_ARR, "reviewed_heads": {"type": "object"}}, ["project"], record_work_review),
    ("set_architecture", "Set a repo's architecture write-up (2-4 sentences) and optionally a Mermaid flowchart string. slug is a repo slug from list_projects.", {"slug": _S, "text": _S, "mermaid": _S}, ["slug", "text"], set_architecture),
    ("set_prompts", "Set a repo's REAL LLM prompts (this replaces the noisy static keyword scan). items: list of {name, text, file, line, dynamic (true if built at runtime — then text is the code that assembles it), note}. Read the actual clone to find genuine system prompts / templates.", {"slug": _S, "items": _OBJ_ARR}, ["slug", "items"], set_prompts),
    ("set_setup", "Set a repo's setup/run panel: the high-level, human-in-the-loop steps the CTO needs to run, deploy, or try what was built — deploy + secret/env commands for its host (Railway/Vercel/Fly/etc.), or a Claude plugin's marketplace-add/install/test workflow. NOT a dev-onboarding bootstrap and NOT the inside of scripts Claude already wrote (give the command that invokes a script, not its steps). Plain text / simple markdown, a few lines.", {"slug": _S, "text": _S}, ["slug", "text"], set_setup),
    ("set_snippets", "Set a repo's key code snippets. items: list of {title, file, line, code, note} — a few important excerpts (entrypoint, core handler, tricky bits) a human can eyeball without opening the repo.", {"slug": _S, "items": _OBJ_ARR}, ["slug", "items"], set_snippets),
    ("set_data_shape", "Set a repo's data model / DB shape (this replaces the static schema scanner, which only knows SQL/Prisma/Django). items: list of {name, kind (table|collection|entity|model|type), fields (list of 'name: type' strings), file, line, note}. Read the actual clone — works for ANY stack: SQL DDL, Prisma, Django/SQLAlchemy, JPA/Hibernate @Entity, .NET EF DbContext, Mongoose/Mongo collections, GORM, ActiveRecord, Diesel. Empty if the repo has no persistent data model.", {"slug": _S, "items": _OBJ_ARR}, ["slug", "items"], set_data_shape),
    ("set_grouping", "Set how repos cluster into projects AND each project's display name (replaces the prefix-stem guess). groups: list of {key, display, repos:[slug]}. Give one group per real product. `key` is a STABLE identity string: reuse the same key across re-groups and NEVER rename it (rename by editing `display` only) — the key is how summaries/digests/launches stay attached. `display` is the human name shown in the sidebar. Cover every slug from get_pending_work's all_repos / list_projects.", {"groups": _OBJ_ARR}, ["groups"], set_grouping),
    ("flag_for_review", "Flag something the CTO must know or act on — NOT a feature recap. Use for: something the human has to do (from a finished-work message — restart, add a key, decide, review a risky change), a non-obvious gotcha in how it now works, or an assumption the Claude made. Never flag 'added feature X'. project may be a group name or repo slug; note is the one specific thing.", {"project": _S, "note": _S}, ["project", "note"], flag_for_review),
    ("record_status", "Post a short status line to a project's activity feed. Accepts a project group name or a repo slug.", {"project": _S, "note": _S}, ["project", "note"], record_status),
    ("launch_dashboard", "Start the Overboard dashboard web server (if not already running) and return its URL.", {}, [], launch_dashboard),
]
_HANDLERS = {name: fn for name, _d, _p, _r, fn in TOOLS}


def _tool_defs():
    return [
        {
            "name": name,
            "description": desc,
            "inputSchema": {"type": "object", "properties": props, "required": req},
        }
        for name, desc, props, req, _fn in TOOLS
    ]


# ============================ stdio JSON-RPC loop ==========================
def _send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _result(mid, result) -> None:
    _send({"jsonrpc": "2.0", "id": mid, "result": result})


def _error(mid, code, message) -> None:
    _send({"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}})


def _handle(msg: dict) -> None:
    method = msg.get("method")
    mid = msg.get("id")
    is_request = mid is not None

    if method == "initialize":
        _result(mid, {
            "protocolVersion": msg.get("params", {}).get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "overboard", "version": "0.2.0"},
        })
    elif method in ("notifications/initialized", "initialized"):
        return  # notification, no response
    elif method == "ping":
        _result(mid, {})
    elif method == "tools/list":
        _result(mid, {"tools": _tool_defs()})
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = _HANDLERS.get(name)
        if fn is None:
            _error(mid, -32602, f"unknown tool: {name}")
            return
        try:
            out = fn(**args)
        except Exception as e:  # tool errors are results, not protocol errors
            _result(mid, {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True})
            return
        text = out if isinstance(out, str) else json.dumps(out)
        _result(mid, {"content": [{"type": "text", "text": text}]})
    elif is_request:
        _error(mid, -32601, f"method not found: {method}")
    # else: unknown notification — ignore


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        try:
            _handle(msg)
        except Exception as e:  # never crash the loop
            print(f"overboard mcp handler error: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()

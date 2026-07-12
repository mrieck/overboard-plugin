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
from datetime import datetime, timezone

# Make the plugin's `overboard` package importable no matter the cwd or how
# we're launched (this file lives at <root>/overboard/mcp_server.py).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from overboard import analysis, bitbucket, events, localrepo, manager, store  # noqa: E402

STOP_TYPES = ("Stop", "SubagentStop")
DASHBOARD_PORT = 8787


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
    branch = None
    for p in state.get("projects", {}).values():
        r = p.get("repos", {}).get(slug)
        if r:
            branch = r.get("branch")
            break
    if not branch:
        return {"error": f"unknown repo {slug!r} (call list_projects)"}
    env = store.load_env()
    session = bitbucket.make_session(env["ATLASSIAN_EMAIL"], env["BITBUCKET_API_TOKEN"])
    try:
        commits = bitbucket.fetch_recent_commits(
            session, config["workspace"], slug, branch, pagelen=min(max(limit, 1), 50)
        )
    except bitbucket.BitbucketError as e:
        return {"error": str(e)}
    return commits[:limit]


def get_repo_analysis(slug: str):
    path = _known_slugs().get(slug)
    if not path:
        return {"error": f"no local clone for {slug!r} on this machine"}
    return analysis.analyze_repo(path, slug)


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
    ai = store.load_ai()
    ai.setdefault("digests", {})[project] = {
        "narrative": (narrative or "").strip()[:600],
        "review": [str(r).strip() for r in (review or []) if str(r).strip()][:6],
        "at": _now_iso(),
        "basis": {"stop_ts": _newest_stop_ts(state, project), "head_sig": p.get("head_sig", "")},
    }
    store.save_ai(ai)
    return f"digest recorded for {project}"


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


def flag_for_review(project: str, note: str):
    slug = _resolve_to_slug(store.load_state(), project)
    if not slug:
        return f"unknown project {project!r} (call get_pending_work / list_projects)"
    events.append_event({"ts": time.time(), "type": "flag", "repo_hint": slug, "note": note.strip()[:500]})
    return f"flagged for review on {project}"


def record_status(project: str, note: str):
    slug = _resolve_to_slug(store.load_state(), project)
    if not slug:
        return f"unknown project {project!r} (call get_pending_work / list_projects)"
    events.append_event({"ts": time.time(), "type": "status", "repo_hint": slug, "note": note.strip()[:500]})
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
TOOLS = [
    ("get_pending_work", "Your to-do list. Each entry is a project needing attention, with need_summary (commits changed) and/or need_digest (new finished work) plus its repo slugs. Start every update pass here; if empty, nothing to do.", {}, [], get_pending_work),
    ("list_projects", "Repo slugs Overboard tracks on this machine + local paths. Use the slug for get_repo_analysis / get_commits / set_architecture.", {}, [], list_projects),
    ("get_commits", "Recent Bitbucket commits for a repo (includes work pushed from other machines). Basis for a commit-status summary.", {"slug": _S, "limit": _I}, ["slug"], get_commits),
    ("get_repo_analysis", "Static analysis of a repo's local clone: prompts, DB-schema shape, file structure, and manifest_digest (turn it into an architecture write-up via set_architecture). No AI is run — that's your job.", {"slug": _S}, ["slug"], get_repo_analysis),
    ("get_project_events", "Recent activity events for a project or repo (Stop/tool/flag/status), most recent last — raw material for a digest.", {"project": _S, "limit": _I}, ["project"], get_project_events),
    ("set_project_summary", "Set a project's commit-status summary (1-2 sentences: what was worked on and where it stands). project is a group name from get_pending_work.", {"project": _S, "text": _S}, ["project", "text"], set_project_summary),
    ("record_digest", "Record the live status digest: narrative (one sentence: what the team is doing now) and review (0-4 specific things the CTO should check). Call after reading the project's new events.", {"project": _S, "narrative": _S, "review": {"type": "array", "items": _S}}, ["project", "narrative"], record_digest),
    ("set_architecture", "Set a repo's architecture write-up (2-4 sentences) and optionally a Mermaid flowchart string. slug is a repo slug from list_projects.", {"slug": _S, "text": _S, "mermaid": _S}, ["slug", "text"], set_architecture),
    ("flag_for_review", "Flag something for the CTO to review on the dashboard. project may be a group name or a repo slug; note is one specific thing to check.", {"project": _S, "note": _S}, ["project", "note"], flag_for_review),
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

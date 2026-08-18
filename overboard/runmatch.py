"""Correlate hook events (events.jsonl) with a scheduled run — WITHOUT parsing
terminal output. Port of the Mac app's RunMatcher (Overboard/Scheduler/RunMatcher.swift).

The plugin's hooks fire for every Claude Code session, so a scheduled run is
identified by capturing the SessionStart in its cwd near its launch time; after
that, events belong to the run by session_id. That is what keeps a *human*
session working in the same directory from being read as the run's completion
(and getting /exit-ed). Only when no SessionStart was seen at all (older hook
payloads, a missed line) do we fall back to cwd + after-launch time.

A Stop while a spawned subagent is still running is a turn boundary, not the
run's end: Task/Agent PostToolUse fires at SPAWN, SubagentStop at finish, so
`completed = SessionEnd or (Stop and pending_subagents <= 0)` — where the Stop
must land AFTER the last subagent event. A Stop that predates the SubagentStop
is the same turn boundary: the parent gets a fresh turn when a background
subagent reports back, and completing on that stale Stop would /exit it.

Pure and deterministic; the scheduler feeds it the tail of the event log each
poll (idempotent — re-reading the same events yields the same answer).
"""

from __future__ import annotations

import os

# Window around launch in which a SessionStart is accepted as this run's. The
# early slack covers hook/clock skew; the late edge is generous because the
# plugin's launch path (ensure_server + agent.start + wait-until-promptable) is
# synchronous and can take a while before claude actually starts.
SESSION_START_EARLY = -5
SESSION_START_LATE = 90

_SUBAGENT_TOOLS = ("Task", "Agent")


def normalize_path(path) -> str:
    """Resolve symlinks + user dir so `/tmp` vs `/private/tmp` and `~` spellings
    compare equal. Never raises; a bad value normalizes to ''."""
    if not path or not isinstance(path, str):
        return ""
    try:
        return os.path.realpath(os.path.expanduser(path))
    except (OSError, ValueError):
        return path


def assess(cwd: str, started_ts: float, session_id, events, claimed) -> dict:
    """Assess `events` (dicts as written by hooks/emit_event.py, in log order)
    against a run identified by `cwd` + `started_ts` (+ `session_id` once known).
    `claimed` = session ids already owned by OTHER runs.

    Returns {"session_id": captured-or-existing-or-None,
             "captured": bool (newly captured this pass),
             "completed": bool,
             "completion_message": str | None,
             "last_activity_ts": float | None}
    """
    ncwd = normalize_path(cwd)
    captured = False

    # Phase 1: capture identity from the first unclaimed SessionStart in this
    # cwd within the acceptance window.
    if not session_id:
        lo = started_ts + SESSION_START_EARLY
        hi = started_ts + SESSION_START_LATE
        for e in events:
            if e.get("type") != "SessionStart":
                continue
            sid = e.get("session_id")
            if not sid or sid in claimed:
                continue
            if normalize_path(e.get("cwd")) != ncwd:
                continue
            ts = e.get("ts") or 0
            if lo <= ts <= hi:
                session_id = sid
                captured = True
                break

    def belongs(e: dict) -> bool:
        if session_id:
            return e.get("session_id") == session_id
        # No SessionStart captured: fall back to cwd + after-launch time.
        if normalize_path(e.get("cwd")) != ncwd:
            return False
        return (e.get("ts") or 0) >= started_ts

    stop_seen = False
    session_ended = False
    completion_message = None
    last_activity = None
    # Subagent launches minus finishes. May dip negative transiently for
    # foreground subagents (SubagentStop and the tool's PostToolUse land
    # together, in either order); only the final tally matters. Every subagent
    # event also forgets any Stop seen so far, so the SubagentStop that zeroes
    # the tally can't complete the run on the strength of an earlier
    # turn-boundary Stop while the parent is being re-invoked with the result.
    pending_subagents = 0

    for e in events:
        if not belongs(e):
            continue
        typ = e.get("type")
        ts = e.get("ts") or 0
        if typ in ("Stop", "SessionEnd"):
            stop_seen = True
            if typ == "SessionEnd":
                session_ended = True
            msg = e.get("last_message")
            if isinstance(msg, str) and msg:
                completion_message = msg
        elif typ == "PostToolUse":
            if e.get("tool_name") in _SUBAGENT_TOOLS:
                pending_subagents += 1
                stop_seen = False
        elif typ == "SubagentStop":
            pending_subagents -= 1
            stop_seen = False
        elif typ != "SessionStart":
            continue
        last_activity = ts if last_activity is None else max(last_activity, ts)

    # A SessionEnd means the session is already gone — nothing left to protect,
    # so it always completes. A lost SubagentStop can pin the count above zero;
    # the slot timeout is the backstop there.
    completed = session_ended or (stop_seen and pending_subagents <= 0)
    return {"session_id": session_id, "captured": captured, "completed": completed,
            "completion_message": completion_message, "last_activity_ts": last_activity}

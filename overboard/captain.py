"""Captain response parsing — the plugin-side twin of the Mac app's
CaptainResponseFile, so the shape has a test oracle here and a future
dashboard host can reuse it. Pure, stdlib, py3.9."""

from __future__ import annotations

import json
from datetime import datetime, timezone

ACTION_TYPES = ("create_task", "create_agent", "update_agent", "disable_agent",
                "question", "goal_progress", "note")
DEFAULT_LIMITS = {"question_max_chars": 700, "log_entry_max_chars": 1200}


def parse_response(raw, limits=None):
    """{"ok", "error", "log_entry", "memory_updated", "actions", "dropped"}.
    A missing log_entry or an unknown action type fails the whole response; a
    question over the length limit is truncated, not dropped. No caps on how
    many actions: everything the captain writes runs."""
    limits = {**DEFAULT_LIMITS, **(limits or {})}
    try:
        data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except ValueError:
        return {"ok": False, "error": "response is not JSON"}
    if not isinstance(data, dict):
        return {"ok": False, "error": "response is not an object"}
    log = data.get("log_entry")
    if not isinstance(log, str) or not log.strip():
        return {"ok": False, "error": "response has no log_entry"}
    log = log.strip()
    if len(log) > limits["log_entry_max_chars"]:
        log = log[: limits["log_entry_max_chars"]] + "…"
    actions = data.get("actions") or []
    if not isinstance(actions, list):
        return {"ok": False, "error": "actions is not a list"}
    out, dropped = [], []
    for i, action in enumerate(actions):
        if not isinstance(action, dict):
            return {"ok": False, "error": f"action {i + 1} is not an object"}
        kind = action.get("type")
        if kind not in ACTION_TYPES:
            return {"ok": False, "error": f"action {i + 1} has unknown type {kind!r}"}
        action = dict(action)
        if kind == "question":
            text = action.get("text")
            if not isinstance(text, str) or not text.strip():
                dropped.append({"index": i, "why": "question has no text"})
                continue
            if len(text) > limits["question_max_chars"]:
                action["text"] = text[: limits["question_max_chars"]] + "…"
        for key in ("plugins", "cwds", "options"):
            value = action.get(key)
            if isinstance(value, str):
                action[key] = [value] if value.strip() else []
        out.append(action)
    return {"ok": True, "error": None, "log_entry": log,
            "memory_updated": bool(data.get("memory_updated")), "actions": out, "dropped": dropped}


def runs_per_day(schedule) -> float:
    """Average daily firings of a ScheduleSpec JSON object."""
    if not isinstance(schedule, dict):
        return 0.0
    kind = schedule.get("kind")
    times = schedule.get("times") or []
    if kind == "daily":
        return float(len(times))
    if kind == "weekly":
        return len(schedule.get("days") or []) * len(times) / 7.0
    if kind == "everyHours":
        interval = schedule.get("interval") or 0
        if interval < 1:
            return 0.0
        window = schedule.get("window")
        hours = (window["endHour"] - window["startHour"]) if window else 24
        return float(max(0, hours) // interval)
    if kind == "once":
        date = schedule.get("date")
        try:
            fire = datetime.fromisoformat(str(date).replace("Z", "+00:00"))
        except ValueError:
            return 0.0
        return 1.0 if fire > datetime.now(timezone.utc) else 0.0
    return 0.0


def is_duplicate_agent(action, agents) -> bool:
    """Same name (case-insensitive), or same command+cwds as an enabled agent."""
    name = (action.get("name") or "").strip().lower()
    cwds = set(action.get("cwds") or [])
    for agent in agents:
        if name and (agent.get("name") or "").strip().lower() == name:
            return True
        if agent.get("enabled") and agent.get("command") == action.get("command") \
                and set(agent.get("cwds") or []) == cwds:
            return True
    return False

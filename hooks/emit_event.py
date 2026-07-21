#!/usr/bin/env python3
"""Overboard hook: append one activity event, then get out of the way.

Runs under the system python for EVERY working Claude session (async), so it is
deliberately stdlib-only, fast, and failure-proof — it never imports the
overboard package (which needs third-party deps), never raises, and always
exits 0 so it can never block or delay the Claude it's observing.

Stdin is the Claude Code hook JSON. We record only cheap, non-sensitive signal:
the event type, session, cwd, the model's closing message (truncated), and — for
tool events — the file path or truncated command target. The dashboard/manager
(running in the real venv) maps cwd -> repo and does any summarization later.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

EVENTS_PATH = Path.home() / ".cache" / "overboard" / "events.jsonl"
_MAX_MSG = 4000
_MAX_CMD = 160


def _target(tool_input: dict):
    if not isinstance(tool_input, dict):
        return None
    path = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("notebook_path")
    if path:
        return str(path)
    cmd = tool_input.get("command")
    if isinstance(cmd, str):
        return cmd[:_MAX_CMD]
    return None


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(data, dict):
        return 0

    evt = {
        "ts": time.time(),
        "type": data.get("hook_event_name"),
        "session_id": data.get("session_id"),
        "cwd": data.get("cwd"),
    }
    msg = data.get("last_assistant_message")
    if isinstance(msg, str) and msg:
        evt["last_message"] = msg[:_MAX_MSG]
    for k in ("agent_type", "reason", "source", "session_title"):
        v = data.get(k)
        if v:
            evt[k] = v
    tool = data.get("tool_name")
    if tool:
        evt["tool_name"] = tool
        evt["target"] = _target(data.get("tool_input") or {})

    try:
        EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(EVENTS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(evt) + "\n")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""What the scheduler ran recently — the sweep's "ran since last report" list.

Read-only over two app-owned stores (never written here):

- the dashboard scheduler's history, ``~/.cache/overboard/scheduler/runs.json``
  (``{"version", "runs": [{slot_name, cwd, prompt, outcome, ended_at, ...}]}``,
  local naive timestamps);
- the Mac app's records, ``~/Library/Application Support/Overboard/runs/<id>.json``
  (``{slotName, cwd, projectDir, command, outcome: {"completed": {}}, endedAt}``,
  fractional ISO-8601 in UTC).

Both are normalised to ``{name, project, kind, outcome, finished_at}`` — titles
only. The CTO wants the assistant *aware* that a blog post was written or a
directory was posted to, not fed the results, so completion messages stay out.
"""

from __future__ import annotations

import glob
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from . import schedule, store

PLUGIN_RUNS_PATH = store.STATE_DIR / "scheduler" / "runs.json"
MAC_RUNS_DIR = Path.home() / "Library" / "Application Support" / "Overboard" / "runs"

SWEEP_COMMAND = "/overboard:overboard"
# The window never reaches further back than this, even with no earlier sweep.
MAX_WINDOW = timedelta(days=7)
DEFAULT_WINDOW = timedelta(hours=24)

# Legacy prefixes from before runs were named after their task.
_DISPATCH_TASK_PREFIX = "Dispatch: "
_DISPATCHER_PREFIXES = ("Dispatcher #", "Overboard dispatch ")
_SWEEP_NAMES = ("Morning report", "Overboard sweep")


def _project(cwd: str, project_dir: "str | None") -> str:
    folder = (project_dir or cwd or "").rstrip("/")
    return os.path.basename(folder) or folder or "?"


def _kind(name: str, command: str) -> str:
    if command == SWEEP_COMMAND or name in _SWEEP_NAMES or name.startswith("Morning report"):
        return "sweep"
    if name.startswith(_DISPATCHER_PREFIXES):
        return "dispatcher"
    if name.startswith(_DISPATCH_TASK_PREFIX):
        return "task"
    return "scheduled"


def _title(name: str) -> str:
    return name[len(_DISPATCH_TASK_PREFIX):] if name.startswith(_DISPATCH_TASK_PREFIX) else name


def _mac_outcome(raw) -> str:
    """``{"completed": {}}`` → ``completed``; ``{"launchFailed": {...}}`` → ``launch_failed``."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict) and raw:
        key = next(iter(raw))
        out = "".join("_" + ch.lower() if ch.isupper() else ch for ch in key)
        return out
    return "unknown"


def _plugin_runs(path: Path) -> list:
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    runs = data.get("runs") if isinstance(data, dict) else data
    out = []
    for r in runs or []:
        if not isinstance(r, dict) or not r.get("ended_at") or not r.get("outcome"):
            continue
        name = str(r.get("slot_name") or "")
        command = str(r.get("prompt") or "").strip()
        out.append({"name": _title(name), "project": _project(r.get("cwd") or "", None),
                    "kind": _kind(name, command), "outcome": str(r["outcome"]),
                    "finished_at": schedule.parse_iso_any(r["ended_at"]),
                    "started_at": schedule.parse_iso_any(r.get("started_at"))})
    return out


def _mac_runs(directory: Path) -> list:
    out = []
    for path in glob.glob(str(directory / "*.json")):
        try:
            with open(path) as f:
                r = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(r, dict) or not r.get("endedAt") or not r.get("outcome") or r.get("hidden"):
            continue
        name = str(r.get("slotName") or "")
        out.append({"name": _title(name),
                    "project": _project(r.get("cwd") or "", r.get("projectDir")),
                    "kind": _kind(name, str(r.get("command") or "").strip()),
                    "outcome": _mac_outcome(r.get("outcome")),
                    "finished_at": schedule.parse_iso_any(r["endedAt"]),
                    "started_at": schedule.parse_iso_any(r.get("startedAt"))})
    return out


def _iso(dt: "datetime | None") -> "str | None":
    return dt.replace(microsecond=0).isoformat() if dt else None


def recent_runs(now: "datetime | None" = None, plugin_path: "Path | None" = None,
                mac_dir: "Path | None" = None) -> dict:
    """``{since, previous_report_at, runs}``: finished scheduled and dispatched
    runs since the previous sweep started (capped at 7 days; 24 h when no
    earlier sweep exists), newest first. Sweeps and the dispatcher's own
    sessions are left out of ``runs``."""
    now = now or datetime.now()
    everything = _plugin_runs(plugin_path or PLUGIN_RUNS_PATH) + _mac_runs(mac_dir or MAC_RUNS_DIR)
    everything = [r for r in everything if r["finished_at"] is not None]

    # The previous report: the newest finished sweep that isn't the one running
    # now (a live sweep has no ended_at, so anything here already finished; the
    # small grace keeps a sweep that just ended a second ago from counting as
    # "previous" for itself).
    grace = now - timedelta(minutes=2)
    sweeps = sorted((r for r in everything if r["kind"] == "sweep" and r["finished_at"] <= grace),
                    key=lambda r: r["finished_at"], reverse=True)
    previous = sweeps[0] if sweeps else None
    floor = now - MAX_WINDOW
    if previous:
        since = max(previous.get("started_at") or previous["finished_at"], floor)
    else:
        since = now - DEFAULT_WINDOW

    runs = [r for r in everything
            if r["kind"] in ("scheduled", "task") and since <= r["finished_at"] <= now]
    runs.sort(key=lambda r: r["finished_at"], reverse=True)
    return {
        "since": _iso(since),
        "previous_report_at": _iso(previous["finished_at"]) if previous else None,
        "runs": [{"name": r["name"], "project": r["project"], "kind": r["kind"],
                  "outcome": r["outcome"], "finished_at": _iso(r["finished_at"])}
                 for r in runs],
    }

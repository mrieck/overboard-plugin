"""What the scheduler ran recently — the sweep's "ran since last report" list.

Read-only over two app-owned stores (never written here):

- the dashboard scheduler's history, ``~/.cache/overboard/scheduler/runs.json``
  (``{"version", "runs": [{slot_name, cwd, prompt, outcome, ended_at, ...}]}``,
  local naive timestamps);
- the Mac app's records: its database
  (``~/Library/Application Support/Overboard/overboard.sqlite``, opened
  read-only — the app is the only writer), or, before the database existed,
  ``~/Library/Application Support/Overboard/runs/<id>.json``
  (``{slotName, cwd, projectDir, command, outcome: {"completed": {}}, endedAt}``,
  fractional ISO-8601 in UTC).

Both are normalised to ``{name, project, kind, outcome, finished_at, ship}`` —
titles only. The CTO wants the assistant *aware* that a blog post was written
or a directory was posted to, not fed the results, so completion messages stay
out. The database also yields ``ships``: each department's last Captain log
entry and the questions it is waiting on the CTO for.
"""

from __future__ import annotations

import glob
import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from . import schedule, store

PLUGIN_RUNS_PATH = store.STATE_DIR / "scheduler" / "runs.json"
MAC_RUNS_DIR = Path.home() / "Library" / "Application Support" / "Overboard" / "runs"
MAC_DB_PATH = Path.home() / "Library" / "Application Support" / "Overboard" / "overboard.sqlite"

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


def _mac_runs_sqlite(db_path: Path) -> "list | None":
    """The Mac app's runs from its database, or None when it can't be read
    (no database yet, a locked file, an older sqlite) — the JSON scan is the
    fallback. Read-only: the app owns the schema."""
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
        conn.execute("PRAGMA query_only = 1")
        rows = conn.execute(
            """SELECT r.agent_name, r.agent_kind, r.cwd, r.project_dir, r.outcome, r.started_at, r.ended_at,
                      r.payload, s.name AS ship
               FROM runs r LEFT JOIN ships s ON s.id = r.ship_id
               WHERE r.ended_at IS NOT NULL AND r.outcome IS NOT NULL AND r.hidden = 0
                 AND r.agent_kind != 'system'"""
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return None
    out = []
    for name, kind, cwd, project_dir, outcome, started, ended, payload, ship in rows:
        try:
            command = str((json.loads(payload) or {}).get("command") or "")
        except (ValueError, TypeError):
            command = ""
        kind = {"dispatch": "task", "oneoff": "task", "captain": "captain"}.get(kind, _kind(str(name), command))
        out.append({"name": _title(str(name)), "project": _project(cwd or "", project_dir),
                    "kind": kind, "outcome": str(outcome),
                    "finished_at": datetime.fromtimestamp(ended), "started_at": datetime.fromtimestamp(started),
                    "ship": ship})
    return out


def _ships(db_path: Path, since: datetime) -> list:
    """Each ship's last Captain log entry, open questions and proposals."""
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
        conn.execute("PRAGMA query_only = 1")
        ships = conn.execute("SELECT id, name, builtin FROM ships WHERE archived_at IS NULL ORDER BY sort_order, name").fetchall()
        logs = conn.execute(
            """SELECT ship_id, summary, ended_at FROM runs WHERE agent_kind = 'captain' AND summary IS NOT NULL
               ORDER BY ended_at DESC""").fetchall()
        items = conn.execute("SELECT ship_id, status, payload FROM work_items WHERE completed_at IS NULL").fetchall()
        conn.close()
    except sqlite3.Error:
        return []
    latest = {}
    for ship_id, summary, ended in logs:
        latest.setdefault(ship_id, (summary, ended))
    questions = {}
    for ship_id, status, payload in items:
        try:
            item = json.loads(payload) or {}
        except ValueError:
            continue
        for q in item.get("humanQA") or []:
            if not q.get("answeredAt") and not q.get("dismissedAt"):
                questions.setdefault(ship_id, []).append((q.get("question") or "").split("\n")[0][:200])
    out = []
    for ship_id, name, builtin in ships:
        if builtin == "one_off" and ship_id not in questions:
            continue
        entry, at = latest.get(ship_id, (None, None))
        out.append({"name": name,
                    "last_captain_log": entry.split("\n")[0] if entry else None,
                    "last_captain_at": _iso(datetime.fromtimestamp(at)) if at else None,
                    "open_questions": questions.get(ship_id, [])})
    return out


def _iso(dt: "datetime | None") -> "str | None":
    return dt.replace(microsecond=0).isoformat() if dt else None


def recent_runs(now: "datetime | None" = None, plugin_path: "Path | None" = None,
                mac_dir: "Path | None" = None, mac_db: "Path | None" = None) -> dict:
    """``{since, previous_report_at, runs, ships}``: finished scheduled and
    dispatched runs since the previous sweep started (capped at 7 days; 24 h
    when no earlier sweep exists), newest first, each with its ``ship`` when it
    belonged to one; and ``ships`` — every department's last Captain log entry
    and what it waits on. Sweeps, captain passes and the dispatcher's own
    sessions are left out of ``runs``."""
    now = now or datetime.now()
    # An explicit JSON directory (tests, another host) without a database
    # means "JSON only" — never the real database behind a caller's back.
    db_path = mac_db if mac_db is not None else (None if mac_dir is not None else MAC_DB_PATH)
    mac = _mac_runs_sqlite(db_path) if db_path is not None else None
    if mac is None:
        mac = _mac_runs(mac_dir or MAC_RUNS_DIR)
    everything = _plugin_runs(plugin_path or PLUGIN_RUNS_PATH) + mac
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
                  "outcome": r["outcome"], "finished_at": _iso(r["finished_at"]),
                  "ship": r.get("ship")}
                 for r in runs],
        "ships": _ships(db_path, since) if db_path is not None else [],
    }

"""The plugin's cron-style scheduler: slots fire unattended claude runs inside
Herdr. A deliberately basic port of the Mac app's SchedulerEngine — one cwd per
slot, FIFO queue, one run at a time, no preflight/usage/power handling.

Runs entirely inside the dashboard server process: `Scheduler.start()` is
called after the HTTP bind succeeds (the bind is the single-instance lock), and
a daemon thread ticks every 5s. When the dashboard isn't running, slots simply
don't fire — missed occurrences are not back-filled; one more than 30 minutes
late is recorded as skipped, not run stale.

State lives in ~/.cache/overboard/scheduler/ (slots.json, runs.json,
active.json, transcripts/). Single writer: this engine's thread(s) inside the
dashboard process — the MCP server and hooks never touch it.

Completion detection: a scheduled claude's Stop hook appends to events.jsonl
(read-only here; append-only by construction) — a Stop event matching the run's
cwd after its start marks the work done; we capture last_message, ask the agent
to /exit, and close the pane.
"""

from __future__ import annotations

import json
import threading
import time as _time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from . import herdr, schedule, store, workspaces

SCHED_DIR = store.SCHEDULER_DIR
SLOTS_PATH = SCHED_DIR / "slots.json"
RUNS_PATH = SCHED_DIR / "runs.json"
ACTIVE_PATH = SCHED_DIR / "active.json"
TRANSCRIPTS_DIR = SCHED_DIR / "transcripts"
EVENTS_PATH = store.STATE_DIR / "events.jsonl"

TICK_SECS = 5
MAX_ACTIVE = 1
HISTORY_CAP = 200
MISSED_GRACE = timedelta(minutes=30)
EXIT_GRACE_SECS = 10
DEFAULT_TIMEOUT_MINUTES = 90
HERDR_STATUS_TTL = 15  # seconds the cached installed/reachable answer is trusted


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_iso(s) -> "datetime | None":
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


class Scheduler:
    """All public methods are called from HTTP handler threads; one lock guards
    slots/queue/active/history. Herdr socket calls happen outside the lock."""

    def __init__(self):
        self._lock = threading.Lock()
        self._slots: list = []
        self._queue: list = []      # run records waiting to launch (FIFO)
        self._active: list = []     # run records with a live pane
        self._history: list = []    # finished run records, newest first
        self._next_due: dict = {}   # slot id -> datetime
        self._started = False
        self._launching = False     # a _begin_run is in flight (socket, slow)
        self._polling = False       # a poll pass is in flight
        self._herdr_status = {"at": 0.0, "installed": False, "reachable": False}

    # ---- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        """Load state, adopt runs that survived a dashboard restart, arm the
        slots from now, and spawn the tick thread. Call once, post-bind."""
        if self._started:
            return
        self._started = True
        self._slots = self._load_list(SLOTS_PATH, "slots")
        self._history = self._load_list(RUNS_PATH, "runs")
        now = datetime.now()
        for slot in self._slots:
            self._next_due[slot["id"]] = schedule.next_fire(slot.get("schedule") or {}, now)
        self._adopt_orphans()
        threading.Thread(target=self._loop, daemon=True, name="ob-scheduler").start()

    @staticmethod
    def _load_list(path: Path, key: str) -> list:
        """Lossy load: a corrupt file or entry loses that entry, not the set."""
        try:
            with open(path) as f:
                data = json.load(f)
            items = data.get(key) if isinstance(data, dict) else None
            return [x for x in items if isinstance(x, dict) and x.get("id")] if items else []
        except (FileNotFoundError, ValueError, OSError):
            return []

    def _adopt_orphans(self) -> None:
        """Runs recorded in active.json belong to a previous dashboard process.
        Re-adopt the ones whose pane is still in herdr; finalize the rest."""
        orphans = self._load_list(ACTIVE_PATH, "runs")
        if not orphans:
            return
        try:
            agents = herdr.call("agent.list").get("agents") or []
            live_panes = {a.get("pane_id") for a in agents}
        except herdr.HerdrError:
            live_panes = set()
        for run in orphans:
            if run.get("pane_id") in live_panes:
                self._active.append(run)
            else:
                run["ended_at"] = _now_iso()
                run["outcome"] = "exited"
                run["reason"] = "dashboard restarted; session no longer in herdr"
                self._history.insert(0, run)
        self._persist_active()
        self._persist_history()

    def _loop(self) -> None:
        while True:
            try:
                self._tick()
            except Exception as e:  # the loop must survive anything
                print(f"[scheduler] tick failed: {e}")
            _time.sleep(TICK_SECS)

    # ---- tick ---------------------------------------------------------------
    def _tick(self) -> None:
        now = datetime.now()
        self._evaluate_fires(now)
        self._pump()
        self._poll_active(now)

    def _evaluate_fires(self, now: datetime) -> None:
        with self._lock:
            for slot in self._slots:
                if not slot.get("enabled"):
                    continue
                due = self._next_due.get(slot["id"])
                if due is None or due > now:
                    continue
                self._next_due[slot["id"]] = schedule.next_fire(slot["schedule"], now)
                slot["last_fired_at"] = _now_iso()
                self._persist_slots()
                if now - due > MISSED_GRACE:
                    # Slept through it — record the miss, don't run a 2am job at 9am.
                    self._history.insert(0, self._run_record(
                        slot, "scheduled", state=None, outcome="skipped_missed_window",
                        reason=f"missed its {due.strftime('%H:%M')} window"))
                    self._persist_history()
                elif not self._slot_busy(slot["id"]):
                    self._queue.append(self._run_record(slot, "scheduled"))

    def _slot_busy(self, slot_id: str) -> bool:
        return any(r.get("slot_id") == slot_id for r in self._queue + self._active)

    @staticmethod
    def _run_record(slot: dict, trigger: str, state: "str | None" = "queued",
                    outcome: "str | None" = None, reason: "str | None" = None) -> dict:
        rec = {"id": uuid.uuid4().hex, "slot_id": slot["id"], "slot_name": slot["name"],
               "cwd": slot["cwd"], "prompt": slot["prompt"], "trigger": trigger,
               "state": state, "started_at": _now_iso(), "ended_at": None,
               "outcome": outcome, "reason": reason, "completion_message": None,
               "transcript_file": None,
               "workspace_id": slot.get("workspace_id"),
               "timeout_minutes": slot.get("timeout_minutes") or DEFAULT_TIMEOUT_MINUTES}
        if outcome:
            rec["ended_at"] = rec["started_at"]
        return rec

    def _pump(self) -> None:
        with self._lock:
            if self._launching or len(self._active) >= MAX_ACTIVE or not self._queue:
                return
            run = self._queue.pop(0)
            self._launching = True
        try:
            self._begin_run(run)
        finally:
            with self._lock:
                self._launching = False

    def _begin_run(self, run: dict) -> None:
        # A workspace-linked run re-resolves the workspace at fire time — a
        # deleted/moved workspace fails loudly instead of running in a ghost dir.
        if run.get("workspace_id"):
            ws = workspaces.workspace_by_id(run["workspace_id"])
            if ws is None:
                return self._record_failure(
                    run, "the linked task workspace no longer exists")
            run["cwd"] = ws["path"]
        cwd = run["cwd"]
        cache_root = str(store.STATE_DIR.resolve())
        plugins_root = str((Path.home() / ".claude" / "plugins").resolve())
        resolved = str(Path(cwd).expanduser().resolve()) if cwd else ""
        if not cwd or not Path(cwd).expanduser().is_dir():
            return self._record_failure(run, f"working directory does not exist: {cwd}")
        if resolved == cache_root or resolved.startswith(cache_root + "/"):
            return self._record_failure(run, "refusing to run inside the plugin cache")
        if resolved == plugins_root or resolved.startswith(plugins_root + "/"):
            # Mirrors the Mac app's PluginCacheGuard: a run here could rewrite
            # installed plugin files out from under every session.
            return self._record_failure(
                run, "refusing to run inside ~/.claude/plugins")
        run["state"] = "launching"
        run["started_at"] = _now_iso()  # queued time isn't run time
        # Normalize to the absolute path so the Stop-event cwd match holds even
        # when the slot was written with a ~.
        run["cwd"] = str(Path(cwd).expanduser())
        try:
            launched = herdr.launch(run["cwd"], run["slot_name"], run["prompt"])
        except herdr.HerdrError as e:
            return self._record_failure(run, e.message or str(e))
        # herdr's result has its own workspace_id (a pane-tree id) — don't let
        # it clobber the run's Overboard task-workspace link.
        launched = dict(launched)
        launched["herdr_workspace_id"] = launched.pop("workspace_id", None)
        run.update(launched)
        run["state"] = "running"
        with self._lock:
            self._active.append(run)
            self._persist_active()

    def _record_failure(self, run: dict, reason: str) -> None:
        run["state"] = None
        run["ended_at"] = _now_iso()
        run["outcome"] = "launch_failed"
        run["reason"] = reason
        with self._lock:
            self._history.insert(0, run)
            self._persist_history()

    # ---- polling active runs ------------------------------------------------
    def _poll_active(self, now: datetime) -> None:
        with self._lock:
            if self._polling or not self._active:
                return
            self._polling = True
            runs = list(self._active)
        try:
            for run in runs:
                self._poll_run(run, now)
        finally:
            with self._lock:
                self._polling = False

    def _poll_run(self, run: dict, now: datetime) -> None:
        started = _parse_iso(run.get("started_at")) or now
        # Timeout backstop — also covers a truly dead herdr host, since probe
        # failures report alive/unknown forever.
        if now - started > timedelta(minutes=run.get("timeout_minutes") or DEFAULT_TIMEOUT_MINUTES):
            herdr.terminate(run["pane_id"])
            return self._finalize(run, "timeout",
                                  reason=f"exceeded {run.get('timeout_minutes')} minutes")
        # An /exit was requested; give it a moment, then close the pane ourselves.
        deadline = _parse_iso(run.get("exit_deadline"))
        if deadline and now > deadline:
            herdr.terminate(run["pane_id"])
            return self._finalize(run, "completed")

        probe = herdr.probe(run["pane_id"])
        if not probe["alive"]:
            outcome = "completed" if (run.get("completion_message") is not None
                                      or run.get("exit_deadline")) else "exited"
            return self._finalize(run, outcome)
        run["herdr_state"] = probe["state"]

        if run.get("exit_deadline"):
            return  # already winding down
        stop = self._match_stop_event(run)
        if stop is not None:
            run["completion_message"] = stop.get("last_message")
            run["state"] = "exiting"
            run["exit_deadline"] = (now + timedelta(seconds=EXIT_GRACE_SECS)).isoformat(
                timespec="seconds")
            with self._lock:
                self._persist_active()
            herdr.request_exit(run["agent_name"], run["pane_id"])

    def _match_stop_event(self, run: dict) -> "dict | None":
        """Scan the tail of events.jsonl for a Stop from this run: same cwd,
        after the run started, session not claimed by another active run."""
        started = _parse_iso(run.get("started_at"))
        if started is None:
            return None
        cutoff = started.timestamp() - 5
        with self._lock:
            claimed = {r.get("stop_session") for r in self._active if r is not run}
        try:
            with open(EVENTS_PATH, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 262144))  # last 256KB is plenty of tail
                tail = f.read().decode("utf-8", "replace")
        except OSError:
            return None
        for line in reversed(tail.splitlines()):
            try:
                evt = json.loads(line)
            except ValueError:
                continue
            if (evt.get("type") == "Stop" and evt.get("cwd") == run["cwd"]
                    and (evt.get("ts") or 0) >= cutoff
                    and evt.get("session_id") not in claimed):
                run["stop_session"] = evt.get("session_id")
                return evt
        return None

    def _finalize(self, run: dict, outcome: str, reason: "str | None" = None) -> None:
        text = herdr.transcript(run["pane_id"])  # best effort, before close
        if text:
            try:
                TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
                (TRANSCRIPTS_DIR / f"{run['id']}.txt").write_text(text)
                run["transcript_file"] = f"{run['id']}.txt"
            except OSError:
                pass
        if outcome in ("completed", "timeout", "cancelled"):
            herdr.terminate(run["pane_id"])
        # A failed/exited run's pane stays open so the user can inspect it.
        run["state"] = None
        run["ended_at"] = _now_iso()
        run["outcome"] = outcome
        run["reason"] = reason
        run.pop("exit_deadline", None)
        run.pop("stop_session", None)
        with self._lock:
            self._active = [r for r in self._active if r["id"] != run["id"]]
            self._history.insert(0, run)
            self._prune_history()
            self._persist_active()
            self._persist_history()

    def _prune_history(self) -> None:
        for stale in self._history[HISTORY_CAP:]:
            if stale.get("transcript_file"):
                try:
                    (TRANSCRIPTS_DIR / stale["transcript_file"]).unlink()
                except OSError:
                    pass
        del self._history[HISTORY_CAP:]

    # ---- persistence (callers hold the lock) --------------------------------
    def _persist_slots(self) -> None:
        store._atomic_write(SLOTS_PATH, {"version": 1, "slots": self._slots})

    def _persist_history(self) -> None:
        store._atomic_write(RUNS_PATH, {"version": 1, "runs": self._history})

    def _persist_active(self) -> None:
        store._atomic_write(ACTIVE_PATH, {"version": 1, "runs": self._active})

    # ---- public API (dashboard /api methods) --------------------------------
    def view(self) -> dict:
        with self._lock:
            slots = []
            for slot in self._slots:
                out = dict(slot)
                nf = self._next_due.get(slot["id"]) if slot.get("enabled") else None
                out["next_fire"] = nf.isoformat(timespec="seconds") if nf else None
                try:
                    out["summary"] = schedule.summary(slot["schedule"])
                except Exception:
                    out["summary"] = "?"
                slots.append(out)
            queued = [dict(r) for r in self._queue]
            active = [dict(r) for r in self._active]
            history = []
            for r in self._history[:50]:
                out = {k: v for k, v in r.items() if k != "prompt"}
                out["has_transcript"] = bool(r.get("transcript_file"))
                history.append(out)
        return {"herdr": self._herdr_health(), "slots": slots,
                "queued": queued, "active": active, "history": history}

    def _herdr_health(self) -> dict:
        now = _time.monotonic()
        if now - self._herdr_status["at"] > HERDR_STATUS_TTL:
            reachable = False
            try:
                herdr.call("ping", timeout=2)
                reachable = True
            except herdr.HerdrError:
                pass
            self._herdr_status = {"at": now, "installed": herdr.find_binary() is not None,
                                  "reachable": reachable}
        return {"installed": self._herdr_status["installed"],
                "reachable": self._herdr_status["reachable"],
                "socket": str(herdr.socket_path())}

    def save_slot(self, slot: dict) -> dict:
        spec = schedule.validate(slot.get("schedule") or {})
        name = (slot.get("name") or "").strip()
        cwd = (slot.get("cwd") or "").strip()
        prompt = (slot.get("prompt") or "").strip()
        workspace_id = slot.get("workspace_id") or None
        if workspace_id:
            # A workspace-linked slot always runs in the workspace — the cwd
            # follows the workspace, whatever the form said.
            ws = workspaces.workspace_by_id(workspace_id)
            if ws is None:
                raise ValueError(f"no such task workspace: {workspace_id}")
            cwd = ws["path"]
        if not name:
            raise ValueError("the slot needs a name")
        if not cwd or not Path(cwd).expanduser().is_dir():
            raise ValueError(f"not a directory: {cwd or '(empty)'}")
        if not prompt:
            raise ValueError("the slot needs a prompt")
        try:
            timeout = max(5, min(24 * 60, int(slot.get("timeout_minutes")
                                              or DEFAULT_TIMEOUT_MINUTES)))
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT_MINUTES
        with self._lock:
            existing = next((s for s in self._slots if s["id"] == slot.get("id")), None)
            if existing:
                existing.update({"name": name, "cwd": cwd, "prompt": prompt,
                                 "schedule": spec, "timeout_minutes": timeout,
                                 "workspace_id": workspace_id,
                                 "enabled": bool(slot.get("enabled"))})
                target = existing
            else:
                target = {"id": uuid.uuid4().hex, "name": name, "cwd": cwd,
                          "prompt": prompt, "schedule": spec,
                          "timeout_minutes": timeout,
                          "workspace_id": workspace_id,
                          # New slots save disabled — enabling is an explicit step.
                          "enabled": False,
                          "last_fired_at": None, "created_at": _now_iso()}
                self._slots.append(target)
            self._next_due[target["id"]] = (
                schedule.next_fire(spec, datetime.now()) if target["enabled"] else None)
            self._persist_slots()
        if workspace_id:
            workspaces.set_slot_link(workspace_id, target["id"])
        return self.view()

    def slot_for_workspace(self, workspace_id: str) -> "dict | None":
        with self._lock:
            return next((dict(s) for s in self._slots
                         if s.get("workspace_id") == workspace_id), None)

    def delete_slot(self, slot_id: str) -> dict:
        with self._lock:
            gone = next((s for s in self._slots if s["id"] == slot_id), None)
            self._slots = [s for s in self._slots if s["id"] != slot_id]
            self._queue = [r for r in self._queue if r["slot_id"] != slot_id]
            self._next_due.pop(slot_id, None)
            self._persist_slots()
        if gone and gone.get("workspace_id"):
            workspaces.set_slot_link(gone["workspace_id"], None)
        return self.view()

    def toggle_slot(self, slot_id: str, enabled: bool) -> dict:
        with self._lock:
            slot = next((s for s in self._slots if s["id"] == slot_id), None)
            if slot:
                slot["enabled"] = bool(enabled)
                self._next_due[slot_id] = (
                    schedule.next_fire(slot["schedule"], datetime.now())
                    if slot["enabled"] else None)
                if not slot["enabled"]:
                    self._queue = [r for r in self._queue if r["slot_id"] != slot_id]
                self._persist_slots()
        return self.view()

    def run_now(self, slot_id: str) -> dict:
        with self._lock:
            slot = next((s for s in self._slots if s["id"] == slot_id), None)
            if slot and not self._slot_busy(slot_id):
                self._queue.append(self._run_record(slot, "manual"))
        return self.view()

    def stop_run(self, run_id: str) -> dict:
        with self._lock:
            queued = next((r for r in self._queue if r["id"] == run_id), None)
            if queued:
                self._queue.remove(queued)
                queued["state"] = None
                queued["ended_at"] = _now_iso()
                queued["outcome"] = "cancelled"
                self._history.insert(0, queued)
                self._persist_history()
                return self.view()
            run = next((r for r in self._active if r["id"] == run_id), None)
        if run:
            self._finalize(run, "cancelled", reason="stopped from the dashboard")
        return self.view()

    def transcript(self, run_id: str) -> dict:
        with self._lock:
            run = next((r for r in self._history if r["id"] == run_id), None)
        if not run or not run.get("transcript_file"):
            return {"text": None}
        try:
            return {"text": (TRANSCRIPTS_DIR / run["transcript_file"]).read_text()}
        except OSError:
            return {"text": None}

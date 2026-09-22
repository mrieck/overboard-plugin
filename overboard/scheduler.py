"""The plugin's cron-style scheduler: slots fire unattended claude runs inside
Herdr. A port of the Mac app's SchedulerEngine, deliberately narrower — one cwd
per slot, a FIFO queue with a small concurrency cap, no preflight/usage/power
handling — but with the same run lifecycle: launch, claim the session from the
hooks, answer the folder-trust dialog, notice "waiting for input", capture the
closing message, /exit, close the pane.

Runs entirely inside the dashboard server process: `Scheduler.start()` is
called after the HTTP bind succeeds (the bind is the single-instance lock), and
a daemon thread ticks every 5s. When the dashboard isn't running, slots simply
don't fire — missed occurrences are not back-filled; one more than 30 minutes
late is recorded as skipped, not run stale.

State lives in ~/.cache/overboard/scheduler/ (slots.json, runs.json,
active.json, queue.json, transcripts/). Single writer: this engine's thread(s)
inside the dashboard process — the MCP server and hooks never touch it.

Completion detection: the plugin's hooks append to events.jsonl (read-only
here; append-only by construction). A run claims the first unclaimed
SessionStart in its cwd near launch, then a Stop/SessionEnd for THAT session
(with no subagent still in flight) marks the work done — see runmatch.py. We
capture last_message, ask the agent to /exit, and close the pane.

Two seams for the dispatcher (overboard/dispatch.py), so this module never
imports it: `on_tick()` runs at the end of every tick, `on_run_committed(run)`
fires once per run as it lands in history (outside the lock).
"""

from __future__ import annotations

import json
import threading
import time as _time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from . import herdr, runmatch, schedule, store, workspaces

SCHED_DIR = store.SCHEDULER_DIR
SLOTS_PATH = SCHED_DIR / "slots.json"
RUNS_PATH = SCHED_DIR / "runs.json"
ACTIVE_PATH = SCHED_DIR / "active.json"
QUEUE_PATH = SCHED_DIR / "queue.json"
TRANSCRIPTS_DIR = SCHED_DIR / "transcripts"
EVENTS_PATH = store.STATE_DIR / "events.jsonl"

TICK_SECS = 5
HISTORY_CAP = 200
MISSED_GRACE = timedelta(minutes=30)
EXIT_GRACE_SECS = 10
EVENTS_TAIL_BYTES = 262144  # last 256KB of events.jsonl is plenty of window
DEFAULT_TIMEOUT_MINUTES = store.DEFAULT_RUN_TIMEOUT_MINUTES
DEFAULT_STALL_MINUTES = 10
HERDR_STATUS_TTL = 15  # seconds the cached installed/reachable answer is trusted
KNOBS_TTL = 10         # seconds the credentials-backed knobs are cached
TRUST_SCAN_LINES = 80  # pane tail read when a blocked run might be the trust dialog
AGENDA_DAYS = 10       # how far ahead view() lists each slot's upcoming fires
EPHEMERAL_PREFIX = "ephemeral:"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_iso(s) -> "datetime | None":
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _read_events_tail(min_ts: float) -> list:
    """Parse the tail of events.jsonl in log order, keeping events at/after
    `min_ts`. Only the tail is read — the file is append-only and can be large."""
    try:
        with open(EVENTS_PATH, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - EVENTS_TAIL_BYTES))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    events = []
    for line in tail.splitlines():
        try:
            evt = json.loads(line)
        except ValueError:
            continue  # a torn first line, or a partial write
        if isinstance(evt, dict) and (evt.get("ts") or 0) >= min_ts:
            events.append(evt)
    return events


def is_ephemeral(slot_id) -> bool:
    return isinstance(slot_id, str) and slot_id.startswith(EPHEMERAL_PREFIX)


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
        self._launching = 0         # _begin_run calls in flight (socket, slow)
        self._polling = False       # a poll pass is in flight
        self._herdr_status = {"at": 0.0, "installed": False, "reachable": False}
        self._knobs = {"at": 0.0, "value": None}
        # Dispatcher seams (see module docstring). Both optional.
        self.on_tick = None
        self.on_run_committed = None

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
        self._restore_queue(now)
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
        committed = []
        for run in orphans:
            if run.get("pane_id") in live_panes:
                self._active.append(run)
            else:
                run["state"] = None
                run["ended_at"] = _now_iso()
                run["outcome"] = "exited"
                run["reason"] = "dashboard restarted; session no longer in herdr"
                self._history.insert(0, run)
                committed.append(run)
        self._persist_active()
        self._persist_history()
        for run in committed:
            self._notify_committed(run)

    def _restore_queue(self, now: datetime) -> None:
        """Runs queued by a previous dashboard process but never launched. Re-queue
        the fresh ones; anything that waited longer than the missed-window grace
        is recorded as skipped (a 2am job must not launch at 9am because the
        server came back), the same policy as a slept-through fire."""
        pending = self._load_list(QUEUE_PATH, "runs")
        if not pending:
            return
        committed = []
        for run in pending:
            queued_at = _parse_iso(run.get("started_at")) or now
            if now - queued_at > MISSED_GRACE:
                run["state"] = None
                run["ended_at"] = _now_iso()
                if run.get("trigger") == "manual":
                    run["outcome"] = "cancelled"
                    run["reason"] = "dashboard restarted before it launched"
                else:
                    run["outcome"] = "skipped_missed_window"
                    run["reason"] = "queued, but the dashboard was down past its window"
                self._history.insert(0, run)
                committed.append(run)
            elif not self._slot_busy(run.get("slot_id")):
                run["state"] = "queued"
                self._queue.append(run)
        self._persist_queue()
        self._persist_history()
        for run in committed:
            self._notify_committed(run)

    def _loop(self) -> None:
        while True:
            try:
                self._tick()
            except Exception as e:  # the loop must survive anything
                print(f"[scheduler] tick failed: {e}")
            _time.sleep(TICK_SECS)

    # ---- knobs (credentials.json, via Settings) ----------------------------
    def knobs(self) -> dict:
        now = _time.monotonic()
        if self._knobs["value"] is None or now - self._knobs["at"] > KNOBS_TTL:
            self._knobs = {"at": now, "value": store.scheduler_knobs()}
        return self._knobs["value"]

    def invalidate_knobs(self) -> None:
        """Settings just saved — re-read credentials on the next use."""
        self._knobs = {"at": 0.0, "value": None}

    def _cap(self) -> int:
        return self.knobs()["concurrency"]

    # ---- tick ---------------------------------------------------------------
    def _tick(self) -> None:
        now = datetime.now()
        self._evaluate_fires(now)
        self._pump()
        self._poll_active(now)
        hook = self.on_tick
        if hook is not None:
            try:
                hook()
            except Exception as e:
                print(f"[scheduler] on_tick failed: {e}")

    def _evaluate_fires(self, now: datetime) -> None:
        with self._lock:
            for slot in self._slots:
                if not slot.get("enabled"):
                    continue
                due = self._next_due.get(slot["id"])
                if due is None or due > now:
                    continue
                slot["last_fired_at"] = _now_iso()
                if (slot.get("schedule") or {}).get("kind") == "once":
                    # A one-off has fired — it's spent. Disable it (it stays in
                    # the list for the record) instead of re-arming.
                    slot["enabled"] = False
                    self._next_due[slot["id"]] = None
                else:
                    self._next_due[slot["id"]] = schedule.next_fire(slot["schedule"], now)
                self._persist_slots()
                if now - due > MISSED_GRACE:
                    # Slept through it — record the miss, don't run a 2am job at 9am.
                    self._history.insert(0, self._run_record(
                        slot, "scheduled", state=None, outcome="skipped_missed_window",
                        reason=f"missed its {due.strftime('%H:%M')} window"))
                    self._persist_history()
                elif not self._slot_busy(slot["id"]):
                    self._queue.append(self._run_record(slot, "scheduled"))
                    self._persist_queue()

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
               # Herdr workspace override (the dispatcher's sessions); a run
               # without one lands in its project's workspace at launch.
               "herdr_group": slot.get("herdr_group"),
               "timeout_minutes": slot.get("timeout_minutes") or DEFAULT_TIMEOUT_MINUTES,
               "stall_minutes": slot.get("stall_minutes") or DEFAULT_STALL_MINUTES,
               "add_dirs": list(slot.get("add_dirs") or []),
               "hidden": False}
        if is_ephemeral(slot["id"]):
            rec["ephemeral"] = True
        if outcome:
            rec["ended_at"] = rec["started_at"]
        return rec

    def _pump(self) -> None:
        """Launch queued runs while there's capacity. Launches go one at a
        time (herdr.launch is slow and shares one workspace), but the loop
        keeps admitting until the cap is reached."""
        while True:
            with self._lock:
                if self._launching or len(self._active) >= self._cap() or not self._queue:
                    return
                run = self._queue.pop(0)
                self._persist_queue()
                self._launching += 1
            try:
                self._begin_run(run)
            finally:
                with self._lock:
                    self._launching -= 1

    def _begin_run(self, run: dict) -> None:
        # A workspace-linked run re-resolves the workspace at fire time — a
        # deleted/moved workspace fails loudly instead of running in a ghost dir.
        group = run.get("herdr_group")
        if run.get("workspace_id"):
            ws = workspaces.workspace_by_id(run["workspace_id"])
            if ws is None:
                return self._record_failure(
                    run, "the linked task workspace no longer exists")
            run["cwd"] = ws["path"]
            # The run launches in ~/OverboardWork/<project>/<task>; its Herdr
            # workspace is the project's.
            group = group or ws.get("project")
        cwd = run["cwd"]
        cache_root = runmatch.normalize_path(str(store.STATE_DIR))
        plugins_root = runmatch.normalize_path(str(Path.home() / ".claude" / "plugins"))
        resolved = runmatch.normalize_path(cwd) if cwd else ""
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
        # Absolute path for launching/display; event matching normalizes further
        # (symlinks, /tmp vs /private/tmp) on both sides — see runmatch.
        run["cwd"] = str(Path(cwd).expanduser())
        run["session_id"] = None  # claimed from the SessionStart hook once seen
        run["herdr_group"] = group or herdr.workspace_label(run["cwd"])
        try:
            launched = herdr.launch(run["cwd"], run["slot_name"], run["prompt"],
                                    add_dirs=run.get("add_dirs") or None,
                                    group=run["herdr_group"])
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
        self._notify_committed(run)

    def _notify_committed(self, run: dict) -> None:
        """Tell the dispatcher (if any) a run has landed in history. Called
        OUTSIDE the lock — the hook may call back into the scheduler."""
        hook = self.on_run_committed
        if hook is None:
            return
        try:
            hook(dict(run))
        except Exception as e:
            print(f"[scheduler] on_run_committed failed: {e}")

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
        probe = herdr.probe(run["pane_id"])
        if not probe["alive"]:
            outcome = "completed" if (run.get("state") == "exiting"
                                      or run.get("completion_message") is not None
                                      or run.get("exit_deadline")
                                      or run.get("taken_over")) else "exited"
            return self._finalize(run, outcome, close_pane=False)
        state = probe["state"]
        run["herdr_state"] = state

        # Herdr classifies a permission prompt as `blocked`, so "waiting for
        # input" is observed rather than inferred from silence.
        if state == "blocked":
            # A fresh claude in a never-trusted folder blocks on the trust
            # dialog before it can even read the prompt — answer that one
            # dialog ourselves (once per run) instead of stranding the run.
            if not run.get("trust_answered"):
                text = herdr.transcript(run["pane_id"], lines=TRUST_SCAN_LINES)
                response = runmatch.trust_prompt_response(text)
                if response is not None:
                    run["trust_answered"] = True
                    herdr.send_text(run["pane_id"], response)
                    with self._lock:
                        self._persist_active()
                    return
            if not run.get("waiting_since"):
                run["waiting_since"] = _now_iso()
                run["stalled_at"] = run.get("stalled_at") or run["waiting_since"]
                with self._lock:
                    self._persist_active()
        else:
            if state == "working":
                run["last_activity_at"] = _now_iso()
            if run.get("waiting_since"):
                run["waiting_since"] = None
                with self._lock:
                    self._persist_active()

        verdict = self._assess_run(run)
        if verdict.get("captured"):
            run["session_id"] = verdict["session_id"]
            with self._lock:
                self._persist_active()
        if verdict.get("last_activity_ts"):
            # Hook activity is the ground truth for "still working" when Herdr
            # can't classify the screen.
            run["last_hook_activity_ts"] = verdict["last_activity_ts"]
        if state == "unknown" and not run.get("stalled_at"):
            if runmatch.is_stalled(started.timestamp(), run.get("last_hook_activity_ts"),
                                   now.timestamp(), run.get("stall_minutes") or DEFAULT_STALL_MINUTES):
                run["stalled_at"] = _now_iso()
                with self._lock:
                    self._persist_active()

        if verdict.get("completed"):
            # The closing message follows the latest terminating Stop.
            run["completion_message"] = verdict.get("completion_message")
            if run.get("state") != "exiting":
                run["state"] = "exiting"
                with self._lock:
                    self._persist_active()
        elif run.get("state") == "exiting" and not run.get("exit_deadline"):
            # A subagent event arrived after the Stop we acted on and no /exit
            # is out yet: the run is mid-flight again, not finishing.
            run["state"] = "running"
            with self._lock:
                self._persist_active()
        if run.get("state") == "exiting":
            if run.get("taken_over"):
                # The CTO has the session: record the result, leave the pane.
                return self._finalize(run, "completed", close_pane=False)
            self._wind_down(run, state, now)

    def _wind_down(self, run: dict, state: str, now: datetime) -> None:
        """A completion has been seen. `/exit` goes in only when herdr shows
        the agent at rest: hook events run ahead of the session (a Stop lands,
        then the agent is re-invoked by a background task or a subagent
        reporting back), and `/exit` typed into a working session is eaten or
        queued behind the turn — after which a fixed deadline closed the pane
        on live work. Seeing it working/blocked cancels any pending force-close;
        the exit is re-asked once it settles again."""
        if state in ("working", "blocked"):
            if run.pop("exit_deadline", None) is not None:
                with self._lock:
                    self._persist_active()
            return
        deadline = _parse_iso(run.get("exit_deadline"))
        if deadline is not None:
            if now > deadline:
                # Asked it to exit and it didn't — close the pane ourselves.
                herdr.terminate(run["pane_id"])
                self._finalize(run, "completed")
            return
        run["exit_deadline"] = (now + timedelta(seconds=EXIT_GRACE_SECS)).isoformat(
            timespec="seconds")
        with self._lock:
            self._persist_active()
        herdr.request_exit(run["agent_name"], run["pane_id"])

    def _assess_run(self, run: dict) -> dict:
        """Read the tail of events.jsonl and let runmatch decide whether this run
        has captured its session and/or finished. Session ids owned by other
        active runs are off limits, so concurrent runs — or a human session in
        the same folder — can't be cross-attributed."""
        started = _parse_iso(run.get("started_at"))
        if started is None:
            return {}
        started_ts = started.timestamp()
        with self._lock:
            claimed = {r.get("session_id") for r in self._active
                       if r is not run and r.get("session_id")}
        events = _read_events_tail(started_ts + runmatch.SESSION_START_EARLY)
        return runmatch.assess(run["cwd"], started_ts, run.get("session_id"),
                               events, claimed)

    def _finalize(self, run: dict, outcome: str, reason: "str | None" = None,
                  close_pane: bool = True) -> None:
        text = herdr.transcript(run["pane_id"])  # best effort, before close
        if text:
            try:
                TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
                (TRANSCRIPTS_DIR / f"{run['id']}.txt").write_text(text)
                run["transcript_file"] = f"{run['id']}.txt"
            except OSError:
                pass
        if close_pane and outcome in ("completed", "timeout", "cancelled"):
            herdr.terminate(run["pane_id"])
        # A failed/exited run's pane stays open so the user can inspect it.
        run["state"] = None
        run["ended_at"] = _now_iso()
        run["outcome"] = outcome
        run["reason"] = reason
        run.pop("exit_deadline", None)
        with self._lock:
            self._active = [r for r in self._active if r["id"] != run["id"]]
            self._history.insert(0, run)
            self._prune_history()
            self._persist_active()
            self._persist_history()
        self._notify_committed(run)

    def _prune_history(self) -> None:
        for stale in self._history[HISTORY_CAP:]:
            self._unlink_transcript(stale)
        del self._history[HISTORY_CAP:]

    @staticmethod
    def _unlink_transcript(run: dict) -> None:
        if run.get("transcript_file"):
            try:
                (TRANSCRIPTS_DIR / run["transcript_file"]).unlink()
            except OSError:
                pass

    # ---- persistence (callers hold the lock) --------------------------------
    def _persist_slots(self) -> None:
        store._atomic_write(SLOTS_PATH, {"version": 1, "slots": self._slots})

    def _persist_history(self) -> None:
        store._atomic_write(RUNS_PATH, {"version": 1, "runs": self._history})

    def _persist_active(self) -> None:
        store._atomic_write(ACTIVE_PATH, {"version": 1, "runs": self._active})

    def _persist_queue(self) -> None:
        store._atomic_write(QUEUE_PATH, {"version": 1, "runs": self._queue})

    # ---- public API (dashboard /api methods) --------------------------------
    def view(self) -> dict:
        now = datetime.now()
        horizon = now + timedelta(days=AGENDA_DAYS)
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
                upcoming = []
                if slot.get("enabled"):
                    try:
                        upcoming = [d.isoformat(timespec="seconds") for d in
                                    schedule.fire_dates(slot["schedule"], now, horizon)]
                    except Exception:
                        upcoming = []
                out["upcoming"] = upcoming
                slots.append(out)
            queued = [dict(r) for r in self._queue]
            active = [dict(r) for r in self._active]
            history = []
            for r in self._history[:50]:
                out = {k: v for k, v in r.items() if k != "prompt"}
                out["has_transcript"] = bool(r.get("transcript_file"))
                history.append(out)
            knobs = self.knobs()
        return {"herdr": self._herdr_health(), "slots": slots,
                "queued": queued, "active": active, "history": history,
                "concurrency": knobs["concurrency"],
                "default_timeout_minutes": knobs["default_timeout_minutes"],
                "supports_once": True, "agenda_days": AGENDA_DAYS}

    def counts(self) -> dict:
        """Cheap numbers for the panel strip's badge/tooltip — no herdr probe."""
        with self._lock:
            fires = [d for s in self._slots if s.get("enabled")
                     for d in [self._next_due.get(s["id"])] if d is not None]
            nf = min(fires) if fires else None
            return {"running": len(self._active), "queued": len(self._queue),
                    "next_fire": nf.isoformat(timespec="seconds") if nf else None}

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

    def herdr_health(self) -> dict:
        return self._herdr_health()

    @staticmethod
    def _clean_minutes(value, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(value or default)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _clean_add_dirs(value) -> list:
        out = []
        for d in value or []:
            if isinstance(d, str) and d.strip() and Path(d).expanduser().is_dir():
                out.append(str(Path(d).expanduser()))
        return out

    @staticmethod
    def _once_passed(spec: dict) -> bool:
        return (spec.get("kind") == "once"
                and schedule.next_fire(spec, datetime.now()) is None)

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
        enabled = bool(slot.get("enabled"))
        if enabled and self._once_passed(spec):
            raise ValueError("that moment has passed — pick a later time for the one-off run")
        knobs = self.knobs()
        timeout = self._clean_minutes(slot.get("timeout_minutes"),
                                      knobs["default_timeout_minutes"], 5, 24 * 60)
        stall = self._clean_minutes(slot.get("stall_minutes"), DEFAULT_STALL_MINUTES, 1, 120)
        add_dirs = self._clean_add_dirs(slot.get("add_dirs"))
        with self._lock:
            existing = next((s for s in self._slots if s["id"] == slot.get("id")), None)
            fields = {"name": name, "cwd": cwd, "prompt": prompt, "schedule": spec,
                      "timeout_minutes": timeout, "stall_minutes": stall,
                      "workspace_id": workspace_id, "add_dirs": add_dirs,
                      "enabled": enabled}
            if existing:
                existing.update(fields)
                target = existing
            else:
                target = dict(fields)
                # Enabling is an explicit step in the UI: the editor's Enabled
                # switch / the workspace form's checkbox. Honor what was sent.
                target.update({"id": uuid.uuid4().hex, "last_fired_at": None,
                               "created_at": _now_iso()})
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

    def slot_by_id(self, slot_id: str) -> "dict | None":
        with self._lock:
            return next((dict(s) for s in self._slots if s["id"] == slot_id), None)

    def delete_slot(self, slot_id: str) -> dict:
        with self._lock:
            gone = next((s for s in self._slots if s["id"] == slot_id), None)
            self._slots = [s for s in self._slots if s["id"] != slot_id]
            self._queue = [r for r in self._queue if r["slot_id"] != slot_id]
            self._next_due.pop(slot_id, None)
            self._persist_slots()
            self._persist_queue()
        if gone and gone.get("workspace_id"):
            workspaces.set_slot_link(gone["workspace_id"], None)
        return self.view()

    def toggle_slot(self, slot_id: str, enabled: bool) -> dict:
        with self._lock:
            slot = next((s for s in self._slots if s["id"] == slot_id), None)
            if slot:
                if enabled and self._once_passed(slot.get("schedule") or {}):
                    raise ValueError("that moment has passed — edit the slot and pick a later time")
                slot["enabled"] = bool(enabled)
                self._next_due[slot_id] = (
                    schedule.next_fire(slot["schedule"], datetime.now())
                    if slot["enabled"] else None)
                if not slot["enabled"]:
                    self._queue = [r for r in self._queue if r["slot_id"] != slot_id]
                    self._persist_queue()
                self._persist_slots()
        return self.view()

    def run_now(self, slot_id: str) -> dict:
        with self._lock:
            slot = next((s for s in self._slots if s["id"] == slot_id), None)
            if slot and not self._slot_busy(slot_id):
                self._queue.append(self._run_record(slot, "manual"))
                self._persist_queue()
        return self.view()

    def run_ephemeral(self, slot: dict) -> dict:
        """Queue a one-shot run of a slot that is never saved (the dispatcher's
        sessions). Validated like save_slot; returns the queued run record."""
        name = (slot.get("name") or "").strip()
        cwd = (slot.get("cwd") or "").strip()
        prompt = (slot.get("prompt") or "").strip()
        if not name or not prompt:
            raise ValueError("an ephemeral run needs a name and a prompt")
        if not cwd or not Path(cwd).expanduser().is_dir():
            raise ValueError(f"not a directory: {cwd or '(empty)'}")
        knobs = self.knobs()
        pseudo = {"id": EPHEMERAL_PREFIX + uuid.uuid4().hex, "name": name,
                  "cwd": str(Path(cwd).expanduser()), "prompt": prompt,
                  "timeout_minutes": self._clean_minutes(
                      slot.get("timeout_minutes"), knobs["default_timeout_minutes"], 5, 24 * 60),
                  "stall_minutes": self._clean_minutes(
                      slot.get("stall_minutes"), DEFAULT_STALL_MINUTES, 1, 120),
                  "add_dirs": self._clean_add_dirs(slot.get("add_dirs")),
                  "workspace_id": None,
                  "herdr_group": (slot.get("herdr_group") or "").strip() or None}
        run = self._run_record(pseudo, "manual")
        with self._lock:
            self._queue.append(run)
            self._persist_queue()
        return dict(run)

    def find_run(self, run_id: str) -> "dict | None":
        """A copy of a run record wherever it lives (queue, active, history)."""
        with self._lock:
            for bucket in (self._active, self._queue, self._history):
                hit = next((r for r in bucket if r["id"] == run_id), None)
                if hit:
                    return dict(hit)
        return None

    def stop_run(self, run_id: str) -> dict:
        with self._lock:
            queued = next((r for r in self._queue if r["id"] == run_id), None)
            if queued:
                self._queue.remove(queued)
                self._persist_queue()
                queued["state"] = None
                queued["ended_at"] = _now_iso()
                queued["outcome"] = "cancelled"
                self._history.insert(0, queued)
                self._persist_history()
            run = None if queued else next((r for r in self._active if r["id"] == run_id), None)
        # view() takes the lock itself — never call it while holding it.
        if queued:
            self._notify_committed(queued)
        if run:
            self._finalize(run, "cancelled", reason="stopped from the dashboard")
        return self.view()

    def take_over(self, run_id: str) -> dict:
        """Hand a live run to the CTO: focus its pane in Herdr and stop
        managing it — no auto-/exit, no pane close. The result is still
        recorded when the session's Stop lands or the pane goes away."""
        pane = None
        with self._lock:
            run = next((r for r in self._active if r["id"] == run_id), None)
            if run:
                run["taken_over"] = True
                run.pop("exit_deadline", None)
                self._persist_active()
                pane = run.get("pane_id")
        if pane:
            herdr.focus(pane)
        return self.view()

    def set_hidden(self, run_id: str, hidden: bool) -> dict:
        with self._lock:
            run = next((r for r in self._history if r["id"] == run_id), None)
            if run:
                run["hidden"] = bool(hidden)
                self._persist_history()
        return self.view()

    def remove_run(self, run_id: str) -> dict:
        with self._lock:
            run = next((r for r in self._history if r["id"] == run_id), None)
            if run:
                self._history.remove(run)
                self._unlink_transcript(run)
                self._persist_history()
        return self.view()

    def clear_history(self) -> dict:
        with self._lock:
            for run in self._history:
                self._unlink_transcript(run)
            self._history = []
            self._persist_history()
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

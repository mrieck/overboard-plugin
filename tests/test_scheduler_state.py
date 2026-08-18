"""Scheduler persistence/state tests that don't need Herdr: queue survives a
restart (fresh runs re-queued, stale ones skipped) and save_slot honors the
form's `enabled` for new slots. Run: python3 -m unittest discover -s tests"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from overboard import scheduler as sched  # noqa: E402


class _TempState(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._saved = {k: getattr(sched, k) for k in
                       ("SLOTS_PATH", "RUNS_PATH", "ACTIVE_PATH", "QUEUE_PATH",
                        "TRANSCRIPTS_DIR", "EVENTS_PATH")}
        sched.SLOTS_PATH = root / "slots.json"
        sched.RUNS_PATH = root / "runs.json"
        sched.ACTIVE_PATH = root / "active.json"
        sched.QUEUE_PATH = root / "queue.json"
        sched.TRANSCRIPTS_DIR = root / "transcripts"
        sched.EVENTS_PATH = root / "events.jsonl"
        self.cwd = root / "proj"
        self.cwd.mkdir()

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(sched, k, v)
        self._tmp.cleanup()

    def slot(self, **over):
        s = {"name": "nightly", "cwd": str(self.cwd), "prompt": "do it",
             "schedule": {"kind": "daily", "times": [{"hour": 2, "minute": 0}]}}
        s.update(over)
        return s


class SaveSlotEnabledTests(_TempState):
    def test_new_slot_defaults_disabled(self):
        s = sched.Scheduler()
        view = s.save_slot(self.slot())
        self.assertFalse(view["slots"][0]["enabled"])
        self.assertIsNone(view["slots"][0]["next_fire"])

    def test_new_slot_honors_enabled_true(self):
        s = sched.Scheduler()
        view = s.save_slot(self.slot(enabled=True))
        self.assertTrue(view["slots"][0]["enabled"])
        self.assertIsNotNone(view["slots"][0]["next_fire"])


class QueuePersistenceTests(_TempState):
    def _write_queue(self, runs):
        sched.QUEUE_PATH.write_text(json.dumps({"version": 1, "runs": runs}))

    def _rec(self, name, trigger, age_minutes):
        ts = (datetime.now() - timedelta(minutes=age_minutes)).isoformat(timespec="seconds")
        return {"id": name, "slot_id": "slot-" + name, "slot_name": name, "cwd": str(self.cwd),
                "prompt": "p", "trigger": trigger, "state": "queued", "started_at": ts,
                "ended_at": None, "outcome": None, "reason": None,
                "completion_message": None, "transcript_file": None,
                "workspace_id": None, "timeout_minutes": 90}

    def test_run_now_persists_queue_and_stop_run_clears_it(self):
        s = sched.Scheduler()
        view = s.save_slot(self.slot())
        sid = view["slots"][0]["id"]
        view = s.run_now(sid)
        self.assertEqual(len(view["queued"]), 1)
        on_disk = json.loads(sched.QUEUE_PATH.read_text())["runs"]
        self.assertEqual([r["slot_id"] for r in on_disk], [sid])
        s.stop_run(view["queued"][0]["id"])
        self.assertEqual(json.loads(sched.QUEUE_PATH.read_text())["runs"], [])

    def test_restore_requeues_fresh_and_skips_stale(self):
        self._write_queue([self._rec("fresh", "scheduled", 2),
                           self._rec("stale", "scheduled", 120),
                           self._rec("oldmanual", "manual", 120)])
        s = sched.Scheduler()
        s._restore_queue(datetime.now())
        self.assertEqual([r["id"] for r in s._queue], ["fresh"])
        outcomes = {r["id"]: (r["outcome"], r["reason"]) for r in s._history}
        self.assertEqual(outcomes["stale"][0], "skipped_missed_window")
        self.assertEqual(outcomes["oldmanual"][0], "cancelled")
        self.assertIn("dashboard", outcomes["stale"][1])
        # And the persisted queue now only holds the survivor.
        self.assertEqual([r["id"] for r in json.loads(sched.QUEUE_PATH.read_text())["runs"]],
                         ["fresh"])



class _FakeHerdr:
    """Scriptable stand-in for overboard.herdr: `states` are consumed per probe
    (the last one repeats); calls are recorded."""
    def __init__(self, states):
        self.states = list(states)
        self.calls = []

    def probe(self, pane_id):
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return {"alive": True, "state": state} if state != "gone" else {"alive": False, "state": None}

    def request_exit(self, agent_name, pane_id):
        self.calls.append("request_exit")

    def terminate(self, pane_id):
        self.calls.append("terminate")

    def transcript(self, pane_id, lines=2000):
        return None


class WindDownTests(_TempState):
    """A completion seen in the hooks only /exits the session once herdr shows
    it at rest, and a session that goes back to work is not force-closed."""

    def setUp(self):
        super().setUp()
        self._real_herdr = sched.herdr
        self.now = datetime.now()
        started = (self.now - timedelta(minutes=5)).isoformat(timespec="seconds")
        self.run = {"id": "r1", "slot_id": "s1", "slot_name": "nightly", "cwd": str(self.cwd),
                    "prompt": "p", "trigger": "manual", "state": "running", "started_at": started,
                    "ended_at": None, "outcome": None, "reason": None, "session_id": "S1",
                    "completion_message": None, "transcript_file": None,
                    "workspace_id": None, "timeout_minutes": 90,
                    "pane_id": "w1:p1", "agent_name": "nightly-ab12"}
        self.s = sched.Scheduler()
        self.s._active = [self.run]
        self._events = []

    def tearDown(self):
        sched.herdr = self._real_herdr
        super().tearDown()

    def event(self, typ, offset_s, **extra):
        e = {"type": typ, "ts": self.now.timestamp() + offset_s, "session_id": "S1",
             "cwd": str(self.cwd)}
        e.update(extra)
        self._events.append(e)
        sched.EVENTS_PATH.write_text("".join(json.dumps(x) + "\n" for x in self._events))

    def poll(self, offset_s=0):
        self.s._poll_run(self.run, self.now + timedelta(seconds=offset_s))

    def test_exit_waits_until_the_agent_is_at_rest(self):
        fake = sched.herdr = _FakeHerdr(["working"])
        self.event("Stop", -10, last_message="handed off")
        self.poll(0)
        self.poll(60)
        self.assertEqual(self.run["state"], "exiting")
        self.assertIsNone(self.run.get("exit_deadline"))
        self.assertEqual(fake.calls, [])
        # Follow-up turn ends: now the exit goes in, and the grace clock starts.
        fake.states = ["idle"]
        self.poll(120)
        self.assertEqual(fake.calls, ["request_exit"])
        self.assertIsNotNone(self.run.get("exit_deadline"))

    def test_resuming_after_exit_cancels_the_force_close(self):
        fake = sched.herdr = _FakeHerdr(["idle"])
        self.event("Stop", -10, last_message="first turn")
        self.poll(0)
        self.assertEqual(fake.calls, ["request_exit"])
        # Working again past the old deadline: hold, don't close.
        fake.states = ["working"]
        self.poll(sched.EXIT_GRACE_SECS + 5)
        self.assertIsNone(self.run.get("exit_deadline"))
        self.assertEqual(fake.calls, ["request_exit"])
        self.assertEqual(self.s._active, [self.run])
        # Settled: exit re-asked; only then does the deadline count.
        fake.states = ["idle"]
        self.poll(120)
        self.assertEqual(fake.calls, ["request_exit", "request_exit"])
        self.poll(120 + sched.EXIT_GRACE_SECS + 1)
        self.assertIn("terminate", fake.calls)
        self.assertEqual(self.s._active, [])
        self.assertEqual(self.s._history[0]["outcome"], "completed")

    def test_stale_stop_then_subagent_event_reverts_completion(self):
        fake = sched.herdr = _FakeHerdr(["working"])
        self.event("Stop", -10, last_message="turn boundary")
        self.poll(0)
        self.assertEqual(self.run["state"], "exiting")
        # A background agent's PostToolUse logs late; the run is mid-flight.
        self.event("PostToolUse", -9, tool_name="Agent")
        fake.states = ["idle"]   # parent idles while the subagent works
        self.poll(5)
        self.assertEqual(self.run["state"], "running")
        self.assertEqual(fake.calls, [])
        # Subagent done, parent's real Stop lands — now it winds down.
        self.event("SubagentStop", 30)
        self.event("Stop", 40, last_message="really done")
        self.poll(45)
        self.assertEqual(fake.calls, ["request_exit"])
        self.assertEqual(self.run["completion_message"], "really done")


if __name__ == "__main__":
    unittest.main()

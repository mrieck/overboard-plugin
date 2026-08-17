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


if __name__ == "__main__":
    unittest.main()

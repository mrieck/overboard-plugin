"""Scheduler engine features beyond the basic lifecycle: concurrency cap, once
slots that spend themselves, the trust-dialog auto-answer, waiting-for-input /
stall flags, take-over, history hide/remove/clear, ephemeral runs and the
dispatcher seams. No Herdr: overboard.herdr is swapped for a scriptable fake.
Run: python3 -m unittest discover -s tests"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from overboard import scheduler as sched  # noqa: E402
from overboard import store  # noqa: E402


class _FakeHerdr:
    def __init__(self, states=("working",), pane_text=None):
        self.states = list(states)
        self.pane_text = pane_text
        self.calls = []
        self.launches = 0

    def probe(self, pane_id):
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return ({"alive": False, "state": "ended"} if state == "gone"
                else {"alive": True, "state": state})

    def launch(self, cwd, name, prompt, ready_timeout=45.0, add_dirs=None):
        self.launches += 1
        self.calls.append(("launch", name, tuple(add_dirs or [])))
        return {"pane_id": f"p{self.launches}", "tab_id": f"t{self.launches}",
                "workspace_id": "w", "agent_name": f"{name}-{self.launches}"}

    def request_exit(self, agent_name, pane_id): self.calls.append("request_exit")
    def terminate(self, pane_id): self.calls.append("terminate")
    def transcript(self, pane_id, lines=2000): return self.pane_text
    def send_text(self, pane_id, text): self.calls.append(("send_text", text)); return True
    def focus(self, pane_id): self.calls.append(("focus", pane_id)); return True
    def find_binary(self): return None
    def socket_path(self): return Path("/tmp/none.sock")
    def call(self, *a, **k): raise sched.herdr.HerdrError("x", "no herdr")  # pragma: no cover


class _Base(unittest.TestCase):
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
        self._cred = store.CREDENTIALS_PATH
        store.CREDENTIALS_PATH = root / "credentials.json"
        self.cwd = root / "proj"
        self.cwd.mkdir()
        self._real_herdr = sched.herdr
        self.fake = _FakeHerdr()
        # Keep the HerdrError class reachable through the fake.
        self.fake.HerdrError = self._real_herdr.HerdrError
        sched.herdr = self.fake
        self.s = sched.Scheduler()
        self.now = datetime.now()

    def tearDown(self):
        sched.herdr = self._real_herdr
        store.CREDENTIALS_PATH = self._cred
        for k, v in self._saved.items():
            setattr(sched, k, v)
        self._tmp.cleanup()

    def slot(self, **over):
        s = {"name": "nightly", "cwd": str(self.cwd), "prompt": "do it",
             "schedule": {"kind": "daily", "times": [{"hour": 2, "minute": 0}]}}
        s.update(over)
        return s

    def knobs(self, **kv):
        store.save_credentials(kv)
        self.s._knobs = {"at": 0.0, "value": None}

    def active_run(self, **over):
        started = (self.now - timedelta(minutes=5)).isoformat(timespec="seconds")
        run = {"id": "r1", "slot_id": "s1", "slot_name": "nightly", "cwd": str(self.cwd),
               "prompt": "p", "trigger": "manual", "state": "running", "started_at": started,
               "ended_at": None, "outcome": None, "reason": None, "session_id": "S1",
               "completion_message": None, "transcript_file": None, "workspace_id": None,
               "timeout_minutes": 90, "stall_minutes": 10, "add_dirs": [], "hidden": False,
               "pane_id": "w1:p1", "agent_name": "nightly-ab12"}
        run.update(over)
        self.s._active = [run]
        return run

    def event(self, typ, offset_s, **extra):
        e = {"type": typ, "ts": self.now.timestamp() + offset_s, "session_id": "S1",
             "cwd": str(self.cwd)}
        e.update(extra)
        with open(sched.EVENTS_PATH, "a") as f:
            f.write(json.dumps(e) + "\n")


class ConcurrencyTests(_Base):
    def test_cap_comes_from_credentials_and_is_clamped(self):
        self.assertEqual(self.s._cap(), 1)
        self.knobs(scheduler_concurrency=3)
        self.assertEqual(self.s._cap(), 3)
        self.knobs(scheduler_concurrency=99)
        self.assertEqual(self.s._cap(), store.SCHEDULER_CONCURRENCY_MAX)
        self.knobs(scheduler_concurrency="junk")
        self.assertEqual(self.s._cap(), 1)

    def test_pump_admits_up_to_the_cap(self):
        self.knobs(scheduler_concurrency=2)
        for n in ("a", "b", "c"):
            v = self.s.save_slot(self.slot(name=n))
            self.s.run_now(next(x["id"] for x in v["slots"] if x["name"] == n))
        self.assertEqual(len(self.s._queue), 3)
        self.s._pump()
        self.assertEqual(len(self.s._active), 2)
        self.assertEqual(len(self.s._queue), 1)
        self.assertEqual(self.fake.launches, 2)
        self.assertEqual(len(json.loads(sched.ACTIVE_PATH.read_text())["runs"]), 2)


class OnceSlotTests(_Base):
    def test_once_slot_spends_itself_when_it_fires(self):
        fire = (self.now + timedelta(minutes=1)).isoformat()
        v = self.s.save_slot(self.slot(schedule={"kind": "once", "date": fire}, enabled=True))
        sid = v["slots"][0]["id"]
        self.s._evaluate_fires(self.now + timedelta(minutes=2))
        slot = next(x for x in self.s._slots if x["id"] == sid)
        self.assertFalse(slot["enabled"])
        self.assertIsNone(self.s._next_due[sid])
        self.assertEqual([r["slot_id"] for r in self.s._queue], [sid])
        on_disk = json.loads(sched.SLOTS_PATH.read_text())["slots"][0]
        self.assertFalse(on_disk["enabled"])

    def test_enabling_a_spent_once_slot_is_refused(self):
        past = (self.now - timedelta(hours=1)).isoformat()
        with self.assertRaises(ValueError):
            self.s.save_slot(self.slot(schedule={"kind": "once", "date": past}, enabled=True))
        v = self.s.save_slot(self.slot(schedule={"kind": "once", "date": past}, enabled=False))
        with self.assertRaises(ValueError):
            self.s.toggle_slot(v["slots"][0]["id"], True)

    def test_view_lists_upcoming_fires(self):
        v = self.s.save_slot(self.slot(enabled=True))
        self.assertEqual(len(v["slots"][0]["upcoming"]), sched.AGENDA_DAYS)
        self.assertTrue(v["supports_once"])
        v = self.s.toggle_slot(v["slots"][0]["id"], False)
        self.assertEqual(v["slots"][0]["upcoming"], [])


class TrustAndWaitingTests(_Base):
    def test_trust_dialog_is_answered_once(self):
        run = self.active_run()
        self.fake.states = ["blocked"]
        self.fake.pane_text = "Do you trust the files in this folder?\n ❯ 1. Yes, proceed"
        self.s._poll_run(run, self.now)
        self.assertIn(("send_text", "\r"), self.fake.calls)
        self.assertTrue(run["trust_answered"])
        self.assertIsNone(run.get("waiting_since"))
        # Still blocked next poll (some other dialog): flagged, not re-answered.
        self.fake.pane_text = "Allow Bash(rm)? 1. Yes 2. No"
        self.s._poll_run(run, self.now + timedelta(seconds=5))
        self.assertEqual(self.fake.calls.count(("send_text", "\r")), 1)
        self.assertIsNotNone(run["waiting_since"])
        self.assertIsNotNone(run["stalled_at"])

    def test_waiting_clears_when_it_works_again(self):
        run = self.active_run(waiting_since="2026-01-01T00:00:00")
        self.fake.states = ["working"]
        self.s._poll_run(run, self.now)
        self.assertIsNone(run["waiting_since"])
        self.assertIsNotNone(run["last_activity_at"])

    def test_unknown_state_uses_the_silence_heuristic(self):
        run = self.active_run(stall_minutes=3)
        run["started_at"] = (self.now - timedelta(minutes=2)).isoformat(timespec="seconds")
        self.fake.states = ["unknown"]
        self.s._poll_run(run, self.now)
        self.assertIsNone(run.get("stalled_at"))
        self.s._poll_run(run, self.now + timedelta(minutes=2))
        self.assertIsNotNone(run.get("stalled_at"))


class TakeOverTests(_Base):
    def test_take_over_focuses_and_stops_managing(self):
        run = self.active_run(exit_deadline="2026-01-01T00:00:00")
        self.s.take_over("r1")
        self.assertTrue(run["taken_over"])
        self.assertNotIn("exit_deadline", run)
        self.assertIn(("focus", "w1:p1"), self.fake.calls)
        # The Stop lands: the result is recorded, the pane is left alone.
        self.event("Stop", -10, last_message="all yours")
        self.fake.states = ["idle"]
        self.s._poll_run(run, self.now)
        self.assertEqual(self.s._active, [])
        self.assertEqual(self.s._history[0]["outcome"], "completed")
        self.assertEqual(self.s._history[0]["completion_message"], "all yours")
        self.assertNotIn("terminate", self.fake.calls)
        self.assertNotIn("request_exit", self.fake.calls)

    def test_pane_gone_after_take_over_counts_as_completed(self):
        run = self.active_run(taken_over=True)
        self.fake.states = ["gone"]
        self.s._poll_run(run, self.now)
        self.assertEqual(self.s._history[0]["outcome"], "completed")


class HistoryOpsTests(_Base):
    def _history(self, n):
        sched.TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
        for i in range(n):
            (sched.TRANSCRIPTS_DIR / f"h{i}.txt").write_text("x")
            self.s._history.append({"id": f"h{i}", "slot_id": "s", "slot_name": "n",
                                    "cwd": str(self.cwd), "prompt": "p", "outcome": "completed",
                                    "state": None, "started_at": "2026-01-01T00:00:00",
                                    "ended_at": "2026-01-01T00:01:00", "hidden": False,
                                    "transcript_file": f"h{i}.txt"})

    def test_hide_remove_clear(self):
        self._history(3)
        v = self.s.set_hidden("h1", True)
        self.assertTrue(next(r for r in v["history"] if r["id"] == "h1")["hidden"])
        v = self.s.remove_run("h0")
        self.assertEqual([r["id"] for r in v["history"]], ["h1", "h2"])
        self.assertFalse((sched.TRANSCRIPTS_DIR / "h0.txt").exists())
        v = self.s.clear_history()
        self.assertEqual(v["history"], [])
        self.assertEqual(list(sched.TRANSCRIPTS_DIR.iterdir()), [])
        self.assertEqual(json.loads(sched.RUNS_PATH.read_text())["runs"], [])


class EphemeralAndHooksTests(_Base):
    def test_ephemeral_run_never_touches_slots(self):
        run = self.s.run_ephemeral({"name": "Overboard dispatch ab12", "cwd": str(self.cwd),
                                    "prompt": "/overboard:dispatch /x", "timeout_minutes": 15,
                                    "add_dirs": [str(self.cwd), "/definitely/not/here"]})
        self.assertTrue(run["ephemeral"])
        self.assertTrue(sched.is_ephemeral(run["slot_id"]))
        self.assertEqual(run["add_dirs"], [str(self.cwd)])
        self.assertEqual(run["timeout_minutes"], 15)
        self.assertEqual(self.s._slots, [])
        self.assertFalse(sched.SLOTS_PATH.exists())
        self.assertEqual(self.s.find_run(run["id"])["id"], run["id"])
        with self.assertRaises(ValueError):
            self.s.run_ephemeral({"name": "x", "cwd": "/nope", "prompt": "y"})

    def test_launch_passes_add_dirs_and_commit_hook_fires(self):
        seen = []
        self.s.on_run_committed = lambda r: seen.append((r["id"], r["outcome"]))
        run = self.s.run_ephemeral({"name": "task", "cwd": str(self.cwd), "prompt": "go",
                                    "add_dirs": [str(self.cwd)]})
        self.s._pump()
        self.assertEqual(self.fake.calls[-1], ("launch", "task", (str(self.cwd),)))
        self.event("Stop", 1, last_message="done", cwd=str(self.cwd))
        active = self.s._active[0]
        active["session_id"] = "S1"
        self.fake.states = ["idle"]
        self.s._poll_run(active, self.now + timedelta(seconds=5))          # asks /exit
        self.s._poll_run(active, self.now + timedelta(seconds=5 + sched.EXIT_GRACE_SECS + 1))
        self.assertEqual(seen, [(run["id"], "completed")])

    def test_failure_and_cancel_also_fire_the_hook(self):
        seen = []
        self.s.on_run_committed = lambda r: seen.append(r["outcome"])
        bad = self.s.run_ephemeral({"name": "t", "cwd": str(self.cwd), "prompt": "go"})
        bad_rec = self.s._queue[0]
        bad_rec["cwd"] = "/gone/away"
        self.s._pump()
        self.assertEqual(seen, ["launch_failed"])
        q = self.s.run_ephemeral({"name": "t2", "cwd": str(self.cwd), "prompt": "go"})
        self.s.stop_run(q["id"])
        self.assertEqual(seen, ["launch_failed", "cancelled"])

    def test_on_tick_runs_every_tick_and_survives_errors(self):
        ticks = []
        def boom(): ticks.append(1); raise RuntimeError("x")
        self.s.on_tick = boom
        self.s._tick(); self.s._tick()
        self.assertEqual(len(ticks), 2)


if __name__ == "__main__":
    unittest.main()

"""The web Dispatcher (overboard/dispatch.py) over a fake scheduler and the
response fixtures under tests/fixtures/dispatch/. Run: python3 -m unittest discover -s tests"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from overboard import dispatch, store  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "dispatch"


class FakeScheduler:
    """Records what the dispatcher asks of it; runs live in `runs` by id."""
    def __init__(self):
        self.ephemeral = []
        self.saved = []
        self.runs = {}
        self.slots = {}
        self._n = 0

    def run_ephemeral(self, slot):
        if not Path(slot["cwd"]).is_dir():
            raise ValueError("not a directory: " + slot["cwd"])
        self._n += 1
        run = {"id": f"run{self._n}", "slot_id": "ephemeral:" + str(self._n), "slot_name": slot["name"],
               "cwd": slot["cwd"], "prompt": slot["prompt"], "state": "queued",
               "started_at": datetime.now().isoformat(timespec="seconds"),
               "timeout_minutes": slot.get("timeout_minutes"), "add_dirs": slot.get("add_dirs") or []}
        self.ephemeral.append(dict(slot))
        self.runs[run["id"]] = run
        return dict(run)

    def save_slot(self, slot):
        self._n += 1
        sid = f"slot{self._n}"
        self.saved.append(dict(slot))
        self.slots[sid] = dict(slot, id=sid)
        return {"slots": [dict(s) for s in self.slots.values()]}

    def find_run(self, run_id):
        r = self.runs.get(run_id)
        return dict(r) if r else None

    def slot_by_id(self, sid):
        s = self.slots.get(sid)
        return dict(s) if s else None


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.run_dir = self.root / "Sites"; self.run_dir.mkdir()
        self.proj = self.run_dir / "proj"; self.proj.mkdir()
        self.sched = FakeScheduler()
        self.installed = [{"id": "overboard@mkt", "name": "overboard", "description": None,
                           "commands": ["/overboard:overboard"], "enabled_user": True},
                          {"id": "seo-blog@mkt", "name": "seo-blog", "description": None,
                           "commands": [], "enabled_user": False}]
        self.knobs = {"concurrency": 1, "default_timeout_minutes": 90, "run_directory": str(self.run_dir)}
        self.d = dispatch.Dispatcher(self.sched, root=self.root / "dispatch",
                                     plugins_getter=lambda: self.installed,
                                     knobs_getter=lambda: self.knobs)

    def tearDown(self):
        self._tmp.cleanup()

    def respond(self, fixture, dispatch_id, **subs):
        text = (FIX / fixture).read_text().replace("DID", dispatch_id)
        text = text.replace("PROJ", str(self.proj)).replace("ROOT", str(self.run_dir))
        for k, v in subs.items():
            text = text.replace(k, v)
        self.d.outbox.mkdir(parents=True, exist_ok=True)
        (self.d.outbox / f"{dispatch_id}.response.json").write_text(text)
        self.d.tick()


class PureHelperTests(unittest.TestCase):
    def test_result_contract_text(self):
        out = dispatch.with_result_contract("  Do X.\n", "/t/ab12")
        self.assertTrue(out.startswith("Do X. When you are completely done, write a JSON file at /t/ab12/result.json shaped like "))
        self.assertIn('"summary": "<2-4 sentences for the CTO: what you did and where it is>"', out)
        self.assertTrue(out.endswith("(absolute paths), then stop."))
        self.assertNotIn("\n", out)
        self.assertEqual(dispatch.with_result_contract("p", None), "p")

    def test_rebuilt_prompt(self):
        rec = {"prompt": "Make a meme", "result": {"summary": "Made it\nat /x.png", "artifacts": [{"path": "/x.png"}]}}
        out = dispatch.rebuilt_prompt(rec, "add a caption")
        self.assertIn("Earlier, in this project, you were asked: Make a meme", out)
        self.assertIn("That finished with: Made it at /x.png", out)
        self.assertIn("Files from that work: /x.png", out)
        self.assertTrue(out.endswith("Now the CTO wants this change: add a caption. Pick up from the existing files rather than starting over."))

    def test_fire_date(self):
        now = datetime(2026, 8, 19, 12, 0)
        self.assertIsNone(dispatch.fire_date("now", now))
        self.assertIsNone(dispatch.fire_date(None, now))
        self.assertIsNone(dispatch.fire_date("tonight", now))
        self.assertIsNone(dispatch.fire_date("2026-08-19T11:00:00", now))
        self.assertEqual(dispatch.fire_date("2026-08-19T23:00:00", now), datetime(2026, 8, 19, 23, 0))
        self.assertIsNotNone(dispatch.fire_date("2026-08-20T01:00:00.000Z", now))

    def test_known_plugins_and_result_parsing(self):
        inst = [{"id": "a@m", "name": "a"}, {"id": "bee@m", "name": "bee"}]
        self.assertEqual(dispatch.known_plugins(["a", "bee@m", "zzz", "be"], inst), ["a@m", "bee@m"])
        self.assertEqual(dispatch.known_plugins("a, bee", inst), ["a@m", "bee@m"])
        self.assertEqual(dispatch.known_plugins(None, inst), [])
        res = dispatch.parse_result({"summary": "s", "artifacts": ["/a", {"path": "/b", "kind": "image"}, {"nope": 1}], "links": ["u", 3]})
        self.assertEqual([a["path"] for a in res["artifacts"]], ["/a", "/b"])
        self.assertEqual(res["artifacts"][1]["kind"], "image")
        self.assertEqual(res["links"], ["u"])
        self.assertIsNone(dispatch.parse_result("junk"))


class BeginTests(_Base):
    def test_begin_writes_request_v3_and_runs_the_dispatcher(self):
        rec = self.d.begin("  Make a meme from https://x/y ")
        self.assertEqual(rec["status"], "dispatching")
        self.assertEqual(rec["dispatcher_run_id"], "run1")
        req = json.loads(self.d.request_path(rec["id"]).read_text())
        self.assertEqual(req["version"], 3)
        self.assertEqual(req["dispatch_id"], rec["id"])
        self.assertEqual(req["message"]["text"], "Make a meme from https://x/y")
        self.assertEqual(req["response_path"], str(self.d.response_path(rec["id"])))
        self.assertEqual(req["root_folder"], str(self.run_dir))
        self.assertEqual(req["recent_tasks"], [])
        self.assertEqual(req["plugins"][0]["id"], "overboard@mkt")
        slot = self.sched.ephemeral[0]
        self.assertEqual(slot["cwd"], str(self.run_dir))
        self.assertEqual(slot["prompt"], f"/overboard:dispatch {self.d.request_path(rec['id'])}")
        self.assertEqual(slot["timeout_minutes"], dispatch.DISPATCHER_TIMEOUT_MINUTES)
        self.assertNotIn("meme", slot["prompt"])
        on_disk = json.loads(self.d.records_path.read_text())["dispatches"]
        self.assertEqual(on_disk[0]["id"], rec["id"])

    def test_begin_without_run_directory_fails_softly(self):
        self.knobs["run_directory"] = None
        rec = self.d.begin("do it")
        self.assertEqual(rec["status"], "failed")
        self.assertIn("run directory", rec["error"])
        self.assertEqual(self.sched.ephemeral, [])
        with self.assertRaises(ValueError):
            self.d.begin("   ")


class ResponseTests(_Base):
    def test_reject_and_reply(self):
        a = self.d.begin("hello there")
        self.respond("reject.json", a["id"])
        self.assertEqual(self.d.record(a["id"])["status"], "rejected")
        self.assertIn("greeting", self.d.record(a["id"])["error"])
        b = self.d.begin("what's running?")
        self.respond("reply.json", b["id"])
        self.assertEqual(self.d.record(b["id"])["status"], "answered")
        self.assertEqual(self.d.record(b["id"])["reply_text"], "Nothing is running right now.")
        # Files archived, outbox empty.
        self.assertEqual(list(self.d.outbox.iterdir()), [])
        self.assertTrue((self.d.archive / f"{a['id']}.response.json").exists())
        self.assertTrue((self.d.archive / f"{a['id']}.request.json").exists())

    def test_create_task_now_runs_with_result_contract_and_add_dir(self):
        rec = self.d.begin("write the launch post")
        self.respond("create_task_now.json", rec["id"])
        r = self.d.record(rec["id"])
        self.assertEqual(r["status"], "task_created")
        self.assertEqual(r["project_path"], str(self.proj))
        self.assertEqual(r["task_name"], "Write the post")
        self.assertEqual(r["task_run_ids"], ["run2"])
        self.assertEqual(r["workspace_plugins"], ["overboard@mkt"])
        self.assertTrue(Path(r["task_dir"]).is_dir())
        slot = self.sched.ephemeral[1]
        self.assertEqual(slot["name"], "Dispatch: Write the post")
        self.assertEqual(slot["cwd"], str(self.proj))
        self.assertEqual(slot["timeout_minutes"], 45)
        self.assertEqual(slot["add_dirs"], [r["task_dir"]])
        self.assertTrue(slot["prompt"].startswith("Write the launch blog post draft in docs/launch.md. When you are completely done"))
        self.assertIn(r["task_dir"] + "/result.json", slot["prompt"])
        self.assertEqual(r["state_label"], "queued")
        # recent_tasks for the next dispatcher session lists it
        self.assertEqual(self.d.recent_tasks()[0]["task_id"], rec["id"])

    def test_create_task_later_becomes_a_once_slot_and_creates_the_folder(self):
        rec = self.d.begin("scaffold newthing tonight")
        when = (datetime.now() + timedelta(hours=3)).isoformat(timespec="seconds")
        self.respond("create_task_later.json", rec["id"], WHEN=when)
        r = self.d.record(rec["id"])
        self.assertEqual(r["status"], "task_created", r.get("error"))
        self.assertTrue((self.run_dir / "newthing").is_dir())
        self.assertEqual(r["task_slot_id"], "slot2")
        self.assertEqual(r["task_run_ids"], [])
        saved = self.sched.saved[0]
        self.assertEqual(saved["schedule"]["kind"], "once")
        self.assertTrue(saved["enabled"])
        self.assertTrue(r["state_label"].startswith("scheduled"))

    def test_create_outside_run_dir_is_refused(self):
        rec = self.d.begin("make a folder somewhere odd")
        text = (FIX / "create_task_later.json").read_text().replace("DID", rec["id"]) \
            .replace("ROOT/newthing", str(self.root / "elsewhere")).replace("WHEN", "now")
        self.d.outbox.mkdir(parents=True, exist_ok=True)
        (self.d.outbox / f"{rec['id']}.response.json").write_text(text)
        self.d.tick()
        r = self.d.record(rec["id"])
        self.assertEqual(r["status"], "failed")
        self.assertIn("run directory", r["error"])
        self.assertFalse((self.root / "elsewhere").exists())

    def test_malformed_and_plugins_as_string(self):
        a = self.d.begin("x")
        self.respond("malformed.json", a["id"])
        self.assertEqual(self.d.record(a["id"])["status"], "failed")
        b = self.d.begin("y")
        self.respond("plugins_as_string.json", b["id"])
        r = self.d.record(b["id"])
        self.assertEqual(r["status"], "task_created")
        self.assertEqual(r["workspace_plugins"], ["overboard@mkt", "seo-blog@mkt"])
        self.assertEqual(r["timeout_minutes"], dispatch.DEFAULT_TASK_TIMEOUT_MINUTES)

    def test_follow_up_rebuilds_a_fresh_run(self):
        a = self.d.begin("make a meme")
        self.respond("create_task_now.json", a["id"])
        run = self.sched.runs["run2"]
        run.update(state=None, outcome="completed", ended_at="2026-08-19T12:00:00",
                   completion_message="Made the meme at /tmp/meme.png")
        (self.d.task_dir(a["id"]) / "result.json").write_text(json.dumps(
            {"summary": "Meme done", "artifacts": ["/tmp/meme.png"]}))
        self.d.run_committed(run)
        self.assertEqual(self.d.record(a["id"])["status"], "completed")
        self.assertEqual(self.d.record(a["id"])["result"]["summary"], "Meme done")
        b = self.d.begin("make it shorter")
        self.respond("follow_up.json", b["id"], TARGET=a["id"][:4])   # unique prefix
        rb = self.d.record(b["id"])
        self.assertEqual(rb["status"], "forwarded")
        self.assertEqual(rb["forwarded_task_id"], a["id"])
        ra = self.d.record(a["id"])
        self.assertEqual(ra["status"], "continuing")
        self.assertEqual(ra["task_run_ids"], ["run2", "run4"])
        slot = self.sched.ephemeral[-1]
        self.assertIn("Earlier, in this project, you were asked: Write the launch blog post draft in docs/launch.md.", slot["prompt"])
        self.assertIn("Files from that work: /tmp/meme.png", slot["prompt"])
        self.assertIn("Now the CTO wants this change: Make it shorter and add a caption.", slot["prompt"])
        self.assertIn("result.json", slot["prompt"])
        self.assertEqual(slot["cwd"], str(self.proj))

    def test_follow_up_to_unknown_task_fails(self):
        b = self.d.begin("make it shorter")
        self.respond("follow_up.json", b["id"], TARGET="zzzz")
        self.assertEqual(self.d.record(b["id"])["status"], "failed")


class CommitTests(_Base):
    def test_dispatcher_run_ending_without_response_fails(self):
        a = self.d.begin("do a thing")
        run = dict(self.sched.runs["run1"], state=None, outcome="timeout")
        self.d.run_committed(run)
        r = self.d.record(a["id"])
        self.assertEqual(r["status"], "failed")
        self.assertIn("timed out", r["error"])

    def test_dispatcher_run_ending_with_response_in_outbox_is_fine(self):
        a = self.d.begin("do a thing")
        text = (FIX / "reply.json").read_text().replace("DID", a["id"])
        self.d.outbox.mkdir(parents=True, exist_ok=True)
        (self.d.outbox / f"{a['id']}.response.json").write_text(text)
        self.d.run_committed(dict(self.sched.runs["run1"], state=None, outcome="completed"))
        self.assertEqual(self.d.record(a["id"])["status"], "answered")

    def test_task_outcomes(self):
        for outcome, status in (("cancelled", "cancelled"), ("timeout", "failed"),
                                ("skipped_missed_window", "failed"), ("completed", "completed")):
            a = self.d.begin("t " + outcome)
            self.respond("create_task_now.json", a["id"])
            rid = self.d.record(a["id"])["task_run_ids"][0]
            self.d.run_committed(dict(self.sched.runs[rid], state=None, outcome=outcome,
                                      ended_at="2026-08-19T12:00:00", completion_message="msg"))
            r = self.d.record(a["id"])
            self.assertEqual(r["status"], status, outcome)
            if outcome in ("timeout", "completed"):
                self.assertEqual(r["completion_message"], "msg")
            if status == "completed":
                self.assertIsNone(r["result"])   # no result.json written → None
                self.assertEqual(r["state_label"], "finished")

    def test_restore_fails_orphans_and_reconciles_finished(self):
        a = self.d.begin("a")                             # dispatcher run gone
        b = self.d.begin("b"); self.respond("create_task_now.json", b["id"])
        c = self.d.begin("c"); self.respond("create_task_now.json", c["id"])
        del self.sched.runs["run1"]
        bid = self.d.record(b["id"])["task_run_ids"][0]
        self.sched.runs[bid].update(state=None, outcome="completed", completion_message="done while away")
        del self.sched.runs[self.d.record(c["id"])["task_run_ids"][0]]
        # A fresh Dispatcher over the same files, like a restarted dashboard.
        d2 = dispatch.Dispatcher(self.sched, root=self.d.root, plugins_getter=lambda: self.installed,
                                 knobs_getter=lambda: self.knobs)
        d2.restore()
        self.assertEqual(d2.record(a["id"])["status"], "failed")
        self.assertEqual(d2.record(b["id"])["status"], "completed")
        self.assertEqual(d2.record(b["id"])["completion_message"], "done while away")
        self.assertEqual(d2.record(c["id"])["status"], "failed")
        self.assertIn("restarted", d2.record(c["id"])["error"])


if __name__ == "__main__":
    unittest.main()

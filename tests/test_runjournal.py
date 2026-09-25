from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from overboard import runjournal


class RunJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.tmp.name) / "run"
        self.env = mock.patch.dict(os.environ, {"OVERBOARD_RUN_ID": "RUN-1",
                                                "OVERBOARD_RUN_DIR": str(self.run_dir)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _lines(self):
        return [json.loads(l) for l in (self.run_dir / "journal.jsonl").read_text().splitlines()]

    def test_outside_a_run_every_tool_returns_an_error_result(self):
        with mock.patch.dict(os.environ, {"OVERBOARD_RUN_ID": ""}):
            for out in (runjournal.add_asset("/tmp/x.mp4"), runjournal.report_progress("hi"),
                        runjournal.ask_user("q"), runjournal.set_summary("s")):
                self.assertIn("error", out)
            self.assertIsNone(runjournal.journal_path())

    def test_add_asset_validates_and_infers_kind(self):
        self.assertIn("error", runjournal.add_asset("relative.mp4"))
        self.assertIn("error", runjournal.add_asset(str(self.run_dir / "missing.mp4")))
        f = Path(self.tmp.name) / "clip.MOV"
        f.write_bytes(b"x" * 10)
        out = runjournal.add_asset(str(f), caption="  the clip  ")
        self.assertEqual(out, {"ok": True, "path": str(f), "kind": "video"})
        page = Path(self.tmp.name) / "report.html"
        page.write_text("<p>hi</p>")
        self.assertEqual(runjournal.add_asset(str(page), kind="bogus")["kind"], "html")
        lines = self._lines()
        self.assertEqual([l["type"] for l in lines], ["asset", "asset"])
        self.assertEqual(lines[0]["run_id"], "RUN-1")
        self.assertEqual(lines[0]["caption"], "the clip")
        self.assertEqual(lines[0]["bytes"], 10)

    def test_progress_question_summary_shapes(self):
        self.assertEqual(runjournal.report_progress("3 of 10"), {"ok": True})
        q = runjournal.ask_user("**Log in?**", options=["Yes", "", "No", 1, 2, 3, 4])
        self.assertTrue(q["ok"])
        self.assertEqual(len(q["question_id"]), 8)
        self.assertEqual(runjournal.set_summary("Done."), {"ok": True})
        self.assertIn("error", runjournal.set_summary("   "))
        lines = self._lines()
        self.assertEqual([l["type"] for l in lines], ["progress", "question", "summary"])
        self.assertEqual(lines[1]["options"], ["Yes", "No", "1", "2", "3"])
        self.assertEqual(lines[1]["id"], q["question_id"])


if __name__ == "__main__":
    unittest.main()

"""`get_pending_work().launches`: every active launch, every pass."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from overboard import manager, store


class PendingLaunchesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._ctx = store.CONTEXT_PATH
        store.CONTEXT_PATH = Path(self._tmp.name) / "context.json"
        self._events = manager._slugs_flagged_since
        manager._slugs_flagged_since = lambda since_ts: set()

    def tearDown(self):
        store.CONTEXT_PATH = self._ctx
        manager._slugs_flagged_since = self._events
        self._tmp.cleanup()

    def _state(self):
        # Two quiet projects with summaries already written: nothing pending.
        return {"projects": {"overboard": {"repos": {"overboard": {}}, "head_sig": "h1"},
                             "seoblog": {"repos": {"seoblog": {}}, "head_sig": "h2"}}}

    def _ai(self):
        return {"summaries": {"overboard": {"head_sig": "h1", "text": "x"},
                              "seoblog": {"head_sig": "h2", "text": "y"}},
                "digests": {}, "work_reviews": {}}

    def test_every_active_launch_is_listed_even_when_nothing_moved(self):
        soon = (date.today() + timedelta(days=3)).isoformat()
        far = (date.today() + timedelta(days=40)).isoformat()
        store.save_context({
            "overboard": {"active_launch": {"type": "Public Launch", "title": "macOS launch",
                                            "action": "Publish Website", "target_date": far,
                                            "goals": "Charge money"}},
            "seoblog": {"active_launch": {"type": "Milestone", "title": "10 posts",
                                          "target_date": soon, "goals": "Ten published posts"}},
        })
        out = manager.pending_work(self._state(), self._ai())
        launches = out["launches"]
        # Nearest first; the far-out one is still there (status None).
        self.assertEqual([l["project"] for l in launches], ["seoblog", "overboard"])
        self.assertEqual(launches[0]["days_until"], 3)
        self.assertEqual(launches[0]["status"], "due_soon")
        self.assertEqual(launches[0]["goals"], "Ten published posts")
        self.assertEqual(launches[1]["status"], None)
        self.assertEqual(launches[1]["action"], "Publish Website")

    def test_no_launches_is_an_empty_list(self):
        store.save_context({"overboard": {"active_launch": None, "vision": "go"}})
        out = manager.pending_work(self._state(), self._ai())
        self.assertEqual(out["launches"], [])


if __name__ == "__main__":
    unittest.main()

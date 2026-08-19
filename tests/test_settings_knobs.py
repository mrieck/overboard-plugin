"""Scheduler/dispatcher knobs in credentials.json (store.scheduler_knobs) and
the run-directory validation the Settings page relies on.
Run: python3 -m unittest discover -s tests"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from overboard import store  # noqa: E402
from overboard.app import _run_directory_problem  # noqa: E402


class KnobTests(unittest.TestCase):
    def test_defaults_and_clamping(self):
        self.assertEqual(store.scheduler_knobs({}),
                         {"concurrency": 1, "default_timeout_minutes": 90, "run_directory": None})
        k = store.scheduler_knobs({"scheduler_concurrency": 99, "default_timeout_minutes": 1,
                                   "run_directory": "  ~/Sites  "})
        self.assertEqual(k["concurrency"], store.SCHEDULER_CONCURRENCY_MAX)
        self.assertEqual(k["default_timeout_minutes"], 5)
        self.assertEqual(k["run_directory"], "~/Sites")
        k = store.scheduler_knobs({"scheduler_concurrency": "junk", "default_timeout_minutes": None,
                                   "run_directory": ""})
        self.assertEqual((k["concurrency"], k["default_timeout_minutes"], k["run_directory"]), (1, 90, None))

    def test_run_directory_problems(self):
        saved = store.STATE_DIR
        with tempfile.TemporaryDirectory() as tmp:
            store.STATE_DIR = Path(tmp) / "cache"
            try:
                self.assertIsNone(_run_directory_problem(tmp))
                self.assertIsNone(_run_directory_problem(None))
                self.assertIn("not a folder", _run_directory_problem(os.path.join(tmp, "nope")))
                self.assertIn("plugin cache", _run_directory_problem(str(store.STATE_DIR / "x")))
                self.assertIn("~/.claude/plugins",
                              _run_directory_problem(str(Path.home() / ".claude" / "plugins" / "y")))
            finally:
                store.STATE_DIR = saved


if __name__ == "__main__":
    unittest.main()

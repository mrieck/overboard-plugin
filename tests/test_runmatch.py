"""Pure-logic tests for overboard.runmatch (port of the Mac app's RunMatcher tests).
Run: python3 -m unittest discover -s tests"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from overboard import runmatch  # noqa: E402

T0 = 1_000_000.0
CWD = "/Users/me/proj"


def ev(typ, ts, sid="s1", cwd=CWD, **extra):
    d = {"type": typ, "ts": ts, "session_id": sid, "cwd": cwd}
    d.update(extra)
    return d


class CaptureTests(unittest.TestCase):
    def test_captures_session_start_in_window(self):
        v = runmatch.assess(CWD, T0, None, [ev("SessionStart", T0 + 3)], set())
        self.assertEqual(v["session_id"], "s1")
        self.assertTrue(v["captured"])
        self.assertFalse(v["completed"])

    def test_ignores_session_start_outside_window(self):
        events = [ev("SessionStart", T0 - 60, sid="old"),
                  ev("SessionStart", T0 + runmatch.SESSION_START_LATE + 1, sid="late")]
        v = runmatch.assess(CWD, T0, None, events, set())
        self.assertIsNone(v["session_id"])

    def test_skips_claimed_sessions(self):
        events = [ev("SessionStart", T0 + 1, sid="other"), ev("SessionStart", T0 + 2, sid="mine")]
        v = runmatch.assess(CWD, T0, None, events, {"other"})
        self.assertEqual(v["session_id"], "mine")

    def test_other_cwd_not_captured(self):
        v = runmatch.assess(CWD, T0, None, [ev("SessionStart", T0 + 1, cwd="/elsewhere")], set())
        self.assertIsNone(v["session_id"])


class CompletionTests(unittest.TestCase):
    def test_human_session_stop_in_same_cwd_is_ignored_once_captured(self):
        # The run captured s1; a human's session h1 (started long before) stops
        # in the same folder — must NOT complete the run.
        events = [ev("SessionStart", T0 + 2, sid="s1"),
                  ev("Stop", T0 + 30, sid="h1", last_message="human done")]
        v = runmatch.assess(CWD, T0, None, events, set())
        self.assertEqual(v["session_id"], "s1")
        self.assertFalse(v["completed"])
        self.assertIsNone(v["completion_message"])

    def test_own_stop_completes_with_message(self):
        events = [ev("SessionStart", T0 + 2), ev("Stop", T0 + 300, last_message="all done")]
        v = runmatch.assess(CWD, T0, None, events, set())
        self.assertTrue(v["completed"])
        self.assertEqual(v["completion_message"], "all done")
        self.assertEqual(v["last_activity_ts"], T0 + 300)

    def test_fallback_by_cwd_when_no_session_start(self):
        events = [ev("Stop", T0 - 100, sid="before", last_message="old"),
                  ev("Stop", T0 + 100, sid="unknown", last_message="new")]
        v = runmatch.assess(CWD, T0, None, events, set())
        self.assertIsNone(v["session_id"])
        self.assertTrue(v["completed"])
        self.assertEqual(v["completion_message"], "new")

    def test_stop_with_pending_subagent_is_not_completion(self):
        events = [ev("SessionStart", T0 + 2),
                  ev("PostToolUse", T0 + 10, tool_name="Task"),
                  ev("Stop", T0 + 20, last_message="turn boundary")]
        v = runmatch.assess(CWD, T0, None, events, set())
        self.assertFalse(v["completed"])
        # ...until the subagent finishes and the wrapper stops again.
        events += [ev("SubagentStop", T0 + 200), ev("Stop", T0 + 210, last_message="really done")]
        v = runmatch.assess(CWD, T0, None, events, set())
        self.assertTrue(v["completed"])
        self.assertEqual(v["completion_message"], "really done")

    def test_stale_stop_before_subagent_stop_does_not_complete(self):
        # The SubagentStop that zeroes the tally must not complete the run on
        # the strength of the earlier turn-boundary Stop — the wrapper is being
        # re-invoked with the result right then. Only a Stop AFTER the last
        # subagent event counts.
        events = [ev("SessionStart", T0 + 2),
                  ev("PostToolUse", T0 + 10, tool_name="Agent"),
                  ev("Stop", T0 + 20, last_message="waiting on the agent"),
                  ev("SubagentStop", T0 + 600)]
        self.assertFalse(runmatch.assess(CWD, T0, None, events, set())["completed"])
        # A second background agent from the follow-up turn resets it again
        # (its PostToolUse may even log after the Stop; order in the log rules).
        events += [ev("Stop", T0 + 610, last_message="second turn"),
                   ev("PostToolUse", T0 + 605, tool_name="Task")]
        self.assertFalse(runmatch.assess(CWD, T0, None, events, set())["completed"])
        events += [ev("SubagentStop", T0 + 900), ev("Stop", T0 + 960, last_message="all collected")]
        v = runmatch.assess(CWD, T0, None, events, set())
        self.assertTrue(v["completed"])
        self.assertEqual(v["completion_message"], "all collected")

    def test_foreground_subagent_order_does_not_matter(self):
        events = [ev("SessionStart", T0 + 2),
                  ev("SubagentStop", T0 + 10),
                  ev("PostToolUse", T0 + 10.1, tool_name="Agent"),
                  ev("Stop", T0 + 20)]
        self.assertTrue(runmatch.assess(CWD, T0, None, events, set())["completed"])

    def test_session_end_always_completes(self):
        events = [ev("SessionStart", T0 + 2), ev("PostToolUse", T0 + 5, tool_name="Task"),
                  ev("SessionEnd", T0 + 50)]
        self.assertTrue(runmatch.assess(CWD, T0, None, events, set())["completed"])

    def test_known_session_id_is_used_directly(self):
        events = [ev("Stop", T0 + 5, sid="s9", cwd="/moved/elsewhere", last_message="ok")]
        v = runmatch.assess(CWD, T0, "s9", events, set())
        self.assertTrue(v["completed"])
        self.assertFalse(v["captured"])


class NormalizeTests(unittest.TestCase):
    def test_symlink_and_tilde_normalize_equal(self):
        with tempfile.TemporaryDirectory() as d:
            real = os.path.join(d, "real")
            os.mkdir(real)
            link = os.path.join(d, "link")
            os.symlink(real, link)
            self.assertEqual(runmatch.normalize_path(link), runmatch.normalize_path(real))
        self.assertEqual(runmatch.normalize_path("~"), os.path.expanduser("~"))
        self.assertEqual(runmatch.normalize_path(None), "")

    def test_symlinked_cwd_matches_events(self):
        with tempfile.TemporaryDirectory() as d:
            real = os.path.join(d, "real")
            os.mkdir(real)
            link = os.path.join(d, "link")
            os.symlink(real, link)
            v = runmatch.assess(link, T0, None, [ev("SessionStart", T0 + 1, cwd=real)], set())
            self.assertEqual(v["session_id"], "s1")


if __name__ == "__main__":
    unittest.main()


class TrustPromptTests(unittest.TestCase):
    def test_only_the_trust_dialog_is_answered(self):
        pane = ("Do you trust the files in this folder?\n"
                "  ❯ 1. Yes, proceed\n    2. No, exit\n")
        self.assertEqual(runmatch.trust_prompt_response(pane), "\r")
        self.assertIsNone(runmatch.trust_prompt_response("Allow Bash(rm -rf)? 1. Yes 2. No"))
        self.assertIsNone(runmatch.trust_prompt_response(""))
        self.assertIsNone(runmatch.trust_prompt_response(None))

    def test_only_the_tail_counts(self):
        old = "trust the files in this ... yes, proceed\n" + ("x" * 3000)
        self.assertIsNone(runmatch.trust_prompt_response(old))


class StallTests(unittest.TestCase):
    def test_measured_from_last_activity_or_launch(self):
        self.assertFalse(runmatch.is_stalled(1000.0, None, 1000.0 + 9 * 60, 10))
        self.assertTrue(runmatch.is_stalled(1000.0, None, 1000.0 + 10 * 60, 10))
        self.assertFalse(runmatch.is_stalled(1000.0, 1000.0 + 8 * 60, 1000.0 + 10 * 60, 10))
        self.assertFalse(runmatch.is_stalled(1000.0, None, 1e9, 0))
        self.assertFalse(runmatch.is_stalled(1000.0, None, 1e9, "nope"))

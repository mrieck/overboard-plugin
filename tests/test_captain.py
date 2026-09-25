from __future__ import annotations

import json
import unittest
from pathlib import Path

from overboard import captain

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "captain"


def fixture(name):
    return (FIXTURES / name).read_text()


class CaptainParseTests(unittest.TestCase):
    def test_full_response_parses_and_normalises(self):
        out = captain.parse_response(fixture("marketing_full.json"))
        self.assertTrue(out["ok"], out)
        self.assertEqual([a["type"] for a in out["actions"]],
                         ["create_task", "create_agent", "update_agent", "question", "goal_progress", "note"])
        self.assertEqual(out["actions"][1]["plugins"], ["socialcue@socialcue"])   # bare string → list
        self.assertTrue(out["memory_updated"])
        self.assertEqual(out["dropped"], [])

    def test_quiet_day_is_valid(self):
        out = captain.parse_response(fixture("quiet_day.json"))
        self.assertTrue(out["ok"])
        self.assertEqual(out["actions"], [])

    def test_missing_log_and_unknown_action_fail_whole_response(self):
        self.assertFalse(captain.parse_response(fixture("malformed_no_log.json"))["ok"])
        self.assertIn("launch_rockets", captain.parse_response(fixture("unknown_action.json"))["error"])
        self.assertFalse(captain.parse_response("not json")["ok"])

    def test_caps(self):
        many = {"log_entry": "x", "actions": [{"type": "note", "text": "n"}] * 9}
        self.assertEqual(len(captain.parse_response(json.dumps(many))["actions"]), 9)   # no cap
        long_q = {"log_entry": "x" * 2000, "actions": [{"type": "question", "text": "q" * 900},
                                                       {"type": "question", "text": "  "}]}
        out = captain.parse_response(json.dumps(long_q))
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["log_entry"]), 1201)
        self.assertEqual(len(out["actions"][0]["text"]), 701)
        self.assertEqual(out["dropped"], [{"index": 1, "why": "question has no text"}])

    def test_runs_per_day(self):
        self.assertEqual(captain.runs_per_day({"kind": "daily", "times": [{"hour": 2, "minute": 0}, {"hour": 9, "minute": 0}]}), 2)
        self.assertAlmostEqual(captain.runs_per_day({"kind": "weekly", "days": [2, 4], "times": [{"hour": 6, "minute": 0}]}), 2 / 7)
        self.assertEqual(captain.runs_per_day({"kind": "everyHours", "interval": 2, "window": {"startHour": 9, "endHour": 18}}), 4)
        self.assertEqual(captain.runs_per_day({"kind": "everyHours", "interval": 3}), 8)
        self.assertEqual(captain.runs_per_day({"kind": "once", "date": "2000-01-01T00:00:00Z"}), 0)
        self.assertEqual(captain.runs_per_day({"kind": "once", "date": "2999-01-01T00:00:00Z"}), 1)

    def test_duplicate_agent(self):
        agents = [{"name": "Social discovery", "enabled": True, "command": "/x", "cwds": ["/p"]}]
        self.assertTrue(captain.is_duplicate_agent({"name": "social DISCOVERY"}, agents))
        self.assertTrue(captain.is_duplicate_agent({"name": "Other", "command": "/x", "cwds": ["/p"]}, agents))
        self.assertFalse(captain.is_duplicate_agent({"name": "Other", "command": "/y", "cwds": ["/p"]}, agents))


if __name__ == "__main__":
    unittest.main()

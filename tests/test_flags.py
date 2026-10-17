"""Flags the assistant can keep right: `flag_for_review` returns an id,
`update_flag` rewrites or retracts it through append-only `flag_update`
events, and the dashboard / `list_flags` read the folded result."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from overboard import events, mcp_server
from overboard.store import review_key


class FlagFoldTests(unittest.TestCase):
    def test_updates_rewrite_and_retract_in_order(self):
        raised = {"ts": 1.0, "type": "flag", "repo_hint": "r", "note": "Kiwi email bounced"}
        raised["id"] = events.flag_id(raised)
        other = {"ts": 2.0, "type": "flag", "repo_hint": "r", "note": "needs a key", "id": "k1"}
        evs = [
            {"ts": 3.0, "type": "flag_update", "flag_id": raised["id"], "note": "Kiwi email sent 10/14"},
            {"ts": 4.0, "type": "flag_update", "flag_id": "k1", "note": None},
            {"ts": 5.0, "type": "flag_update", "flag_id": "ghost", "note": "ignored"},
            other, raised,
        ]
        open_ = events.open_flags(evs)
        self.assertEqual([(f["id"], f["note"]) for f in open_],
                         [(raised["id"], "Kiwi email sent 10/14")])

    def test_legacy_flag_without_id_is_addressable(self):
        legacy = {"ts": 1700000000.123, "type": "flag", "repo_hint": "r", "note": "old"}
        fid = events.flag_id(legacy)
        self.assertEqual(len(fid), 8)
        self.assertEqual(events.flag_id({**legacy, "repo": "r"}), fid)
        gone = events.open_flags([legacy, {"ts": 2e9, "type": "flag_update", "flag_id": fid}])
        self.assertEqual(gone, [])


class FlagToolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._path = events.EVENTS_PATH
        events.EVENTS_PATH = Path(self._tmp.name) / "events.jsonl"
        self.state = {"projects": {"Overboard": {"repos": {"overboard-mac": {}, "overboard-plugin": {}}}}}
        self._patches = [
            mock.patch.object(mcp_server.store, "load_state", side_effect=lambda: self.state),
            mock.patch.object(mcp_server, "_known_slugs", return_value={"overboard-mac": "/x", "overboard-plugin": "/y"}),
            mock.patch.object(events.localrepo, "links_for_machine",
                              return_value={"overboard-mac": "/x", "overboard-plugin": "/y"}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        events.EVENTS_PATH = self._path
        self._tmp.cleanup()

    def test_raise_update_and_retract(self):
        msg = mcp_server.flag_for_review("Overboard", "SnipKiwi: Kiwi email still unsent")
        self.assertIn("(flag ", msg)
        fid = msg.split("(flag ")[1].rstrip(")")
        listed = mcp_server.list_flags()["flags"]
        self.assertEqual([(f["id"], f["project"], f["repo"]) for f in listed], [(fid, "Overboard", "overboard-mac")])

        # Rewritten by id: the dashboard-facing text changes, the id stays.
        msg = mcp_server.update_flag("Overboard", fid, "SnipKiwi: Kiwi email sent 10/14, awaiting reply", "CTO said so")
        self.assertIn("now reads", msg)
        self.assertEqual(mcp_server.list_flags("Overboard")["flags"][0]["note"],
                         "SnipKiwi: Kiwi email sent 10/14, awaiting reply")

        # Retracted by its current text; an unknown flag is refused with the open ids.
        self.assertIn("call list_flags", mcp_server.update_flag("Overboard", "nope"))
        msg = mcp_server.update_flag("Overboard", "SnipKiwi: Kiwi email sent 10/14, awaiting reply")
        self.assertEqual(msg, f"flag {fid} on Overboard retracted")
        self.assertEqual(mcp_server.list_flags()["flags"], [])
        self.assertIn("unknown project", mcp_server.update_flag("Nope", fid))

    def test_cto_dismissal_hides_the_flag_from_the_agent_too(self):
        fid = mcp_server.flag_for_review("overboard-plugin", "restart the dashboard").split("(flag ")[1].rstrip(")")
        self.state["dismissed_reviews"] = [review_key("Overboard", "restart the dashboard")]
        self.assertEqual(mcp_server.list_flags()["flags"], [])
        self.assertIn("no open flag", mcp_server.update_flag("Overboard", fid))


if __name__ == "__main__":
    unittest.main()

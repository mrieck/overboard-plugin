"""`get_pending_work().one_shot`: a session the Mac app launched must not loop."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from overboard import mcp_server


class OneShotTests(unittest.TestCase):
    def _pending(self, run_id):
        env = {k: v for k, v in os.environ.items() if k != "OVERBOARD_RUN_ID"}
        if run_id is not None:
            env["OVERBOARD_RUN_ID"] = run_id
        work = {"items": [], "deferred": ["seoblog"]}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(mcp_server.store, "load_state", return_value={}), \
                mock.patch.object(mcp_server.store, "load_ai", return_value={}), \
                mock.patch.object(mcp_server.manager, "pending_work", return_value=work):
            return mcp_server.get_pending_work()

    def test_app_launched_session_is_one_shot_even_with_work_deferred(self):
        work = self._pending("5F0E1B1C-0000-4000-8000-000000000001")
        self.assertTrue(work["one_shot"])
        self.assertEqual(work["deferred"], ["seoblog"])

    def test_a_session_the_user_started_may_loop(self):
        self.assertFalse(self._pending(None)["one_shot"])
        self.assertFalse(self._pending("")["one_shot"])


if __name__ == "__main__":
    unittest.main()

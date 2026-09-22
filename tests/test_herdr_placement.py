"""Where a run lands in Herdr: one workspace per project, one tab per run,
no product name inside the session."""
from __future__ import annotations

import unittest

from overboard import herdr


class WorkspaceLabelTests(unittest.TestCase):
    def test_workspace_is_named_after_the_project_folder(self):
        self.assertEqual(herdr.workspace_label("/Users/x/Sites/seoblog"), "seoblog")
        self.assertEqual(herdr.workspace_label("/Users/x/Sites/seoblog/"), "seoblog")
        self.assertEqual(herdr.workspace_label("/Users/x/Sites/My Site"), "My Site")

    def test_unusable_folders_fall_back(self):
        for bad in ("", "/", "   "):
            self.assertEqual(herdr.workspace_label(bad), herdr.FALLBACK_WORKSPACE)

    def test_no_label_says_overboard(self):
        self.assertNotIn("overboard", herdr.ASSISTANT_WORKSPACE.lower())
        self.assertNotIn("overboard", herdr.agent_name("Security Review"))


class PanePlacementTests(unittest.TestCase):
    """`_pane_for_run` against a recorded fake of `herdr.call`."""

    def setUp(self):
        self.sent = []
        self._real_call = herdr.call

    def tearDown(self):
        herdr.call = self._real_call

    def _fake(self, replies):
        def call(method, params=None, timeout=15.0):
            self.sent.append((method, params or {}))
            if method not in replies:
                raise herdr.HerdrError("not_found", f"unstubbed {method}")
            return replies[method]
        herdr.call = call

    def test_creates_the_project_workspace_and_names_its_root_tab(self):
        self._fake({
            "workspace.list": {"workspaces": []},
            "workspace.create": {"workspace": {"workspace_id": "w9", "label": "demo"},
                                 "tab": {"tab_id": "w9:t1"},
                                 "root_pane": {"pane_id": "w9:p1", "tab_id": "w9:t1",
                                               "workspace_id": "w9"}},
            "tab.rename": {"type": "ok"},
        })
        pane = herdr._pane_for_run("/Users/x/Sites/demo", "Nightly", "demo")
        self.assertEqual(pane["pane_id"], "w9:p1")
        methods = [m for m, _ in self.sent]
        self.assertEqual(methods, ["workspace.list", "workspace.create", "tab.rename"])
        created = dict(self.sent[1][1])
        self.assertEqual(created["label"], "demo")
        self.assertEqual(created["cwd"], "/Users/x/Sites/demo")
        self.assertEqual(created["env"], {"OVERBOARD_NO_BROWSER": "1"})
        # workspace.create takes no tab label: the root tab is renamed to the run.
        self.assertEqual(self.sent[2][1], {"tab_id": "w9:t1", "label": "Nightly"})

    def test_a_refused_rename_does_not_cost_the_run(self):
        self._fake({
            "workspace.list": {"workspaces": []},
            "workspace.create": {"workspace": {"workspace_id": "w9", "label": "demo"},
                                 "tab": {"tab_id": "w9:t1"},
                                 "root_pane": {"pane_id": "w9:p1"}},
        })
        pane = herdr._pane_for_run("/Users/x/Sites/demo", "Nightly", "demo")
        self.assertEqual(pane["pane_id"], "w9:p1")

    def test_reuses_the_workspace_picked_by_label(self):
        self._fake({
            "workspace.list": {"workspaces": [
                {"workspace_id": "w2", "label": "Assistant"},
                {"workspace_id": "w3", "label": "demo"}]},
            "tab.create": {"tab": {"tab_id": "w3:t7"},
                           "root_pane": {"pane_id": "w3:p7", "tab_id": "w3:t7",
                                         "workspace_id": "w3"}},
        })
        pane = herdr._pane_for_run("/Users/x/Sites/demo", "Nightly", "demo")
        self.assertEqual(pane["pane_id"], "w3:p7")
        methods = [m for m, _ in self.sent]
        self.assertEqual(methods, ["workspace.list", "tab.create"])
        tab = self.sent[1][1]
        self.assertEqual(tab["workspace_id"], "w3")
        self.assertEqual(tab["label"], "Nightly")
        self.assertEqual(tab["cwd"], "/Users/x/Sites/demo")


if __name__ == "__main__":
    unittest.main()

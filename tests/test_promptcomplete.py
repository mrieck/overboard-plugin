"""Prompt completion: token detection, command-name derivation, the slash
catalog over a fake ~/.claude + plugin install, path completion over an
injected lister, and enablement precedence. Run: python3 -m unittest discover -s tests"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from overboard import promptcomplete as pc  # noqa: E402


class TokenTests(unittest.TestCase):
    def test_active_token_table(self):
        cases = [
            ("/over", 5, ("slash", "over", 0, 5)),
            ("/frontend:comp", 14, ("slash", "frontend:comp", 0, 14)),
            ("/", 1, ("slash", "", 0, 1)),
            ("/usr/bin", 8, ("path", "/usr/bin", 0, 8)),
            ("do ~/Si", 7, ("path", "~/Si", 3, 7)),
            ("read ./src/", 11, ("path", "./src/", 5, 11)),
            ("hello /over", 11, ("path", "/over", 6, 11)),   # not leading → a path
            ("hello there", 11, None),
            ("/over now", 9, None),                           # caret in "now"
            ("", 0, None),
            ("/over", 0, None),
            ("/ov er", 3, ("slash", "ov", 0, 3)),
        ]
        for text, cursor, want in cases:
            got = pc.active_token(text, cursor)
            if want is None:
                self.assertIsNone(got, (text, cursor))
            else:
                self.assertEqual((got["kind"], got["partial"], got["start"], got["end"]), want, (text, cursor))

    def test_leading_command(self):
        self.assertEqual(pc.leading_command("/overboard:overboard now"), ("overboard:overboard", True))
        self.assertEqual(pc.leading_command("/review"), ("review", False))
        self.assertIsNone(pc.leading_command("review this"))
        self.assertIsNone(pc.leading_command("/usr/bin/x"))
        self.assertIsNone(pc.leading_command("/bad!name x"))


class DeriveTests(unittest.TestCase):
    def test_names(self):
        self.assertEqual(pc.name_from_relative_path("deploy.md"), "deploy")
        self.assertEqual(pc.name_from_relative_path("frontend/component.md"), "frontend:component")
        self.assertIsNone(pc.name_from_relative_path("README.txt"))
        self.assertIsNone(pc.name_from_relative_path(".md"))

    def test_skill_name(self):
        self.assertEqual(pc.skill_name("---\nname: tidy-up\ndescription: x\n---\nbody", "dir"), "tidy-up")
        self.assertEqual(pc.skill_name("---\ndescription: x\n---\n", "dir"), "dir")
        self.assertEqual(pc.skill_name(None, "dir"), "dir")
        self.assertEqual(pc.skill_name("no frontmatter\nname: nope", "dir"), "dir")

    def test_plugin_commands_canonical_plus_alias(self):
        cmds = pc.plugin_commands("rev@mkt", "reviewer", "/p", ["review.md", "ui/audit.md"],
                                  [("tidy", "---\nname: tidy-up\n---")])
        self.assertEqual([c["name"] for c in cmds], ["reviewer:review", "reviewer:ui:audit", "reviewer:tidy-up"])
        self.assertEqual(cmds[0]["aliases"], ["review"])
        self.assertEqual(cmds[0]["file"], "/p/commands/review.md")
        self.assertEqual(cmds[2]["source"], "skill")
        self.assertTrue(pc.command_matches(cmds[0], "revi"))
        self.assertTrue(pc.command_matches(cmds[0], "reviewer:re"))
        self.assertFalse(pc.command_matches(cmds[0], "audit"))


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"
        (self.home / ".claude" / "commands" / "ops").mkdir(parents=True)
        (self.home / ".claude" / "commands" / "deploy.md").write_text("x")
        (self.home / ".claude" / "commands" / "ops" / "rotate.md").write_text("x")
        self.proj = Path(self._tmp.name) / "proj"
        (self.proj / ".claude" / "commands").mkdir(parents=True)
        (self.proj / ".claude" / "commands" / "ship.md").write_text("x")
        self.install = Path(self._tmp.name) / "plug"
        (self.install / "commands").mkdir(parents=True)
        (self.install / "commands" / "overboard.md").write_text("x")
        (self.install / "skills" / "cto-assistant").mkdir(parents=True)
        (self.install / "skills" / "cto-assistant" / "SKILL.md").write_text("---\nname: cto-assistant\n---\n")
        self.inv = {"plugins": {"overboard@mkt": {"name": "overboard", "installs": [
            {"install_path": "/nope"}, {"install_path": str(self.install)}]}}}

    def tearDown(self):
        self._tmp.cleanup()

    def test_scan_and_suggest(self):
        cmds = pc.scan_catalog(str(self.proj), home=self.home, inventory=self.inv)
        names = sorted(c["name"] for c in cmds)
        self.assertEqual(names, ["deploy", "ops:rotate", "overboard:cto-assistant", "overboard:overboard", "ship"])
        sugg = pc.command_suggestions(cmds, "over")
        self.assertEqual([s["label"] for s in sugg], ["/overboard:cto-assistant", "/overboard:overboard"])
        self.assertEqual(sugg[0]["insertion"], "/overboard:cto-assistant ")
        self.assertEqual(sugg[0]["detail"], "overboard")
        self.assertEqual(pc.command_suggestions(cmds, "sh")[0]["detail"], "project")
        self.assertEqual(pc.exact_command(cmds, "overboard")["name"], "overboard:overboard")
        self.assertIsNone(pc.exact_command(cmds, "nope"))


class PathTests(unittest.TestCase):
    def lister(self, d):
        tree = {"/home/me": [("projects", True), ("Pictures", True), (".ssh", True), ("notes.txt", False), ("prog.log", False)],
                "/home/me/projects": [("b", True), ("a", True)],
                "/work/src": [("main.py", False)],
                "/": [("usr", True), ("etc", True)]}
        return tree.get(d, [])

    def test_user_spelling_dirs_first_hidden_rules(self):
        os.environ["HOME"] = "/home/me"
        # case-insensitive prefix, dirs first, then case-insensitive name order
        self.assertEqual(pc.path_completions("~/p", "/x", self.lister), ["~/Pictures/", "~/projects/", "~/prog.log"])
        self.assertEqual(pc.path_completions("~/", "/x", self.lister)[:3], ["~/Pictures/", "~/projects/", "~/notes.txt"])
        self.assertEqual(pc.path_completions("~/.s", "/x", self.lister), ["~/.ssh/"])
        self.assertEqual(pc.path_completions("~/projects/", "/x", self.lister), ["~/projects/a/", "~/projects/b/"])
        self.assertEqual(pc.path_completions("./src/m", "/work", self.lister), ["./src/main.py"])
        self.assertEqual(pc.path_completions("/u", "/x", self.lister), ["/usr/"])
        self.assertEqual(pc.path_completions("nope", "/x", self.lister), [])


class EnablementTests(unittest.TestCase):
    def test_precedence(self):
        maps = {"user": {"a@m": True, "b@m": True}, "project": {"b@m": False}, "local": {}}
        self.assertTrue(pc.is_enabled("a@m", maps))
        self.assertFalse(pc.is_enabled("b@m", maps))
        maps["local"] = {"b@m": True}
        self.assertTrue(pc.is_enabled("b@m", maps))
        self.assertFalse(pc.is_enabled("c@m", maps))


if __name__ == "__main__":
    unittest.main()

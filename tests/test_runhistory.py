"""The sweep's "ran since last report" list, over both run-record formats."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from overboard import runhistory


def _plugin_run(name, cwd, outcome, ended, prompt="go", started=None):
    return {"slot_name": name, "cwd": cwd, "prompt": prompt, "outcome": outcome,
            "started_at": (started or ended - timedelta(minutes=5)).isoformat(timespec="seconds"),
            "ended_at": ended.isoformat(timespec="seconds")}


def _mac_run(name, cwd, outcome, ended, command="go", project_dir=None, started=None, hidden=None):
    def z(dt):  # the app writes fractional ISO-8601 in UTC; the test clock is UTC too
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {"slotName": name, "cwd": cwd, "projectDir": project_dir, "command": command,
            "outcome": outcome, "startedAt": z(started or ended - timedelta(minutes=5)),
            "endedAt": z(ended), "hidden": hidden}


class RecentRunsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.plugin = root / "runs.json"
        self.mac = root / "mac"
        self.mac.mkdir()
        # A fixed "now" that is local == UTC for the Mac timestamps: parse_iso_any
        # converts Z to local, so build the Mac records from a UTC now.
        self.now_utc = datetime.utcnow().replace(microsecond=0)
        self.now = datetime.now().replace(microsecond=0)
        self.offset = self.now - self.now_utc

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, plugin_runs=(), mac_runs=()):
        self.plugin.write_text(json.dumps({"version": 1, "runs": list(plugin_runs)}))
        for i, r in enumerate(mac_runs):
            (self.mac / f"r{i}.json").write_text(json.dumps(r))

    def _recent(self):
        return runhistory.recent_runs(now=self.now, plugin_path=self.plugin, mac_dir=self.mac)

    def test_window_starts_at_the_previous_sweep(self):
        h = timedelta(hours=1)
        self._write(
            plugin_runs=[
                _plugin_run("socialcue/daily-posts", "/Users/x/OverboardWork/socialcue/daily-posts",
                            "completed", self.now - 3 * h),
                _plugin_run("Old nightly", "/Users/x/Sites/old", "completed", self.now - 30 * h),
            ],
            mac_runs=[
                _mac_run("Morning report", "/Users/x/Sites", {"completed": {}}, self.now_utc - 10 * h,
                         command="/overboard:overboard"),
                _mac_run("Write a blog for tryoverboard.com", "/Users/x/Sites/overboard-website",
                         {"timeout": {}}, self.now_utc - 5 * h),
                _mac_run("Dispatch: Fix CSS", "/Users/x/Sites/site", {"completed": {}}, self.now_utc - 2 * h),
                _mac_run("Dispatcher #ab12", "/Users/x/Sites", {"completed": {}}, self.now_utc - 2 * h),
                _mac_run("Before the report", "/Users/x/Sites/site", {"completed": {}}, self.now_utc - 12 * h),
            ])
        out = self._recent()
        self.assertEqual(out["previous_report_at"], (self.now - 10 * h).isoformat())
        # The window opens where the previous sweep *started* (5 min before it ended).
        self.assertEqual(out["since"], (self.now - 10 * h - timedelta(minutes=5)).isoformat())
        names = [(r["name"], r["project"], r["kind"], r["outcome"]) for r in out["runs"]]
        self.assertEqual(names, [
            ("Fix CSS", "site", "task", "completed"),
            ("socialcue/daily-posts", "daily-posts", "scheduled", "completed"),
            ("Write a blog for tryoverboard.com", "overboard-website", "scheduled", "timeout"),
        ])

    def test_no_previous_sweep_means_the_last_day(self):
        h = timedelta(hours=1)
        self._write(plugin_runs=[
            _plugin_run("Nightly", "/Users/x/Sites/a", "completed", self.now - 20 * h),
            _plugin_run("Nightly", "/Users/x/Sites/a", "completed", self.now - 30 * h),
        ])
        out = self._recent()
        self.assertIsNone(out["previous_report_at"])
        self.assertEqual(out["since"], (self.now - 24 * h).isoformat())
        self.assertEqual(len(out["runs"]), 1)

    def test_window_is_capped_at_a_week(self):
        d = timedelta(days=1)
        self._write(mac_runs=[
            _mac_run("Overboard sweep", "/Users/x/Sites", {"completed": {}}, self.now_utc - 20 * d,
                     command="/overboard:overboard"),
            _mac_run("Nightly", "/Users/x/Sites/a", {"completed": {}}, self.now_utc - 10 * d),
            _mac_run("Nightly", "/Users/x/Sites/a", {"completed": {}}, self.now_utc - 3 * d),
        ])
        out = self._recent()
        self.assertEqual(out["since"], (self.now - 7 * d).isoformat())
        self.assertEqual(len(out["runs"]), 1)

    def test_mac_outcomes_and_project_folder(self):
        self._write(mac_runs=[
            _mac_run("Task Workspace Run", "/Users/x/OverboardWork/seoblog/task-workspace-run",
                     {"launchFailed": {"reason": "x"}}, self.now_utc - timedelta(hours=1),
                     project_dir="/Users/x/Sites/seoblog"),
            _mac_run("Hidden one", "/Users/x/Sites/seoblog", {"completed": {}},
                     self.now_utc - timedelta(hours=1), hidden=True),
        ])
        runs = self._recent()["runs"]
        self.assertEqual([(r["project"], r["outcome"]) for r in runs], [("seoblog", "launch_failed")])

    def test_missing_stores_are_empty_not_fatal(self):
        out = runhistory.recent_runs(now=self.now, plugin_path=Path(self._tmp.name) / "nope.json",
                                     mac_dir=Path(self._tmp.name) / "nodir")
        self.assertEqual(out["runs"], [])
        self.assertIsNone(out["previous_report_at"])


if __name__ == "__main__":
    unittest.main()


class SqliteRunsTests(unittest.TestCase):
    """The Mac app's database, read read-only; the JSON scan is the fallback."""

    def setUp(self):
        import sqlite3
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.plugin = root / "runs.json"
        self.plugin.write_text(json.dumps({"version": 1, "runs": []}))
        self.mac = root / "mac"
        self.mac.mkdir()
        self.db = root / "overboard.sqlite"
        conn = sqlite3.connect(self.db)
        conn.executescript((Path(__file__).resolve().parents[1] / "overboard" / "sql" / "schema_v1.sql").read_text())
        self.now = datetime.now().replace(microsecond=0)
        t = self.now.timestamp()
        conn.execute("INSERT INTO ships VALUES ('S1','Marketing','marketing',NULL,1,0,NULL,?,?,'{}')", (t, t))
        conn.execute("INSERT INTO ships VALUES ('S0','One-off','one-off','one_off',1,99,NULL,?,?,'{}')", (t, t))
        rows = [
            ("R1", "A1", "Social discovery", "scheduled", "S1", "completed", t - 7200, t - 3600, 0),
            ("R2", "A2", "Meme", "dispatch", "S1", "timeout", t - 5400, t - 1800, 0),
            ("R3", "A3", "Nightly", "scheduled", None, "completed", t - 4000, t - 900, 0),
            ("R4", "A4", "Captain · Marketing", "captain", "S1", "completed", t - 2000, t - 600, 0),
            ("R5", "A5", "Morning report", "system", None, "completed", t - 40000, t - 39000, 0),
            ("R6", "A6", "Hidden", "scheduled", "S1", "completed", t - 3000, t - 2000, 1),
        ]
        for rid, aid, name, kind, ship, outcome, started, ended, hidden in rows:
            conn.execute("""INSERT INTO runs (id, agent_id, agent_name, agent_kind, ship_id, trigger, cwd, started_at,
                            ended_at, outcome, hidden, payload, summary)
                            VALUES (?,?,?,?,?,'scheduled','/Users/x/Sites/proj',?,?,?,?,'{"command":"/x"}',?)""",
                         (rid, aid, name, kind, ship, started, ended, outcome, hidden,
                          "Directories: 3 done, 7 to go.\nMore." if kind == "captain" else None))
        conn.execute("""INSERT INTO work_items VALUES ('W1','S1','blocked','captain',NULL,NULL,?,?,NULL,?)""",
                     (t, t, json.dumps({"title": "Spend credits?", "humanQA": [{"question": "**Spend credits?**\nmore"}]})))
        conn.execute("""INSERT INTO work_items VALUES ('W2','S1','proposed','captain',NULL,NULL,?,?,NULL,?)""",
                     (t, t, json.dumps({"title": "New agent: Nightly directories"})))
        conn.commit()
        conn.close()

    def tearDown(self):
        self._tmp.cleanup()

    def test_reads_runs_and_ships_from_the_database(self):
        out = runhistory.recent_runs(now=self.now, plugin_path=self.plugin, mac_dir=self.mac, mac_db=self.db)
        names = [(r["name"], r["kind"], r["ship"]) for r in out["runs"]]
        self.assertEqual(names, [("Nightly", "scheduled", None), ("Meme", "task", "Marketing"),
                                 ("Social discovery", "scheduled", "Marketing")])
        self.assertEqual(out["runs"][1]["outcome"], "timeout")
        ships = out["ships"]
        self.assertEqual([s["name"] for s in ships], ["Marketing"])   # One-off with nothing pending is left out
        self.assertEqual(ships[0]["last_captain_log"], "Directories: 3 done, 7 to go.")
        self.assertEqual(ships[0]["open_questions"], ["**Spend credits?**"])
        self.assertNotIn("pending_proposals", ships[0])

    def test_missing_database_falls_back_to_json(self):
        out = runhistory.recent_runs(now=self.now, plugin_path=self.plugin, mac_dir=self.mac,
                                     mac_db=Path(self._tmp.name) / "nope.sqlite")
        self.assertEqual(out["runs"], [])
        self.assertEqual(out["ships"], [])

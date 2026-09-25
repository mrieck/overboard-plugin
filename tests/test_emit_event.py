from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "emit_event.py"


def _run(payload: dict, env_extra: dict) -> dict:
    with tempfile.TemporaryDirectory() as home:
        env = {**os.environ, "HOME": home, **env_extra}
        subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload).encode(),
                       env=env, check=True)
        lines = (Path(home) / ".cache" / "overboard" / "events.jsonl").read_text().splitlines()
        assert len(lines) == 1, lines
        return json.loads(lines[0])


class EmitEventTests(unittest.TestCase):
    def test_run_id_rides_from_environment(self):
        evt = _run({"hook_event_name": "Stop", "session_id": "S1", "cwd": "/p",
                    "last_assistant_message": "done"},
                   {"OVERBOARD_RUN_ID": "ABC-123"})
        self.assertEqual(evt["run_id"], "ABC-123")
        self.assertEqual(evt["type"], "Stop")
        self.assertEqual(evt["last_message"], "done")

    def test_no_run_id_outside_an_overboard_run(self):
        env = {k: v for k, v in os.environ.items() if k != "OVERBOARD_RUN_ID"}
        with tempfile.TemporaryDirectory() as home:
            env["HOME"] = home
            subprocess.run([sys.executable, str(HOOK)],
                           input=json.dumps({"hook_event_name": "SessionStart", "session_id": "S"}).encode(),
                           env=env, check=True)
            evt = json.loads((Path(home) / ".cache/overboard/events.jsonl").read_text().splitlines()[0])
        self.assertNotIn("run_id", evt)

    def test_long_closing_message_keeps_sixteen_thousand_chars(self):
        evt = _run({"hook_event_name": "Stop", "session_id": "S", "last_assistant_message": "x" * 20000}, {})
        self.assertEqual(len(evt["last_message"]), 16000)


if __name__ == "__main__":
    unittest.main()

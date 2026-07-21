"""Lightweight timestamped debug logging for the dashboard.

Writes one line per event to stderr. The dashboard server is spawned by the MCP
`launch_dashboard` tool with its stderr redirected to
``~/.cache/overboard/dashboard.log`` (see mcp_server.launch_dashboard), so these
lines are visible live with::

    tail -f ~/.cache/overboard/dashboard.log

Frontend timings go to the browser DevTools console instead. Stdlib-only, never
raises — logging must never take down a refresh.
"""

from __future__ import annotations

import sys
import time


def log(msg: str) -> None:
    try:
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


class timer:
    """Context manager that logs a phase's start and its wall-clock duration.

        with debuglog.timer("refresh: fetch commits repo=foo"):
            ...
    """

    def __init__(self, label: str):
        self.label = label

    def __enter__(self):
        self.t0 = time.perf_counter()
        log(f"→ {self.label}")
        return self

    def __exit__(self, *exc):
        dt = (time.perf_counter() - self.t0) * 1000
        log(f"✓ {self.label} ({dt:.0f} ms)")
        return False

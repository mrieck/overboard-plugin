"""Herdr socket client — the plugin's port of the Mac app's HerdrClient /
HerdrLocator / HerdrLauncher (stdlib only, works on Linux and macOS).

Wire protocol: NDJSON over a Unix domain socket. One request line
{"id", "method", "params"} (snake_case keys) → one response line
{"result": ...} or {"error": {"code", "message"}}; the server closes the
connection after answering (only events.subscribe streams — not used here).

Launch semantics (mirrors HerdrLauncher, including its live-debugged gotchas):
one reusable workspace labelled "Overboard", one tab per run, `agent.start`
kind "claude" with --permission-mode auto (never `claude -p`). `agent.start`
returns while `launch_pending` is still true and prompts are rejected with
agent_not_ready until it clears — poll `agent.get` before `agent.prompt`.
`pane.read` nests its payload under result["read"].
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

WORKSPACE_LABEL = "Overboard"
AGENT_KIND = "claude"
# How long Herdr itself waits for claude to become interactive (its cap is 300s).
STARTUP_TIMEOUT_MS = 60_000
# Marks the session as Overboard-scheduled so the plugin's launch_dashboard
# doesn't pop a browser tab from inside a scheduled run.
SESSION_ENV = {"OVERBOARD_NO_BROWSER": "1"}


class HerdrError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class HerdrUnreachable(HerdrError):
    def __init__(self, message: str):
        super().__init__("unreachable", message)


def socket_path() -> Path:
    env = os.environ.get("HERDR_SOCKET_PATH")
    if env:
        return Path(env)
    return Path.home() / ".config" / "herdr" / "herdr.sock"


def call(method: str, params: "dict | None" = None, timeout: float = 15.0) -> dict:
    """One request/response exchange. Raises HerdrError on an API error and
    HerdrUnreachable when the server isn't there."""
    payload = json.dumps({"id": "ob-" + uuid.uuid4().hex[:8],
                          "method": method, "params": params or {}})
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        try:
            sock.connect(str(socket_path()))
            sock.sendall(payload.encode() + b"\n")
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
        except (FileNotFoundError, ConnectionRefusedError) as e:
            raise HerdrUnreachable(f"herdr server not reachable at {socket_path()}: {e}")
        except socket.timeout:
            raise HerdrError("timeout", f"{method} timed out after {timeout}s")
        except OSError as e:
            raise HerdrUnreachable(f"socket error talking to herdr: {e}")
    finally:
        sock.close()

    line = b"".join(chunks).split(b"\n", 1)[0]
    if not line:
        raise HerdrError("empty_response", f"{method} returned no data")
    try:
        reply = json.loads(line)
    except ValueError:
        raise HerdrError("bad_response", f"{method} returned non-JSON data")
    err = reply.get("error")
    if err:
        raise HerdrError(err.get("code") or "error", err.get("message") or "")
    return reply.get("result") or {}


def find_binary() -> "str | None":
    for candidate in (Path.home() / ".local" / "bin" / "herdr",
                      Path("/opt/homebrew/bin/herdr"),
                      Path("/usr/local/bin/herdr")):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which("herdr")


def ensure_server() -> None:
    """Ping; if the server is down, spawn `herdr server` detached and wait for
    the socket to come up (20 × 500ms, like the Mac app)."""
    try:
        call("ping", timeout=3)
        return
    except HerdrUnreachable:
        pass
    binary = find_binary()
    if not binary:
        raise HerdrUnreachable(
            "Herdr is not installed — scheduled runs need it (https://herdr.dev)")
    subprocess.Popen([binary, "server"], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    for _ in range(20):
        time.sleep(0.5)
        try:
            call("ping", timeout=3)
            return
        except HerdrUnreachable:
            continue
    raise HerdrUnreachable("started `herdr server` but its socket never came up")


def agent_name(slot_name: str) -> str:
    """Herdr agent names are identifiers, not prose: slug + short suffix so two
    runs of one slot don't collide."""
    slug = "".join(ch if ch.isalnum() else "-" for ch in slot_name.lower())
    name = "-".join(p for p in slug.split("-") if p)[:24].rstrip("-")
    return f"{name or 'run'}-{uuid.uuid4().hex[:4]}"


def _pane_for_run(cwd: str, label: str) -> dict:
    """A pane at a shell prompt in `cwd` inside the Overboard workspace
    (created if the user closed it between runs)."""
    workspaces = call("workspace.list").get("workspaces") or []
    match = next((w for w in workspaces if w.get("label") == WORKSPACE_LABEL), None)
    if match:
        tab = call("tab.create", {"workspace_id": match["workspace_id"],
                                  "label": label, "cwd": cwd,
                                  "env": SESSION_ENV, "focus": False})
        return tab["root_pane"]
    created = call("workspace.create", {"label": WORKSPACE_LABEL, "cwd": cwd,
                                        "env": SESSION_ENV, "focus": False})
    return created["root_pane"]


def _wait_until_promptable(pane_id: str, timeout: float) -> None:
    """`agent.start` returns once the process is up, but Herdr holds
    launch_pending a beat longer and rejects prompts (agent_not_ready) until it
    clears. Note launch_pending is absent, not false, once cleared."""
    deadline = time.monotonic() + timeout
    last_seen = "no agent yet"
    while time.monotonic() < deadline:
        try:
            info = call("agent.get", {"target": pane_id}).get("agent") or {}
            if info.get("agent") is not None and info.get("launch_pending") is not True:
                return
            last_seen = ("still launching" if info.get("launch_pending")
                         else "no agent in the pane")
        except HerdrError:
            pass
        time.sleep(0.25)
    raise HerdrError("agent_not_ready",
                     f"claude was not ready to take a prompt after {int(timeout)}s — {last_seen}")


def launch(cwd: str, name: str, prompt: str, ready_timeout: float = 45.0) -> dict:
    """Start an unattended claude in a fresh tab and submit `prompt`. Returns
    {pane_id, tab_id, workspace_id, agent_name}."""
    ensure_server()
    aname = agent_name(name)
    pane = _pane_for_run(cwd, name)
    started = call("agent.start",
                   {"name": aname, "kind": AGENT_KIND, "pane_id": pane["pane_id"],
                    "args": ["--permission-mode", "auto"],
                    "timeout_ms": STARTUP_TIMEOUT_MS},
                   timeout=STARTUP_TIMEOUT_MS / 1000 + 15)
    agent = started.get("agent") or {}
    run = {"pane_id": agent.get("pane_id") or pane["pane_id"],
           "tab_id": agent.get("tab_id"),
           "workspace_id": agent.get("workspace_id"),
           "agent_name": agent.get("name") or aname}
    try:
        _wait_until_promptable(run["pane_id"], ready_timeout)
        call("agent.prompt", {"target": run["agent_name"], "text": prompt})
    except HerdrError:
        # The agent is up but wouldn't take the prompt — don't strand a live
        # session in the user's workspace.
        terminate(run["pane_id"])
        raise
    return run


def probe(pane_id: str) -> dict:
    """{"alive": bool, "state": str}. A failed poll is not proof the run ended —
    a momentarily unreachable server must not finalize a working session, so any
    error reports alive/unknown; the run timeout is the backstop."""
    try:
        agents = call("agent.list").get("agents") or []
        info = next((a for a in agents if a.get("pane_id") == pane_id), None)
        if info is None:
            return {"alive": False, "state": "ended"}
        # A pane that dropped its agent (claude exited to the shell) is finished
        # even though the pane is still around.
        if info.get("agent") is None and info.get("launch_pending") is not True:
            return {"alive": False, "state": "ended"}
        return {"alive": True, "state": info.get("agent_status") or "unknown"}
    except HerdrError:
        return {"alive": True, "state": "unknown"}


def transcript(pane_id: str, lines: int = 2000) -> "str | None":
    try:
        read = call("pane.read", {"pane_id": pane_id, "source": "recent",
                                  "lines": lines, "strip_ansi": True,
                                  "format": "text"}).get("read") or {}
        text = read.get("text")
        if text is None:
            return None
        return ("…earlier output truncated…\n" + text) if read.get("truncated") else text
    except HerdrError:
        return None


def request_exit(agent_name_: str, pane_id: str) -> None:
    try:
        call("agent.prompt", {"target": agent_name_, "text": "/exit"})
    except HerdrError:
        # It may already be halfway out; fall back to typing into the pane.
        try:
            call("pane.send_text", {"pane_id": pane_id, "text": "/exit\r"})
        except HerdrError:
            pass


def terminate(pane_id: str) -> None:
    """Closing the run's only pane closes its tab with it."""
    try:
        call("pane.close", {"pane_id": pane_id})
    except HerdrError:
        pass

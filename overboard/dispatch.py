"""The dashboard's Dispatcher: the CTO types "what should get done?" and a
short-lived `/overboard:dispatch` session works out the project, the prompt
and the time; the scheduler then runs the task and the result lands back in
the ledger. A port of the *app-originated* half of the Mac app's
DispatchCoordinator/DispatchStore/DispatchReporting — no Telegram or Slack, no
chat ids, no Keychain: the phone path is the Mac app's (paid) feature.

Layout, under ~/.cache/overboard/dispatch/ (dashboard-owned — written only by
this module inside the dashboard process; the MCP server and hooks never
touch it):

  dispatches.json            the ledger (newest first, capped)
  inbox/<id>.request.json    what the dispatcher session reads
  outbox/<id>.response.json  what it writes back (create_task / follow_up /
                             reply / reject — see commands/dispatch.md)
  archive/                   processed request/response files, for diagnosis
  tasks/<id>/result.json     the worker's report, in a folder granted to it
                             via --add-dir

Wiring: the scheduler calls `tick()` every 5s (outbox scan — replaces the Mac
app's `overboard://dispatch/wake`) and `run_committed(run)` as runs land in
history; both seams are injected by Api so scheduler.py never imports this.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path

from . import claudeplugins, promptcomplete, schedule, store

DISPATCH_DIR = store.STATE_DIR / "dispatch"
RESULT_FILE = "result.json"
HISTORY_LIMIT = 100
RECENT_TASKS = 10
RUN_NOW_WINDOW_SECS = 120          # a fire this close to now just runs
DISPATCHER_TIMEOUT_MINUTES = 15    # one file in, one file out — a hang is a bug
DEFAULT_TASK_TIMEOUT_MINUTES = 90
DISPATCH_COMMAND = "/overboard:dispatch"
FEED_LIMIT = 16

SETTLED = ("completed", "failed", "rejected", "answered", "forwarded", "cancelled")


def _now() -> datetime:
    return datetime.now()


def _iso(d: "datetime | None") -> "str | None":
    return d.isoformat(timespec="seconds") if d else None


def new_id() -> str:
    """Short hex — doubles as the request/response filename stem and the #id
    the CTO sees in the feed."""
    return uuid.uuid4().hex[:8]


# ---- pure helpers (ported verbatim where the wording matters) --------------
def with_result_contract(prompt: str, task_dir: "str | None") -> str:
    """The one-line contract every worker prompt ends with: where and how to
    report, so the dashboard can show the CTO the summary and the files.
    Single paragraph — the launcher may retype it into the terminal, where a
    hard newline submits early."""
    if not task_dir:
        return prompt
    path = os.path.join(task_dir, RESULT_FILE)
    return (prompt.strip()
            + f" When you are completely done, write a JSON file at {path} shaped like "
            + '{"summary": "<2-4 sentences for the CTO: what you did and where it is>", '
            + '"artifacts": [{"path": "/absolute/path", "kind": "video|image|document|text", '
            + '"caption": "<one line>"}], "links": ["<url>"]}'
            + " — artifacts is the deliverable: the one file the CTO asked for (the video, the image, "
            + "the post), first; add a second only if they asked for a second distinct thing. Never list "
            + "source files, scripts, JSON or config, intermediates, or things you merely changed — "
            + "mention those in the summary instead. Absolute paths. Then stop.")


def rebuilt_prompt(record: dict, follow_up: str) -> str:
    """When there's no session to continue: rebuild the situation in one
    prompt (the plugin has no parked sessions — every follow-up is this)."""
    parts = [f"Earlier, in this project, you were asked: {record.get('prompt') or record.get('message_text') or ''}"]
    result = record.get("result") or {}
    summary = (result.get("summary") or record.get("completion_message") or "").strip()
    if summary:
        parts.append(f"That finished with: {summary[:600]}")
    artifacts = [a.get("path") for a in (result.get("artifacts") or []) if a.get("path")]
    if artifacts:
        parts.append("Files from that work: " + ", ".join(artifacts))
    parts.append(f"Now the CTO wants this change: {follow_up}. Pick up from the existing "
                 "files rather than starting over.")
    return " ".join(parts).replace("\n", " ")


def fire_date(when, now: "datetime | None" = None) -> "datetime | None":
    """"now"/None/past/unparseable → None (run immediately); a future ISO-8601
    instant → that local datetime."""
    now = now or _now()
    if not isinstance(when, str):
        return None
    when = when.strip()
    if not when or when.lower() == "now":
        return None
    d = schedule.parse_iso_any(when)
    return d if d is not None and d > now else None


def known_plugins(requested, installed: list) -> list:
    """Plugin ids the dispatcher asked for that are actually installed —
    matched by id, by name, or by id prefix; the rest are dropped, not fatal.
    `requested` is agent-written: a list, or a comma/space-separated string."""
    if isinstance(requested, str):
        requested = re.split(r"[,\s]+", requested)
    out: list = []
    for raw in requested or []:
        want = str(raw).strip() if raw is not None else ""
        if not want:
            continue
        hit = next((p for p in installed if p["id"] == want), None) or next(
            (p for p in installed if p["name"] == want or p["id"].startswith(want + "@")), None)
        if hit and hit["id"] not in out:
            out.append(hit["id"])
    return out


def is_inside(path: str, root: str) -> bool:
    p = os.path.realpath(os.path.expanduser(path))
    r = os.path.realpath(os.path.expanduser(root))
    return p == r or p.startswith(r.rstrip("/") + "/")


def parse_result(raw) -> "dict | None":
    """result.json, defensively: {"summary", "artifacts": [{path, kind,
    caption}], "links": []}. Artifacts may be bare path strings."""
    if not isinstance(raw, dict):
        return None
    out = {"summary": raw.get("summary") if isinstance(raw.get("summary"), str) else None,
           "artifacts": [], "links": []}
    for a in raw.get("artifacts") or []:
        if isinstance(a, str) and a.strip():
            out["artifacts"].append({"path": a.strip(), "kind": None, "caption": None})
        elif isinstance(a, dict) and isinstance(a.get("path"), str) and a["path"].strip():
            out["artifacts"].append({"path": a["path"].strip(),
                                     "kind": a.get("kind") if isinstance(a.get("kind"), str) else None,
                                     "caption": a.get("caption") if isinstance(a.get("caption"), str) else None})
    for link in raw.get("links") or []:
        if isinstance(link, str) and link.strip():
            out["links"].append(link.strip())
    return out


def summary_text(record: dict, limit: int = 1500) -> "str | None":
    text = ((record.get("result") or {}).get("summary") or "").strip()
    body = text or (record.get("completion_message") or "").strip()
    if not body:
        return None
    return body[:limit] + "…" if len(body) > limit else body


# ---- the coordinator ------------------------------------------------------
class Dispatcher:
    """Owns the ledger + drop-box. `scheduler` is overboard.scheduler.Scheduler
    (or anything with run_ephemeral / save_slot / find_run / view); knobs
    (the Overboard run directory) are read fresh from credentials."""

    def __init__(self, scheduler, root: "Path | None" = None,
                 plugins_getter=None, knobs_getter=None):
        self.scheduler = scheduler
        self.root = Path(root) if root else DISPATCH_DIR
        self._lock = threading.RLock()
        self.records: list = []
        self._plugins_getter = plugins_getter or self._installed_plugins
        self._knobs_getter = knobs_getter or store.scheduler_knobs
        self._loaded = False

    # paths
    @property
    def inbox(self) -> Path: return self.root / "inbox"
    @property
    def outbox(self) -> Path: return self.root / "outbox"
    @property
    def archive(self) -> Path: return self.root / "archive"
    @property
    def tasks_dir(self) -> Path: return self.root / "tasks"
    @property
    def records_path(self) -> Path: return self.root / "dispatches.json"

    def task_dir(self, dispatch_id: str) -> Path:
        return self.tasks_dir / dispatch_id

    def request_path(self, dispatch_id: str) -> Path:
        return self.inbox / f"{dispatch_id}.request.json"

    def response_path(self, dispatch_id: str) -> Path:
        return self.outbox / f"{dispatch_id}.response.json"

    # ---- ledger -------------------------------------------------------------
    def load(self) -> None:
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
            try:
                with open(self.records_path) as f:
                    data = json.load(f)
                items = data.get("dispatches") if isinstance(data, dict) else None
                self.records = [r for r in (items or []) if isinstance(r, dict) and r.get("id")]
            except (FileNotFoundError, ValueError, OSError):
                self.records = []

    def _persist(self) -> None:
        with self._lock:
            del self.records[HISTORY_LIMIT:]
            store._atomic_write(self.records_path, {"version": 1, "dispatches": self.records})
            keep = {r["id"] for r in self.records}
        # Task folders of pruned records go too.
        try:
            for child in self.tasks_dir.iterdir():
                if child.is_dir() and child.name not in keep:
                    shutil.rmtree(child, ignore_errors=True)
        except OSError:
            pass

    def _find(self, dispatch_id: str) -> "dict | None":
        return next((r for r in self.records if r["id"] == dispatch_id), None)

    def _resolve_task(self, prefix: "str | None") -> "dict | None":
        """A task by #id or a unique prefix; None when nothing matches."""
        if not prefix:
            return None
        want = str(prefix).lstrip("#").strip().lower()
        if not want:
            return None
        hits = [r for r in self.records if self._is_task(r) and r["id"].lower().startswith(want)]
        exact = [r for r in hits if r["id"].lower() == want]
        if exact:
            return exact[0]
        return hits[0] if len(hits) == 1 else None

    @staticmethod
    def _is_task(record: dict) -> bool:
        return bool(record.get("task_run_ids") or record.get("task_slot_id"))

    # ---- inputs to the dispatcher session --------------------------------
    def _installed_plugins(self) -> list:
        """Every installed plugin as the request lists them (id, name,
        commands, enabled_user) so the dispatcher can pick some for the task."""
        try:
            inv = claudeplugins.inventory()
        except Exception:
            return []
        commands_by_plugin: dict = {}
        try:
            for cmd in promptcomplete.catalog(None):
                if cmd.get("plugin_id"):
                    commands_by_plugin.setdefault(cmd["plugin_id"], []).append("/" + cmd["name"])
        except Exception:
            pass
        out = []
        for pid, p in sorted((inv.get("plugins") or {}).items()):
            if not p.get("installs"):
                continue
            out.append({"id": pid, "name": p.get("name") or pid.partition("@")[0],
                        "description": None, "commands": commands_by_plugin.get(pid, []),
                        "enabled_user": bool(p.get("enabled_user"))})
        return out

    def state_label(self, record: dict, now: "datetime | None" = None) -> str:
        """What the feed (and the dispatcher's recent_tasks) shows."""
        now = now or _now()
        if self._is_task(record) and record.get("status") in ("task_created", "continuing"):
            run = None
            for rid in reversed(record.get("task_run_ids") or []):
                run = self.scheduler.find_run(rid)
                if run:
                    break
            if run and run.get("state") == "queued":
                return "queued"
            if run and run.get("state"):
                started = schedule.parse_iso_any(run.get("started_at") or "") or now
                mins = max(0, int((now - started).total_seconds() // 60))
                label = ("waiting for input" if run.get("waiting_since")
                         else run.get("herdr_state") or "working")
                if label in ("unknown", "idle"):
                    label = "working"
                return f"{label} · {mins} min in"
            if record.get("task_slot_id") and not run:
                slot = self.scheduler.slot_by_id(record["task_slot_id"])
                if slot and slot.get("enabled"):
                    try:
                        return "scheduled — " + schedule.summary(slot.get("schedule") or {})
                    except Exception:
                        return "scheduled"
        return {"completed": "finished", "failed": "failed", "cancelled": "cancelled",
                "continuing": "continuing", "task_created": "starting",
                "dispatching": "dispatching", "received": "dispatching",
                "rejected": "rejected", "forwarded": "forwarded",
                "answered": "answered"}.get(record.get("status") or "", "dispatching")

    def recent_tasks(self, now: "datetime | None" = None) -> list:
        out = []
        for r in self.records:
            if not self._is_task(r):
                continue
            out.append({"task_id": r["id"],
                        "name": r.get("task_name") or (r.get("message_text") or "")[:40],
                        "project": r.get("project_name"),
                        "status": self.state_label(r, now),
                        "summary": summary_text(r, 400),
                        "artifacts": [a["path"] for a in (r.get("result") or {}).get("artifacts") or []],
                        "finished_at": r.get("completed_at")})
            if len(out) >= RECENT_TASKS:
                break
        return out

    # ---- begin ---------------------------------------------------------------
    def begin(self, text: str) -> dict:
        """The source-agnostic half of a new request: write the request file,
        run the ephemeral dispatcher session, return the record."""
        self.load()
        text = (text or "").strip()
        if not text:
            raise ValueError("type what should get done first")
        run_dir = self._knobs_getter().get("run_directory")
        now = _now()
        record = {"id": new_id(), "received_at": _iso(now), "message_text": text,
                  "source": "app", "status": "received", "dispatcher_run_id": None,
                  "task_slot_id": None, "task_run_ids": [], "project_name": None,
                  "project_path": None, "task_name": None, "prompt": None,
                  "task_dir": None, "workspace_plugins": [], "error": None,
                  "reply_text": None, "forwarded_task_id": None, "result": None,
                  "completion_message": None, "completed_at": None, "follow_ups": []}
        if not run_dir or not Path(run_dir).expanduser().is_dir():
            record["status"] = "failed"
            record["error"] = "no Overboard run directory configured (Settings ▸ Misc)"
            with self._lock:
                self.records.insert(0, record)
                self._persist()
            return dict(record)
        request = {"version": 3, "dispatch_id": record["id"], "received_at": _iso(now),
                   "message": {"text": text, "from": None, "message_id": 0},
                   "response_path": str(self.response_path(record["id"])),
                   "root_folder": str(Path(run_dir).expanduser()),
                   "recent_tasks": self.recent_tasks(now),
                   "plugins": self._plugins_getter()}
        try:
            self.inbox.mkdir(parents=True, exist_ok=True)
            self.outbox.mkdir(parents=True, exist_ok=True)
            store._atomic_write(self.request_path(record["id"]), request)
        except OSError as e:
            record["status"] = "failed"
            record["error"] = f"couldn't write the dispatch request: {e}"
            with self._lock:
                self.records.insert(0, record)
                self._persist()
            return dict(record)
        # Ephemeral, never saved as a slot — exactly like the sweep. The
        # message text never rides in the command; only the request file's path.
        try:
            run = self.scheduler.run_ephemeral({
                "name": f"Overboard dispatch {record['id']}",
                "cwd": str(Path(run_dir).expanduser()),
                "prompt": f"{DISPATCH_COMMAND} {self.request_path(record['id'])}",
                "timeout_minutes": DISPATCHER_TIMEOUT_MINUTES})
            record["dispatcher_run_id"] = run["id"]
            record["status"] = "dispatching"
        except ValueError as e:
            record["status"] = "failed"
            record["error"] = str(e)
        with self._lock:
            self.records.insert(0, record)
            self._persist()
        return dict(record)

    # ---- responses -----------------------------------------------------------
    def tick(self) -> None:
        self.load()
        self.scan_responses()

    def scan_responses(self) -> None:
        try:
            files = sorted(p for p in self.outbox.iterdir() if p.name.endswith(".response.json"))
        except OSError:
            return
        for path in files:
            try:
                with open(path) as f:
                    response = json.load(f)
            except (ValueError, OSError):
                response = None
            if not isinstance(response, dict) or not response.get("dispatch_id"):
                self._archive_file(path)
                continue
            try:
                self.process(response)
            finally:
                self._archive(str(response.get("dispatch_id")))
                self._archive_file(path)

    def _archive_file(self, path: Path) -> None:
        try:
            self.archive.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(self.archive / path.name))
        except OSError:
            try:
                path.unlink()
            except OSError:
                pass

    def _archive(self, dispatch_id: str) -> None:
        req = self.request_path(dispatch_id)
        if req.exists():
            self._archive_file(req)

    def process(self, response: dict) -> None:
        with self._lock:
            record = self._find(str(response.get("dispatch_id")))
            if record is None or record.get("status") not in ("dispatching", "received"):
                return
            action = response.get("action")
            if action == "reject":
                record["status"] = "rejected"
                record["error"] = response.get("reason") or "no matching project"
            elif action == "reply":
                record["status"] = "answered"
                text = response.get("text")
                record["reply_text"] = text if isinstance(text, str) and text.strip() \
                    else "(the dispatcher had nothing to say)"
            elif action == "follow_up":
                self._follow_up(record, response)
            elif action == "create_task":
                self._create_task(record, response)
            else:
                record["status"] = "failed"
                record["error"] = f"malformed dispatcher response (action {action!r})"
            self._persist()

    def _follow_up(self, record: dict, response: dict) -> None:
        task = response.get("task") if isinstance(response.get("task"), dict) else {}
        prompt = (task.get("prompt") or "").strip()
        target = self._resolve_task(response.get("task_id"))
        if target is None or not prompt:
            record["status"] = "failed"
            record["error"] = "malformed follow_up response"
            return
        record["status"] = "forwarded"
        record["forwarded_task_id"] = target["id"]
        record["error"] = f"→ #{target['id']}"
        # No parked sessions in the plugin: every follow-up rebuilds the
        # situation in a fresh session of the same task.
        target.setdefault("follow_ups", []).append({"text": prompt, "received_at": _iso(_now())})
        if not target.get("project_path") or not Path(target["project_path"]).is_dir():
            target["status"] = "failed"
            target["error"] = "the task's project folder is gone — can't continue it"
            return
        try:
            task_dir = self._ensure_task_dir(target["id"])
            run = self.scheduler.run_ephemeral({
                "name": f"Dispatch: {target.get('task_name') or 'Task'}",
                "cwd": target["project_path"],
                "prompt": with_result_contract(rebuilt_prompt(target, prompt), task_dir),
                "timeout_minutes": target.get("timeout_minutes") or DEFAULT_TASK_TIMEOUT_MINUTES,
                "add_dirs": [task_dir] if task_dir else []})
            target.setdefault("task_run_ids", []).append(run["id"])
            target["status"] = "continuing"
            target["error"] = None
        except ValueError as e:
            target["status"] = "failed"
            target["error"] = str(e)

    def _ensure_task_dir(self, dispatch_id: str) -> "str | None":
        try:
            d = self.task_dir(dispatch_id)
            d.mkdir(parents=True, exist_ok=True)
            return str(d)
        except OSError:
            return None

    def _project_problem(self, path: str, create: bool, run_dir: "str | None") -> "str | None":
        """None when `path` is usable; otherwise why not. Agent-written, so
        validated like any untrusted input: an existing folder anywhere but
        the plugin cache is fine; a new folder only under the run directory."""
        for root, why in ((str(store.STATE_DIR), "the plugin cache"),
                          (str(Path.home() / ".claude" / "plugins"), "~/.claude/plugins")):
            if is_inside(path, root):
                return f"won't run inside {why}"
        p = Path(path).expanduser()
        if p.exists():
            return None if p.is_dir() else f"not a folder: {path}"
        if not create:
            return f"project folder is missing: {path}"
        if not run_dir or not is_inside(path, run_dir):
            return f"won't create {path} — new folders must live under the Overboard run directory"
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return f"couldn't create {path}: {e}"
        return None

    def _create_task(self, record: dict, response: dict) -> None:
        project = response.get("project") if isinstance(response.get("project"), dict) else None
        task = response.get("task") if isinstance(response.get("task"), dict) else None
        prompt = (task or {}).get("prompt")
        if not project or not task or not isinstance(prompt, str) or not prompt.strip():
            record["status"] = "failed"
            record["error"] = "malformed dispatcher response"
            return
        path = str(project.get("path") or "").strip()
        if not path:
            record["status"] = "failed"
            record["error"] = "malformed dispatcher response (no project path)"
            return
        run_dir = self._knobs_getter().get("run_directory")
        problem = self._project_problem(path, bool(project.get("create")), run_dir)
        if problem:
            record["status"] = "failed"
            record["error"] = problem
            return
        path = str(Path(path).expanduser())
        now = _now()
        name = (task.get("name") or "").strip() or "Task"
        task_dir = self._ensure_task_dir(record["id"])
        fire = fire_date(task.get("when"), now)
        try:
            timeout = max(5, min(24 * 60, int(task.get("timeout_minutes") or DEFAULT_TASK_TIMEOUT_MINUTES)))
        except (TypeError, ValueError):
            timeout = DEFAULT_TASK_TIMEOUT_MINUTES
        plugins = known_plugins(task.get("plugins"), self._plugins_getter())
        record.update({"project_name": project.get("name") or os.path.basename(path),
                       "project_path": path, "task_name": name, "prompt": prompt.strip(),
                       "task_dir": task_dir, "workspace_plugins": plugins,
                       "timeout_minutes": timeout})
        spec = {"name": f"Dispatch: {name}", "cwd": path,
                "prompt": with_result_contract(prompt, task_dir),
                "timeout_minutes": timeout, "add_dirs": [task_dir] if task_dir else []}
        try:
            if fire and (fire - now).total_seconds() > RUN_NOW_WINDOW_SECS:
                # A real once slot: it survives a restart and shows in the agenda.
                spec["schedule"] = {"kind": "once", "date": schedule.to_utc_iso(fire)}
                spec["enabled"] = True
                view = self.scheduler.save_slot(spec)
                slot = next((s for s in reversed(view.get("slots") or [])
                             if s.get("name") == spec["name"] and s.get("cwd") == path), None)
                record["task_slot_id"] = slot["id"] if slot else None
                record["scheduled_for"] = _iso(fire)
            else:
                run = self.scheduler.run_ephemeral(spec)
                record["task_run_ids"].append(run["id"])
            record["status"] = "task_created"
            record["error"] = None
        except ValueError as e:
            record["status"] = "failed"
            record["error"] = str(e)

    # ---- run commits ---------------------------------------------------------
    def run_committed(self, run: dict) -> None:
        """A run landed in history. The dispatcher session ending without a
        response is a failure; a task run ending records the result."""
        self.load()
        with self._lock:
            rec = next((r for r in self.records if r.get("dispatcher_run_id") == run.get("id")), None)
            if rec is not None:
                self.scan_responses()   # the response may be sitting there right now
                if rec.get("status") == "dispatching":
                    rec["status"] = "failed"
                    rec["error"] = f"dispatcher session {self._outcome_label(run)} without a response"
                    if run.get("completion_message"):
                        rec["completion_message"] = run["completion_message"]
                    self._persist()
                return
            rec = next((r for r in self.records
                        if run.get("id") in (r.get("task_run_ids") or [])
                        or (r.get("task_slot_id") and r["task_slot_id"] == run.get("slot_id"))), None)
            if rec is None or rec.get("status") not in ("task_created", "continuing"):
                return
            if run.get("id") not in rec.get("task_run_ids", []):
                rec.setdefault("task_run_ids", []).append(run["id"])
            outcome = run.get("outcome")
            if outcome == "cancelled":
                rec["status"] = "cancelled"
            elif outcome == "skipped_missed_window":
                rec["status"] = "failed"
                rec["error"] = "missed its scheduled window (the dashboard wasn't running)"
            elif outcome == "completed":
                rec["status"] = "completed"
                rec["error"] = None
                rec["completed_at"] = run.get("ended_at") or _iso(_now())
                rec["completion_message"] = run.get("completion_message")
                rec["result"] = self.read_result(rec["id"])
            else:
                rec["status"] = "failed"
                rec["error"] = self._outcome_label(run)
                if run.get("completion_message"):
                    rec["completion_message"] = run["completion_message"]
            self._persist()

    @staticmethod
    def _outcome_label(run: dict) -> str:
        return {"completed": "completed", "exited": "exited without finishing",
                "timeout": "timed out", "cancelled": "was cancelled",
                "launch_failed": "failed to launch: " + (run.get("reason") or ""),
                "skipped_missed_window": "missed its window"}.get(
                    run.get("outcome") or "", run.get("outcome") or "ended")

    def read_result(self, dispatch_id: str) -> "dict | None":
        try:
            with open(self.task_dir(dispatch_id) / RESULT_FILE) as f:
                return parse_result(json.load(f))
        except (FileNotFoundError, ValueError, OSError):
            return None

    # ---- restart reconcile -------------------------------------------------
    def restore(self) -> None:
        """After a dashboard restart: records left mid-flight whose run is
        gone are failed (the scheduler only re-adopts runs whose pane still
        lives); a task whose run finished while nobody was listening is
        reconciled through that run's history record."""
        self.load()
        with self._lock:
            snapshot = [dict(r) for r in self.records]
        to_commit = []
        for rec in snapshot:
            if rec.get("status") in ("task_created", "continuing"):
                runs = [self.scheduler.find_run(rid) for rid in rec.get("task_run_ids") or []]
                finished = [r for r in runs if r and r.get("outcome") and not r.get("state")]
                if finished and not any(r.get("state") for r in runs if r):
                    to_commit.append(finished[-1])
        for run in to_commit:
            self.run_committed(run)
        with self._lock:
            changed = False
            for rec in self.records:
                status = rec.get("status")
                if status in ("dispatching", "received"):
                    run = self.scheduler.find_run(rec.get("dispatcher_run_id") or "")
                    if run is not None and run.get("state"):
                        continue  # re-adopted by the scheduler; still going
                    self.scan_responses()
                    if rec.get("status") in ("dispatching", "received"):
                        rec["status"] = "failed"
                        rec["error"] = "interrupted — the dashboard restarted before the dispatcher answered"
                        changed = True
                elif status in ("task_created", "continuing"):
                    if rec.get("task_slot_id") and not rec.get("task_run_ids"):
                        continue  # still scheduled — the once slot carries it
                    runs = [self.scheduler.find_run(rid) for rid in rec.get("task_run_ids") or []]
                    if any(r and r.get("state") for r in runs):
                        continue
                    rec["status"] = "failed"
                    rec["error"] = "interrupted — the dashboard restarted while the task was running"
                    changed = True
            if changed:
                self._persist()

    # ---- read ----------------------------------------------------------------
    def view(self, limit: int = FEED_LIMIT) -> dict:
        self.load()
        knobs = self._knobs_getter()
        run_dir = knobs.get("run_directory")
        now = _now()
        with self._lock:
            out = []
            for r in self.records[:limit]:
                item = dict(r)
                item["state_label"] = self.state_label(r, now)
                item["summary"] = summary_text(r, 600)
                item["is_task"] = self._is_task(r)
                out.append(item)
        return {"records": out,
                "run_directory": run_dir or "",
                "run_directory_set": bool(run_dir and Path(run_dir).expanduser().is_dir()),
                "total": len(self.records)}

    def record(self, dispatch_id: str) -> "dict | None":
        self.load()
        with self._lock:
            r = self._find(dispatch_id)
            if r is None:
                return None
            item = dict(r)
            item["state_label"] = self.state_label(r)
            item["summary"] = summary_text(r)
            item["is_task"] = self._is_task(r)
            return item

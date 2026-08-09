"""Task workspaces: ~/OverboardWork/<project>/<task>/.

A workspace is a folder a scheduled claude run uses as its cwd. Its own
.claude/settings.json enables exactly the plugins that task may use (Claude
Code reads project-scope settings from the session's cwd), CLAUDE.md carries
the task brief, and work/ is the task's persistent archive — the scheduled
session reads it to see previous output and writes new results into it.

The filesystem is the registry: a workspace exists iff its workspace.json
does (written last during scaffold, so a half-created folder is invisible).
Single writer: the dashboard Api. Scheduled sessions write only work/ — the
generated CLAUDE.md tells them so.
"""

from __future__ import annotations

import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from . import claudeplugins, store

WORK_ROOT = Path.home() / "OverboardWork"
VERSION = 1
_SLUG_RE = re.compile(r"[^a-z0-9-_]+")

BRIEF_HEADER = """\
# Task: {task} ({project})

This folder is an Overboard task workspace. Your persistent archive is
`./work/` — read it before writing new output so you build on previous runs,
and save your results into it. Do not modify `.claude/`, `CLAUDE.md`, or
`workspace.json`; they belong to the Overboard dashboard.

## Brief

{brief}
"""


def _slug(text: str, what: str) -> str:
    slug = _SLUG_RE.sub("-", (text or "").strip().lower()).strip("-")
    if not slug or slug in (".", ".."):
        raise ValueError(f"not a usable {what} name: {text!r}")
    return slug


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ws_path(project: str, task: str) -> Path:
    return WORK_ROOT / project / task


def _load(manifest: Path) -> "dict | None":
    data = claudeplugins._load_json(manifest)
    if not data.get("id"):
        return None
    ws = dict(data)
    ws["path"] = str(manifest.parent)
    return ws


def list_workspaces() -> list:
    """Every valid workspace, sorted by project/task. Lossy: a corrupt
    manifest loses that workspace, not the listing."""
    out = []
    try:
        manifests = sorted(WORK_ROOT.glob("*/*/workspace.json"))
    except OSError:
        return out
    for manifest in manifests:
        ws = _load(manifest)
        if ws:
            out.append(ws)
    return out


def workspace_by_id(workspace_id: str) -> "dict | None":
    return next((w for w in list_workspaces() if w["id"] == workspace_id), None)


def _write_settings(ws_dir: Path, plugins: list, marketplaces: dict) -> None:
    """Project-scope settings enabling exactly this task's plugins, with the
    marketplaces they come from pinned (copied from Claude's own registry)."""
    extra = {}
    for plugin_id in plugins:
        _, _, mkt = plugin_id.partition("@")
        if not mkt:
            raise ValueError(f"plugin id must be name@marketplace: {plugin_id!r}")
        known = marketplaces.get(mkt)
        if known is None:
            raise ValueError(f"unknown marketplace {mkt!r} — add it first "
                             f"(claude plugin marketplace add ...)")
        if known.get("source"):
            extra[mkt] = {"source": known["source"]}
    settings = {"enabledPlugins": {pid: True for pid in plugins}}
    if extra:
        settings["extraKnownMarketplaces"] = extra
    (ws_dir / ".claude").mkdir(parents=True, exist_ok=True)
    store._atomic_write(ws_dir / ".claude" / "settings.json", settings)


def _write_brief(ws_dir: Path, project: str, task: str, brief: str) -> None:
    text = BRIEF_HEADER.format(task=task, project=project,
                               brief=(brief or "").strip() or "(no brief yet)")
    tmp = ws_dir / "CLAUDE.md"
    tmp.write_text(text)


def create(project: str, task: str, plugins: "list | None" = None,
           brief: str = "") -> dict:
    project = _slug(project, "project")
    task = _slug(task, "task")
    plugins = [p for p in (plugins or []) if isinstance(p, str) and p]
    ws_dir = _ws_path(project, task)
    if (ws_dir / "workspace.json").exists():
        raise ValueError(f"workspace already exists: {project}/{task}")
    marketplaces = claudeplugins.inventory()["marketplaces"]
    ws_dir.mkdir(parents=True, exist_ok=True)
    _write_settings(ws_dir, plugins, marketplaces)
    _write_brief(ws_dir, project, task, brief)
    (ws_dir / "work").mkdir(exist_ok=True)
    manifest = {"version": VERSION, "id": uuid.uuid4().hex, "project": project,
                "task": task, "created_at": _now_iso(), "plugins": plugins,
                "brief": brief, "slot_id": None}
    # Written last: its presence marks the workspace valid.
    store._atomic_write(ws_dir / "workspace.json", manifest)
    # Pre-warm: register each plugin at project scope via the CLI so the first
    # scheduled run doesn't hit a marketplace trust prompt.
    jobs = []
    for plugin_id in plugins:
        try:
            jobs.append(claudeplugins.plugin_action(
                "install", plugin_id, "project", cwd=str(ws_dir)))
        except ValueError as e:
            jobs.append({"plugin": plugin_id, "state": "failed", "output": str(e)})
    ws = dict(manifest)
    ws["path"] = str(ws_dir)
    ws["jobs"] = jobs
    return ws


def update(workspace_id: str, plugins: "list | None" = None,
           brief: "str | None" = None) -> dict:
    ws = workspace_by_id(workspace_id)
    if not ws:
        raise ValueError(f"no such workspace: {workspace_id}")
    ws_dir = Path(ws["path"])
    jobs = []
    if plugins is not None:
        plugins = [p for p in plugins if isinstance(p, str) and p]
        marketplaces = claudeplugins.inventory()["marketplaces"]
        _write_settings(ws_dir, plugins, marketplaces)
        for plugin_id in set(plugins) - set(ws.get("plugins") or []):
            try:
                jobs.append(claudeplugins.plugin_action(
                    "install", plugin_id, "project", cwd=str(ws_dir)))
            except ValueError as e:
                jobs.append({"plugin": plugin_id, "state": "failed", "output": str(e)})
        ws["plugins"] = plugins
    if brief is not None:
        ws["brief"] = brief
        _write_brief(ws_dir, ws["project"], ws["task"], brief)
    _persist(ws)
    ws["jobs"] = jobs
    return ws


def set_slot_link(workspace_id: str, slot_id: "str | None") -> "dict | None":
    ws = workspace_by_id(workspace_id)
    if ws:
        ws["slot_id"] = slot_id
        _persist(ws)
    return ws


def delete(workspace_id: str, keep_work: bool = False) -> None:
    """Remove a workspace. Refusal to delete while a slot links it is the
    caller's job (the Api knows the scheduler). keep_work leaves the folder
    with only work/ intact."""
    ws = workspace_by_id(workspace_id)
    if not ws:
        return
    ws_dir = Path(ws["path"])
    # Belt and braces: never delete outside the work root.
    if WORK_ROOT.resolve() not in ws_dir.resolve().parents:
        raise ValueError(f"refusing to delete outside {WORK_ROOT}: {ws_dir}")
    if keep_work:
        for name in ("workspace.json", "CLAUDE.md"):
            try:
                (ws_dir / name).unlink()
            except OSError:
                pass
        shutil.rmtree(ws_dir / ".claude", ignore_errors=True)
    else:
        shutil.rmtree(ws_dir, ignore_errors=True)
        parent = ws_dir.parent  # drop the <project> dir too once empty
        try:
            parent.rmdir()
        except OSError:
            pass


def _persist(ws: dict) -> None:
    manifest = {k: v for k, v in ws.items() if k not in ("path", "jobs")}
    store._atomic_write(Path(ws["path"]) / "workspace.json", manifest)

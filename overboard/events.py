"""Read the activity event log that the hook (hooks/emit_event.py) appends to.

The hook is stdlib-only and writes raw events keyed by `cwd`; here (in the real
venv) we read them, map `cwd` -> repo slug via localrepo, and hand them to the
manager. Same on-disk path as the hook, sourced from store.STATE_DIR."""

import json
import os
from pathlib import Path

from overboard import localrepo, store

EVENTS_PATH = store.STATE_DIR / "events.jsonl"


def read_events(since_ts: float = 0.0, limit: int = 5000) -> list[dict]:
    """All events newer than `since_ts` (most recent `limit` kept)."""
    if not EVENTS_PATH.exists():
        return []
    out: list[dict] = []
    try:
        with open(EVENTS_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("ts", 0) > since_ts:
                    out.append(e)
    except OSError:
        return []
    return out[-limit:]


def _cwd_to_slug(cwd: str, links: dict[str, str]) -> str | None:
    """Map an event's cwd to a repo slug: the slug whose local path is a prefix
    of cwd (handles subdir cwds inside a repo). Longest match wins."""
    if not cwd:
        return None
    cwd = os.path.abspath(cwd)
    best, best_len = None, -1
    for slug, path in links.items():
        p = os.path.abspath(path)
        if (cwd == p or cwd.startswith(p + os.sep)) and len(p) > best_len:
            best, best_len = slug, len(p)
    return best


def events_by_repo(state: dict, since_ts: float = 0.0) -> dict[str, list[dict]]:
    """Group events under known repos (drops events from untracked dirs).
    An explicit `repo_hint` (from MCP self-reports) wins over cwd mapping."""
    links = localrepo.links_for_machine(state)
    known = set(links)
    grouped: dict[str, list[dict]] = {}
    for e in read_events(since_ts):
        hint = e.get("repo_hint")
        slug = hint if hint in known else _cwd_to_slug(e.get("cwd", ""), links)
        if slug:
            grouped.setdefault(slug, []).append({**e, "repo": slug})
    return grouped


def append_event(evt: dict) -> None:
    """Test/helper: append an event the same way the hook does."""
    EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(EVENTS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(evt) + "\n")

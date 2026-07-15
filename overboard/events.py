"""Read the activity event log that the hook (hooks/emit_event.py) appends to.

The hook is stdlib-only and writes raw events keyed by `cwd`; here (in the real
venv) we read them, map `cwd` -> repo slug via localrepo, and hand them to the
manager. Same on-disk path as the hook, sourced from store.STATE_DIR."""

import json
import os
import tempfile
import threading
import time
from pathlib import Path

from overboard import localrepo, store

EVENTS_PATH = store.STATE_DIR / "events.jsonl"

# Retention for the raw log. Nothing reads past the dashboard's 30-day window
# (the recent feed keeps 25 events/repo and pending-work only compares fresh
# stop timestamps), so older lines are pure parse cost on every read.
RETENTION_DAYS = 30
MAX_BYTES = 5 * 1024 * 1024
_COMPACT_EVERY_S = 24 * 3600

_compact_lock = threading.Lock()
_last_compact = 0.0


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


def maybe_compact() -> bool:
    """Cheap guard around compact(): at most once per day, unless the log has
    blown past MAX_BYTES. Never raises — safe to kick from any refresh path."""
    global _last_compact
    if not _compact_lock.acquire(blocking=False):
        return False
    try:
        try:
            size = EVENTS_PATH.stat().st_size
        except OSError:
            return False
        now = time.monotonic()
        if size <= MAX_BYTES and _last_compact and now - _last_compact < _COMPACT_EVERY_S:
            return False
        _last_compact = now
        try:
            return compact()
        except Exception:
            return False
    finally:
        _compact_lock.release()


def compact(max_age_days: int = RETENTION_DAYS, max_bytes: int = MAX_BYTES) -> bool:
    """Trim the event log: drop events older than max_age_days, then, if the
    survivors still exceed max_bytes, drop oldest-first until they fit.

    Hooks append concurrently (they must never coordinate), so after writing
    the kept lines to a temp file we carry over any bytes appended past the
    offset we read to, then atomically replace. The residual race window is a
    few syscalls wide; worst case is one lost activity event."""
    cutoff = time.time() - max_age_days * 86400
    kept: list[bytes] = []
    offset = 0
    dropped = False
    try:
        with open(EVENTS_PATH, "rb") as f:
            for line in f:
                offset += len(line)
                try:
                    ts = json.loads(line).get("ts", 0)
                except (ValueError, AttributeError):
                    dropped = True  # malformed line: drop it
                    continue
                if ts > cutoff:
                    kept.append(line)
                else:
                    dropped = True
    except OSError:
        return False

    kept_bytes = sum(len(ln) for ln in kept)
    while kept and kept_bytes > max_bytes:
        kept_bytes -= len(kept.pop(0))
        dropped = True
    if not dropped:
        return False

    fd, tmp = tempfile.mkstemp(dir=str(EVENTS_PATH.parent),
                               prefix=EVENTS_PATH.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as out:
            out.writelines(kept)
            # Carry over anything hooks appended while we were reading.
            with open(EVENTS_PATH, "rb") as f:
                f.seek(offset)
                out.write(f.read())
        os.replace(tmp, EVENTS_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True

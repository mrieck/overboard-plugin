"""Per-run journal: what a session tells the Mac app while it works.

A session the Mac app launched carries OVERBOARD_RUN_ID and OVERBOARD_RUN_DIR
in its environment (the MCP server inherits both). Each tool below appends one
JSON line to <run dir>/journal.jsonl; the app tails the file and turns the
lines into assets, progress, questions and the run's summary. Append-only,
one os.write per line, never raises: outside an Overboard run every tool
returns an error *result* so a sweep on someone else's machine keeps going.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

KINDS = ("video", "image", "html", "markdown", "document", "text", "other")
_EXT_KINDS = {
    "mp4": "video", "mov": "video", "m4v": "video", "webm": "video", "mkv": "video",
    "png": "image", "jpg": "image", "jpeg": "image", "gif": "image", "webp": "image",
    "heic": "image", "svg": "image",
    "html": "html", "htm": "html",
    "md": "markdown", "markdown": "markdown",
    "pdf": "document", "docx": "document", "pptx": "document", "xlsx": "document",
    "key": "document", "numbers": "document", "rtf": "document",
    "txt": "text", "log": "text", "json": "text", "csv": "text",
}
_MAX_TEXT = 4000
NOT_IN_RUN = "not running inside an Overboard agent run (no OVERBOARD_RUN_ID)"


def _context():
    run_id = os.environ.get("OVERBOARD_RUN_ID")
    if not run_id:
        return None, None
    run_dir = os.environ.get("OVERBOARD_RUN_DIR") or str(
        Path.home() / "Library" / "Application Support" / "Overboard" / "runs" / run_id)
    return run_id, Path(run_dir)


def journal_path():
    """The current run's journal file, or None outside a run."""
    run_id, run_dir = _context()
    if not run_id:
        return None
    return run_dir / "journal.jsonl"


def _append(entry: dict) -> dict:
    run_id, run_dir = _context()
    if not run_id:
        return {"error": NOT_IN_RUN}
    entry = {"ts": time.time(), "run_id": run_id, **entry}
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(entry) + "\n").encode("utf-8")
        fd = os.open(str(run_dir / "journal.jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError as e:
        return {"error": f"could not write the run journal: {e}"}
    return entry


def infer_kind(path: str, hint=None) -> str:
    if hint in KINDS:
        return hint
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    return _EXT_KINDS.get(ext, "other")


def add_asset(path: str, kind=None, caption=None):
    """Register a file this run produced (a video, an image, an HTML page, a
    Markdown doc) so the Overboard board can show and play it."""
    if not isinstance(path, str) or not path:
        return {"error": "path is required"}
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        return {"error": "path must be absolute"}
    if not os.path.isfile(path):
        return {"error": f"no file at {path}"}
    resolved = infer_kind(path, kind)
    entry = {"type": "asset", "path": path, "kind": resolved, "bytes": os.path.getsize(path)}
    if isinstance(caption, str) and caption.strip():
        entry["caption"] = caption.strip()[:300]
    out = _append(entry)
    if "error" in out:
        return out
    return {"ok": True, "path": path, "kind": resolved}


def report_progress(text: str):
    """One line about where the run is (a milestone, not a log)."""
    if not isinstance(text, str) or not text.strip():
        return {"error": "text is required"}
    out = _append({"type": "progress", "text": text.strip()[:_MAX_TEXT]})
    return out if "error" in out else {"ok": True}


def ask_user(question: str, options=None):
    """Ask the CTO something only they can decide. Recorded for the board and
    the phone; the run must not wait for the answer."""
    if not isinstance(question, str) or not question.strip():
        return {"error": "question is required"}
    opts = []
    if isinstance(options, list):
        opts = [str(o).strip()[:80] for o in options if str(o).strip()][:5]
    qid = uuid.uuid4().hex[:8]
    out = _append({"type": "question", "id": qid, "question": question.strip()[:700], "options": opts})
    if "error" in out:
        return out
    return {"ok": True, "question_id": qid,
            "note": "Recorded. Don't wait for the answer: finish what doesn't depend on it, then stop. "
                    "The answer arrives as a follow-up prompt."}


def set_summary(text: str):
    """What the run did, for the CTO (2-4 sentences). Shown on the board and
    sent to the phone; replaces the closing message."""
    if not isinstance(text, str) or not text.strip():
        return {"error": "text is required"}
    out = _append({"type": "summary", "text": text.strip()[:_MAX_TEXT]})
    return out if "error" in out else {"ok": True}

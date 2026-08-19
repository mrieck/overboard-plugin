"""Recurrence model for scheduler slots — a Python port of the Mac app's
ScheduleSpec (same JSON shapes, hand-editable, deliberately simpler than cron):

  {"kind": "daily", "times": [{"hour": 2, "minute": 0}]}
  {"kind": "weekly", "days": [2, 6], "times": [{"hour": 9, "minute": 30}]}
  {"kind": "everyHours", "interval": 2, "window": {"startHour": 9, "endHour": 18}}
  {"kind": "once", "date": "2026-08-20T23:30:00Z"}

`once` fires a single time (the engine disables the slot after it fires); its
`date` is ISO-8601 and is stored in UTC with a Z, the way the Mac app encodes a
Date, so a slots.json written by either side reads on the other.

Weekdays are Calendar-style, 1 = Sunday … 7 = Saturday. `endHour` is exclusive
(9–18 with interval 2 fires at 9, 11, …, 17, always on minute 0).

All datetimes are naive local time — consistent with the rest of the codebase.
Across a DST spring-forward a fire can shift by an hour; that matches cron's
practical behavior and is fine for this scheduler.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone

KINDS = ("daily", "weekly", "everyHours", "once")
_WEEKDAY_NAMES = {1: "Sun", 2: "Mon", 3: "Tue", 4: "Wed", 5: "Thu", 6: "Fri", 7: "Sat"}


def validate(spec: dict) -> dict:
    """Normalize a spec, raising ValueError with a human message on bad input."""
    if not isinstance(spec, dict):
        raise ValueError("schedule must be an object")
    kind = spec.get("kind")
    if kind not in KINDS:
        raise ValueError(f"schedule kind must be one of {', '.join(KINDS)}")

    if kind == "once":
        fire = parse_iso_any(spec.get("date"))
        if fire is None:
            raise ValueError("pick a date and time for the one-off run")
        return {"kind": "once", "date": to_utc_iso(fire)}

    if kind in ("daily", "weekly"):
        times = _clean_times(spec.get("times"))
        if not times:
            raise ValueError("add at least one time (HH:MM)")
        out = {"kind": kind, "times": times}
        if kind == "weekly":
            days = sorted({int(d) for d in (spec.get("days") or []) if 1 <= int(d) <= 7})
            if not days:
                raise ValueError("pick at least one weekday")
            out["days"] = days
        return out

    try:
        interval = int(spec.get("interval") or 0)
    except (TypeError, ValueError):
        interval = 0
    if not 1 <= interval <= 24:
        raise ValueError("interval must be 1–24 hours")
    out = {"kind": "everyHours", "interval": interval}
    window = spec.get("window")
    if window:
        try:
            start, end = int(window.get("startHour")), int(window.get("endHour"))
        except (TypeError, ValueError):
            raise ValueError("window hours must be numbers")
        if not (0 <= start <= 23 and 1 <= end <= 24):
            raise ValueError("window hours must be within 0–24")
        if start >= end:
            raise ValueError("window start must be before its end")
        out["window"] = {"startHour": start, "endHour": end}
    return out


_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?"
    r"\s*(Z|z|[+-]\d{2}:?\d{2})?$")


def parse_iso_any(value) -> "datetime | None":
    """Parse an ISO-8601 datetime the way the Mac app and browsers write them —
    with or without seconds/fractions, a trailing `Z`, or a ±HH:MM offset —
    into a NAIVE LOCAL datetime (what the rest of the scheduler compares
    against). Python 3.9's fromisoformat can't take `Z`, hence the regex.
    A bare wall-clock time (no zone) is taken as local. None when unparsable."""
    if not isinstance(value, str):
        return None
    m = _ISO_RE.match(value.strip())
    if not m:
        return None
    y, mo, d, hh, mm = (int(m.group(i)) for i in range(1, 6))
    ss = int(m.group(6) or 0)
    frac = m.group(7)
    micro = int((frac + "000000")[:6]) if frac else 0
    zone = m.group(8)
    try:
        naive = datetime(y, mo, d, hh, mm, ss, micro)
    except ValueError:
        return None
    if not zone:
        return naive.replace(microsecond=0)
    if zone in ("Z", "z"):
        aware = naive.replace(tzinfo=timezone.utc)
    else:
        sign = 1 if zone[0] == "+" else -1
        digits = zone[1:].replace(":", "")
        off = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
        aware = naive.replace(tzinfo=timezone(sign * off))
    return aware.astimezone().replace(tzinfo=None, microsecond=0)


def to_utc_iso(local_naive: datetime) -> str:
    """A naive local datetime → the Mac app's wire form, e.g. 2026-08-20T23:30:00Z."""
    aware = local_naive.replace(microsecond=0).astimezone(timezone.utc)
    return aware.strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_times(times) -> list:
    out = []
    for t in times or []:
        try:
            h, m = int(t.get("hour")), int(t.get("minute"))
        except (TypeError, ValueError, AttributeError):
            raise ValueError("times must be {hour, minute} objects")
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError(f"{h}:{m:02d} is not a valid time")
        out.append({"hour": h, "minute": m})
    out.sort(key=lambda t: (t["hour"], t["minute"]))
    # Dedupe (sorted, so equal entries are adjacent).
    return [t for i, t in enumerate(out) if i == 0 or t != out[i - 1]]


def expand(spec: dict) -> list:
    """Concrete fire points as (weekday_or_None, hour, minute) tuples.
    weekday None means "every day"."""
    kind = spec.get("kind")
    if kind == "once":
        return []  # a point in time, not a recurrence — see next_fire
    if kind == "daily":
        return [(None, t["hour"], t["minute"]) for t in spec["times"]]
    if kind == "weekly":
        return [(d, t["hour"], t["minute"]) for d in spec["days"] for t in spec["times"]]
    interval = spec["interval"]
    window = spec.get("window") or {}
    start, end = window.get("startHour", 0), window.get("endHour", 24)
    return [(None, h, 0) for h in range(start, end, interval)]


def _cal_weekday(d: date) -> int:
    """ISO weekday (Mon=1…Sun=7) → Calendar weekday (Sun=1…Sat=7)."""
    return d.isoweekday() % 7 + 1


def next_fire(spec: dict, after: datetime) -> "datetime | None":
    """The next moment strictly after `after` this schedule fires, or None if it
    can never fire. Scans day by day; 9 days covers any weekly spec."""
    try:
        clean = validate(spec)
    except ValueError:
        return None
    if clean["kind"] == "once":
        fire = parse_iso_any(clean["date"])
        return fire if fire is not None and fire > after else None
    points = expand(clean)
    if not points:
        return None
    for day_offset in range(9):
        d = after.date() + timedelta(days=day_offset)
        wd = _cal_weekday(d)
        candidates = [datetime.combine(d, time(h, m))
                      for weekday, h, m in points
                      if weekday is None or weekday == wd]
        hits = [c for c in candidates if c > after]
        if hits:
            return min(hits)
    return None


def fire_dates(spec: dict, start: datetime, end: datetime, limit: int = 400) -> list:
    """Every fire strictly after `start` and at/before `end`, ascending — the
    agenda's input. Capped at `limit` so a 1-hourly slot over 10 days stays
    small."""
    out: list = []
    cursor = start
    while len(out) < limit:
        nxt = next_fire(spec, cursor)
        if nxt is None or nxt > end:
            break
        out.append(nxt)
        cursor = nxt
    return out


def _clock(t: dict) -> str:
    ampm = "am" if t["hour"] < 12 else "pm"
    h12 = t["hour"] % 12 or 12
    return f"{h12}:{t['minute']:02d} {ampm}"


def summary(spec: dict) -> str:
    kind = spec.get("kind")
    if kind == "once":
        fire = parse_iso_any(spec.get("date"))
        if fire is None:
            return "once"
        return "once at " + fire.strftime("%a %d %b, ") + _clock(
            {"hour": fire.hour, "minute": fire.minute})
    if kind == "daily":
        return "daily at " + ", ".join(_clock(t) for t in spec["times"])
    if kind == "weekly":
        days = ", ".join(_WEEKDAY_NAMES.get(d, "?") for d in spec["days"])
        return days + " at " + ", ".join(_clock(t) for t in spec["times"])
    base = f"every {spec['interval']}h"
    window = spec.get("window")
    if window:
        return base + f" between {window['startHour']}:00–{window['endHour']}:00"
    return base

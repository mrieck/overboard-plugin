"""The `once` schedule kind + fire_dates + ISO parsing (schedule.py).
Run: python3 -m unittest discover -s tests"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from overboard import schedule  # noqa: E402


class ParseIsoTests(unittest.TestCase):
    def test_local_wall_clock_without_zone(self):
        self.assertEqual(schedule.parse_iso_any("2026-08-20T23:30"),
                         datetime(2026, 8, 20, 23, 30))
        self.assertEqual(schedule.parse_iso_any("2026-08-20 23:30:15"),
                         datetime(2026, 8, 20, 23, 30, 15))

    def test_z_and_offset_and_fraction_become_local(self):
        utc = datetime(2026, 8, 20, 21, 30, tzinfo=timezone.utc)
        want = utc.astimezone().replace(tzinfo=None)
        self.assertEqual(schedule.parse_iso_any("2026-08-20T21:30:00Z"), want)
        self.assertEqual(schedule.parse_iso_any("2026-08-20T21:30:00.000Z"), want)
        self.assertEqual(schedule.parse_iso_any("2026-08-20T23:30:00+02:00"), want)
        self.assertEqual(schedule.parse_iso_any("2026-08-20T23:30:00+0200"), want)

    def test_garbage_is_none(self):
        for bad in (None, "", "tonight", "2026-13-40T00:00", 42):
            self.assertIsNone(schedule.parse_iso_any(bad))

    def test_roundtrip_through_mac_wire_form(self):
        local = datetime(2026, 8, 20, 23, 30)
        wire = schedule.to_utc_iso(local)
        self.assertTrue(wire.endswith("Z"))
        self.assertEqual(schedule.parse_iso_any(wire), local)


class OnceSpecTests(unittest.TestCase):
    def test_validate_normalizes_to_utc_z(self):
        spec = schedule.validate({"kind": "once", "date": "2026-08-20T23:30"})
        self.assertEqual(spec["kind"], "once")
        self.assertTrue(spec["date"].endswith("Z"))
        # Mac-shaped input decodes and re-encodes unchanged.
        mac = {"kind": "once", "date": "2026-08-20T21:30:00Z"}
        self.assertEqual(schedule.validate(mac), mac)

    def test_validate_rejects_missing_date(self):
        with self.assertRaises(ValueError):
            schedule.validate({"kind": "once"})
        with self.assertRaises(ValueError):
            schedule.validate({"kind": "once", "date": "whenever"})

    def test_next_fire_is_the_date_only_while_future(self):
        spec = {"kind": "once", "date": "2026-08-20T23:30"}
        self.assertEqual(schedule.next_fire(spec, datetime(2026, 8, 20, 23, 0)),
                         datetime(2026, 8, 20, 23, 30))
        self.assertIsNone(schedule.next_fire(spec, datetime(2026, 8, 20, 23, 30)))
        self.assertIsNone(schedule.next_fire(spec, datetime(2026, 8, 21, 0, 0)))

    def test_summary(self):
        spec = schedule.validate({"kind": "once", "date": "2026-08-20T23:30"})
        self.assertEqual(schedule.summary(spec), "once at Thu 20 Aug, 11:30 pm")


class FireDatesTests(unittest.TestCase):
    def test_daily_over_ten_days(self):
        spec = {"kind": "daily", "times": [{"hour": 2, "minute": 0}]}
        start = datetime(2026, 8, 19, 12, 0)
        out = schedule.fire_dates(spec, start, start + timedelta(days=10))
        self.assertEqual(len(out), 10)
        self.assertEqual(out[0], datetime(2026, 8, 20, 2, 0))
        self.assertTrue(all(a < b for a, b in zip(out, out[1:])))

    def test_weekly_and_window(self):
        spec = {"kind": "weekly", "days": [2], "times": [{"hour": 9, "minute": 0}]}  # Mondays
        start = datetime(2026, 8, 19, 0, 0)  # a Wednesday
        out = schedule.fire_dates(spec, start, start + timedelta(days=10))
        self.assertEqual(out, [datetime(2026, 8, 24, 9, 0)])
        spec = {"kind": "everyHours", "interval": 3, "window": {"startHour": 9, "endHour": 18}}
        out = schedule.fire_dates(spec, datetime(2026, 8, 19, 0, 0), datetime(2026, 8, 19, 23, 59))
        self.assertEqual([d.hour for d in out], [9, 12, 15])

    def test_once_and_cap(self):
        spec = {"kind": "once", "date": "2026-08-20T23:30"}
        start = datetime(2026, 8, 19, 0, 0)
        self.assertEqual(schedule.fire_dates(spec, start, start + timedelta(days=10)),
                         [datetime(2026, 8, 20, 23, 30)])
        self.assertEqual(schedule.fire_dates(spec, datetime(2026, 8, 21), datetime(2026, 8, 30)), [])
        hourly = {"kind": "everyHours", "interval": 1}
        self.assertEqual(len(schedule.fire_dates(hourly, start, start + timedelta(days=30), limit=50)), 50)


if __name__ == "__main__":
    unittest.main()

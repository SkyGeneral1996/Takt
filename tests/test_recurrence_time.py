import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from takt.core.models import Recurrence, Task
from takt.core.recurrence import next_after
from takt.core.store import Store


def utc(value):
    return datetime.fromisoformat(value)


class TestRecurrenceTime(unittest.TestCase):
    def test_weekly_interval_keeps_active_week_days(self):
        rec = Recurrence("weekly", interval=2, weekdays=[0, 3])
        self.assertEqual(next_after(rec, utc("2026-11-23T09:00:00+00:00")),
                         utc("2026-11-26T09:00:00+00:00"))

    def test_weekly_interval_skips_inactive_weeks(self):
        rec = Recurrence("weekly", interval=2, weekdays=[0, 3])
        self.assertEqual(next_after(rec, utc("2026-11-19T09:00:00+00:00")),
                         utc("2026-11-30T09:00:00+00:00"))

    def test_monthly_anchor_survives_short_month(self):
        rec = Recurrence("monthly")
        feb = next_after(rec, utc("2026-01-31T09:00:00+00:00"))
        mar = next_after(rec, feb)
        self.assertEqual(feb, utc("2026-02-28T09:00:00+00:00"))
        self.assertEqual(mar, utc("2026-03-31T09:00:00+00:00"))
        self.assertEqual(rec.anchor_day, 31)

    def test_daily_preserves_wall_time_across_dst(self):
        rec = Recurrence("daily", timezone="America/New_York")
        result = next_after(rec, utc("2026-03-07T14:00:00+00:00"))
        self.assertEqual(result, utc("2026-03-08T13:00:00+00:00"))
        self.assertEqual(result.astimezone(ZoneInfo("America/New_York")).hour, 9)

    def test_invalid_timezone_is_rejected(self):
        with self.assertRaises(ValueError):
            next_after(Recurrence("daily", timezone="Not/AZone"),
                       utc("2026-11-23T09:00:00+00:00"))

    def test_store_persists_recurrence_anchor_and_timezone(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "data.db", device_id="devA")
            try:
                task = store.save_task(Task(
                    title="Месяц", due_at="2026-01-31T09:00:00+00:00",
                    recurrence=Recurrence("monthly", timezone="UTC").to_dict()))
                with patch("takt.core.store.now_iso",
                           return_value="2026-01-31T09:00:00.000+00:00"):
                    store.complete_task(task.id)
                saved = store.get_task(task.id)
                self.assertEqual(saved.recurrence["anchor_day"], 31)
                self.assertEqual(saved.recurrence["timezone"], "UTC")
                self.assertEqual(saved.due_at, "2026-02-28T09:00:00.000+00:00")
            finally:
                store.close()

if __name__ == "__main__":
    unittest.main()

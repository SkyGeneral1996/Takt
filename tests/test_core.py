import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from takt.core.models import Task, Category, Recurrence, now_iso, now_utc, parse_iso
from takt.core.recurrence import next_after
from takt.core.store import Store


def dt(y, m, d, h=9):
    return datetime(y, m, d, h, 0, tzinfo=timezone.utc)


class TestRecurrence(unittest.TestCase):
    def test_daily(self):
        rec = Recurrence("daily")
        self.assertEqual(next_after(rec, dt(2026, 11, 23)), dt(2026, 11, 24))

    def test_every_n_days(self):
        rec = Recurrence("daily", interval=3)
        self.assertEqual(next_after(rec, dt(2026, 11, 23)), dt(2026, 11, 26))

    def test_weekdays_skips_weekend(self):
        rec = Recurrence("weekdays")
        friday = dt(2026, 11, 20)
        self.assertEqual(friday.weekday(), 4)
        self.assertEqual(next_after(rec, friday), dt(2026, 11, 23))  # понедельник

    def test_weekly_same_day(self):
        rec = Recurrence("weekly")
        self.assertEqual(next_after(rec, dt(2026, 11, 23)), dt(2026, 11, 30))

    def test_weekly_selected_days(self):
        rec = Recurrence("weekly", weekdays=[0, 2, 4])  # Пн Ср Пт
        monday = dt(2026, 11, 23)
        self.assertEqual(next_after(rec, monday), dt(2026, 11, 25))  # среда

    def test_monthly_clamp(self):
        rec = Recurrence("monthly")
        self.assertEqual(next_after(rec, dt(2026, 1, 31)), dt(2026, 2, 28))

    def test_monthly_year_rollover(self):
        rec = Recurrence("monthly")
        self.assertEqual(next_after(rec, dt(2026, 12, 15)), dt(2027, 1, 15))

    def test_time_preserved(self):
        rec = Recurrence("daily")
        self.assertEqual(next_after(rec, dt(2026, 11, 23, 18)).hour, 18)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "test.db", device_id="devA")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_crud_and_changelog(self):
        t = self.store.save_task(Task(title="Купить молоко"))
        self.assertEqual(self.store.get_task(t.id).title, "Купить молоко")
        events = self.store.local_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["entity"], "task")
        self.store.delete_task(t.id)
        self.assertIsNone(self.store.get_task(t.id))
        self.assertEqual(len(self.store.local_events()), 2)
        self.assertTrue(self.store.local_events()[-1]["payload"]["deleted"])

    def test_complete_simple(self):
        t = self.store.save_task(Task(title="Разовая"))
        done = self.store.complete_task(t.id)
        self.assertTrue(done.completed)
        self.assertEqual(len(self.store.completions_for(t.id)), 1)

    def test_complete_recurring_advances(self):
        base = now_utc() - timedelta(days=1)
        t = self.store.save_task(Task(
            title="Зарядка",
            due_at=base.isoformat(),
            recurrence=Recurrence("daily").to_dict(),
            subtasks=[{"id": "s1", "title": "разминка", "done": True}]))
        nxt = self.store.complete_task(t.id)
        self.assertFalse(nxt.completed)
        self.assertEqual(parse_iso(nxt.due_at).date(),
                         (now_utc() + timedelta(days=1)).date())
        self.assertFalse(nxt.subtasks[0]["done"])
        self.assertEqual(len(self.store.completions_for(t.id)), 1)

    def test_complete_recurring_catches_up(self):
        t = self.store.save_task(Task(
            title="Старая",
            due_at=dt(2020, 1, 1).isoformat(),
            recurrence=Recurrence("daily").to_dict()))
        nxt = self.store.complete_task(t.id)
        self.assertGreater(parse_iso(nxt.due_at), parse_iso(now_iso()))

    def test_categories(self):
        c = self.store.save_category(Category(name="Работа"))
        self.assertEqual(self.store.list_categories()[0].name, "Работа")
        self.store.delete_category(c.id)
        self.assertEqual(self.store.list_categories(), [])


class TestSyncMerge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.a = Store(Path(self.tmp.name) / "a.db", device_id="devA")
        self.b = Store(Path(self.tmp.name) / "b.db", device_id="devB")

    def tearDown(self):
        self.a.close()
        self.b.close()
        self.tmp.cleanup()

    def sync(self, src, dst):
        return dst.apply_remote_events(src.local_events())

    def test_replication(self):
        t = self.a.save_task(Task(title="С устройства A"))
        self.assertEqual(self.sync(self.a, self.b), 1)
        self.assertEqual(self.b.get_task(t.id).title, "С устройства A")
        # повторное применение идемпотентно
        self.assertEqual(self.sync(self.a, self.b), 0)

    def test_lww_conflict(self):
        t = self.a.save_task(Task(title="Версия 1"))
        self.sync(self.a, self.b)
        tb = self.b.get_task(t.id)
        tb.title = "Правка на B"
        self.b.save_task(tb)
        ta = self.a.get_task(t.id)
        ta.title = "Более поздняя правка на A"
        self.a.save_task(ta)
        self.sync(self.b, self.a)
        self.sync(self.a, self.b)
        self.assertEqual(self.a.get_task(t.id).title, "Более поздняя правка на A")
        self.assertEqual(self.b.get_task(t.id).title, "Более поздняя правка на A")

    def test_delete_propagates(self):
        t = self.a.save_task(Task(title="Удаляемая"))
        self.sync(self.a, self.b)
        self.a.delete_task(t.id)
        self.sync(self.a, self.b)
        self.assertIsNone(self.b.get_task(t.id))


if __name__ == "__main__":
    unittest.main()
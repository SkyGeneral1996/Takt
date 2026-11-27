import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from takt.core.habits import week_checks
from takt.core.lists import item_sum
from takt.core.models import CheckList, Habit, Task, new_list_item
from takt.core.store import Store
from takt.ui.helpers import dialog_height, dialog_width, is_overdue, is_today, parse_user_datetime


class TestTimeAndNumbers(unittest.TestCase):
    def test_dialog_dimensions_fit_small_screens(self):
        page = SimpleNamespace(width=390, height=844)
        self.assertEqual(dialog_width(page, 440), 358)
        self.assertEqual(dialog_height(page, 560), 560)
        self.assertEqual(dialog_height(SimpleNamespace(height=500), 560), 340)
        self.assertEqual(dialog_height(SimpleNamespace(), 560), 560)

    def test_date_without_time_means_end_of_local_day(self):
        today = date.today()
        value = parse_user_datetime(today.strftime("%Y-%m-%d"))
        self.assertEqual(value.astimezone().date(), today)
        self.assertEqual(value.astimezone().hour, 23)
        self.assertEqual(value.astimezone().minute, 59)
        self.assertTrue(is_today(value.isoformat()))
        self.assertFalse(is_overdue(value.isoformat()))

    def test_zero_quantity_is_preserved(self):
        item = new_list_item(qty=0, price=10)
        self.assertEqual(item_sum(item), 0)
        checklist = CheckList(items=[item])
        self.assertEqual(checklist.total, 0)

    def test_future_scheduled_day_is_not_marked_missed(self):
        today = date(2026, 11, 24)
        habit = Habit(name="Будущее", weekdays=[0, 1, 2, 3, 4])
        self.assertEqual(week_checks(habit, today), [False, False, None, None, None, None, None])

    def test_completion_range_uses_instants(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "data.db", device_id="devA")
            try:
                task = store.save_task(Task(title="Смещение"))
                store.complete_task(task.id)
                at = store.completions_for(task.id)[0]
                instant = datetime.fromisoformat(at)
                start = (instant - timedelta(seconds=1)).isoformat()
                end = (instant + timedelta(seconds=1)).isoformat()
                self.assertEqual(len(store.completions_between(start, end)), 1)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()

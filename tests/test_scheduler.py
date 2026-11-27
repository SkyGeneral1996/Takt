import tempfile
import threading
import unittest
from pathlib import Path

from takt.core.models import Task
from takt.core.scheduler import ReminderScheduler
from takt.core.store import Store


class TestScheduler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data.db", device_id="devA")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def overdue(self, title="Просрочена"):
        return self.store.save_task(Task(title=title, due_at="2020-01-01T00:00:00+00:00"))

    def test_success_is_not_repeated(self):
        task = self.overdue()
        calls = []
        scheduler = ReminderScheduler(self.store, calls.append)
        self.assertEqual(scheduler.check_once(), 1)
        self.assertEqual(scheduler.check_once(), 0)
        self.assertEqual([item.id for item in calls], [task.id])

    def test_callback_failure_is_retried(self):
        task = self.overdue()
        calls = []

        def fail_once(item):
            calls.append(item)
            if len(calls) == 1:
                raise RuntimeError("notification failed")

        scheduler = ReminderScheduler(self.store, fail_once)
        self.assertEqual(scheduler.check_once(), 0)
        self.assertEqual(scheduler.check_once(), 1)
        self.assertEqual(len(calls), 2)

    def test_bad_due_date_does_not_block_other_tasks(self):
        self.store.save_task(Task(title="Плохая", due_at="not-a-date"))
        good = self.overdue("Хорошая")
        calls = []
        scheduler = ReminderScheduler(self.store, calls.append)
        self.assertEqual(scheduler.check_once(), 1)
        self.assertEqual([item.id for item in calls], [good.id])

    def test_concurrent_checks_claim_once(self):
        self.overdue()
        calls = []
        entered = threading.Event()
        release = threading.Event()

        def notify(task):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("notification was not released")
            calls.append(task)

        scheduler = ReminderScheduler(self.store, notify)
        results = []
        first = threading.Thread(target=lambda: results.append(scheduler.check_once()))
        second = threading.Thread(target=lambda: results.append(scheduler.check_once()))
        first.start()
        self.assertTrue(entered.wait(5))
        second.start()
        second.join(5)
        self.assertEqual(results, [0])
        release.set()
        first.join(5)
        self.assertFalse(first.is_alive())
        self.assertEqual(sorted(results), [0, 1])
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()

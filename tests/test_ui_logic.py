import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from takt.core.models import Task, Category, now_utc
from takt.core.store import Store


class FakePage:
    def __init__(self):
        self.title = ""
        self.theme = None
        self.theme_mode = None
        self.padding = 0
        self.appbar = None
        self.floating_action_button = None
        self.controls = []

    def add(self, *controls):
        self.controls.extend(controls)

    def update(self):
        pass

    def show_dialog(self, dialog):
        pass


class TestUiLogic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "ui.db", device_id="devU")
        from takt.ui.app import TaktApp
        self.app = TaktApp(FakePage(), self.store)
        self.app.scheduler.stop()

        now = now_utc()
        self.today = self.store.save_task(Task(
            title="Купить молоко", tags=["дом"],
            due_at=(now + timedelta(hours=2)).isoformat()))
        self.overdue = self.store.save_task(Task(
            title="Позвонить врачу",
            due_at=(now - timedelta(days=1)).isoformat()))
        self.done = self.store.save_task(Task(title="Старая выполненная"))
        self.store.complete_task(self.done.id)
        self.future = self.store.save_task(Task(
            title="Отчёт через неделю",
            due_at=(now + timedelta(days=7)).isoformat()))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def titles(self):
        return {t.title for t in self.app.visible_tasks()}

    def test_all_shows_active_only(self):
        self.assertEqual(self.titles(),
                         {"Купить молоко", "Позвонить врачу", "Отчёт через неделю"})

    def test_today(self):
        self.app.set_filter("today")
        self.assertEqual(self.titles(), {"Купить молоко"})

    def test_overdue(self):
        self.app.set_filter("overdue")
        self.assertEqual(self.titles(), {"Позвонить врачу"})

    def test_done(self):
        self.app.set_filter("done")
        self.assertEqual(self.titles(), {"Старая выполненная"})

    def test_search(self):
        self.app.search = "молок"
        self.assertEqual(self.titles(), {"Купить молоко"})

    def test_search_by_tag(self):
        self.app.search = "дом"
        self.assertEqual(self.titles(), {"Купить молоко"})

    def test_category_filter(self):
        cat = self.store.save_category(Category(name="Работа"))
        t = self.store.get_task(self.future.id)
        t.category_id = cat.id
        self.store.save_task(t)
        self.app.category_filter = cat.id
        self.assertEqual(self.titles(), {"Отчёт через неделю"})

    def test_sort_overdue_first(self):
        tasks = self.app.visible_tasks()
        self.assertEqual(tasks[0].title, "Позвонить врачу")


if __name__ == "__main__":
    unittest.main()
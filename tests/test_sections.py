import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from takt.core.models import Task, CheckList, Habit, Note, new_list_item, now_utc
from takt.core.store import Store
from takt.core import habits as habits_core
from takt.core import lists as lists_core
from takt.transfer import build_payload


def texts(control):
    out = []
    value = getattr(control, "value", None)
    if not isinstance(value, str):
        value = getattr(control, "text", None)
    if isinstance(value, str):
        out.append(value)
    for child in getattr(control, "controls", []) or []:
        out.extend(texts(child))
    content = getattr(control, "content", None)
    if isinstance(content, str):
        out.append(content)
    elif content is not None:
        out.extend(texts(content))
    return out


class FakePage:
    def __init__(self):
        self.title = ""
        self.theme = None
        self.theme_mode = None
        self.padding = 0
        self.appbar = None
        self.floating_action_button = None
        self.navigation_bar = None
        self.controls = []
        self.services = []
        self.dialogs = []
        self.width = 500
        self.height = 800
        self.window = SimpleNamespace(icon=None)

    def add(self, *controls):
        self.controls.extend(controls)

    def update(self):
        pass

    def show_dialog(self, dialog):
        self.dialogs.append(dialog)

    def pop_dialog(self):
        if self.dialogs:
            self.dialogs.pop()


class CoreSectionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "s.db", device_id="devS")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_template_and_sort(self):
        items = lists_core.template_items("Покупки")
        self.assertEqual(len(items), 6)
        self.assertEqual(items[0]["group"], "Молочное")
        items[0]["done"] = True
        ordered = lists_core.sorted_items(items)
        self.assertIs(ordered[-1], items[0])
        groups = [i["group"] for i in ordered[:-1]]
        self.assertEqual(groups, sorted(groups))

    def test_item_sum_and_total(self):
        lst = CheckList(name="Покупки", items=[
            new_list_item(text="Молоко", qty=2, price=80),
            new_list_item(text="Хлеб", qty=1, price=None),
        ])
        self.assertEqual(lists_core.item_sum(lst.items[0]), 160)
        self.assertEqual(lst.total, 160)
        self.assertEqual(lst.progress, (0, 2))

    def test_reset_list_cycles(self):
        lst = self.store.save_list(CheckList(name="L", items=[
            new_list_item(text="a"), new_list_item(text="b")]))
        lst.items[0]["done"] = True
        self.store.save_list(lst)
        reset = self.store.reset_list(lst.id)
        self.assertEqual(reset.cycles, 1)
        self.assertEqual(reset.progress, (0, 2))

    def test_streak_daily(self):
        today = date(2026, 11, 24)
        h = Habit(name="h", checks={
            today.isoformat(): True,
            (today - timedelta(days=1)).isoformat(): True,
            (today - timedelta(days=3)).isoformat(): True,
        })
        self.assertEqual(habits_core.current_streak(h, today), 2)

    def test_streak_today_unchecked(self):
        today = date(2026, 11, 24)
        h = Habit(name="h", checks={
            (today - timedelta(days=1)).isoformat(): True,
            (today - timedelta(days=2)).isoformat(): True,
        })
        self.assertEqual(habits_core.current_streak(h, today), 2)

    def test_streak_weekdays_skips_unscheduled(self):
        today = date(2026, 11, 24)  # вторник
        h = Habit(name="h", freq="weekdays", weekdays=[0, 1, 2, 3, 4], checks={
            today.isoformat(): True,
            (today - timedelta(days=1)).isoformat(): True,
            (today - timedelta(days=4)).isoformat(): True,  # пятница
        })
        self.assertEqual(habits_core.current_streak(h, today), 3)

    def test_week_checks(self):
        today = date(2026, 11, 24)
        h = Habit(name="h", freq="weekdays", weekdays=[0, 1], checks={
            today.isoformat(): True})
        week = habits_core.week_checks(h, today)
        self.assertEqual(week, [False, True, None, None, None, None, None])

    def test_notes_pinned_first(self):
        old = self.store.save_note(Note(title="old pinned", pinned=True))
        self.store.save_note(Note(title="new plain"))
        notes = self.store.list_notes()
        self.assertEqual(notes[0].id, old.id)

    def test_payload_only_list(self):
        lst = self.store.save_list(CheckList(name="share"))
        self.store.save_note(Note(title="private"))
        self.store.save_task(Task(title="task"))
        payload = build_payload(self.store, only={("list", lst.id)})
        self.assertEqual(len(payload["events"]), 1)
        self.assertEqual(payload["events"][0]["entity"], "list")
        self.assertEqual(payload["events"][0]["entity_id"], lst.id)


class ViewsSmokeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "v.db", device_id="devV")
        from takt.ui.app import TaktApp
        self.app = TaktApp(FakePage(), self.store)
        self.app.scheduler.stop()
        self.store.save_list(CheckList(name="Покупки",
                                       items=lists_core.template_items("Покупки")))
        self.store.save_habit(Habit(name="Зарядка"))
        self.store.save_note(Note(title="Заметка", text="текст", pinned=True))
        self.store.save_task(Task(title="Задача",
                                  due_at=now_utc().isoformat()))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_sections_build(self):
        from takt.ui import (lists_view, habits_view, notes_view, stats_view,
                             calendar_view, dashboard_view)
        for builder in (lists_view.build, habits_view.build, notes_view.build,
                        stats_view.build, calendar_view.build_full,
                        dashboard_view.build):
            control = builder(self.app)
            self.assertIsNotNone(control)
        self.assertIsNotNone(calendar_view.mini(self.app))

    def test_dashboard_shows_overdue_and_upcoming(self):
        from takt.ui import dashboard_view
        self.store.complete_task(self.store.list_tasks()[0].id)
        past = (now_utc() - timedelta(days=2)).isoformat()
        future = (now_utc() + timedelta(days=1)).isoformat()
        self.store.save_task(Task(title="Просроченная", due_at=past))
        self.store.save_task(Task(title="Завтрашняя", due_at=future))
        self.store.save_task(Task(title="Без срока"))
        found = texts(dashboard_view.build(self.app))
        self.assertTrue(any("Просрочено: 1" in t for t in found))
        self.assertTrue(any("Просроченная" in t for t in found))
        self.assertTrue(any("Завтрашняя" in t for t in found))
        self.assertTrue(any("Без срока" in t for t in found))
        self.assertTrue(any("Ближайшие задачи" in t for t in found))

    def test_mobile_nav_labels_fit(self):
        import flet as ft
        self.app.page.width = 500
        self.app._build_nav()
        self.assertEqual(self.app.nav.label_behavior,
                         ft.NavigationBarLabelBehavior.ALWAYS_HIDE)
        self.app.page.width = 700
        self.app._build_nav()
        self.assertEqual(self.app.nav.label_behavior,
                         ft.NavigationBarLabelBehavior.ALWAYS_SHOW)
        self.app.page.width = 1200
        self.app._build_nav()
        self.assertIsNone(self.app.page.navigation_bar)

    def test_tasks_wide_calendar_sidebar(self):
        import flet as ft
        self.app.page.width = 1200
        self.app.show_section("tasks")
        row = self.app.content.controls[0]
        self.assertIsInstance(row, ft.Row)
        self.assertEqual(row.controls[1].width, 320)
        self.app.page.width = 500
        self.app.show_section("tasks")
        self.assertIs(self.app.content.controls[0], self.app.mini_holder)

    def test_resize_rerenders_responsive_content(self):
        import flet as ft
        self.app.page.width = 1200
        self.app.show_section("tasks")
        self.assertIsInstance(self.app.content.controls[0], ft.Row)
        self.app.page.width = 500
        self.app._on_resized(None)
        self.assertIs(self.app.content.controls[0], self.app.mini_holder)
        self.app.page.width = 1200
        self.app._on_resized(None)
        self.assertIsInstance(self.app.content.controls[0], ft.Row)

    def test_notes_search_control_is_reused(self):
        from takt.ui import notes_view
        first = notes_view.build(self.app).controls[0]
        self.app.notes_search = "заметк"
        second = notes_view.build(self.app).controls[0]
        self.assertIs(first, self.app.notes_search_f)
        self.assertIs(second, first)
        self.assertEqual(second.value, "заметк")

    def test_mobile_week_calendar_scrolls(self):
        import flet as ft
        from takt.ui import calendar_view
        self.app.page.width = 500
        self.app.cal_week = True
        column = calendar_view.build_full(self.app)
        grid = column.controls[1]
        self.assertIsInstance(grid, ft.Row)
        self.assertEqual(grid.scroll, ft.ScrollMode.AUTO)
        self.assertTrue(all(cell.width == 120 and not cell.expand
                            for cell in grid.controls))

    def test_settings_exposes_backup_and_categories(self):
        from takt.ui.settings import open_settings
        open_settings(self.app)
        dialog = self.app.page.dialogs[-1]
        labels = texts(dialog)
        self.assertTrue(any("Резервная копия" in text for text in labels))
        self.assertTrue(any("Скачать копию" in text for text in labels))
        self.assertTrue(any("Категории" in text for text in labels))
        self.assertEqual(dialog.content.height, 620)
        self.assertTrue(dialog.content.content.expand)
        self.assertEqual(len(self.app.page.services), 1)
        dialog.actions[0].on_click(None)
        self.assertEqual(self.app.page.services, [])

    def test_category_manager_creates_category(self):
        import flet as ft
        from takt.ui.editor import open_category_manager
        open_category_manager(self.app)
        dialog = self.app.page.dialogs[-1]
        form = dialog.content.content.controls[0]
        form.controls[0].value = "Новая группа"
        dialog.content.content.controls[1].controls[0].on_click(None)
        self.assertEqual([c.name for c in self.store.list_categories()], ["Новая группа"])
        self.assertIsInstance(dialog, ft.AlertDialog)

    def test_task_delete_requires_confirmation(self):
        from takt.ui.editor import open_task_editor
        task = self.store.list_tasks()[0]
        open_task_editor(self.app, task)
        editor = self.app.page.dialogs[-1]
        editor.actions[0].on_click(None)
        confirmation = self.app.page.dialogs[-1]
        self.assertIsNotNone(self.store.get_task(task.id))
        confirmation.actions[1].on_click(None)
        self.assertIsNone(self.store.get_task(task.id))

    def test_subtask_can_be_renamed(self):
        import flet as ft
        from takt.ui.editor import open_task_editor
        task = self.store.list_tasks()[0]
        task.subtasks = [{"id": "sub1", "title": "Старое", "done": False}]
        self.store.save_task(task)
        open_task_editor(self.app, task)
        editor = self.app.page.dialogs[-1]
        self.assertEqual(editor.content.height, 620)
        column = editor.content.content
        subtasks = next(control for control in column.controls
                        if isinstance(control, ft.Column) and control.controls
                        and isinstance(control.controls[0], ft.Row))
        title_f = subtasks.controls[0].controls[1]
        title_f.value = "Новое"
        title_f.on_change(SimpleNamespace(control=title_f))
        editor.actions[-1].on_click(None)
        self.assertEqual(self.store.get_task(task.id).subtasks[0]["title"], "Новое")

    def test_stats_summary(self):
        from takt.ui import stats_view
        t = self.store.list_tasks()[0]
        self.store.complete_task(t.id)
        today = now_utc().astimezone().date().isoformat()
        h = self.store.list_habits()[0]
        self.store.check_habit(h.id, today)
        s = stats_view._summary(self.app, 7)
        self.assertEqual(s["completed"], 1)
        self.assertEqual(s["habits"], 1)
        self.assertEqual(s["active"], 0)
        cats = stats_view._by_category(self.app, s["comps"])
        self.assertEqual(cats[0][0], "Без категории")

    def test_stats_tiles_not_expanded_in_wrap_row(self):
        from takt.ui import stats_view
        col = stats_view.build(self.app)
        tiles_row = col.controls[1]
        self.assertTrue(tiles_row.wrap)
        for tile in tiles_row.controls:
            self.assertFalse(tile.expand)

    def test_notes_search(self):
        from takt.ui import notes_view
        self.app.notes_search = "заметк"
        self.assertTrue(any("Заметка" in t for t in texts(notes_view.build(self.app))))
        self.app.notes_search = "нет такого"
        self.assertTrue(any("Нет заметок" in t
                            for t in texts(notes_view.build(self.app))))

    def test_calendar_day_filter(self):
        from takt.ui import calendar_view
        t = self.store.list_tasks()[0]
        day = calendar_view.due_local_date(t.due_at)
        self.app.day_filter = day
        self.assertEqual([x.title for x in self.app.visible_tasks()], ["Задача"])
        self.app.day_filter = "2000-01-01"
        self.assertEqual(self.app.visible_tasks(), [])


if __name__ == "__main__":
    unittest.main()
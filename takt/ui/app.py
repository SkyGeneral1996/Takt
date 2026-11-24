from __future__ import annotations

import os
import threading
from pathlib import Path

import flet as ft

from ..core.models import Task, parse_iso
from ..core.recurrence import describe, Recurrence
from ..core.scheduler import ReminderScheduler
from ..core.store import Store
from ..sync import YandexDiskProvider, SyncEngine
from .editor import open_task_editor
from .helpers import PRIORITY_COLORS, fmt_due, is_overdue, is_today
from .settings import open_settings, check_updates_silent

FILTERS = [("all", "Все"), ("today", "Сегодня"), ("overdue", "Просроченные"),
           ("done", "Выполненные")]


def data_home() -> Path:
    home = Path(os.environ.get("TAKT_HOME", str(Path.home() / ".takt")))
    home.mkdir(parents=True, exist_ok=True)
    return home


class TaktApp:
    def __init__(self, page: ft.Page, store: Store):
        self.page = page
        self.store = store
        self.filter = "all"
        self.search = ""
        self.category_filter = ""

        page.title = "Takt — задачи"
        page.theme = ft.Theme(color_scheme_seed=ft.Colors.INDIGO)
        page.theme_mode = (ft.ThemeMode.DARK
                           if store.get_meta("theme") == "dark" else ft.ThemeMode.LIGHT)
        page.padding = 12

        self.search_f = ft.TextField(
            hint_text="Поиск по названию, заметкам, тегам…",
            prefix_icon=ft.Icons.SEARCH, dense=True, expand=True,
            on_change=self.on_search, on_submit=self.on_search,
            on_blur=self.on_search)
        self.filter_row = ft.Row(spacing=4, wrap=True)
        self.cat_dd = ft.Dropdown(
            hint_text="Категория", dense=True, width=200,
            on_select=self.on_category_filter)
        self.list_col = ft.Column(spacing=6, expand=True, scroll=ft.ScrollMode.AUTO)
        self.empty_t = ft.Text("", color=ft.Colors.GREY, text_align=ft.TextAlign.CENTER)

        self.theme_btn = ft.IconButton(
            ft.Icons.DARK_MODE if page.theme_mode == ft.ThemeMode.LIGHT
            else ft.Icons.LIGHT_MODE,
            tooltip="Сменить тему", on_click=self.toggle_theme)
        page.appbar = ft.AppBar(
            title=ft.Text("Takt", weight=ft.FontWeight.BOLD),
            center_title=False,
            actions=[
                ft.IconButton(ft.Icons.SYNC, tooltip="Синхронизировать",
                              on_click=lambda e: self.sync_now()),
                self.theme_btn,
                ft.IconButton(ft.Icons.SETTINGS, tooltip="Настройки",
                              on_click=lambda e: open_settings(self)),
            ],
        )
        page.floating_action_button = ft.FloatingActionButton(
            icon=ft.Icons.ADD, tooltip="Новая задача",
            on_click=lambda e: open_task_editor(self))

        page.add(ft.Row([self.search_f]), ft.Row([self.filter_row, self.cat_dd],
                 alignment=ft.MainAxisAlignment.SPACE_BETWEEN), self.empty_t, self.list_col)

        self.scheduler = ReminderScheduler(store, self.on_reminder)
        self.scheduler.start()
        self.refresh()
        if store.get_meta("yandex_token"):
            threading.Thread(target=self._sync_worker, daemon=True).start()
        check_updates_silent(self)

    # ---------- уведомления ----------

    def notify(self, message: str) -> None:
        self.page.show_dialog(ft.SnackBar(ft.Text(message)))
        self.page.update()

    def on_reminder(self, task: Task) -> None:
        self.notify(f"Напоминание: {task.title} (срок {fmt_due(task.due_at)})")

    # ---------- фильтры ----------

    def on_search(self, e):
        self.search = (e.control.value or "").lower().strip()
        self.refresh()

    def on_category_filter(self, e):
        self.category_filter = e.control.value or ""
        self.refresh()

    def set_filter(self, key: str):
        self.filter = key
        self.refresh()

    def toggle_theme(self, e):
        dark = self.page.theme_mode == ft.ThemeMode.LIGHT
        self.page.theme_mode = ft.ThemeMode.DARK if dark else ft.ThemeMode.LIGHT
        self.theme_btn.icon = ft.Icons.LIGHT_MODE if dark else ft.Icons.DARK_MODE
        self.store.set_meta("theme", "dark" if dark else "light")
        self.page.update()

    # ---------- синхронизация ----------

    def sync_now(self):
        if not self.store.get_meta("yandex_token"):
            self.notify("Сначала укажите токен Яндекс.Диска в настройках")
            return
        self.notify("Синхронизация…")
        threading.Thread(target=self._sync_worker, daemon=True).start()

    def _sync_worker(self):
        try:
            provider = YandexDiskProvider(self.store.get_meta("yandex_token", ""))
            engine = SyncEngine(self.store, provider,
                                self.store.get_meta("sync_folder", "Takt"))
            result = engine.sync()
            self.refresh()
            if result.applied:
                self.notify(f"Синхронизация: получено {result.applied} изменений")
        except Exception as ex:
            self.notify(f"Ошибка синхронизации: {ex}")

    # ---------- список ----------

    def visible_tasks(self) -> list[Task]:
        tasks = self.store.list_tasks()
        if self.filter == "today":
            tasks = [t for t in tasks if not t.completed and is_today(t.due_at)]
        elif self.filter == "overdue":
            tasks = [t for t in tasks if not t.completed and is_overdue(t.due_at)]
        elif self.filter == "done":
            tasks = [t for t in tasks if t.completed]
        else:
            tasks = [t for t in tasks if not t.completed]
        if self.category_filter:
            tasks = [t for t in tasks if t.category_id == self.category_filter]
        if self.search:
            def match(t: Task) -> bool:
                hay = " ".join([t.title, t.notes, " ".join(t.tags)]).lower()
                return self.search in hay
            tasks = [t for t in tasks if match(t)]
        tasks.sort(key=lambda t: (t.completed, t.due_at is None, t.due_at or "",
                                  -t.priority))
        return tasks

    def refresh(self):
        self.filter_row.controls = [
            ft.TextButton(
                content=label,
                style=ft.ButtonStyle(
                    bgcolor=ft.Colors.PRIMARY_CONTAINER if key == self.filter else None),
                on_click=lambda e, k=key: self.set_filter(k))
            for key, label in FILTERS
        ]
        cats = self.store.list_categories()
        self.cat_dd.options = [ft.DropdownOption(key="", text="Все категории")] + [
            ft.DropdownOption(key=c.id, text=c.name) for c in cats]
        if self.category_filter and self.category_filter not in {c.id for c in cats}:
            self.category_filter = ""
        self.cat_dd.value = self.category_filter

        cat_names = {c.id: c for c in cats}
        tasks = self.visible_tasks()
        self.list_col.controls = [self.task_card(t, cat_names) for t in tasks]
        self.empty_t.value = "" if tasks else "Нет задач. Нажмите +, чтобы создать."
        self.page.update()

    def task_card(self, t: Task, cat_names: dict) -> ft.Control:
        meta: list[ft.Control] = []
        if t.due_at:
            overdue = is_overdue(t.due_at) and not t.completed
            meta.append(ft.Text(
                fmt_due(t.due_at), size=12,
                color=ft.Colors.RED if overdue else ft.Colors.GREY,
                weight=ft.FontWeight.BOLD if overdue else None))
        if t.recurrence:
            meta.append(ft.Text(describe(Recurrence.from_dict(t.recurrence)),
                                size=12, color=ft.Colors.TEAL))
        cat = cat_names.get(t.category_id)
        if cat:
            meta.append(ft.Container(
                content=ft.Text(cat.name, size=11, color=ft.Colors.WHITE),
                bgcolor=cat.color, border_radius=8,
                padding=ft.Padding.symmetric(horizontal=6, vertical=2)))
        for tag in t.tags:
            meta.append(ft.Text(f"#{tag}", size=12, color=ft.Colors.INDIGO_300))
        if t.subtasks:
            done = sum(1 for s in t.subtasks if s.get("done"))
            meta.append(ft.Text(f"Подзадачи: {done}/{len(t.subtasks)}",
                                size=12, color=ft.Colors.GREY))

        def on_check(e, task=t):
            if e.control.value:
                updated = self.store.complete_task(task.id)
                if updated and updated.recurrence:
                    self.notify(f"Выполнено. Следующий срок: {fmt_due(updated.due_at)}")
            else:
                self.store.uncomplete_task(task.id)
            self.refresh()

        return ft.Card(
            content=ft.Container(
                padding=ft.Padding.symmetric(horizontal=8, vertical=4),
                content=ft.Row([
                    ft.Container(width=6, height=36, border_radius=3,
                                 bgcolor=PRIORITY_COLORS.get(t.priority)),
                    ft.Checkbox(value=t.completed, on_change=on_check),
                    ft.Column([
                        ft.Text(t.title, size=15,
                                weight=ft.FontWeight.W_500,
                                color=ft.Colors.GREY if t.completed else None,
                                spans=None),
                        ft.Row(meta, spacing=8, wrap=True),
                    ], spacing=2, expand=True, tight=True),
                    ft.IconButton(ft.Icons.EDIT, icon_size=18, tooltip="Изменить",
                                  on_click=lambda e, task=t: open_task_editor(self, task)),
                ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ),
        )


def main(page: ft.Page):
    store = Store(data_home() / "takt.db")
    TaktApp(page, store)
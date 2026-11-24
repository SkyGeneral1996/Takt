from __future__ import annotations

from datetime import date, datetime

import flet as ft

from ..core.habits import current_streak
from . import calendar_view, habits_view, lists_view, notes_view
from .editor import open_task_editor
from .helpers import PRIORITY_COLORS, fmt_due, is_overdue, is_today
from .stats_view import _summary

_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг",
             "пятница", "суббота", "воскресенье"]
_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня",
           "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def _greeting() -> str:
    h = datetime.now().hour
    if 5 <= h < 12:
        return "Доброе утро"
    if 12 <= h < 18:
        return "Добрый день"
    if 18 <= h < 23:
        return "Добрый вечер"
    return "Доброй ночи"


def _kpi(label: str, value: str, color: str, icon: str) -> ft.Control:
    return ft.Container(
        padding=12, border_radius=12, width=150,
        bgcolor=ft.Colors.with_opacity(0.1, color),
        content=ft.Column([
            ft.Row([ft.Icon(icon, size=16, color=color),
                    ft.Text(value, size=22, weight=ft.FontWeight.BOLD, color=color)],
                   spacing=6),
            ft.Text(label, size=11, color=ft.Colors.GREY),
        ], spacing=2, tight=True))


def _task_row(app, t, overdue: bool = False) -> ft.Control:
    def on_check(e, task=t):
        if e.control.value:
            updated = app.store.complete_task(task.id)
            if updated and updated.recurrence:
                app.notify(f"Выполнено. Следующий срок: {fmt_due(updated.due_at)}")
        else:
            app.store.uncomplete_task(task.id)
        app.refresh()

    due_color = ft.Colors.RED if overdue else (
        ft.Colors.INDIGO if is_today(t.due_at) else ft.Colors.GREY)
    return ft.Row([
        ft.Container(width=4, height=30, border_radius=2,
                     bgcolor=PRIORITY_COLORS.get(t.priority)),
        ft.Column([
            ft.Text(t.title, size=14, weight=ft.FontWeight.W_500,
                    max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            ft.Text(("Просрочено: " if overdue else "") + (fmt_due(t.due_at) or "Без срока"),
                    size=11, color=due_color,
                    weight=ft.FontWeight.BOLD if overdue else None),
        ], spacing=1, expand=True, tight=True),
        ft.Checkbox(value=False, on_change=on_check, tooltip="Выполнить"),
        ft.IconButton(ft.Icons.EDIT, icon_size=16, tooltip="Изменить",
                      on_click=lambda e, task=t: open_task_editor(app, task)),
    ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER)


def _card(title: str, rows: list[ft.Control], icon: str,
          color: str, action: ft.Control | None = None) -> ft.Control:
    head = [ft.Icon(icon, size=18, color=color),
            ft.Text(title, size=15, weight=ft.FontWeight.BOLD, expand=True)]
    if action:
        head.append(action)
    return ft.Container(
        padding=12, border_radius=12,
        bgcolor=ft.Colors.with_opacity(0.05, color),
        content=ft.Column([ft.Row(head, spacing=6), *rows], spacing=6, tight=True))


def _habits_card(app) -> ft.Control:
    today = date.today()
    habits = [h for h in app.store.list_habits() if h.scheduled_on(today.weekday())]
    rows: list[ft.Control] = []
    if not habits:
        rows.append(ft.Text("На сегодня привычек нет.", size=12, color=ft.Colors.GREY))
    for h in habits:
        checked = bool(h.checks.get(today.isoformat()))

        def on_check(e, habit=h):
            app.store.check_habit(habit.id, today.isoformat(), e.control.value)
            app.refresh()

        rows.append(ft.Row([
            ft.Checkbox(value=checked, on_change=on_check),
            ft.Text(h.name, size=14, expand=True,
                    max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            ft.Icon(ft.Icons.LOCAL_FIRE_DEPARTMENT, size=14, color=ft.Colors.ORANGE),
            ft.Text(str(current_streak(h, today)), size=12, color=ft.Colors.GREY),
        ], spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER))
    return _card("Привычки сегодня", rows, ft.Icons.LOOP, ft.Colors.ORANGE)


def _notes_card(app) -> ft.Control | None:
    notes = app.store.list_notes()[:2]
    if not notes:
        return None
    rows = []
    for n in notes:
        preview = (n.text or "").strip().replace("\n", " ")
        rows.append(ft.Row([
            ft.Icon(ft.Icons.PUSH_PIN if n.pinned else ft.Icons.STICKY_NOTE_2,
                    size=14, color=ft.Colors.AMBER if n.pinned else ft.Colors.GREY),
            ft.Column([
                ft.Text(n.title or "Без названия", size=13, weight=ft.FontWeight.W_500,
                        max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                ft.Text(preview[:80], size=11, color=ft.Colors.GREY,
                        max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            ], spacing=0, expand=True, tight=True),
        ], spacing=6))
    return _card("Заметки", rows, ft.Icons.STICKY_NOTE_2, ft.Colors.AMBER,
                 action=ft.TextButton(content="Все",
                                      on_click=lambda e: app.show_section("notes")))


def build(app) -> ft.Control:
    store = app.store
    today = date.today()
    active = [t for t in store.list_tasks() if not t.completed]
    overdue = sorted((t for t in active if is_overdue(t.due_at)),
                     key=lambda t: t.due_at or "")
    today_tasks = [t for t in active if is_today(t.due_at)]
    dated = sorted((t for t in active if t.due_at and not is_overdue(t.due_at)),
                   key=lambda t: t.due_at)
    undated = sorted((t for t in active if not t.due_at), key=lambda t: -t.priority)
    upcoming = (dated + undated)[:6]
    habits = store.list_habits()
    scheduled = [h for h in habits if h.scheduled_on(today.weekday())]
    done_h = sum(1 for h in scheduled if h.checks.get(today.isoformat()))
    week_done = _summary(app, 7)["completed"]

    header = ft.Row([
        ft.Column([
            ft.Text(_greeting(), size=22, weight=ft.FontWeight.BOLD),
            ft.Text(f"{_WEEKDAYS[today.weekday()]}, {today.day} {_MONTHS[today.month - 1]}",
                    size=13, color=ft.Colors.GREY),
        ], spacing=2, expand=True, tight=True),
        ft.IconButton(ft.Icons.CALENDAR_MONTH, tooltip="Календарь",
                      on_click=lambda e: app.show_section("calendar")),
    ])

    kpis = ft.Row([
        _kpi("Сегодня", str(len(today_tasks)), ft.Colors.INDIGO, ft.Icons.TODAY),
        _kpi("Просрочено", str(len(overdue)),
             ft.Colors.RED if overdue else ft.Colors.GREEN,
             ft.Icons.ERROR_OUTLINE if overdue else ft.Icons.CHECK_CIRCLE),
        _kpi("Привычки", f"{done_h}/{len(scheduled)}", ft.Colors.ORANGE,
             ft.Icons.LOCAL_FIRE_DEPARTMENT),
        _kpi("За 7 дней", str(week_done), ft.Colors.GREEN, ft.Icons.TRENDING_UP),
    ], spacing=8, wrap=True)

    actions = ft.Row([
        ft.OutlinedButton(content="Задача", icon=ft.Icons.ADD_TASK,
                          on_click=lambda e: open_task_editor(app)),
        ft.OutlinedButton(content="Список", icon=ft.Icons.PLAYLIST_ADD,
                          on_click=lambda e: lists_view.open_list_editor(app)),
        ft.OutlinedButton(content="Привычка", icon=ft.Icons.LOOP,
                          on_click=lambda e: habits_view.open_habit_editor(app)),
        ft.OutlinedButton(content="Заметка", icon=ft.Icons.NOTE_ADD,
                          on_click=lambda e: notes_view.open_note_editor(app)),
    ], spacing=6, wrap=True)

    blocks: list[ft.Control] = [header, kpis, actions]
    if overdue:
        blocks.append(_card(f"Просрочено: {len(overdue)}",
                            [_task_row(app, t, overdue=True) for t in overdue[:5]],
                            ft.Icons.WARNING_AMBER, ft.Colors.RED,
                            action=ft.TextButton(
                                content="Все",
                                on_click=lambda e: (setattr(app, "filter", "overdue"),
                                                    app.show_section("tasks")))))
    up_rows = [_task_row(app, t) for t in upcoming]
    if not up_rows:
        up_rows = [ft.Text("Нет активных задач. Создайте первую кнопкой «Задача».",
                           size=12, color=ft.Colors.GREY)]
    blocks.append(_card("Ближайшие задачи", up_rows, ft.Icons.SCHEDULE, ft.Colors.INDIGO,
                        action=ft.TextButton(content="Все",
                                             on_click=lambda e: app.show_section("tasks"))))
    notes_card = _notes_card(app)
    if notes_card:
        blocks.append(notes_card)
    if not active and not habits:
        blocks.append(ft.Container(
            padding=16, border_radius=12,
            bgcolor=ft.Colors.with_opacity(0.08, ft.Colors.INDIGO),
            content=ft.Column([
                ft.Text("Начните с первой задачи", size=15, weight=ft.FontWeight.BOLD),
                ft.Text("Срок, приоритет и повторность — в редакторе. "
                        "Привычки и списки появятся здесь же.", size=12,
                     color=ft.Colors.GREY),
                ft.FilledButton(content="Создать задачу", icon=ft.Icons.ADD_TASK,
                                on_click=lambda e: open_task_editor(app)),
            ], spacing=6, tight=True)))

    habits_card = _habits_card(app)
    width = getattr(app.page, "width", None) or 0
    if width >= 900:
        return ft.Row([
            ft.Column(blocks, spacing=10, expand=True, scroll=ft.ScrollMode.AUTO),
            ft.Container(width=320, content=ft.Column(
                [calendar_view.mini(app), habits_card], spacing=10)),
        ], spacing=10, expand=True, vertical_alignment=ft.CrossAxisAlignment.START)
    return ft.Column([*blocks, habits_card, calendar_view.mini(app)],
                     spacing=10, expand=True, scroll=ft.ScrollMode.AUTO)
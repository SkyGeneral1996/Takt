from __future__ import annotations

from datetime import date

import flet as ft

from ..core.habits import current_streak, week_checks
from ..core.models import Habit
from ..core.recurrence import WEEKDAY_LABELS
from .helpers import confirm_action, dialog_width


def build(app) -> ft.Control:
    habits = app.store.list_habits()
    today = date.today()
    cards: list[ft.Control] = []
    if not habits:
        cards.append(ft.Text("Нет привычек. Нажмите +, чтобы создать.",
                             color=ft.Colors.GREY))
    for h in habits:
        streak = current_streak(h, today)
        week = week_checks(h, today)
        dots = []
        for i, state in enumerate(week):
            color = (ft.Colors.GREY_700 if state is None else
                     ft.Colors.GREEN if state else
                     ft.Colors.with_opacity(0.25, ft.Colors.RED))
            dots.append(ft.Container(
                width=16, height=16, border_radius=8, bgcolor=color,
                tooltip=WEEKDAY_LABELS[i]))
        scheduled_today = h.scheduled_on(today.weekday())
        checked = bool(h.checks.get(today.isoformat()))

        def on_check(e, habit=h):
            app.store.check_habit(habit.id, today.isoformat(), e.control.value)
            app.refresh()

        cards.append(ft.Card(content=ft.Container(
            padding=ft.Padding.symmetric(horizontal=10, vertical=6),
            content=ft.Row([
                ft.Checkbox(value=checked, on_change=on_check,
                            disabled=not scheduled_today),
                ft.Column([
                    ft.Text(h.name, size=15, weight=ft.FontWeight.W_500),
                    ft.Row([
                        ft.Icon(ft.Icons.LOCAL_FIRE_DEPARTMENT, size=14,
                                color=ft.Colors.ORANGE),
                        ft.Text(f"Серия: {streak}", size=12, color=ft.Colors.GREY),
                        ft.Text(f"Всего: {len(h.checks)}", size=12, color=ft.Colors.GREY),
                        *dots,
                    ], spacing=6),
                ], spacing=2, expand=True, tight=True),
                ft.IconButton(ft.Icons.EDIT, icon_size=18,
                              on_click=lambda e, habit=h: open_habit_editor(app, habit)),
            ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
        )))
    return ft.Column(cards, spacing=6, scroll=ft.ScrollMode.AUTO, expand=True)


def open_habit_editor(app, habit: Habit | None = None) -> None:
    page = app.page
    editing = habit is not None
    habit = habit or Habit()

    name_f = ft.TextField(label="Название привычки", value=habit.name,
                          autofocus=not editing)
    freq_dd = ft.Dropdown(label="Расписание", dense=True, width=220,
                          value=habit.freq,
                          options=[ft.DropdownOption(key="daily", text="Ежедневно"),
                                   ft.DropdownOption(key="weekdays", text="По дням недели")])
    wd_row = ft.Row(spacing=2, wrap=True)
    wd_boxes = []
    for i, label in enumerate(WEEKDAY_LABELS):
        cb = ft.Checkbox(label=label, value=i in habit.weekdays)
        wd_boxes.append(cb)
        wd_row.controls.append(cb)
    wd_row.visible = habit.freq == "weekdays"

    def on_freq(e):
        wd_row.visible = freq_dd.value == "weekdays"
        page.update()

    freq_dd.on_select = on_freq

    def save(e):
        if not (name_f.value or "").strip():
            app.notify("Укажите название привычки")
            return
        habit.name = name_f.value.strip()
        habit.freq = freq_dd.value or "daily"
        habit.weekdays = [i for i, cb in enumerate(wd_boxes) if cb.value]
        if habit.freq == "weekdays" and not habit.weekdays:
            app.notify("Выберите хотя бы один день")
            return
        app.store.save_habit(habit)
        page.pop_dialog()
        app.refresh()

    def delete(e):
        def apply_delete():
            app.store.delete_habit(habit.id)
            app.refresh()
        confirm_action(app, f"Удалить привычку «{habit.name}»?", apply_delete,
                       title="Удаление привычки", parent=dialog)

    actions = [ft.TextButton("Закрыть", on_click=lambda e: page.pop_dialog()),
               ft.FilledButton("Сохранить", on_click=save)]
    if editing:
        actions.insert(0, ft.TextButton("Удалить", on_click=delete))

    dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Привычка" if editing else "Новая привычка"),
        content=ft.Container(width=dialog_width(page, 380), content=ft.Column(
            [name_f, freq_dd, wd_row], tight=True, spacing=10)),
        actions=actions,
    )
    page.show_dialog(dialog)
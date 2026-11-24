from __future__ import annotations

import flet as ft

from ..core.models import (
    Task, Category, Recurrence, new_id,
    PRIORITY_LOW, PRIORITY_MEDIUM, PRIORITY_HIGH, PRIORITY_NAMES,
)
from ..core.recurrence import (
    FREQ_NONE, FREQ_DAILY, FREQ_WEEKDAYS, FREQ_WEEKLY, FREQ_MONTHLY,
    FREQ_LABELS, WEEKDAY_LABELS,
)
from .helpers import parse_user_datetime, to_user_input

NEW_CATEGORY = "__new__"


def open_task_editor(app, task: Task | None = None) -> None:
    store = app.store
    page = app.page
    editing = task is not None
    task = task or Task()

    title_f = ft.TextField(label="Название *", value=task.title, autofocus=not editing)
    notes_f = ft.TextField(label="Заметки", value=task.notes, multiline=True,
                           min_lines=2, max_lines=4)

    cats = store.list_categories()
    cat_options = [ft.DropdownOption(key="", text="Без категории")]
    cat_options += [ft.DropdownOption(key=c.id, text=c.name) for c in cats]
    cat_options.append(ft.DropdownOption(key=NEW_CATEGORY, text="+ Новая категория…"))
    cat_dd = ft.Dropdown(label="Категория", value=task.category_id or "",
                         options=cat_options)
    new_cat_f = ft.TextField(label="Название новой категории",
                             visible=(cat_dd.value == NEW_CATEGORY))

    def cat_changed(e):
        new_cat_f.visible = cat_dd.value == NEW_CATEGORY
        page.update()

    cat_dd.on_select = cat_changed

    prio_dd = ft.Dropdown(
        label="Приоритет", value=str(task.priority),
        options=[ft.DropdownOption(key=str(p), text=PRIORITY_NAMES[p])
                 for p in (PRIORITY_LOW, PRIORITY_MEDIUM, PRIORITY_HIGH)])

    due_f = ft.TextField(label="Срок (ГГГГ-ММ-ДД ЧЧ:ММ)", value=to_user_input(task.due_at),
                         hint_text="например: 2026-12-01 18:00")
    error_t = ft.Text("", color=ft.Colors.RED, size=12)

    rec = Recurrence.from_dict(task.recurrence) if task.recurrence else None
    freq_dd = ft.Dropdown(
        label="Повторение", value=rec.freq if rec else FREQ_NONE,
        options=[ft.DropdownOption(key=k, text=v) for k, v in FREQ_LABELS.items()])
    interval_f = ft.TextField(label="Интервал (N)", value=str(rec.interval if rec else 1),
                              width=130, keyboard_type=ft.KeyboardType.NUMBER)
    weekday_checks = [
        ft.Checkbox(label=WEEKDAY_LABELS[i],
                    value=bool(rec and i in rec.weekdays))
        for i in range(7)
    ]
    weekdays_row = ft.Row(weekday_checks, wrap=True)

    def freq_changed(e):
        f = freq_dd.value
        interval_f.visible = f in (FREQ_DAILY, FREQ_WEEKLY, FREQ_MONTHLY)
        weekdays_row.visible = f == FREQ_WEEKLY
        page.update()

    freq_dd.on_select = freq_changed
    freq_changed(None)

    tags_f = ft.TextField(label="Теги (через запятую)", value=", ".join(task.tags))

    subtask_rows: list[dict] = []
    subtasks_col = ft.Column(spacing=4, tight=True)

    def rebuild_subtasks():
        subtasks_col.controls.clear()
        for st in subtask_rows:
            subtasks_col.controls.append(ft.Row([
                ft.Checkbox(value=st["done"],
                            on_change=lambda e, s=st: s.update(done=e.control.value)),
                ft.Text(st["title"], expand=True),
                ft.IconButton(ft.Icons.CLOSE, icon_size=16,
                              on_click=lambda e, s=st: remove_subtask(s)),
            ], spacing=4))
        page.update()

    def remove_subtask(st):
        subtask_rows.remove(st)
        rebuild_subtasks()

    for st in task.subtasks:
        subtask_rows.append(dict(st))

    new_sub_f = ft.TextField(label="Новая подзадача", expand=True,
                             on_submit=lambda e: add_subtask(e))

    def add_subtask(e):
        text = (new_sub_f.value or "").strip()
        if text:
            subtask_rows.append({"id": new_id(), "title": text, "done": False})
            new_sub_f.value = ""
            rebuild_subtasks()

    rebuild_subtasks()

    def save(e):
        title = (title_f.value or "").strip()
        if not title:
            error_t.value = "Введите название задачи"
            page.update()
            return
        try:
            due = parse_user_datetime(due_f.value or "")
        except ValueError as ex:
            error_t.value = str(ex)
            page.update()
            return

        task.title = title
        task.notes = (notes_f.value or "").strip()
        task.priority = int(prio_dd.value or PRIORITY_MEDIUM)
        task.due_at = due.isoformat(timespec="milliseconds") if due else None
        task.tags = [t.strip() for t in (tags_f.value or "").split(",") if t.strip()]
        task.subtasks = list(subtask_rows)

        if cat_dd.value == NEW_CATEGORY:
            name = (new_cat_f.value or "").strip()
            task.category_id = store.save_category(Category(name=name)).id if name else None
        else:
            task.category_id = cat_dd.value or None

        freq = freq_dd.value
        if freq and freq != FREQ_NONE:
            try:
                interval = max(1, int(interval_f.value or 1))
            except ValueError:
                interval = 1
            task.recurrence = Recurrence(
                freq=freq, interval=interval,
                weekdays=[i for i, c in enumerate(weekday_checks) if c.value],
            ).to_dict()
        else:
            task.recurrence = None

        store.save_task(task)
        page.pop_dialog()
        app.refresh()
        app.notify("Задача сохранена")

    def delete(e):
        store.delete_task(task.id)
        page.pop_dialog()
        app.refresh()
        app.notify("Задача удалена")

    actions = [
        ft.TextButton("Отмена", on_click=lambda e: page.pop_dialog()),
        ft.FilledButton("Сохранить", on_click=save),
    ]
    if editing:
        actions.insert(0, ft.TextButton(
            "Удалить", style=ft.ButtonStyle(color=ft.Colors.RED), on_click=delete))

    dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Редактирование задачи" if editing else "Новая задача"),
        content=ft.Container(
            width=440,
            content=ft.Column([
                title_f, notes_f,
                ft.Row([cat_dd, prio_dd], spacing=8),
                new_cat_f,
                due_f,
                ft.Row([freq_dd, interval_f], spacing=8),
                weekdays_row,
                tags_f,
                ft.Text("Подзадачи", weight=ft.FontWeight.BOLD, size=13),
                subtasks_col,
                ft.Row([new_sub_f, ft.IconButton(ft.Icons.ADD, on_click=add_subtask)]),
                error_t,
            ], scroll=ft.ScrollMode.AUTO, tight=True, spacing=10),
        ),
        actions=actions,
    )
    page.show_dialog(dialog)
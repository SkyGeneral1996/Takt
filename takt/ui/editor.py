from __future__ import annotations

import flet as ft

from ..core.models import (
    Task, Category, Recurrence, new_id,
    PRIORITY_LOW, PRIORITY_MEDIUM, PRIORITY_HIGH, PRIORITY_NAMES,
)
from ..core.recurrence import (
    FREQ_NONE, FREQ_DAILY, FREQ_WEEKDAYS, FREQ_WEEKLY, FREQ_MONTHLY,
    FREQ_LABELS, WEEKDAY_LABELS, local_timezone_name,
)
from .helpers import (
    confirm_action, dialog_height, dialog_width, parse_user_datetime, to_user_input,
)

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
                ft.TextField(value=st["title"], dense=True, expand=True,
                             on_change=lambda e, s=st: s.update(title=e.control.value or "")),
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
                interval = int(interval_f.value or 1)
            except ValueError:
                error_t.value = "Интервал должен быть целым числом"
                page.update()
                return
            if interval < 1:
                error_t.value = "Интервал должен быть не меньше 1"
                page.update()
                return
            weekdays = [i for i, checkbox in enumerate(weekday_checks) if checkbox.value]
            if freq == FREQ_WEEKLY and not weekdays:
                error_t.value = "Выберите хотя бы один день недели"
                page.update()
                return
            task.recurrence = Recurrence(
                freq=freq, interval=interval, weekdays=weekdays,
                anchor_day=rec.anchor_day if rec else None,
                timezone=rec.timezone if rec and rec.timezone else local_timezone_name(),
            ).to_dict()
        else:
            task.recurrence = None

        store.save_task(task)
        page.pop_dialog()
        app.refresh()
        app.notify("Задача сохранена")

    def delete(e):
        def apply_delete():
            store.delete_task(task.id)
            app.refresh()
            app.notify("Задача удалена")
        confirm_action(app, f"Удалить задачу «{task.title}»?", apply_delete,
                       title="Удаление задачи", parent=dialog)

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
            width=dialog_width(page, 440),
            height=dialog_height(page, 620),
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
            ], scroll=ft.ScrollMode.AUTO, tight=True, spacing=10, expand=True),
        ),
        actions=actions,
    )
    page.show_dialog(dialog)


def open_category_manager(app) -> None:
    page = app.page
    store = app.store
    selected: Category | None = None
    name_f = ft.TextField(label="Название категории", dense=True)
    colors = ["#2196F3", "#4CAF50", "#FF9800", "#E91E63", "#9C27B0", "#607D8B"]
    color_dd = ft.Dropdown(
        label="Цвет", dense=True, value=colors[0],
        options=[ft.DropdownOption(key=color, text=color) for color in colors])
    rows_col = ft.Column(spacing=4, tight=True, scroll=ft.ScrollMode.AUTO, expand=True)
    error_t = ft.Text("", size=12, color=ft.Colors.RED)
    delete_btn = ft.OutlinedButton("Удалить", icon=ft.Icons.DELETE, disabled=True)

    def clear_editor():
        nonlocal selected
        selected = None
        name_f.value = ""
        color_dd.value = colors[0]
        error_t.value = ""
        delete_btn.disabled = True

    def rebuild():
        rows = []
        for category in store.list_categories():
            rows.append(ft.Row([
                ft.Container(width=14, height=14, border_radius=7,
                             bgcolor=category.color),
                ft.Text(category.name, expand=True),
                ft.IconButton(ft.Icons.EDIT, icon_size=16, tooltip="Изменить",
                              on_click=lambda e, item=category: edit(item)),
            ], spacing=6))
        rows_col.controls = rows or [ft.Text("Нет категорий", color=ft.Colors.GREY,
                                             size=12)]
        page.update()

    def edit(category: Category):
        nonlocal selected
        selected = category
        name_f.value = category.name
        color_dd.value = category.color
        error_t.value = ""
        delete_btn.disabled = False
        page.update()

    def save(e):
        name = (name_f.value or "").strip()
        if not name:
            error_t.value = "Введите название категории"
            page.update()
            return
        if any(c.id != (selected.id if selected else None)
               and c.name.casefold() == name.casefold()
               for c in store.list_categories()):
            error_t.value = "Такая категория уже существует"
            page.update()
            return
        category = selected or Category()
        category.name = name
        category.color = color_dd.value or colors[0]
        category.touch()
        store.save_category(category)
        clear_editor()
        rebuild()
        app.refresh()

    def delete(e):
        category = selected
        if category is None:
            return

        def apply_delete():
            store.delete_category(category.id)
            clear_editor()
            rebuild()
            app.refresh()
            app.notify("Категория удалена; задачи сохранены")
        confirm_action(app, f"Удалить категорию «{category.name}»? Задачи останутся.",
                       apply_delete, title="Удаление категории", parent=dialog)

    delete_btn.on_click = delete
    rebuild()
    dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Категории"),
        content=ft.Container(width=dialog_width(page, 420),
                             height=dialog_height(page, 480), content=ft.Column([
            ft.Row([name_f, color_dd], spacing=8, wrap=True),
            ft.Row([
                ft.FilledButton("Сохранить", icon=ft.Icons.SAVE, on_click=save),
                delete_btn,
            ], spacing=8),
            error_t,
            ft.Divider(height=8),
            rows_col,
        ], tight=True, spacing=8, expand=True)),
        actions=[ft.TextButton("Закрыть", on_click=lambda e: page.pop_dialog())],
    )
    page.show_dialog(dialog)
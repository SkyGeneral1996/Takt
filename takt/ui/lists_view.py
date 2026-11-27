from __future__ import annotations

import math

import flet as ft

from ..core.lists import TEMPLATES, template_items, sorted_items, item_sum
from ..core.models import CheckList, new_list_item
from ..core.recurrence import describe, Recurrence
from ..transfer import build_payload
from . import settings
from .helpers import confirm_action, dialog_height, dialog_width


def build(app) -> ft.Control:
    lists = app.store.list_lists()
    tasks = {t.id: t for t in app.store.list_tasks()}
    cards: list[ft.Control] = []
    if not lists:
        cards.append(ft.Text("Нет списков. Нажмите +, чтобы создать.",
                             color=ft.Colors.GREY))
    for lst in lists:
        done, total = lst.progress
        meta: list[ft.Control] = [ft.Text(f"{done}/{total}", size=12, color=ft.Colors.GREY)]
        if lst.total:
            meta.append(ft.Text(f"Σ {lst.total:.0f}", size=12, color=ft.Colors.TEAL))
        if lst.task_id and lst.task_id in tasks:
            meta.append(ft.Text(f"→ {tasks[lst.task_id].title}", size=12,
                                color=ft.Colors.INDIGO_300))
        if lst.recurrence:
            meta.append(ft.Text(describe(Recurrence.from_dict(lst.recurrence)),
                                size=12, color=ft.Colors.TEAL))
        cards.append(ft.Card(content=ft.Container(
            padding=ft.Padding.symmetric(horizontal=10, vertical=6),
            content=ft.Row([
                ft.Icon(ft.Icons.LIST_ALT, color=ft.Colors.INDIGO),
                ft.Column([
                    ft.Text(lst.name, size=15, weight=ft.FontWeight.W_500),
                    ft.Row(meta, spacing=8, wrap=True),
                ], spacing=2, expand=True, tight=True),
                ft.IconButton(ft.Icons.QR_CODE, icon_size=18, tooltip="Передать список",
                              on_click=lambda e, l=lst: open_share(app, l)),
                ft.IconButton(ft.Icons.EDIT, icon_size=18, tooltip="Открыть",
                              on_click=lambda e, l=lst: open_list_editor(app, l)),
            ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
            on_click=lambda e, l=lst: open_list_editor(app, l), ink=True,
        )))
    return ft.Column(cards, spacing=6, scroll=ft.ScrollMode.AUTO, expand=True)


def open_share(app, lst: CheckList) -> None:
    payload = build_payload(app.store, only={("list", lst.id)})
    settings.open_send(app, payload=payload, title=f"Передать список «{lst.name}»")


def open_list_editor(app, lst: CheckList | None = None) -> None:
    page = app.page
    store = app.store
    editing = lst is not None
    lst = lst or CheckList()

    name_f = ft.TextField(label="Название списка", value=lst.name, autofocus=not editing)
    tpl_dd = ft.Dropdown(label="Шаблон", dense=True, width=220,
                         options=[ft.DropdownOption(key="", text="Пустой список")] +
                         [ft.DropdownOption(key=k, text=k) for k in TEMPLATES])
    tasks = store.list_tasks()
    task_dd = ft.Dropdown(label="Привязать к задаче", dense=True, width=220,
                          value=lst.task_id or "",
                          options=[ft.DropdownOption(key="", text="Без задачи")] +
                          [ft.DropdownOption(key=t.id, text=t.title) for t in tasks])
    freq_dd = ft.Dropdown(label="Повторение", dense=True, width=220,
                          value=(lst.recurrence or {}).get("freq", ""),
                          options=[ft.DropdownOption(key="", text="Нет"),
                                   ft.DropdownOption(key="daily", text="Ежедневно"),
                                   ft.DropdownOption(key="weekly", text="Еженедельно"),
                                   ft.DropdownOption(key="monthly", text="Ежемесячно")])

    items_col = ft.Column(spacing=4, tight=True)

    def rebuild_items():
        items_col.controls = [_item_row(app, lst, it, rebuild_items)
                              for it in sorted_items(lst.items)]
        page.update()

    def add_item(e):
        lst.items.append(new_list_item(position=len(lst.items)))
        rebuild_items()

    def apply_template(e):
        if not tpl_dd.value:
            return

        def replace():
            lst.items = template_items(tpl_dd.value)
            if not name_f.value:
                name_f.value = tpl_dd.value
            rebuild_items()

        if lst.items:
            confirm_action(app, "Заменить текущие позиции выбранным шаблоном?",
                           replace, title="Замена шаблона")
        else:
            replace()

    tpl_dd.on_select = apply_template

    def save(e):
        if not (name_f.value or "").strip():
            app.notify("Укажите название списка")
            return
        lst.name = name_f.value.strip()
        lst.task_id = task_dd.value or None
        lst.recurrence = {"freq": freq_dd.value, "interval": 1, "weekdays": []} \
            if freq_dd.value else None
        store.save_list(lst)
        page.pop_dialog()
        app.refresh()

    def delete(e):
        def apply_delete():
            store.delete_list(lst.id)
            app.refresh()
        confirm_action(app, f"Удалить список «{lst.name}»?", apply_delete,
                       title="Удаление списка", parent=dialog)

    def reset(e):
        def apply_reset():
            store.reset_list(lst.id)
            lst_new = store.get_list(lst.id)
            lst.items = lst_new.items
            lst.cycles = lst_new.cycles
            rebuild_items()
            app.notify("Список обновлён для нового цикла")
            app.refresh()
        confirm_action(app, "Сбросить отметки и начать новый цикл?",
                       apply_reset, title="Новый цикл")

    def to_subtasks(e):
        task = store.get_task(lst.task_id) if lst.task_id else None
        if not task:
            app.notify("Сначала привяжите список к задаче и сохраните")
            return
        existing = {s.get("title") for s in task.subtasks}
        for it in sorted_items(lst.items):
            if it["text"] and it["text"] not in existing:
                task.subtasks.append({"id": it["id"], "title": it["text"],
                                      "done": bool(it.get("done"))})
        store.save_task(task)
        app.notify("Позиции добавлены в подзадачи")

    actions = [ft.TextButton("Закрыть", on_click=lambda e: page.pop_dialog()),
               ft.FilledButton("Сохранить", on_click=save)]
    if editing:
        actions.insert(0, ft.TextButton("Удалить", on_click=delete))

    extra: list[ft.Control] = []
    if editing and lst.recurrence:
        extra.append(ft.Row([
            ft.OutlinedButton("Новый цикл (сбросить отметки)", icon=ft.Icons.REFRESH,
                              on_click=reset),
            ft.Text(f"Циклов: {lst.cycles}", size=12, color=ft.Colors.GREY),
        ]))
    if editing and lst.task_id:
        extra.append(ft.TextButton(content="Позиции → подзадачи задачи",
                                   on_click=to_subtasks))

    rebuild_items()
    dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Список" if editing else "Новый список"),
        content=ft.Container(
            width=dialog_width(page, 460),
            height=dialog_height(page, 580),
            content=ft.Column([
                name_f,
                ft.Row([tpl_dd, task_dd], wrap=True, spacing=8),
                freq_dd,
                *extra,
                ft.Divider(height=8),
                items_col,
                ft.TextButton(content="+ Позиция", on_click=add_item),
            ], scroll=ft.ScrollMode.AUTO, tight=True, spacing=8, expand=True),
        ),
        actions=actions,
    )
    page.show_dialog(dialog)


def _item_row(app, lst: CheckList, it: dict, rebuild) -> ft.Control:
    text_f = ft.TextField(value=it.get("text", ""), dense=True, expand=True,
                          hint_text="Позиция")
    qty_f = ft.TextField(value=str(1 if it.get("qty") is None else it["qty"]), dense=True, width=55,
                         hint_text="кол-во")
    unit_f = ft.TextField(value=it.get("unit") or "", dense=True, width=55,
                          hint_text="ед.")
    price_f = ft.TextField(value="" if it.get("price") is None else str(it["price"]),
                           dense=True, width=70, hint_text="цена")
    group_f = ft.TextField(value=it.get("group") or "", dense=True, width=100,
                           hint_text="группа")

    def persist(e=None):
        try:
            qty = float((qty_f.value or "1").replace(",", "."))
            price_text = (price_f.value or "").replace(",", ".").strip()
            price = float(price_text) if price_text else None
            if not math.isfinite(qty) or qty < 0:
                raise ValueError
            if price is not None and (not math.isfinite(price) or price < 0):
                raise ValueError
        except ValueError:
            app.notify("Количество и цена должны быть конечными числами не меньше нуля")
            return
        it["text"] = text_f.value or ""
        it["qty"] = qty
        it["unit"] = unit_f.value or ""
        it["price"] = price
        it["group"] = group_f.value or ""
        app.store.save_list(lst)
        app.refresh()

    for f in (text_f, qty_f, unit_f, price_f, group_f):
        f.on_blur = persist

    def on_done(e):
        it["done"] = e.control.value
        app.store.save_list(lst)
        rebuild()
        app.refresh()

    def remove(e):
        lst.items = [x for x in lst.items if x["id"] != it["id"]]
        app.store.save_list(lst)
        rebuild()
        app.refresh()

    return ft.Container(
        padding=ft.Padding.only(bottom=6),
        content=ft.Column([
            ft.Row([
                ft.Checkbox(value=bool(it.get("done")), on_change=on_done),
                text_f,
                ft.IconButton(ft.Icons.CLOSE, icon_size=14, on_click=remove),
            ], spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Row([qty_f, unit_f, price_f, group_f], spacing=4),
        ], spacing=2, tight=True),
    )
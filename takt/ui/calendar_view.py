from __future__ import annotations

import calendar as cal
from datetime import date, timedelta

import flet as ft

from ..core.models import parse_iso

MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
          "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def due_local_date(iso: str) -> str:
    return parse_iso(iso).astimezone().date().isoformat()


def day_counts(app) -> dict[str, int]:
    counts: dict[str, int] = {}
    for t in app.store.list_tasks():
        if t.due_at and not t.completed:
            d = due_local_date(t.due_at)
            counts[d] = counts.get(d, 0) + 1
    return counts


def _header(app, year: int, month: int, on_prev, on_next, extra: list | None = None):
    return ft.Row([
        ft.IconButton(ft.Icons.CHEVRON_LEFT, icon_size=18, on_click=on_prev),
        ft.Text(f"{MONTHS[month - 1]} {year}", weight=ft.FontWeight.BOLD, expand=True,
                text_align=ft.TextAlign.CENTER),
        ft.IconButton(ft.Icons.CHEVRON_RIGHT, icon_size=18, on_click=on_next),
        *(extra or []),
    ], spacing=0)


def _grid(app, year: int, month: int, counts: dict, selected: str | None,
          compact: bool, on_day) -> ft.Control:
    today = date.today().isoformat()
    weeks = cal.Calendar(firstweekday=0).monthdatescalendar(year, month)
    rows = [ft.Row([ft.Text(w, size=11, color=ft.Colors.GREY, width=34 if compact else 44,
                            text_align=ft.TextAlign.CENTER) for w in WD], spacing=2)]
    for week in weeks:
        cells = []
        for d in week:
            in_month = d.month == month
            iso = d.isoformat()
            n = counts.get(iso, 0)
            is_sel = iso == selected
            is_today = iso == today
            body: list[ft.Control] = [ft.Text(
                str(d.day), size=12 if compact else 14,
                color=(ft.Colors.WHITE if is_sel else
                       None if in_month else ft.Colors.GREY_600),
                weight=ft.FontWeight.BOLD if is_today else None)]
            if n:
                if compact:
                    body.append(ft.Container(width=5, height=5, border_radius=3,
                                             bgcolor=ft.Colors.WHITE if is_sel
                                             else ft.Colors.INDIGO))
                else:
                    body.append(ft.Text(f"{n} зад.", size=10,
                                        color=ft.Colors.WHITE if is_sel
                                        else ft.Colors.INDIGO_300))
            cells.append(ft.Container(
                width=34 if compact else 44, height=34 if compact else 48,
                border_radius=8,
                bgcolor=ft.Colors.INDIGO if is_sel else
                ft.Colors.with_opacity(0.12, ft.Colors.INDIGO) if is_today else None,
                border=ft.Border.all(1, ft.Colors.INDIGO) if is_today and not is_sel else None,
                content=ft.Column(body, spacing=1,
                                  alignment=ft.MainAxisAlignment.CENTER,
                                  horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                on_click=lambda e, iso=iso: on_day(iso),
                ink=True,
            ))
        rows.append(ft.Row(cells, spacing=2))
    return ft.Column(rows, spacing=2, tight=True)


def mini(app) -> ft.Control:
    """Компактный интерактивный календарь для главного экрана."""
    if not getattr(app, "mini_ym", None):
        t = date.today()
        app.mini_ym = (t.year, t.month)
    y, m = app.mini_ym

    def shift(delta):
        nonlocal y, m
        m += delta
        if m < 1:
            m, y = 12, y - 1
        elif m > 12:
            m, y = 1, y + 1
        app.mini_ym = (y, m)
        app.refresh()

    def on_day(iso):
        app.day_filter = None if app.day_filter == iso else iso
        app.refresh()

    return ft.Container(
        padding=8, border_radius=12, bgcolor=ft.Colors.with_opacity(0.05, ft.Colors.INDIGO),
        content=ft.Column([
            _header(app, y, m, lambda e: shift(-1), lambda e: shift(1),
                    extra=[ft.IconButton(ft.Icons.OPEN_IN_FULL, icon_size=16,
                                         tooltip="Полный календарь",
                                         on_click=lambda e: app.show_section("calendar"))]),
            _grid(app, y, m, day_counts(app), app.day_filter, True, on_day),
            ft.Text("Клик по дню — фильтр задач", size=10, color=ft.Colors.GREY)
            if not app.day_filter else
            ft.Row([ft.Text(f"Фильтр: {app.day_filter}", size=11, color=ft.Colors.INDIGO),
                    ft.TextButton(content="Сбросить",
                                  on_click=lambda e: (setattr(app, "day_filter", None),
                                                      app.refresh()))], spacing=4),
        ], spacing=4, tight=True),
    )


def build_full(app) -> ft.Control:
    """Полный календарь: месяц/неделя, панель задач выбранного дня."""
    if not getattr(app, "cal_ym", None):
        t = date.today()
        app.cal_ym = (t.year, t.month)
    if not getattr(app, "cal_week", False):
        app.cal_week = False
    y, m = app.cal_ym
    width = getattr(app.page, "width", None) or 0
    compact_week = width < 700
    counts = day_counts(app)
    selected = app.day_filter or date.today().isoformat()

    def shift(delta):
        nonlocal y, m
        if app.cal_week:
            d = date.fromisoformat(selected) + timedelta(weeks=delta)
            app.day_filter = d.isoformat()
            app.cal_ym = (d.year, d.month)
        else:
            m += delta
            if m < 1:
                m, y = 12, y - 1
            elif m > 12:
                m, y = 1, y + 1
            app.cal_ym = (y, m)
        app.refresh()

    def on_day(iso):
        app.day_filter = iso
        app.refresh()

    def toggle_mode(e):
        app.cal_week = not app.cal_week
        app.refresh()

    if app.cal_week:
        sel = date.fromisoformat(selected)
        monday = sel - timedelta(days=sel.weekday())
        days = [monday + timedelta(days=i) for i in range(7)]
        tasks_by_day: dict[str, list] = {d.isoformat(): [] for d in days}
        for t in app.store.list_tasks():
            if t.due_at and not t.completed:
                k = due_local_date(t.due_at)
                if k in tasks_by_day:
                    tasks_by_day[k].append(t)
        cols = []
        for d in days:
            iso = d.isoformat()
            items = [ft.Text(f"{WD[d.weekday()]} {d.day}", size=12,
                             weight=ft.FontWeight.BOLD,
                             color=ft.Colors.INDIGO if iso == selected else None)]
            for t in tasks_by_day[iso][:6]:
                items.append(ft.Text(f"• {t.title}", size=11, no_wrap=True,
                                     overflow=ft.TextOverflow.ELLIPSIS))
            cols.append(ft.Container(
                expand=not compact_week, width=120 if compact_week else None,
                padding=6, border_radius=8,
                bgcolor=ft.Colors.with_opacity(0.08, ft.Colors.INDIGO)
                if iso == selected else None,
                content=ft.Column(items, spacing=3, tight=True),
                on_click=lambda e, iso=iso: on_day(iso), ink=True))
        grid = ft.Row(cols, spacing=4,
                      scroll=ft.ScrollMode.AUTO if compact_week else None,
                      vertical_alignment=ft.CrossAxisAlignment.START)
        title = f"{monday.day} {MONTHS[monday.month - 1]} — неделя"
    else:
        grid = _grid(app, y, m, counts, selected, width < 420, on_day)
        title = f"{MONTHS[m - 1]} {y}"

    day_tasks = [t for t in app.store.list_tasks()
                 if t.due_at and due_local_date(t.due_at) == selected]
    day_tasks.sort(key=lambda t: (t.completed, t.due_at or ""))
    panel: list[ft.Control] = [ft.Text(f"Задачи на {selected}", weight=ft.FontWeight.BOLD)]
    if not day_tasks:
        panel.append(ft.Text("Нет задач на этот день", color=ft.Colors.GREY, size=12))
    for t in day_tasks:
        def on_check(e, task=t):
            if e.control.value:
                app.store.complete_task(task.id)
            else:
                app.store.uncomplete_task(task.id)
            app.refresh()
        panel.append(ft.Row([
            ft.Checkbox(value=t.completed, on_change=on_check),
            ft.Text(t.title, size=13, expand=True,
                    color=ft.Colors.GREY if t.completed else None),
        ], spacing=4))

    return ft.Column([
        ft.Row([
            ft.IconButton(ft.Icons.CHEVRON_LEFT, on_click=lambda e: shift(-1)),
            ft.Text(title, weight=ft.FontWeight.BOLD, expand=True),
            ft.TextButton(content="Неделя" if not app.cal_week else "Месяц",
                          on_click=toggle_mode),
            ft.IconButton(ft.Icons.CHEVRON_RIGHT, on_click=lambda e: shift(1)),
        ]),
        grid,
        ft.Divider(height=8),
        *panel,
    ], spacing=8, scroll=ft.ScrollMode.AUTO, expand=True)
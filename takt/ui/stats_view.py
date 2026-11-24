from __future__ import annotations

from datetime import timedelta

import flet as ft

from ..core.models import now_utc, parse_iso
from ..core.store import Store


def _summary(app, days: int) -> dict:
    store: Store = app.store
    now = now_utc()
    start = (now - timedelta(days=days)).isoformat(timespec="milliseconds")
    comps = store.completions_between(start, now.isoformat(timespec="milliseconds"))
    tasks = store.list_tasks()
    habit_checks = 0
    cutoff = (now - timedelta(days=days)).date().isoformat()
    for h in store.list_habits():
        habit_checks += sum(1 for d in h.checks if d >= cutoff)
    return {
        "completed": len(comps),
        "active": sum(1 for t in tasks if not t.completed),
        "habits": habit_checks,
        "comps": comps,
    }


def _by_category(app, comps: list[dict]) -> list[tuple[str, str, int]]:
    store = app.store
    cats = {c.id: c for c in store.list_categories()}
    counts: dict[str, int] = {}
    for c in comps:
        t = store.get_task(c["task_id"], include_deleted=True)
        key = t.category_id if t and t.category_id else ""
        counts[key] = counts.get(key, 0) + 1
    out = []
    for key, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        cat = cats.get(key)
        out.append((cat.name if cat else "Без категории",
                    cat.color if cat else "#9E9E9E", n))
    return out


def _heatmap(app) -> ft.Control:
    """Тепловая карта активности за 12 недель: выполнения задач + отметки привычек."""
    store = app.store
    today = now_utc().astimezone().date()
    start = today - timedelta(days=today.weekday() + 7 * 11)
    end = now_utc().isoformat(timespec="milliseconds")
    start_iso = parse_iso(start.isoformat()).isoformat(timespec="milliseconds")
    per_day: dict[str, int] = {}
    for c in store.completions_between(start_iso, end):
        d = parse_iso(c["at"]).astimezone().date().isoformat()
        per_day[d] = per_day.get(d, 0) + 1
    for h in store.list_habits():
        for d in h.checks:
            if d >= start.isoformat():
                per_day[d] = per_day.get(d, 0) + 1

    cols = []
    for w in range(12):
        cells = []
        for i in range(7):
            d = start + timedelta(days=w * 7 + i)
            if d > today:
                cells.append(ft.Container(width=14, height=14))
                continue
            n = per_day.get(d.isoformat(), 0)
            color = (ft.Colors.with_opacity(0.08, ft.Colors.GREEN) if n == 0 else
                     ft.Colors.with_opacity(min(0.25 + 0.2 * n, 1.0), ft.Colors.GREEN))
            cells.append(ft.Container(width=14, height=14, border_radius=3,
                                      bgcolor=color, tooltip=f"{d.isoformat()}: {n}"))
        cols.append(ft.Column(cells, spacing=2))
    return ft.Column([
        ft.Text("Активность за 12 недель", size=12, color=ft.Colors.GREY),
        ft.Row(cols, spacing=2),
    ], spacing=4)


def build(app) -> ft.Control:
    days = getattr(app, "stats_days", 7)

    def set_days(e):
        app.stats_days = int(e.control.value)
        app.refresh()

    s = _summary(app, days)
    tiles = ft.Row([
        _tile("Выполнено", str(s["completed"]), ft.Colors.GREEN),
        _tile("Активных задач", str(s["active"]), ft.Colors.INDIGO),
        _tile("Отметок привычек", str(s["habits"]), ft.Colors.ORANGE),
    ], spacing=8, wrap=True)

    bars: list[ft.Control] = [ft.Text("По категориям", weight=ft.FontWeight.BOLD)]
    by_cat = _by_category(app, s["comps"])
    if not by_cat:
        bars.append(ft.Text("Нет выполнений за период", color=ft.Colors.GREY, size=12))
    max_n = max((n for _, _, n in by_cat), default=1)
    for name, color, n in by_cat:
        bars.append(ft.Row([
            ft.Text(name, size=12, width=130, no_wrap=True,
                    overflow=ft.TextOverflow.ELLIPSIS),
            ft.Container(width=max(8, int(220 * n / max_n)), height=14,
                         border_radius=4, bgcolor=color),
            ft.Text(str(n), size=12),
        ], spacing=8))

    return ft.Column([
        ft.Row([
            ft.Text("Период:", size=13),
            ft.Dropdown(dense=True, width=160, value=str(days),
                        options=[ft.DropdownOption(key="7", text="7 дней"),
                                 ft.DropdownOption(key="30", text="30 дней")],
                        on_select=set_days),
        ]),
        tiles,
        ft.Divider(height=8),
        *bars,
        ft.Divider(height=8),
        _heatmap(app),
    ], spacing=8, scroll=ft.ScrollMode.AUTO, expand=True)


def _tile(label: str, value: str, color: str) -> ft.Control:
    return ft.Container(
        padding=12, border_radius=12, width=150,
        bgcolor=ft.Colors.with_opacity(0.1, color),
        content=ft.Column([
            ft.Text(value, size=24, weight=ft.FontWeight.BOLD, color=color),
            ft.Text(label, size=12, color=ft.Colors.GREY),
        ], spacing=2, tight=True),
    )
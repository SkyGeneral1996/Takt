from __future__ import annotations

from datetime import datetime, timezone

import flet as ft

from ..core.models import parse_iso, PRIORITY_LOW, PRIORITY_MEDIUM, PRIORITY_HIGH

PRIORITY_COLORS = {
    PRIORITY_LOW: ft.Colors.BLUE_GREY,
    PRIORITY_MEDIUM: ft.Colors.AMBER,
    PRIORITY_HIGH: ft.Colors.RED,
}

_DATETIME_FORMATS = ["%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M", "%Y-%m-%d", "%d.%m.%Y"]


def parse_user_datetime(text: str) -> datetime | None:
    """Пользовательский ввод (локальное время) -> UTC. None, если пусто."""
    text = text.strip()
    if not text:
        return None
    for fmt in _DATETIME_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    raise ValueError("Формат даты: ГГГГ-ММ-ДД ЧЧ:ММ или ДД.ММ.ГГГГ ЧЧ:ММ")


def to_user_input(iso: str | None) -> str:
    if not iso:
        return ""
    return parse_iso(iso).astimezone().strftime("%Y-%m-%d %H:%M")


def fmt_due(iso: str | None) -> str:
    if not iso:
        return ""
    local = parse_iso(iso).astimezone()
    if local.year == datetime.now().year:
        return local.strftime("%d.%m %H:%M")
    return local.strftime("%d.%m.%Y %H:%M")


def is_overdue(iso: str | None) -> bool:
    return bool(iso) and parse_iso(iso) < datetime.now(timezone.utc)


def is_today(iso: str | None) -> bool:
    if not iso:
        return False
    return parse_iso(iso).astimezone().date() == datetime.now().date()
from __future__ import annotations

import calendar
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import Recurrence

FREQ_NONE = "none"
FREQ_DAILY = "daily"
FREQ_WEEKDAYS = "weekdays"
FREQ_WEEKLY = "weekly"
FREQ_MONTHLY = "monthly"

FREQ_LABELS = {
    FREQ_NONE: "Без повторения",
    FREQ_DAILY: "Каждые N дней",
    FREQ_WEEKDAYS: "По будням",
    FREQ_WEEKLY: "Еженедельно",
    FREQ_MONTHLY: "Ежемесячно",
}

WEEKDAY_LABELS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def local_timezone_name() -> str | None:
    try:
        from tzlocal import get_localzone_name
        name = get_localzone_name()
        return str(name) if name else None
    except Exception:
        return None


def _zone(value: str | None):
    if not value:
        return None
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Неизвестный часовой пояс повторения") from exc


def _to_wall(dt: datetime, zone) -> tuple[datetime, object]:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(zone) if zone else dt, zone or dt.tzinfo


def _from_wall(value: datetime, zone, original_tzinfo) -> datetime:
    candidate = value.replace(tzinfo=zone)
    return candidate.astimezone(original_tzinfo if zone is None else timezone.utc)


def next_after(rec: Recurrence, dt: datetime) -> datetime:
    """Ближайшее вхождение строго после dt с сохранением локального времени."""
    interval = max(1, rec.interval)
    zone = _zone(rec.timezone)
    wall, wall_zone = _to_wall(dt, zone)
    original_tz = dt.tzinfo or timezone.utc
    if rec.freq == FREQ_DAILY:
        return _from_wall(wall + timedelta(days=interval), wall_zone, original_tz)
    if rec.freq == FREQ_WEEKDAYS:
        nxt = wall + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
        return _from_wall(nxt, wall_zone, original_tz)
    if rec.freq == FREQ_WEEKLY:
        days = sorted(set(rec.weekdays)) if rec.weekdays else [wall.weekday()]
        week_start = wall - timedelta(days=wall.weekday())
        for offset in range(1, 7):
            candidate = week_start + timedelta(days=offset)
            if candidate > wall and candidate.weekday() in days:
                return _from_wall(candidate, wall_zone, original_tz)
        active_week = week_start + timedelta(weeks=interval)
        for offset in range(7):
            candidate = active_week + timedelta(days=offset)
            if candidate.weekday() in days:
                return _from_wall(candidate, wall_zone, original_tz)
        return _from_wall(active_week + timedelta(days=wall.weekday()), wall_zone, original_tz)
    if rec.freq == FREQ_MONTHLY:
        anchor = rec.anchor_day if rec.anchor_day is not None else wall.day
        if not 1 <= anchor <= 31:
            raise ValueError("Некорректный день месяца")
        rec.anchor_day = anchor
        month_index = wall.year * 12 + (wall.month - 1) + interval
        year, month = divmod(month_index, 12)
        month += 1
        day = min(anchor, calendar.monthrange(year, month)[1])
        candidate = wall.replace(year=year, month=month, day=day)
        return _from_wall(candidate, wall_zone, original_tz)
    raise ValueError(f"Неизвестная периодичность: {rec.freq}")


def describe(rec: Recurrence) -> str:
    if rec.freq == FREQ_DAILY:
        return "каждый день" if rec.interval == 1 else f"каждые {rec.interval} дн."
    if rec.freq == FREQ_WEEKDAYS:
        return "по будням"
    if rec.freq == FREQ_WEEKLY:
        interval = "еженед." if rec.interval == 1 else f"каждые {rec.interval} нед."
        if rec.weekdays:
            return interval + ": " + ", ".join(WEEKDAY_LABELS[d] for d in sorted(set(rec.weekdays)))
        return interval
    if rec.freq == FREQ_MONTHLY:
        return "ежемесячно" if rec.interval == 1 else f"каждые {rec.interval} мес."
    return ""

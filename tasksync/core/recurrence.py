from __future__ import annotations

import calendar
from datetime import datetime, timedelta

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


def next_after(rec: Recurrence, dt: datetime) -> datetime:
    """Ближайшее следующее вхождение строго после dt, время суток сохраняется."""
    interval = max(1, rec.interval)
    if rec.freq == FREQ_DAILY:
        return dt + timedelta(days=interval)
    if rec.freq == FREQ_WEEKDAYS:
        nxt = dt + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
        return nxt
    if rec.freq == FREQ_WEEKLY:
        days = sorted(set(rec.weekdays)) if rec.weekdays else [dt.weekday()]
        for offset in range(1, 8):
            cand = dt + timedelta(days=offset)
            if cand.weekday() in days:
                return cand + timedelta(weeks=interval - 1)
        return dt + timedelta(weeks=interval)
    if rec.freq == FREQ_MONTHLY:
        month_index = dt.year * 12 + (dt.month - 1) + interval
        year, month = divmod(month_index, 12)
        month += 1
        day = min(dt.day, calendar.monthrange(year, month)[1])
        return dt.replace(year=year, month=month, day=day)
    raise ValueError(f"Неизвестная периодичность: {rec.freq}")


def describe(rec: Recurrence) -> str:
    if rec.freq == FREQ_DAILY:
        return "каждый день" if rec.interval == 1 else f"каждые {rec.interval} дн."
    if rec.freq == FREQ_WEEKDAYS:
        return "по будням"
    if rec.freq == FREQ_WEEKLY:
        if rec.weekdays:
            return "еженед.: " + ", ".join(WEEKDAY_LABELS[d] for d in sorted(set(rec.weekdays)))
        return "еженедельно" if rec.interval == 1 else f"каждые {rec.interval} нед."
    if rec.freq == FREQ_MONTHLY:
        return "ежемесячно" if rec.interval == 1 else f"каждые {rec.interval} мес."
    return ""
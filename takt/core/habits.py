from __future__ import annotations

from datetime import date, timedelta

from .models import Habit


def current_streak(habit: Habit, today: date) -> int:
    """Серия подряд выполненных запланированных дней, оканчивающаяся сегодня
    (или вчера, если сегодняшний день ещё не отмечен)."""
    d = today
    if habit.scheduled_on(d.weekday()) and not habit.checks.get(d.isoformat()):
        d -= timedelta(days=1)
    count = 0
    for _ in range(3650):
        if habit.scheduled_on(d.weekday()):
            if habit.checks.get(d.isoformat()):
                count += 1
            else:
                break
        d -= timedelta(days=1)
    return count


def week_checks(habit: Habit, today: date) -> list[bool | None]:
    """Пн..Вс: True — отмечено, False — пропущено, None — не запланировано/будущее."""
    monday = today - timedelta(days=today.weekday())
    out = []
    for i in range(7):
        d = monday + timedelta(days=i)
        if d > today or not habit.scheduled_on(d.weekday()):
            out.append(None)
        else:
            out.append(bool(habit.checks.get(d.isoformat())))
    return out
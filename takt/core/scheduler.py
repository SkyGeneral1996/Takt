from __future__ import annotations

import threading
from typing import Callable

from .models import Task, now_iso, parse_iso
from .store import Store


class ReminderScheduler:
    """Периодически проверяет сроки и повторяет неудачные уведомления."""

    def __init__(self, store: Store, on_reminder: Callable[[Task], None],
                 interval_sec: float = 30.0,
                 on_error: Callable[[Task, Exception], None] | None = None):
        if interval_sec <= 0:
            raise ValueError("Интервал напоминаний должен быть положительным")
        self.store = store
        self.on_reminder = on_reminder
        self.on_error = on_error
        self.interval = interval_sec
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="reminders")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=max(1.0, self.interval + 1.0))

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.check_once()
            except Exception:
                pass

    def check_once(self) -> int:
        now = parse_iso(now_iso())
        notified = 0
        for task in self.store.list_tasks(include_completed=False):
            if not task.due_at:
                continue
            try:
                due = parse_iso(task.due_at)
            except (TypeError, ValueError):
                continue
            if due > now:
                continue
            key = f"notified:{task.id}:{task.due_at}"
            if not self.store.claim_meta(key):
                continue
            try:
                self.on_reminder(task)
            except Exception as exc:
                self.store.release_meta(key)
                if self.on_error:
                    self.on_error(task, exc)
                continue
            self.store.set_meta(key, "1")
            notified += 1
        return notified

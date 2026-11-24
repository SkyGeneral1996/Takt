from __future__ import annotations

import threading
from typing import Callable

from .models import Task, now_iso, parse_iso
from .store import Store


class ReminderScheduler:
    """Фоновый поток: раз в 30 секунд ищет невыполненные задачи с наступившим
    сроком и вызывает callback. Повторные уведомления по тому же сроку
    подавляются через meta-ключи."""

    def __init__(self, store: Store, on_reminder: Callable[[Task], None],
                 interval_sec: float = 30.0):
        self.store = store
        self.on_reminder = on_reminder
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

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.check_once()
            except Exception:
                pass  # хранилище могло закрыться при выходе

    def check_once(self) -> None:
        now = parse_iso(now_iso())
        for task in self.store.list_tasks(include_completed=False):
            if not task.due_at:
                continue
            if parse_iso(task.due_at) > now:
                continue
            key = f"notified:{task.id}:{task.due_at}"
            if self.store.get_meta(key):
                continue
            self.store.set_meta(key, "1")
            self.on_reminder(task)
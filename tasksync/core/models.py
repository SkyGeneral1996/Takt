from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat(timespec="milliseconds")


def new_id() -> str:
    return uuid.uuid4().hex


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


PRIORITY_LOW, PRIORITY_MEDIUM, PRIORITY_HIGH = 0, 1, 2
PRIORITY_NAMES = {PRIORITY_LOW: "Низкий", PRIORITY_MEDIUM: "Средний", PRIORITY_HIGH: "Высокий"}


@dataclass
class Recurrence:
    # daily | weekdays | weekly | monthly
    freq: str
    interval: int = 1
    weekdays: list[int] = field(default_factory=list)  # 0 = понедельник

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Recurrence":
        return cls(freq=d["freq"], interval=int(d.get("interval", 1)),
                   weekdays=list(d.get("weekdays", [])))


@dataclass
class Task:
    id: str = field(default_factory=new_id)
    title: str = ""
    notes: str = ""
    category_id: str | None = None
    tags: list[str] = field(default_factory=list)
    priority: int = PRIORITY_MEDIUM
    due_at: str | None = None          # UTC ISO
    recurrence: dict | None = None     # Recurrence.to_dict()
    subtasks: list[dict] = field(default_factory=list)  # {id, title, done}
    completed: bool = False
    completed_at: str | None = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    deleted: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def touch(self) -> None:
        self.updated_at = now_iso()


@dataclass
class Category:
    id: str = field(default_factory=new_id)
    name: str = ""
    color: str = "#2196F3"
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    deleted: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Category":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def touch(self) -> None:
        self.updated_at = now_iso()
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
SCHEMA_VERSION = 1


@dataclass
class Recurrence:
    # daily | weekdays | weekly | monthly
    freq: str
    interval: int = 1
    weekdays: list[int] = field(default_factory=list)  # 0 = понедельник
    anchor_day: int | None = None
    timezone: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Recurrence":
        anchor = d.get("anchor_day")
        timezone_name = d.get("timezone")
        return cls(freq=d["freq"], interval=int(d.get("interval", 1)),
                   weekdays=list(d.get("weekdays", [])),
                   anchor_day=None if anchor is None else int(anchor),
                   timezone=timezone_name if isinstance(timezone_name, str) else None)


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
        return {**asdict(self), "_schema_version": SCHEMA_VERSION}

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        if int(d.get("_schema_version", SCHEMA_VERSION)) > SCHEMA_VERSION:
            raise ValueError("Файл создан более новой версией Takt")
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
        return {**asdict(self), "_schema_version": SCHEMA_VERSION}

    @classmethod
    def from_dict(cls, d: dict) -> "Category":
        if int(d.get("_schema_version", SCHEMA_VERSION)) > SCHEMA_VERSION:
            raise ValueError("Файл создан более новой версией Takt")
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def touch(self) -> None:
        self.updated_at = now_iso()


def new_list_item(text: str = "", qty: float = 1, unit: str = "",
                  price: float | None = None, group: str = "",
                  position: int = 0) -> dict:
    return {"id": new_id(), "text": text, "done": False, "qty": qty,
            "unit": unit, "price": price, "group": group, "position": position}


@dataclass
class CheckList:
    id: str = field(default_factory=new_id)
    name: str = ""
    items: list[dict] = field(default_factory=list)  # ListItem.to_dict()
    task_id: str | None = None
    recurrence: dict | None = None     # Recurrence.to_dict() — повторяемый список
    cycles: int = 0                    # сколько раз список обновляли
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    deleted: bool = False

    def to_dict(self) -> dict:
        return {**asdict(self), "_schema_version": SCHEMA_VERSION}

    @classmethod
    def from_dict(cls, d: dict) -> "CheckList":
        if int(d.get("_schema_version", SCHEMA_VERSION)) > SCHEMA_VERSION:
            raise ValueError("Файл создан более новой версией Takt")
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def touch(self) -> None:
        self.updated_at = now_iso()

    @property
    def progress(self) -> tuple[int, int]:
        return sum(1 for i in self.items if i.get("done")), len(self.items)

    @property
    def total(self) -> float:
        return sum((1 if i.get("qty") is None else i["qty"])
                   * (0 if i.get("price") is None else i["price"])
                   for i in self.items)


@dataclass
class Habit:
    id: str = field(default_factory=new_id)
    name: str = ""
    color: str = "#4CAF50"
    freq: str = "daily"                # daily | weekdays
    weekdays: list[int] = field(default_factory=list)  # 0 = понедельник
    checks: dict = field(default_factory=dict)         # "YYYY-MM-DD" (локальная дата) -> True
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    deleted: bool = False

    def to_dict(self) -> dict:
        return {**asdict(self), "_schema_version": SCHEMA_VERSION}

    @classmethod
    def from_dict(cls, d: dict) -> "Habit":
        if int(d.get("_schema_version", SCHEMA_VERSION)) > SCHEMA_VERSION:
            raise ValueError("Файл создан более новой версией Takt")
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def touch(self) -> None:
        self.updated_at = now_iso()

    def scheduled_on(self, weekday: int) -> bool:
        return self.freq == "daily" or weekday in self.weekdays


@dataclass
class Note:
    id: str = field(default_factory=new_id)
    title: str = ""
    text: str = ""
    tags: list[str] = field(default_factory=list)
    pinned: bool = False
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    deleted: bool = False

    def to_dict(self) -> dict:
        return {**asdict(self), "_schema_version": SCHEMA_VERSION}

    @classmethod
    def from_dict(cls, d: dict) -> "Note":
        if int(d.get("_schema_version", SCHEMA_VERSION)) > SCHEMA_VERSION:
            raise ValueError("Файл создан более новой версией Takt")
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def touch(self) -> None:
        self.updated_at = now_iso()
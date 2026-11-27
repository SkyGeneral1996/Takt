from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import (SCHEMA_VERSION, Task, Category, Recurrence, CheckList,
                     Habit, Note, new_id, now_iso, parse_iso)
from .recurrence import (FREQ_DAILY, FREQ_MONTHLY, FREQ_WEEKDAYS, FREQ_WEEKLY,
                         local_timezone_name, next_after)

ENTITY_TABLES = {"task": "tasks", "category": "categories",
                 "list": "lists", "habit": "habits", "note": "notes"}
ENTITY_MODELS = {"task": Task, "category": Category, "list": CheckList,
                 "habit": Habit, "note": Note}
INTERNAL_FIELDS = {"_schema_version", "_field_clocks", "_checks_clocks"}
DB_VERSION = 1
_EMPTY_CLOCK = (datetime.min.replace(tzinfo=timezone.utc), "", "")


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _utc_iso(value: str) -> str:
    return parse_iso(value).astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _clock(value) -> tuple[datetime, str, str]:
    if (not isinstance(value, (list, tuple)) or len(value) != 3
            or not all(isinstance(part, str) for part in value)):
        raise ValueError("Повреждённые часы поля")
    return parse_iso(value[0]), value[1], value[2]


def _clock_json(clock: tuple[datetime, str, str]) -> list[str]:
    return [clock[0].isoformat(timespec="milliseconds"), clock[1], clock[2]]


class Store:
    """SQLite-хранилище и журнал изменений с версионированными миграциями."""

    def __init__(self, db_path: str | Path, device_id: str | None = None):
        self._lock = threading.RLock()
        self.sync_lock = threading.RLock()
        path = Path(db_path)
        self._conn = sqlite3.connect(str(path), timeout=30.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._backup_before_migration(path)
        self._init_schema()
        self.device_id = device_id or self._get_meta("device_id")
        if not self.device_id:
            self.device_id = new_id()[:12]
            self._set_meta("device_id", self.device_id)
            self._conn.commit()

    def _backup_before_migration(self, path: Path) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version != 0 or path.suffix in {"", ":memory:"}:
            return
        tables = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' LIMIT 1").fetchone()
        if not tables:
            return
        backup = path.with_name(f"{path.stem}.pre-v{DB_VERSION}{path.suffix}")
        if backup.exists():
            return
        target = sqlite3.connect(str(backup))
        try:
            self._conn.backup(target)
        finally:
            target.close()

    def _init_schema(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version > DB_VERSION:
            raise ValueError("База данных создана более новой версией Takt")
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS tasks(
                id TEXT PRIMARY KEY, data TEXT NOT NULL,
                updated_at TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
                source_device_id TEXT NOT NULL DEFAULT '',
                source_event_id TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS categories(
                id TEXT PRIMARY KEY, data TEXT NOT NULL,
                updated_at TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
                source_device_id TEXT NOT NULL DEFAULT '',
                source_event_id TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS lists(
                id TEXT PRIMARY KEY, data TEXT NOT NULL,
                updated_at TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
                source_device_id TEXT NOT NULL DEFAULT '',
                source_event_id TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS habits(
                id TEXT PRIMARY KEY, data TEXT NOT NULL,
                updated_at TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
                source_device_id TEXT NOT NULL DEFAULT '',
                source_event_id TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS notes(
                id TEXT PRIMARY KEY, data TEXT NOT NULL,
                updated_at TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
                source_device_id TEXT NOT NULL DEFAULT '',
                source_event_id TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS completions(
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, at TEXT NOT NULL,
                at_utc TEXT NOT NULL, device_id TEXT NOT NULL,
                source_event_id TEXT NOT NULL, removed INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS changes(
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE NOT NULL,
                device_id TEXT NOT NULL,
                entity TEXT NOT NULL, entity_id TEXT NOT NULL,
                op TEXT NOT NULL, payload TEXT NOT NULL,
                updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS applied(event_id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        """)
        for table in ENTITY_TABLES.values():
            columns = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if "source_device_id" not in columns:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN source_device_id TEXT NOT NULL DEFAULT ''")
            if "source_event_id" not in columns:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN source_event_id TEXT NOT NULL DEFAULT ''")
        self._migrate_completions()
        self._conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_completions_task ON completions(task_id, removed);
            CREATE INDEX IF NOT EXISTS idx_completions_range ON completions(at_utc, removed);
            CREATE INDEX IF NOT EXISTS idx_changes_entity ON changes(entity, entity_id, seq);
        """)
        self._conn.execute(f"PRAGMA user_version={DB_VERSION}")
        self._conn.commit()

    def _migrate_completions(self) -> None:
        columns = {r["name"]: r for r in self._conn.execute("PRAGMA table_info(completions)")}
        if columns["id"]["type"].upper() != "INTEGER":
            return
        self._conn.execute("ALTER TABLE completions RENAME TO completions_legacy")
        self._conn.execute("""
            CREATE TABLE completions(
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, at TEXT NOT NULL,
                at_utc TEXT NOT NULL, device_id TEXT NOT NULL,
                source_event_id TEXT NOT NULL, removed INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL)
        """)
        rows = self._conn.execute(
            "SELECT id,task_id,at FROM completions_legacy ORDER BY id").fetchall()
        for row in rows:
            event_id = f"legacy-completion:{row['id']}"
            payload = {"id": event_id, "task_id": row["task_id"], "at": row["at"],
                       "deleted": False, "_schema_version": SCHEMA_VERSION}
            self._conn.execute(
                "INSERT INTO completions(id,task_id,at,at_utc,device_id,source_event_id,removed,updated_at) "
                "VALUES(?,?,?,?,?,?,0,?)",
                (event_id, row["task_id"], row["at"], _utc_iso(row["at"]),
                 "legacy", event_id, row["at"]))
            self._conn.execute(
                "INSERT OR IGNORE INTO changes(event_id,device_id,entity,entity_id,op,payload,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (event_id, "legacy", "completion", event_id, "upsert",
                 _canonical(payload), row["at"]))
        self._conn.execute("DROP TABLE completions_legacy")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- meta ----------

    def _get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def _set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            return self._get_meta(key, default)

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._set_meta(key, value)

    def claim_meta(self, key: str, pending_ttl: float = 120.0) -> bool:
        with self._lock, self._conn:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            if row:
                if row["value"] == "1":
                    return False
                try:
                    pending_at = parse_iso(row["value"])
                except ValueError:
                    pending_at = None
                if pending_at and (parse_iso(now_iso()) - pending_at).total_seconds() < pending_ttl:
                    return False
            self._set_meta(key, now_iso())
            return True

    def release_meta(self, key: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM meta WHERE key=? AND value!='1'", (key,))

    # ---------- validation and field clocks ----------

    @classmethod
    def validate_event(cls, event: dict) -> dict:
        if not isinstance(event, dict):
            raise ValueError("Событие переноса повреждено")
        normalized = dict(event)
        for key in ("event_id", "device_id", "entity", "entity_id"):
            value = normalized.get(key)
            if not isinstance(value, str) or not value or len(value) > 256:
                raise ValueError(f"Некорректное поле события: {key}")
        if normalized["entity"] not in {*ENTITY_TABLES, "completion"}:
            raise ValueError("Неизвестный тип данных")
        if normalized.get("op") not in {"upsert", "delete"}:
            raise ValueError("Некорректная операция")
        payload = normalized.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("Некорректные данные события")
        if int(payload.get("_schema_version", SCHEMA_VERSION)) > SCHEMA_VERSION:
            raise ValueError("Данные созданы более новой версией Takt")
        updated_at = payload.get("updated_at", normalized.get("updated_at"))
        if not isinstance(updated_at, str):
            raise ValueError("Нет времени изменения")
        parse_iso(updated_at)
        normalized["updated_at"] = updated_at
        if normalized["entity"] == "completion":
            cls._validate_completion_payload(normalized["entity_id"], payload)
        else:
            cls._validate_entity_payload(normalized["entity"], normalized["entity_id"], payload)
            clocks = payload.get("_field_clocks", {})
            if not isinstance(clocks, dict):
                raise ValueError("Повреждённые часы полей")
            for value in clocks.values():
                _clock(value)
            if normalized["entity"] == "habit":
                checks = payload.get("_checks_clocks", {})
                if not isinstance(checks, dict):
                    raise ValueError("Повреждённые часы привычки")
                for value in checks.values():
                    _clock(value)
        _canonical(normalized)
        return normalized

    @staticmethod
    def _validate_completion_payload(entity_id: str, payload: dict) -> None:
        if payload.get("id") != entity_id:
            raise ValueError("Идентификатор выполнения не совпадает")
        if not isinstance(payload.get("task_id"), str) or not payload["task_id"]:
            raise ValueError("Нет задачи для выполнения")
        if not isinstance(payload.get("at"), str):
            raise ValueError("Нет времени выполнения")
        parse_iso(payload["at"])
        if not isinstance(payload.get("deleted", False), bool):
            raise ValueError("Некорректный признак удаления")

    @classmethod
    def _validate_entity_payload(cls, entity: str, entity_id: str, payload: dict) -> None:
        if payload.get("id") != entity_id:
            raise ValueError("Идентификатор объекта не совпадает")
        model = ENTITY_MODELS[entity]
        try:
            model.from_dict(payload)
        except (TypeError, ValueError, KeyError) as exc:
            raise ValueError("Объект имеет неверную структуру") from exc
        for key in ("created_at", "updated_at"):
            if not isinstance(payload.get(key), str):
                raise ValueError(f"Нет поля {key}")
            parse_iso(payload[key])
        if not isinstance(payload.get("deleted", False), bool):
            raise ValueError("Некорректный признак удаления")
        if entity == "task":
            cls._validate_task(payload)
        elif entity == "category":
            cls._validate_text(payload, "name")
            cls._validate_color(payload)
        elif entity == "list":
            cls._validate_text(payload, "name")
            cls._validate_list_items(payload.get("items"))
        elif entity == "habit":
            cls._validate_habit(payload)
            cls._validate_color(payload)
        elif entity == "note":
            cls._validate_text(payload, "title")
            cls._validate_text(payload, "text")

    @staticmethod
    def _validate_text(payload: dict, key: str) -> None:
        if not isinstance(payload.get(key), str):
            raise ValueError(f"Некорректное поле {key}")

    @staticmethod
    def _validate_color(payload: dict) -> None:
        color = payload.get("color")
        if not isinstance(color, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
            raise ValueError("Некорректный цвет")

    @staticmethod
    def _validate_task(payload: dict) -> None:
        Store._validate_text(payload, "title")
        Store._validate_text(payload, "notes")
        if not isinstance(payload.get("completed"), bool):
            raise ValueError("Некорректный статус задачи")
        if not isinstance(payload.get("priority"), int) or isinstance(payload.get("priority"), bool):
            raise ValueError("Некорректный приоритет")
        if not isinstance(payload.get("tags"), list) or not all(
                isinstance(tag, str) for tag in payload["tags"]):
            raise ValueError("Некорректные метки")
        if payload.get("due_at") is not None:
            parse_iso(payload["due_at"])
        if payload.get("recurrence") is not None:
            rec = payload["recurrence"]
            if not isinstance(rec, dict) or rec.get("freq") not in {
                    FREQ_DAILY, FREQ_WEEKDAYS, FREQ_WEEKLY, FREQ_MONTHLY}:
                raise ValueError("Некорректное повторение")
            interval = rec.get("interval", 1)
            if not isinstance(interval, int) or isinstance(interval, bool) or interval < 1:
                raise ValueError("Некорректный интервал")
            weekdays = rec.get("weekdays", [])
            if not isinstance(weekdays, list) or not all(
                    isinstance(day, int) and not isinstance(day, bool) and 0 <= day <= 6
                    for day in weekdays):
                raise ValueError("Некорректные дни недели")
            anchor = rec.get("anchor_day")
            if anchor is not None and (not isinstance(anchor, int) or isinstance(anchor, bool)
                                       or not 1 <= anchor <= 31):
                raise ValueError("Некорректный день месяца")
            timezone_name = rec.get("timezone")
            if timezone_name is not None:
                if not isinstance(timezone_name, str) or not timezone_name:
                    raise ValueError("Некорректный часовой пояс")
                try:
                    ZoneInfo(timezone_name)
                except (ZoneInfoNotFoundError, ValueError) as exc:
                    raise ValueError("Неизвестный часовой пояс") from exc
        subtasks = payload.get("subtasks")
        if not isinstance(subtasks, list):
            raise ValueError("Некорректные подзадачи")
        for item in subtasks:
            if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                    or not isinstance(item.get("title"), str)
                    or not isinstance(item.get("done"), bool)):
                raise ValueError("Некорректная подзадача")

    @staticmethod
    def _validate_list_items(items) -> None:
        if not isinstance(items, list):
            raise ValueError("Некорректные позиции списка")
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                raise ValueError("Некорректная позиция списка")
            for key in ("qty", "price"):
                value = item.get(key)
                if value is not None and (not isinstance(value, (int, float))
                                          or isinstance(value, bool)
                                          or not math.isfinite(value)):
                    raise ValueError(f"Некорректное число: {key}")

    @staticmethod
    def _validate_habit(payload: dict) -> None:
        Store._validate_text(payload, "name")
        if payload.get("freq") not in {FREQ_DAILY, FREQ_WEEKDAYS}:
            raise ValueError("Некорректная частота привычки")
        weekdays = payload.get("weekdays")
        if not isinstance(weekdays, list) or not all(
                isinstance(day, int) and not isinstance(day, bool) and 0 <= day <= 6
                for day in weekdays):
            raise ValueError("Некорректные дни недели")
        checks = payload.get("checks")
        if not isinstance(checks, dict):
            raise ValueError("Некорректные отметки привычки")
        for day, done in checks.items():
            try:
                parsed = date.fromisoformat(day)
            except (TypeError, ValueError) as exc:
                raise ValueError("Некорректная дата привычки") from exc
            if parsed.isoformat() != day or done is not True:
                raise ValueError("Некорректная отметка привычки")

    def _prepare_payload(self, entity: str, obj, event_id: str,
                         previous: sqlite3.Row | None) -> dict:
        payload = obj.to_dict()
        old = json.loads(previous["data"]) if previous else {}
        old_clocks = old.get("_field_clocks", {}) if isinstance(old.get("_field_clocks"), dict) else {}
        default = ((parse_iso(previous["updated_at"]), previous["source_device_id"],
                    previous["source_event_id"]) if previous else None)
        current = (parse_iso(obj.updated_at), self.device_id, event_id)
        clocks = dict(old_clocks)
        for key, value in payload.items():
            if key in INTERNAL_FIELDS or key == "updated_at":
                continue
            if previous is None or old.get(key) != value:
                clocks[key] = _clock_json(current)
            elif key not in clocks and default is not None:
                clocks[key] = _clock_json(default)
        if entity == "habit":
            old_checks = old.get("checks", {})
            check_clocks = dict(old.get("_checks_clocks", {}))
            for day in set(old_checks) | set(payload["checks"]):
                if old_checks.get(day) != payload["checks"].get(day):
                    check_clocks[day] = _clock_json(current)
                elif day not in check_clocks and default is not None:
                    check_clocks[day] = _clock_json(default)
            payload["_checks_clocks"] = check_clocks
            if check_clocks:
                clocks["checks"] = _clock_json(max(_clock(v) for v in check_clocks.values()))
            elif "checks" in clocks:
                clocks["checks"] = _clock_json(current)
        payload["_field_clocks"] = clocks
        return payload

    @staticmethod
    def _merge_value(entity: str, field: str, local_payload: dict,
                     incoming_payload: dict, local_base: tuple, incoming_base: tuple):
        local_clocks = local_payload.get("_field_clocks", {})
        incoming_clocks = incoming_payload.get("_field_clocks", {})
        if field == "checks" and entity == "habit":
            local_checks = local_payload.get("checks", {})
            incoming_checks = incoming_payload.get("checks", {})
            local_check_clocks = local_payload.get("_checks_clocks", {})
            incoming_check_clocks = incoming_payload.get("_checks_clocks", {})
            merged_checks = {}
            merged_clocks = {}
            for day in set(local_checks) | set(incoming_checks) | set(local_check_clocks) | set(incoming_check_clocks):
                local_value = True if day in local_checks else None
                incoming_value = True if day in incoming_checks else None
                local_clock = (_clock(local_check_clocks[day]) if day in local_check_clocks
                               else _clock(local_clocks["checks"]) if day in local_checks
                               else _EMPTY_CLOCK)
                incoming_clock = (_clock(incoming_check_clocks[day]) if day in incoming_check_clocks
                                  else _clock(incoming_clocks["checks"]) if day in incoming_checks
                                  else _EMPTY_CLOCK)
                local_key = (*local_clock, _canonical(local_value))
                incoming_key = (*incoming_clock, _canonical(incoming_value))
                chosen_clock = incoming_clock if incoming_key >= local_key else local_clock
                merged_clocks[day] = _clock_json(chosen_clock)
                if incoming_key >= local_key:
                    if incoming_value is not None:
                        merged_checks[day] = True
                elif local_value is not None:
                    merged_checks[day] = True
            return merged_checks, merged_clocks
        local_clock = _clock(local_clocks.get(field) or _clock_json(local_base))
        incoming_clock = _clock(incoming_clocks.get(field) or _clock_json(incoming_base))
        local_key = (*local_clock, _canonical(local_payload.get(field)))
        incoming_key = (*incoming_clock, _canonical(incoming_payload.get(field)))
        if incoming_key >= local_key:
            return incoming_payload.get(field), incoming_clock
        return local_payload.get(field), local_clock

    def _merge_entity(self, entity: str, row: sqlite3.Row | None,
                      event: dict) -> tuple[dict, bool]:
        incoming = dict(event["payload"])
        if event["op"] == "delete":
            incoming["deleted"] = True
        if row is None:
            event_clock = [event["updated_at"], event["device_id"], event["event_id"]]
            field_clocks = dict(incoming.get("_field_clocks", {}))
            for key in incoming:
                if key not in INTERNAL_FIELDS and key != "updated_at":
                    field_clocks.setdefault(key, event_clock)
            incoming["_field_clocks"] = field_clocks
            if entity == "habit":
                check_clocks = dict(incoming.get("_checks_clocks", {}))
                for day in incoming.get("checks", {}):
                    check_clocks.setdefault(day, event_clock)
                incoming["_checks_clocks"] = check_clocks
            return incoming, True
        local = json.loads(row["data"])
        local_base = (parse_iso(row["updated_at"]), row["source_device_id"], row["source_event_id"])
        incoming_base = (parse_iso(event["updated_at"]), event["device_id"], event["event_id"])
        fields = ((set(local) | set(incoming)) - INTERNAL_FIELDS - {"updated_at"})
        if entity == "habit":
            fields.discard("checks")
        merged = {}
        clocks = {}
        for field in fields:
            value, clock = self._merge_value(entity, field, local, incoming, local_base, incoming_base)
            merged[field] = value
            clocks[field] = _clock_json(clock)
        if entity == "habit":
            checks, check_clocks = self._merge_value(
                entity, "checks", local, incoming, local_base, incoming_base)
            merged["checks"] = checks
            merged["_checks_clocks"] = check_clocks
            if check_clocks:
                clocks["checks"] = _clock_json(max(_clock(v) for v in check_clocks.values()))
        merged["_field_clocks"] = clocks
        merged["_schema_version"] = SCHEMA_VERSION
        timestamps = [parse_iso(row["updated_at"]), parse_iso(event["updated_at"])]
        timestamps.extend(_clock(value)[0] for value in clocks.values())
        merged["updated_at"] = max(timestamps).astimezone(timezone.utc).isoformat(timespec="milliseconds")
        changed = (_canonical(local) != _canonical(merged)
                   or bool(row["deleted"]) != bool(merged.get("deleted")))
        return merged, changed

    # ---------- journal ----------

    def _append_change(self, entity: str, entity_id: str, op: str,
                       payload: dict, updated_at: str, event_id: str) -> None:
        self._conn.execute(
            "INSERT INTO changes(event_id,device_id,entity,entity_id,op,payload,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (event_id, self.device_id, entity, entity_id, op,
             _canonical(payload), updated_at))

    def journal_batch(self) -> tuple[list[dict], int]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM changes ORDER BY seq").fetchall()
        events = [{"event_id": r["event_id"], "device_id": r["device_id"],
                   "entity": r["entity"], "entity_id": r["entity_id"],
                   "op": r["op"], "payload": json.loads(r["payload"]),
                   "updated_at": r["updated_at"]} for r in rows]
        return events, rows[-1]["seq"] if rows else 0

    def local_events(self) -> list[dict]:
        return self.journal_batch()[0]

    def compact_changes(self, up_to_seq: int) -> int:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM changes WHERE seq<=? AND seq NOT IN ("
                "SELECT MAX(seq) FROM changes WHERE seq<=? GROUP BY entity,entity_id)",
                (up_to_seq, up_to_seq))
            return cursor.rowcount

    def apply_remote_events(self, events: list[dict]) -> int:
        validated = []
        for event in events:
            normalized = self.validate_event(event)
            if normalized["device_id"] != self.device_id:
                validated.append(normalized)
        applied = 0
        with self._lock, self._conn:
            for event in validated:
                if self._conn.execute(
                        "SELECT 1 FROM applied WHERE event_id=?", (event["event_id"],)).fetchone():
                    continue
                if event["entity"] == "completion":
                    changed = self._apply_completion(event)
                else:
                    changed = self._apply_entity(event)
                self._conn.execute("INSERT INTO applied(event_id) VALUES(?)", (event["event_id"],))
                applied += int(changed)
        return applied

    def preview_remote_events(self, events: list[dict]) -> list[tuple[dict, str]]:
        validated = []
        for event in events:
            normalized = self.validate_event(event)
            if normalized["device_id"] != self.device_id:
                validated.append(normalized)
        actions = []
        with self._lock:
            self._conn.execute("SAVEPOINT transfer_preview")
            try:
                for event in validated:
                    incoming_deleted = bool(event["payload"].get("deleted")) or event["op"] == "delete"
                    if event["entity"] == "completion":
                        row = self._conn.execute(
                            "SELECT * FROM completions WHERE id=?", (event["entity_id"],)).fetchone()
                        changed = self._apply_completion(event)
                    else:
                        table = ENTITY_TABLES[event["entity"]]
                        row = self._conn.execute(
                            f"SELECT * FROM {table} WHERE id=?", (event["entity_id"],)).fetchone()
                        changed = self._apply_entity(event)
                    self._conn.execute(
                        "INSERT OR IGNORE INTO applied(event_id) VALUES(?)", (event["event_id"],))
                    current_deleted = bool(row["deleted"] if event["entity"] != "completion" else row["removed"]) if row else False
                    if row is None:
                        action = "unchanged" if incoming_deleted else "new"
                    elif changed:
                        if incoming_deleted and not current_deleted:
                            action = "deleted"
                        elif current_deleted and not incoming_deleted:
                            action = "new"
                        else:
                            action = "updated"
                    else:
                        if event["entity"] == "completion":
                            local_clock = (parse_iso(row["updated_at"]), row["device_id"],
                                           row["source_event_id"])
                        else:
                            local_clock = (parse_iso(row["updated_at"]), row["source_device_id"],
                                           row["source_event_id"])
                        incoming_clock = (parse_iso(event["updated_at"]), event["device_id"],
                                          event["event_id"])
                        action = "older" if incoming_clock < local_clock else "unchanged"
                    actions.append((event, action))
            finally:
                self._conn.execute("ROLLBACK TO transfer_preview")
                self._conn.execute("RELEASE transfer_preview")
        return actions

    def _apply_entity(self, event: dict) -> bool:
        table = ENTITY_TABLES[event["entity"]]
        row = self._conn.execute(
            f"SELECT * FROM {table} WHERE id=?", (event["entity_id"],)).fetchone()
        payload, changed = self._merge_entity(event["entity"], row, event)
        if not changed:
            return False
        self._conn.execute(
            f"INSERT INTO {table}(id,data,updated_at,deleted,source_device_id,source_event_id) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data, "
            "updated_at=excluded.updated_at, deleted=excluded.deleted, "
            "source_device_id=excluded.source_device_id, source_event_id=excluded.source_event_id",
            (event["entity_id"], _canonical(payload), payload["updated_at"],
             int(bool(payload.get("deleted"))), event["device_id"], event["event_id"]))
        return True

    def _apply_completion(self, event: dict) -> bool:
        payload = event["payload"]
        row = self._conn.execute(
            "SELECT * FROM completions WHERE id=?", (event["entity_id"],)).fetchone()
        incoming_clock = (parse_iso(event["updated_at"]), event["device_id"], event["event_id"])
        if row:
            local_clock = (parse_iso(row["updated_at"]), row["device_id"], row["source_event_id"])
            if incoming_clock <= local_clock:
                return False
        removed = bool(payload.get("deleted")) or event["op"] == "delete"
        self._conn.execute(
            "INSERT INTO completions(id,task_id,at,at_utc,device_id,source_event_id,removed,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET task_id=excluded.task_id, "
            "at=excluded.at, at_utc=excluded.at_utc, device_id=excluded.device_id, "
            "source_event_id=excluded.source_event_id, removed=excluded.removed, "
            "updated_at=excluded.updated_at",
            (event["entity_id"], payload["task_id"], payload["at"], _utc_iso(payload["at"]),
             event["device_id"], event["event_id"], int(removed), event["updated_at"]))
        return True

    # ---------- entity persistence ----------

    def _write_entity(self, entity: str, obj) -> None:
        table = ENTITY_TABLES[entity]
        obj.touch()
        event_id = new_id()
        row = self._conn.execute(
            f"SELECT * FROM {table} WHERE id=?", (obj.id,)).fetchone()
        payload = self._prepare_payload(entity, obj, event_id, row)
        self._conn.execute(
            f"INSERT INTO {table}(id,data,updated_at,deleted,source_device_id,source_event_id) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data, "
            "updated_at=excluded.updated_at, deleted=excluded.deleted, "
            "source_device_id=excluded.source_device_id, source_event_id=excluded.source_event_id",
            (obj.id, _canonical(payload), obj.updated_at, int(bool(obj.deleted)),
             self.device_id, event_id))
        self._append_change(entity, obj.id, "delete" if obj.deleted else "upsert",
                            payload, obj.updated_at, event_id)

    def _read_entity(self, entity: str, entity_id: str, include_deleted: bool = False):
        table = ENTITY_TABLES[entity]
        query = f"SELECT data FROM {table} WHERE id=?"
        if not include_deleted:
            query += " AND deleted=0"
        row = self._conn.execute(query, (entity_id,)).fetchone()
        return ENTITY_MODELS[entity].from_dict(json.loads(row["data"])) if row else None

    def _delete_entity(self, entity: str, entity_id: str) -> None:
        obj = self._read_entity(entity, entity_id, include_deleted=True)
        if obj and not obj.deleted:
            obj.deleted = True
            self._write_entity(entity, obj)

    # ---------- categories ----------

    def save_category(self, cat: Category) -> Category:
        with self._lock, self._conn:
            self._write_entity("category", cat)
            return cat

    def delete_category(self, category_id: str) -> None:
        with self._lock, self._conn:
            self._delete_entity("category", category_id)

    def get_category(self, category_id: str) -> Category | None:
        with self._lock:
            return self._read_entity("category", category_id)

    def list_categories(self) -> list[Category]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM categories WHERE deleted=0 ORDER BY updated_at").fetchall()
        return sorted((Category.from_dict(json.loads(r["data"])) for r in rows),
                      key=lambda cat: cat.name.lower())

    # ---------- tasks ----------

    def save_task(self, task: Task) -> Task:
        with self._lock, self._conn:
            self._write_entity("task", task)
            return task

    def delete_task(self, task_id: str) -> None:
        with self._lock, self._conn:
            self._delete_entity("task", task_id)

    def get_task(self, task_id: str, include_deleted: bool = False) -> Task | None:
        with self._lock:
            return self._read_entity("task", task_id, include_deleted)

    def list_tasks(self, include_completed: bool = True) -> list[Task]:
        with self._lock:
            rows = self._conn.execute("SELECT data FROM tasks WHERE deleted=0").fetchall()
        tasks = [Task.from_dict(json.loads(r["data"])) for r in rows]
        return tasks if include_completed else [task for task in tasks if not task.completed]

    def tasks_by_ids(self, task_ids) -> dict[str, Task]:
        ids = list(set(task_ids))
        tasks = {}
        with self._lock:
            for offset in range(0, len(ids), 500):
                chunk = ids[offset:offset + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = self._conn.execute(
                    f"SELECT data FROM tasks WHERE id IN ({placeholders})", chunk).fetchall()
                tasks.update({task.id: task for task in
                              (Task.from_dict(json.loads(row["data"])) for row in rows)})
        return tasks

    def complete_task(self, task_id: str) -> Task | None:
        with self._lock, self._conn:
            task = self._read_entity("task", task_id)
            if not task or (task.completed and not task.recurrence):
                return task
            now = now_iso()
            if task.recurrence and task.due_at and parse_iso(task.due_at) > parse_iso(now):
                return task
            if task.recurrence:
                occurrence = task.due_at or task.updated_at
                completion_id = "completion:" + hashlib.sha256(
                    f"{task.id}:{occurrence}".encode("utf-8")).hexdigest()
            else:
                completion_id = new_id()
            exists = self._conn.execute(
                "SELECT 1 FROM completions WHERE id=? AND removed=0", (completion_id,)).fetchone()
            if exists:
                return task
            completion = {"id": completion_id, "task_id": task.id, "at": now,
                          "deleted": False, "_schema_version": SCHEMA_VERSION}
            self._conn.execute(
                "INSERT INTO completions(id,task_id,at,at_utc,device_id,source_event_id,removed,updated_at) "
                "VALUES(?,?,?,?,?,?,0,?)",
                (completion_id, task.id, now, _utc_iso(now), self.device_id, completion_id, now))
            self._append_change("completion", completion_id, "upsert", completion, now, completion_id)
            if task.recurrence:
                rec = Recurrence.from_dict(task.recurrence)
                if not rec.timezone:
                    rec.timezone = local_timezone_name()
                base = parse_iso(task.due_at) if task.due_at else parse_iso(now)
                nxt = next_after(rec, base)
                current = parse_iso(now)
                iterations = 0
                while nxt <= current and iterations < 100000:
                    nxt = next_after(rec, nxt)
                    iterations += 1
                task.due_at = nxt.isoformat(timespec="milliseconds")
                task.completed = False
                task.completed_at = None
                for subtask in task.subtasks:
                    subtask["done"] = False
                task.recurrence = rec.to_dict()
            else:
                task.completed = True
                task.completed_at = now
            self._write_entity("task", task)
            return task

    def uncomplete_task(self, task_id: str) -> Task | None:
        with self._lock, self._conn:
            task = self._read_entity("task", task_id)
            if not task or not task.completed:
                return task
            row = self._conn.execute(
                "SELECT * FROM completions WHERE task_id=? AND removed=0 "
                "ORDER BY updated_at DESC, at_utc DESC, id DESC LIMIT 1", (task_id,)).fetchone()
            if row:
                now = now_iso()
                payload = {"id": row["id"], "task_id": task_id, "at": row["at"],
                           "deleted": True, "_schema_version": SCHEMA_VERSION}
                event_id = new_id()
                self._conn.execute(
                    "UPDATE completions SET removed=1,updated_at=?,source_event_id=? WHERE id=?",
                    (now, event_id, row["id"]))
                self._append_change("completion", row["id"], "delete", payload, now, event_id)
            task.completed = False
            task.completed_at = None
            self._write_entity("task", task)
            return task

    def completions_for(self, task_id: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT at FROM completions WHERE task_id=? AND removed=0 ORDER BY at_utc",
                (task_id,)).fetchall()
        return [row["at"] for row in rows]

    def completions_between(self, start_iso: str, end_iso: str) -> list[dict]:
        start, end = _utc_iso(start_iso), _utc_iso(end_iso)
        with self._lock:
            rows = self._conn.execute(
                "SELECT task_id,at FROM completions WHERE removed=0 AND at_utc>=? AND at_utc<? "
                "ORDER BY at_utc", (start, end)).fetchall()
        return [{"task_id": row["task_id"], "at": row["at"]} for row in rows]

    # ---------- lists, habits, notes ----------

    def save_list(self, lst: CheckList) -> CheckList:
        with self._lock, self._conn:
            self._write_entity("list", lst)
            return lst

    def delete_list(self, list_id: str) -> None:
        with self._lock, self._conn:
            self._delete_entity("list", list_id)

    def get_list(self, list_id: str) -> CheckList | None:
        with self._lock:
            return self._read_entity("list", list_id)

    def list_lists(self) -> list[CheckList]:
        with self._lock:
            rows = self._conn.execute("SELECT data FROM lists WHERE deleted=0").fetchall()
        return sorted((CheckList.from_dict(json.loads(r["data"])) for r in rows),
                      key=lambda lst: lst.updated_at, reverse=True)

    def reset_list(self, list_id: str) -> CheckList | None:
        with self._lock, self._conn:
            lst = self._read_entity("list", list_id)
            if not lst:
                return None
            for item in lst.items:
                item["done"] = False
            lst.cycles += 1
            self._write_entity("list", lst)
            return lst

    def save_habit(self, habit: Habit) -> Habit:
        with self._lock, self._conn:
            self._write_entity("habit", habit)
            return habit

    def delete_habit(self, habit_id: str) -> None:
        with self._lock, self._conn:
            self._delete_entity("habit", habit_id)

    def get_habit(self, habit_id: str) -> Habit | None:
        with self._lock:
            return self._read_entity("habit", habit_id)

    def list_habits(self) -> list[Habit]:
        with self._lock:
            rows = self._conn.execute("SELECT data FROM habits WHERE deleted=0").fetchall()
        return sorted((Habit.from_dict(json.loads(r["data"])) for r in rows),
                      key=lambda habit: habit.created_at)

    def check_habit(self, habit_id: str, day: str, done: bool = True) -> Habit | None:
        try:
            parsed = date.fromisoformat(day)
        except (TypeError, ValueError) as exc:
            raise ValueError("Некорректная дата привычки") from exc
        if parsed.isoformat() != day:
            raise ValueError("Некорректная дата привычки")
        with self._lock, self._conn:
            habit = self._read_entity("habit", habit_id)
            if not habit:
                return None
            if done:
                habit.checks[day] = True
            else:
                habit.checks.pop(day, None)
            self._write_entity("habit", habit)
            return habit

    def save_note(self, note: Note) -> Note:
        with self._lock, self._conn:
            self._write_entity("note", note)
            return note

    def delete_note(self, note_id: str) -> None:
        with self._lock, self._conn:
            self._delete_entity("note", note_id)

    def get_note(self, note_id: str) -> Note | None:
        with self._lock:
            return self._read_entity("note", note_id)

    def list_notes(self) -> list[Note]:
        with self._lock:
            rows = self._conn.execute("SELECT data FROM notes WHERE deleted=0").fetchall()
        notes = [Note.from_dict(json.loads(r["data"])) for r in rows]
        notes.sort(key=lambda note: note.updated_at, reverse=True)
        notes.sort(key=lambda note: not note.pinned)
        return notes

    # ---------- export ----------

    def export_state(self) -> list[dict]:
        with self._lock:
            out = []
            for entity, table in ENTITY_TABLES.items():
                for row in self._conn.execute(f"SELECT id,data,updated_at FROM {table}"):
                    out.append({"entity": entity, "entity_id": row["id"],
                                "payload": json.loads(row["data"]),
                                "updated_at": row["updated_at"]})
            for row in self._conn.execute("SELECT * FROM completions"):
                out.append({"entity": "completion", "entity_id": row["id"],
                            "payload": {"id": row["id"], "task_id": row["task_id"],
                                        "at": row["at"], "deleted": bool(row["removed"]),
                                        "_schema_version": SCHEMA_VERSION},
                            "updated_at": row["updated_at"]})
            return out

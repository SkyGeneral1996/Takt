from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .models import (Task, Category, Recurrence, CheckList, Habit, Note,
                     new_id, now_iso, parse_iso)
from .recurrence import next_after

ENTITY_TABLES = {"task": "tasks", "category": "categories",
                 "list": "lists", "habit": "habits", "note": "notes"}


class Store:
    """SQLite-хранилище + append-only журнал изменений для синхронизации.

    Каждая локальная мутация порождает событие в changes. Удаление — tombstone
    (deleted=True), чтобы удаление корректно разъезжалось по устройствам.
    """

    def __init__(self, db_path: str | Path, device_id: str | None = None):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        self.device_id = device_id or self._get_meta("device_id")
        if not self.device_id:
            self.device_id = new_id()[:12]
            self._set_meta("device_id", self.device_id)

    def _init_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS tasks(
                id TEXT PRIMARY KEY, data TEXT NOT NULL,
                updated_at TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS categories(
                id TEXT PRIMARY KEY, data TEXT NOT NULL,
                updated_at TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS lists(
                id TEXT PRIMARY KEY, data TEXT NOT NULL,
                updated_at TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS habits(
                id TEXT PRIMARY KEY, data TEXT NOT NULL,
                updated_at TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS notes(
                id TEXT PRIMARY KEY, data TEXT NOT NULL,
                updated_at TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS completions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL, at TEXT NOT NULL);
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
        self._conn.commit()

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
        self._conn.commit()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            return self._get_meta(key, default)

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._set_meta(key, value)

    # ---------- журнал ----------

    def _append_change(self, entity: str, obj: Task | Category, op: str = "upsert") -> None:
        self._conn.execute(
            "INSERT INTO changes(event_id,device_id,entity,entity_id,op,payload,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (new_id(), self.device_id, entity, obj.id, op,
             json.dumps(obj.to_dict(), ensure_ascii=False), obj.updated_at))

    def local_events(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM changes ORDER BY seq").fetchall()
        events = []
        for r in rows:
            events.append({
                "event_id": r["event_id"], "device_id": r["device_id"],
                "entity": r["entity"], "entity_id": r["entity_id"],
                "op": r["op"], "payload": json.loads(r["payload"]),
                "updated_at": r["updated_at"],
            })
        return events

    def apply_remote_events(self, events: list[dict]) -> int:
        """Применяет чужие события. Конфликт решает LWW по updated_at сущности."""
        applied = 0
        with self._lock:
            for ev in events:
                if ev.get("device_id") == self.device_id:
                    continue
                eid = ev["event_id"]
                if self._conn.execute("SELECT 1 FROM applied WHERE event_id=?", (eid,)).fetchone():
                    continue
                self._conn.execute("INSERT INTO applied(event_id) VALUES(?)", (eid,))
                table = ENTITY_TABLES.get(ev["entity"])
                if table is None:
                    continue
                row = self._conn.execute(
                    f"SELECT updated_at FROM {table} WHERE id=?", (ev["entity_id"],)).fetchone()
                payload = ev["payload"]
                if row and row["updated_at"] >= payload.get("updated_at", ev["updated_at"]):
                    continue
                self._conn.execute(
                    f"INSERT INTO {table}(id,data,updated_at,deleted) VALUES(?,?,?,?) "
                    f"ON CONFLICT(id) DO UPDATE SET data=excluded.data, "
                    f"updated_at=excluded.updated_at, deleted=excluded.deleted",
                    (ev["entity_id"], json.dumps(payload, ensure_ascii=False),
                     payload.get("updated_at", ev["updated_at"]),
                     1 if payload.get("deleted") else 0))
                applied += 1
            self._conn.commit()
        return applied

    # ---------- категории ----------

    def save_category(self, cat: Category) -> Category:
        with self._lock:
            cat.touch()
            self._conn.execute(
                "INSERT INTO categories(id,data,updated_at,deleted) VALUES(?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data, "
                "updated_at=excluded.updated_at, deleted=excluded.deleted",
                (cat.id, json.dumps(cat.to_dict(), ensure_ascii=False),
                 cat.updated_at, 1 if cat.deleted else 0))
            self._append_change("category", cat)
            self._conn.commit()
            return cat

    def delete_category(self, category_id: str) -> None:
        cat = self.get_category(category_id)
        if cat:
            cat.deleted = True
            self.save_category(cat)

    def get_category(self, category_id: str) -> Category | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM categories WHERE id=? AND deleted=0", (category_id,)).fetchone()
        return Category.from_dict(json.loads(row["data"])) if row else None

    def list_categories(self) -> list[Category]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM categories WHERE deleted=0 ORDER BY updated_at").fetchall()
        cats = [Category.from_dict(json.loads(r["data"])) for r in rows]
        return sorted(cats, key=lambda c: c.name.lower())

    # ---------- задачи ----------

    def save_task(self, task: Task) -> Task:
        with self._lock:
            task.touch()
            self._conn.execute(
                "INSERT INTO tasks(id,data,updated_at,deleted) VALUES(?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data, "
                "updated_at=excluded.updated_at, deleted=excluded.deleted",
                (task.id, json.dumps(task.to_dict(), ensure_ascii=False),
                 task.updated_at, 1 if task.deleted else 0))
            self._append_change("task", task)
            self._conn.commit()
            return task

    def delete_task(self, task_id: str) -> None:
        task = self.get_task(task_id, include_deleted=True)
        if task:
            task.deleted = True
            self.save_task(task)

    def get_task(self, task_id: str, include_deleted: bool = False) -> Task | None:
        with self._lock:
            q = "SELECT data FROM tasks WHERE id=?"
            if not include_deleted:
                q += " AND deleted=0"
            row = self._conn.execute(q, (task_id,)).fetchone()
        return Task.from_dict(json.loads(row["data"])) if row else None

    def list_tasks(self, include_completed: bool = True) -> list[Task]:
        with self._lock:
            rows = self._conn.execute("SELECT data FROM tasks WHERE deleted=0").fetchall()
        tasks = [Task.from_dict(json.loads(r["data"])) for r in rows]
        if not include_completed:
            tasks = [t for t in tasks if not t.completed]
        return tasks

    def complete_task(self, task_id: str) -> Task | None:
        """Повторяющаяся задача: фиксируем выполнение и переносим на следующий срок.
        Обычная: помечаем выполненной."""
        task = self.get_task(task_id)
        if not task:
            return None
        with self._lock:
            self._conn.execute(
                "INSERT INTO completions(task_id,at) VALUES(?,?)", (task.id, now_iso()))
            if task.recurrence:
                rec = Recurrence.from_dict(task.recurrence)
                base = parse_iso(task.due_at) if task.due_at else parse_iso(now_iso())
                nxt = next_after(rec, base)
                # если срок давно прошёл, догоняем до будущего
                now = parse_iso(now_iso())
                while nxt <= now:
                    nxt = next_after(rec, nxt)
                task.due_at = nxt.isoformat(timespec="milliseconds")
                task.completed = False
                task.completed_at = None
                for st in task.subtasks:
                    st["done"] = False
            else:
                task.completed = True
                task.completed_at = now_iso()
            return self.save_task(task)

    def uncomplete_task(self, task_id: str) -> Task | None:
        task = self.get_task(task_id)
        if task and task.completed:
            task.completed = False
            task.completed_at = None
            return self.save_task(task)
        return task

    def completions_for(self, task_id: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT at FROM completions WHERE task_id=? ORDER BY at", (task_id,)).fetchall()
        return [r["at"] for r in rows]

    def completions_between(self, start_iso: str, end_iso: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT task_id, at FROM completions WHERE at>=? AND at<? ORDER BY at",
                (start_iso, end_iso)).fetchall()
        return [{"task_id": r["task_id"], "at": r["at"]} for r in rows]

    # ---------- универсальные сущности (списки, привычки, заметки) ----------

    def _save_entity(self, entity: str, obj) -> None:
        table = ENTITY_TABLES[entity]
        obj.touch()
        self._conn.execute(
            f"INSERT INTO {table}(id,data,updated_at,deleted) VALUES(?,?,?,?) "
            f"ON CONFLICT(id) DO UPDATE SET data=excluded.data, "
            f"updated_at=excluded.updated_at, deleted=excluded.deleted",
            (obj.id, json.dumps(obj.to_dict(), ensure_ascii=False),
             obj.updated_at, 1 if obj.deleted else 0))
        self._append_change(entity, obj)
        self._conn.commit()

    def _get_entity(self, entity: str, entity_id: str, cls, include_deleted: bool = False):
        table = ENTITY_TABLES[entity]
        with self._lock:
            q = f"SELECT data FROM {table} WHERE id=?"
            if not include_deleted:
                q += " AND deleted=0"
            row = self._conn.execute(q, (entity_id,)).fetchone()
        return cls.from_dict(json.loads(row["data"])) if row else None

    def _list_entities(self, entity: str, cls) -> list:
        table = ENTITY_TABLES[entity]
        with self._lock:
            rows = self._conn.execute(f"SELECT data FROM {table} WHERE deleted=0").fetchall()
        return [cls.from_dict(json.loads(r["data"])) for r in rows]

    def _delete_entity(self, entity: str, entity_id: str, cls) -> None:
        obj = self._get_entity(entity, entity_id, cls, include_deleted=True)
        if obj:
            obj.deleted = True
            with self._lock:
                self._save_entity(entity, obj)

    # ---------- списки ----------

    def save_list(self, lst: CheckList) -> CheckList:
        with self._lock:
            self._save_entity("list", lst)
            return lst

    def delete_list(self, list_id: str) -> None:
        self._delete_entity("list", list_id, CheckList)

    def get_list(self, list_id: str) -> CheckList | None:
        return self._get_entity("list", list_id, CheckList)

    def list_lists(self) -> list[CheckList]:
        return sorted(self._list_entities("list", CheckList),
                      key=lambda l: l.updated_at, reverse=True)

    def reset_list(self, list_id: str) -> CheckList | None:
        """Новый цикл повторяемого списка: все позиции снова не отмечены."""
        lst = self.get_list(list_id)
        if not lst:
            return None
        for item in lst.items:
            item["done"] = False
        lst.cycles += 1
        return self.save_list(lst)

    # ---------- привычки ----------

    def save_habit(self, habit: Habit) -> Habit:
        with self._lock:
            self._save_entity("habit", habit)
            return habit

    def delete_habit(self, habit_id: str) -> None:
        self._delete_entity("habit", habit_id, Habit)

    def get_habit(self, habit_id: str) -> Habit | None:
        return self._get_entity("habit", habit_id, Habit)

    def list_habits(self) -> list[Habit]:
        return sorted(self._list_entities("habit", Habit), key=lambda h: h.created_at)

    def check_habit(self, habit_id: str, day: str, done: bool = True) -> Habit | None:
        habit = self.get_habit(habit_id)
        if not habit:
            return None
        if done:
            habit.checks[day] = True
        else:
            habit.checks.pop(day, None)
        return self.save_habit(habit)

    # ---------- заметки ----------

    def save_note(self, note: Note) -> Note:
        with self._lock:
            self._save_entity("note", note)
            return note

    def delete_note(self, note_id: str) -> None:
        self._delete_entity("note", note_id, Note)

    def get_note(self, note_id: str) -> Note | None:
        return self._get_entity("note", note_id, Note)

    def list_notes(self) -> list[Note]:
        notes = self._list_entities("note", Note)
        notes.sort(key=lambda n: n.updated_at, reverse=True)
        notes.sort(key=lambda n: not n.pinned)
        return notes

    def export_state(self) -> list[dict]:
        """Полное текущее состояние сущностей (включая tombstone) для переноса."""
        with self._lock:
            out = []
            for entity, table in ENTITY_TABLES.items():
                for r in self._conn.execute(f"SELECT id,data,updated_at FROM {table}"):
                    out.append({"entity": entity, "entity_id": r["id"],
                                "payload": json.loads(r["data"]),
                                "updated_at": r["updated_at"]})
            return out
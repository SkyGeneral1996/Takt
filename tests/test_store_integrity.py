import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from takt.core.models import Category, Habit, Recurrence, Task
from takt.core.store import Store


def event(entity, entity_id, payload, event_id="remote-event", device_id="remote",
          updated_at=None, op="upsert"):
    return {"event_id": event_id, "device_id": device_id, "entity": entity,
            "entity_id": entity_id, "op": op, "payload": payload,
            "updated_at": updated_at or payload.get("updated_at", "2026-11-26T08:00:00+00:00")}


class TestStoreIntegrity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.a = Store(Path(self.tmp.name) / "a.db", device_id="devA")
        self.b = Store(Path(self.tmp.name) / "b.db", device_id="devB")

    def tearDown(self):
        self.a.close()
        self.b.close()
        self.tmp.cleanup()

    def sync(self, source, target):
        return target.apply_remote_events(source.local_events())

    def test_malformed_batch_rolls_back_everything(self):
        task = Task(title="Не должна примениться")
        payload = task.to_dict()
        payload["updated_at"] = "2026-11-26T08:00:00+00:00"
        good = event("task", task.id, payload, event_id="good")
        bad = {"event_id": "bad", "device_id": "remote", "entity": "task"}
        with self.assertRaises(ValueError):
            self.b.apply_remote_events([good, bad])
        self.assertIsNone(self.b.get_task(task.id))
        self.assertIsNone(self.b.get_meta("good"))

    def test_lww_compares_instants_not_iso_strings(self):
        payload = Task(title="Локальная").to_dict()
        payload["updated_at"] = "2026-11-26T08:00:00+00:00"
        self.b.apply_remote_events([event("task", payload["id"], payload, event_id="local")])
        incoming = Task(title="Старше по времени").to_dict()
        incoming["id"] = payload["id"]
        incoming["updated_at"] = "2026-11-26T10:00:00+03:00"
        self.b.apply_remote_events([
            event("task", incoming["id"], incoming, event_id="older", updated_at=incoming["updated_at"])])
        self.assertEqual(self.b.get_task(incoming["id"]).title, "Локальная")

    def test_independent_task_fields_merge(self):
        task = self.a.save_task(Task(title="Исходная", notes="Старые заметки"))
        self.sync(self.a, self.b)
        left = self.a.get_task(task.id)
        left.title = "Название с A"
        self.a.save_task(left)
        right = self.b.get_task(task.id)
        right.notes = "Заметки с B"
        self.b.save_task(right)
        self.sync(self.a, self.b)
        self.sync(self.b, self.a)
        self.assertEqual(self.a.get_task(task.id).title, "Название с A")
        self.assertEqual(self.a.get_task(task.id).notes, "Заметки с B")
        self.assertEqual(self.b.get_task(task.id).title, "Название с A")
        self.assertEqual(self.b.get_task(task.id).notes, "Заметки с B")

    def test_habit_checks_merge_and_uncheck_propagates(self):
        habit = self.a.save_habit(Habit(name="Чтение"))
        self.sync(self.a, self.b)
        self.a.check_habit(habit.id, "2026-11-24")
        self.b.check_habit(habit.id, "2026-11-25")
        self.sync(self.a, self.b)
        self.sync(self.b, self.a)
        self.assertEqual(set(self.a.get_habit(habit.id).checks), {"2026-11-24", "2026-11-25"})
        self.assertEqual(set(self.b.get_habit(habit.id).checks), {"2026-11-24", "2026-11-25"})
        self.a.check_habit(habit.id, "2026-11-24", done=False)
        self.sync(self.a, self.b)
        self.assertEqual(self.b.get_habit(habit.id).checks, {"2026-11-25": True})

    def test_completion_history_and_undo_replicate(self):
        task = self.a.save_task(Task(title="Разовая"))
        self.a.complete_task(task.id)
        completion_events = [e for e in self.a.local_events() if e["entity"] == "completion"]
        self.assertEqual(len(completion_events), 1)
        self.sync(self.a, self.b)
        self.assertEqual(len(self.b.completions_for(task.id)), 1)
        self.a.uncomplete_task(task.id)
        self.sync(self.a, self.b)
        self.assertEqual(self.b.completions_for(task.id), [])
        self.assertFalse(self.b.get_task(task.id).completed)

    def test_concurrent_completion_is_idempotent(self):
        task = self.a.save_task(Task(title="Параллельная"))
        barrier = threading.Barrier(8)
        errors = []

        def complete():
            try:
                barrier.wait()
                self.a.complete_task(task.id)
            except Exception as exc:  # noqa: BLE001 - test records thread failures
                errors.append(exc)

        threads = [threading.Thread(target=complete) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.a.completions_for(task.id)), 1)

    def test_concurrent_habit_dates_are_not_lost(self):
        habit = self.a.save_habit(Habit(name="Параллельная"))
        barrier = threading.Barrier(2)

        def check(day):
            barrier.wait()
            self.a.check_habit(habit.id, day)

        threads = [threading.Thread(target=check, args=(day,))
                   for day in ("2026-11-24", "2026-11-25")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(set(self.a.get_habit(habit.id).checks), {"2026-11-24", "2026-11-25"})

    def test_recurring_double_complete_does_skip_occurrence(self):
        task = self.a.save_task(Task(
            title="Повтор", due_at="2026-11-20T09:00:00+00:00",
            recurrence=Recurrence("daily").to_dict()))
        self.a.complete_task(task.id)
        first_due = self.a.get_task(task.id).due_at
        self.a.complete_task(task.id)
        self.assertEqual(self.a.get_task(task.id).due_at, first_due)
        self.assertEqual(len(self.a.completions_for(task.id)), 1)

    def test_id_mismatch_and_newer_schema_are_rejected(self):
        payload = Task(title="Подмена").to_dict()
        payload["id"] = "other"
        with self.assertRaises(ValueError):
            self.b.apply_remote_events([event("task", "expected", payload)])
        payload["id"] = "expected"
        payload["_schema_version"] = 999
        with self.assertRaises(ValueError):
            self.b.apply_remote_events([event("task", "expected", payload)])

    def test_invalid_colors_are_rejected(self):
        for entity, model in (("category", Category(name="Цвет")),
                              ("habit", Habit(name="Цвет"))):
            payload = model.to_dict()
            payload["color"] = "red"
            with self.subTest(entity=entity), self.assertRaises(ValueError):
                self.b.apply_remote_events([
                    event(entity, payload["id"], payload, event_id=f"bad-{entity}")])

    def test_invalid_habit_date_is_rejected(self):
        habit = self.a.save_habit(Habit(name="Дата"))
        with self.assertRaises(ValueError):
            self.a.check_habit(habit.id, "2026-02-30")

    def test_schema_version_and_migration_backup(self):
        path = Path(self.tmp.name) / "legacy.db"
        conn = sqlite3.connect(path)
        try:
            conn.execute("CREATE TABLE completions(id INTEGER PRIMARY KEY AUTOINCREMENT, "
                         "task_id TEXT NOT NULL, at TEXT NOT NULL)")
            conn.execute("INSERT INTO completions(task_id,at) VALUES('task-1','2026-11-20T09:00:00+00:00')")
            conn.commit()
        finally:
            conn.close()
        store = Store(path, device_id="legacy-device")
        try:
            self.assertEqual(store._conn.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(store.completions_for("task-1"), ["2026-11-20T09:00:00+00:00"])
            self.assertTrue(path.with_name("legacy.pre-v1.db").exists())
            self.assertTrue(any(e["entity"] == "completion" for e in store.local_events()))
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()

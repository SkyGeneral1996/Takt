import json
import tempfile
import threading
import unittest
from pathlib import Path

from takt.core.models import Task
from takt.core.store import Store
from takt.sync.engine import SyncEngine


class FakeCloud:
    """Общий словарь файлов — эмуляция облачного диска для двух устройств."""

    def __init__(self):
        self.files: dict[str, str] = {}
        self.folders: set[str] = set()

    def ensure_folder(self, path):
        self.folders.add(path)

    def list_files(self, folder):
        prefix = folder + "/"
        return [p[len(prefix):] for p in self.files if p.startswith(prefix)]

    def upload_text(self, remote_path, text):
        self.files[remote_path] = text

    def download_text(self, remote_path):
        return self.files.get(remote_path)


class TestSyncEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cloud = FakeCloud()
        self.sa = Store(Path(self.tmp.name) / "a.db", device_id="devA")
        self.sb = Store(Path(self.tmp.name) / "b.db", device_id="devB")
        self.ea = SyncEngine(self.sa, self.cloud)
        self.eb = SyncEngine(self.sb, self.cloud)

    def tearDown(self):
        self.sa.close()
        self.sb.close()
        self.tmp.cleanup()

    def test_two_devices_roundtrip(self):
        t = self.sa.save_task(Task(title="Задача с ПК"))
        r = self.ea.sync()
        self.assertEqual(r.pushed, 1)
        r = self.eb.sync()
        self.assertEqual(r.applied, 1)
        self.assertEqual(self.sb.get_task(t.id).title, "Задача с ПК")

        tb = self.sb.save_task(Task(title="Задача с телефона"))
        self.eb.sync()
        self.ea.sync()
        self.assertEqual(self.sa.get_task(tb.id).title, "Задача с телефона")

    def test_no_reupload_when_unchanged(self):
        self.sa.save_task(Task(title="Раз"))
        self.assertEqual(self.ea.sync().pushed, 1)
        self.assertEqual(self.ea.sync().pushed, 0)

    def test_repeated_sync_idempotent(self):
        self.sa.save_task(Task(title="Раз"))
        self.ea.sync()
        self.eb.sync()
        self.assertEqual(self.eb.sync().applied, 0)

    def test_concurrent_sync_uploads_latest_snapshot(self):
        entered = threading.Event()
        release = threading.Event()
        uploads = []
        original_upload = self.cloud.upload_text

        def blocked_upload(path, text):
            uploads.append(text)
            if len(uploads) == 1:
                entered.set()
                release.wait(5)
            original_upload(path, text)

        self.cloud.upload_text = blocked_upload
        self.sa.save_task(Task(title="Первая"))
        first = SyncEngine(self.sa, self.cloud)
        second = SyncEngine(self.sa, self.cloud)
        results = []
        thread1 = threading.Thread(target=lambda: results.append(first.sync()))
        thread2 = threading.Thread(target=lambda: results.append(second.sync()))
        thread1.start()
        self.assertTrue(entered.wait(5))
        latest = self.sa.save_task(Task(title="Вторая"))
        thread2.start()
        release.set()
        thread1.join(10)
        thread2.join(10)
        self.assertFalse(thread1.is_alive())
        self.assertFalse(thread2.is_alive())
        remote = self.cloud.files["TaskSync/devices/devA.jsonl"]
        self.assertIn(latest.id, remote)
        self.assertEqual(SyncEngine(self.sa, self.cloud).sync().pushed, 0)

    def test_corrupt_line_does_not_block_valid_events(self):
        task = self.sa.save_task(Task(title="Валидная"))
        valid = self.sa.local_events()[0]
        self.cloud.files["TaskSync/devices/devC.jsonl"] = (
            json.dumps(valid, ensure_ascii=False) + "\n{broken\n")
        result = self.eb.sync()
        self.assertEqual(result.applied, 1)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(self.sb.get_task(task.id).title, "Валидная")

    def test_unsafe_remote_filename_is_ignored(self):
        self.cloud.files["TaskSync/devices/../escape.jsonl"] = "{}"
        result = self.eb.sync()
        self.assertEqual(result.applied, 0)
        self.assertNotIn("TaskSync/escape.jsonl", self.cloud.files)

    def test_compaction_keeps_latest_entity_and_completion_history(self):
        task = self.sa.save_task(Task(title="До"))
        self.sa.save_task(Task(id=task.id, title="После"))
        self.sa.complete_task(task.id)
        self.ea.sync()
        events = self.sa.local_events()
        task_events = [event for event in events if event["entity"] == "task"]
        completion_events = [event for event in events if event["entity"] == "completion"]
        self.assertEqual(len(task_events), 1)
        self.assertEqual(task_events[0]["payload"]["title"], "После")
        self.assertEqual(len(completion_events), 1)


if __name__ == "__main__":
    unittest.main()
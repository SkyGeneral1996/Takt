import tempfile
import unittest
from pathlib import Path

from tasksync.core.models import Task
from tasksync.core.store import Store
from tasksync.sync.engine import SyncEngine


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


if __name__ == "__main__":
    unittest.main()
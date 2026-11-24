import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from takt.core.models import Task, Category
from takt.core.store import Store
from takt import transfer


class TestTransfer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.a = Store(Path(self.tmp.name) / "a.db", device_id="devA")
        self.b = Store(Path(self.tmp.name) / "b.db", device_id="devB")

    def tearDown(self):
        self.a.close()
        self.b.close()
        self.tmp.cleanup()

    def test_payload_roundtrip(self):
        t = self.a.save_task(Task(title="С ПК"))
        self.a.save_category(Category(name="Работа"))
        payload = transfer.build_payload(self.a)
        self.assertEqual(payload["format"], transfer.FORMAT)
        applied = transfer.apply_payload(self.b, payload)
        self.assertEqual(applied, 2)
        self.assertEqual(self.b.get_task(t.id).title, "С ПК")
        self.assertEqual(len(self.b.list_categories()), 1)

    def test_diff_counts(self):
        t1 = self.a.save_task(Task(title="Общая"))
        gone = self.a.save_task(Task(title="Удалённая"))
        transfer.apply_payload(self.b, transfer.build_payload(self.a))
        # на A: изменяем общую, добавляем новую, удаляем существующую у B
        t1b = self.a.get_task(t1.id)
        t1b.title = "Общая v2"
        self.a.save_task(t1b)
        self.a.save_task(Task(title="Новая с ПК"))
        self.a.delete_task(gone.id)

        d = transfer.diff_payload(self.b, transfer.build_payload(self.a))
        self.assertEqual(d.new, 1)
        self.assertEqual(d.updated, 1)
        self.assertEqual(d.deleted, 1)
        self.assertEqual(d.total_changes, 3)

    def test_diff_older_skipped(self):
        t = self.a.save_task(Task(title="Старая версия"))
        transfer.apply_payload(self.b, transfer.build_payload(self.a))
        newer = self.b.get_task(t.id)
        newer.title = "Более новая на телефоне"
        self.b.save_task(newer)
        d = transfer.diff_payload(self.b, transfer.build_payload(self.a))
        self.assertEqual(d.older, 1)
        self.assertEqual(d.total_changes, 0)
        transfer.apply_payload(self.b, transfer.build_payload(self.a))
        self.assertEqual(self.b.get_task(t.id).title, "Более новая на телефоне")

    def test_validate_rejects_garbage(self):
        with self.assertRaises(ValueError):
            transfer.validate_payload({"hello": "world"})

    def test_qr_png(self):
        b64 = transfer.qr_png_base64("http://192.168.1.10:1234/t/abc")
        self.assertTrue(len(b64) > 100)

    def test_server_roundtrip(self):
        self.a.save_task(Task(title="По сети"))
        server = transfer.TransferServer(self.a, ttl_sec=30)
        server.start()
        try:
            local_data = server.data_url.replace(transfer.lan_ip(), "127.0.0.1")
            payload = transfer.fetch_payload(local_data)
            self.assertEqual(payload["device_id"], "devA")
            self.assertTrue(server.received.wait(5))
            wrong = local_data.split("token=")[0] + "token=wrong"
            resp = requests.get(wrong, timeout=5)
            self.assertEqual(resp.status_code, 404)
            page = requests.get(server.url.replace(transfer.lan_ip(), "127.0.0.1"),
                                timeout=5)
            self.assertIn("Takt", page.text)
        finally:
            server.stop()


class TestUpdate(unittest.TestCase):
    def test_parse_version(self):
        from takt.update import parse_version
        self.assertEqual(parse_version("v0.2.0"), (0, 2, 0))
        self.assertEqual(parse_version("1.10"), (1, 10, 0))
        self.assertGreater(parse_version("v0.10.0"), parse_version("v0.9.9"))

    def _release(self, tag, assets):
        class Resp:
            status_code = 200
            def json(self):
                return {"tag_name": tag, "body": "notes", "html_url": "u",
                        "assets": [{"name": n, "browser_download_url": "http://x/" + n,
                                    "size": 1} for n in assets]}
        return Resp()

    def test_check_update_found(self):
        from takt import update
        with patch.object(update.requests, "get",
                          return_value=self._release("v9.9.9", ["Takt-Setup.exe", "Takt-Portable.exe"])):
            info = update.check_update("owner/takt", "0.2.0")
        self.assertIsNotNone(info)
        self.assertEqual(info.version, "9.9.9")
        asset = update.choose_asset(info.assets)
        self.assertEqual(asset["name"], "Takt-Setup.exe")

    def test_check_update_none_when_current(self):
        from takt import update
        with patch.object(update.requests, "get",
                          return_value=self._release("v0.2.0", [])):
            self.assertIsNone(update.check_update("owner/takt", "0.2.0"))

    def test_check_update_404(self):
        from takt import update
        class Resp:
            status_code = 404
        with patch.object(update.requests, "get", return_value=Resp()):
            with self.assertRaises(update.UpdateError):
                update.check_update("owner/takt")

    def test_choose_asset_zip_fallback(self):
        from takt.update import choose_asset
        asset = choose_asset([{"name": "Takt-win.zip", "url": "u", "size": 1}])
        self.assertEqual(asset["name"], "Takt-win.zip")


if __name__ == "__main__":
    unittest.main()
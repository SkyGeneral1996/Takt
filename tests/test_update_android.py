import json
import os
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from takt import update


def make_code_zip(path: Path, stamp="abc", main=b"new") -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("main.pyc", main)
        z.writestr("takt/ui/app.pyc", b"app")
        z.writestr("takt/native_stamp.json",
                   json.dumps({"native": stamp, "version": "0.4.1"}))
    return path


class TestAndroidUpdate(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.base = root / "flet"
        (self.base / "app").mkdir(parents=True)
        (self.base / "app" / "main.pyc").write_bytes(b"old")
        (self.base / ".key").write_text("app:0.4.0+4+1700000000")
        env = patch.dict(os.environ, {
            "FLET_APP_STORAGE_DATA": str(root / "data"),
            "ANDROID_ROOT": "/system",
        })
        env.start()
        self.addCleanup(env.stop)

    def test_is_android_and_base(self):
        self.assertTrue(update.is_android())
        self.assertEqual(update.android_base(), self.base)

    def test_installed_apk_version(self):
        self.assertEqual(update.installed_apk_version(), "0.4.0")

    def test_android_assets(self):
        code, apk = update.android_assets([
            {"name": "Takt-Setup.exe"}, {"name": "Takt-Android.apk"},
            {"name": "Takt-Code.zip"}])
        self.assertEqual(code["name"], "Takt-Code.zip")
        self.assertEqual(apk["name"], "Takt-Android.apk")
        self.assertEqual(update.android_assets([{"name": "a.exe"}]), (None, None))

    def test_apply_code_update(self):
        z = make_code_zip(Path(self.tmp.name) / "code.zip")
        update.apply_code_update(z)
        self.assertEqual((self.base / "app" / "main.pyc").read_bytes(), b"new")
        self.assertTrue((self.base / "app" / "takt" / "ui" / "app.pyc").exists())
        self.assertFalse((self.base / "app.new").exists())
        self.assertFalse((self.base / "app.old").exists())
        self.assertEqual((self.base / ".key").read_text(), "app:0.4.0+4+1700000000")

    def test_apply_rejects_incomplete_zip(self):
        z = Path(self.tmp.name) / "bad.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("main.pyc", b"x")
        with self.assertRaises(update.UpdateError):
            update.apply_code_update(z)
        self.assertEqual((self.base / "app" / "main.pyc").read_bytes(), b"old")

    def test_apply_rejects_traversal(self):
        z = Path(self.tmp.name) / "evil.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("main.pyc", b"x")
            zf.writestr("takt/ui/app.pyc", b"x")
            zf.writestr("takt/native_stamp.json", "{}")
            zf.writestr("../evil.txt", b"x")
        with self.assertRaises(update.UpdateError):
            update.apply_code_update(z)
        self.assertFalse((Path(self.tmp.name) / "evil.txt").exists())

    def test_zip_native_stamp(self):
        z = make_code_zip(Path(self.tmp.name) / "code.zip", stamp="fingerprint")
        self.assertEqual(update.zip_native_stamp(z), "fingerprint")

    def test_native_stamp_reads_bundled_file(self):
        fake = Path(self.tmp.name) / "takt" / "update.py"
        fake.parent.mkdir()
        (fake.parent / "native_stamp.json").write_text('{"native": "n1"}')
        with patch.object(update, "__file__", str(fake)):
            self.assertEqual(update.native_stamp(), "n1")

    def test_restart_without_jnius_asks_manual(self):
        with patch.dict("sys.modules", {"jnius": None}):
            with self.assertRaises(update.UpdateError) as cm:
                update.restart_android()
        self.assertIn("вручную", str(cm.exception))

    def test_check_update_custom_feed(self):
        class Resp:
            status_code = 200
            def json(self):
                return {"tag_name": "v9.9.9", "body": "", "html_url": "u", "assets": []}
        with patch.object(update.requests, "get", return_value=Resp()) as get:
            info = update.check_update("http://10.0.2.2:8560/", "0.4.0")
        self.assertEqual(get.call_args[0][0], "http://10.0.2.2:8560/releases/latest")
        self.assertEqual(info.version, "9.9.9")


if __name__ == "__main__":
    unittest.main()
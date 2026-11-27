import hashlib
import json
import os
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from takt import update


def make_code_zip(path: Path, stamp="abc", main=b"new") -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("main.pyc", main)
        z.writestr("takt/ui/app.pyc", b"app")
        z.writestr("takt/native_stamp.json",
                   json.dumps({"native": stamp, "version": "0.4.1"}))
    return path


class Response:
    def __init__(self, content=b"", status_code=200):
        self.content = content
        self.status_code = status_code
        self.headers = {"Content-Length": str(len(content))}

    def iter_content(self, chunk_size=64 * 1024):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset:offset + chunk_size]

    def close(self):
        pass


class TestAndroidUpdate(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.base = root / "flet"
        (self.base / "app").mkdir(parents=True)
        (self.base / "app" / "main.pyc").write_bytes(b"old")
        (self.base / ".key").write_text("app:0.4.0+4+1700000000")
        self.cache = root / "cache"
        env = patch.dict(os.environ, {
            "FLET_APP_STORAGE_DATA": str(root / "data"),
            "FLET_APP_STORAGE_CACHE": str(self.cache),
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
            {"name": "Takt-Setup.exe", "kind": "windows-setup"},
            {"name": "Takt-Android.apk", "kind": "android-apk"},
            {"name": "Takt-Code.zip", "kind": "android-code"},
            {"name": "Takt-Code.zip", "kind": "windows-portable"},
        ])
        self.assertEqual(code["name"], "Takt-Code.zip")
        self.assertEqual(code["kind"], "android-code")
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

    def test_check_update_custom_feed_requires_https_and_signature(self):
        with patch.object(update.requests, "get") as get:
            with self.assertRaisesRegex(update.UpdateError, "HTTPS"):
                update.check_update("http://10.0.2.2:8560/", "0.4.0")
        get.assert_not_called()

        private = Ed25519PrivateKey.generate()
        public = private.public_key().public_bytes_raw().hex()
        apk_data = b"apk"
        apk_url = "https://files.test/Takt-Android.apk"
        manifest = {
            "format": update.UPDATE_FORMAT, "version": "9.9.9", "channel": "stable",
            "assets": [{
                "name": "Takt-Android.apk", "url": apk_url, "size": len(apk_data),
                "sha256": hashlib.sha256(apk_data).hexdigest(), "kind": "android-apk",
            }],
        }
        canonical = update.canonical_manifest(manifest)
        signature = private.sign(canonical)
        manifest_url = "https://files.test/update-manifest.json"
        signature_url = "https://files.test/update-manifest.json.sig"
        release = {
            "tag_name": "v9.9.9", "body": "", "html_url": "https://example.test/release",
            "assets": [
                {"name": "update-manifest.json", "browser_download_url": manifest_url,
                 "size": len(canonical)},
                {"name": "update-manifest.json.sig", "browser_download_url": signature_url,
                 "size": len(signature)},
            ],
        }
        routes = {
            "https://updates.test/releases/latest": release,
            manifest_url: canonical,
            signature_url: signature,
        }

        def get(url, **kwargs):
            value = routes[url]
            return Response(json.dumps(value).encode() if isinstance(value, dict) else value)

        with patch.object(update, "UPDATE_PUBLIC_KEY_HEX", public), \
                patch.object(update.requests, "get", side_effect=get):
            info = update.check_update("https://updates.test/", "0.4.0")
        self.assertEqual(info.version, "9.9.9")
        self.assertEqual(info.assets[0]["kind"], "android-apk")

    def test_android_apk_uses_fixed_name_and_manifest_checks(self):
        from takt.ui import settings
        data = b"apk-data"
        asset = {
            "name": "Takt-Android.apk", "url": "https://files.test/Takt-Android.apk",
            "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "kind": "android-apk",
        }
        status = SimpleNamespace(value="")
        page = SimpleNamespace(update=lambda: None)
        calls = []
        installed = []

        def download(url, dest, progress, cancel_event, **kwargs):
            calls.append((dest, kwargs))
            dest.write_bytes(data)
            progress(1.0)

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(settings, "download_asset", side_effect=download), \
                patch.object(settings, "install_apk_android", side_effect=installed.append):
            settings._update_android(
                SimpleNamespace(assets=[asset]), status, page, threading.Event(),
                lambda callback: callback(), Path(tmp),
            )
        self.assertEqual(calls[0][0], self.cache / "takt-update.apk")
        self.assertEqual(calls[0][1]["expected_size"], len(data))
        self.assertEqual(calls[0][1]["expected_sha256"], asset["sha256"])
        self.assertEqual(installed, [self.cache / "takt-update.apk"])
        self.assertIn("Подтвердите", status.value)

    def test_android_cancel_does_not_install(self):
        from takt.ui import settings
        data = b"apk-data"
        asset = {
            "name": "Takt-Android.apk", "url": "https://files.test/Takt-Android.apk",
            "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "kind": "android-apk",
        }
        cancel = threading.Event()
        cancel.set()
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(settings, "install_apk_android") as install:
            with self.assertRaises(update.UpdateCancelled):
                settings._update_android(
                    SimpleNamespace(assets=[asset]), SimpleNamespace(value=""),
                    SimpleNamespace(update=lambda: None), cancel,
                    lambda callback: callback(), Path(tmp),
                )
            install.assert_not_called()
        self.assertFalse((self.cache / "takt-update.apk").exists())

    def test_android_code_download_forwards_hash_and_size(self):
        from takt.ui import settings
        code_zip = make_code_zip(Path(self.tmp.name) / "signed.zip", stamp="abc", main=b"ota")
        data = code_zip.read_bytes()
        asset = {
            "name": "Takt-Code.zip", "url": "https://files.test/Takt-Code.zip",
            "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "kind": "android-code",
        }
        calls = []

        def download(url, dest, progress, cancel_event, **kwargs):
            calls.append(kwargs)
            dest.write_bytes(data)

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(settings, "download_asset", side_effect=download), \
                patch.object(settings, "native_stamp", return_value="abc"), \
                patch.object(settings, "android_restart_mode", return_value="auto"), \
                patch.object(settings, "restart_android") as restart:
            settings._update_android(
                SimpleNamespace(assets=[asset]), SimpleNamespace(value=""),
                SimpleNamespace(update=lambda: None), threading.Event(),
                lambda callback: callback(), Path(tmp),
            )
        self.assertEqual(calls[0]["expected_size"], len(data))
        self.assertEqual(calls[0]["expected_sha256"], asset["sha256"])
        self.assertEqual((self.base / "app" / "main.pyc").read_bytes(), b"ota")
        restart.assert_called_once()


if __name__ == "__main__":
    unittest.main()
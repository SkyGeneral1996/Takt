import asyncio
import base64
import contextlib
import hashlib
import io
import json
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

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

    def test_backup_file_roundtrip(self):
        task = self.a.save_task(Task(title="В резервной копии"))
        self.a.complete_task(task.id)
        self.a.save_category(Category(name="Работа"))
        payload = transfer.build_payload(self.a)
        path = Path(self.tmp.name) / "takt-backup.json"
        transfer.save_payload_file(path, payload)
        restored = transfer.load_payload_file(str(path))
        self.assertEqual(transfer.apply_payload(self.b, restored), 3)
        self.assertEqual(self.b.get_task(task.id).title, "В резервной копии")
        self.assertEqual(len(self.b.completions_for(task.id)), 1)
        self.assertEqual([c.name for c in self.b.list_categories()], ["Работа"])
        self.assertFalse(list(Path(self.tmp.name).glob("*.part")))

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
        raw = base64.b64decode(b64)
        self.assertEqual(raw[:8], b"\x89PNG\r\n\x1a\n")
        w, h = struct.unpack(">II", raw[16:24])
        self.assertGreater(w, 100)
        self.assertEqual(w, h)

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


class FakeResponse:
    def __init__(self, content=b"", status_code=200, headers=None, on_iter=None):
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}
        self.on_iter = on_iter
        self.closed = False

    def json(self):
        return json.loads(self.content)

    def iter_content(self, chunk_size=64 * 1024):
        if self.on_iter:
            self.on_iter()
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset:offset + chunk_size]

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class TestUpdate(unittest.TestCase):
    def setUp(self):
        from takt import update
        self.update = update
        self.production_key = update.UPDATE_PUBLIC_KEY_HEX
        self.private_key = Ed25519PrivateKey.generate()
        public = self.private_key.public_key().public_bytes_raw().hex()
        key_patch = patch.object(update, "UPDATE_PUBLIC_KEY_HEX", public)
        key_patch.start()
        self.addCleanup(key_patch.stop)

    def _asset(self, name, data, kind):
        return {
            "name": name, "url": f"https://files.test/{name}", "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "kind": kind,
        }

    def _signed_release(self, tag, definitions, *, version=None, channel="stable"):
        assets = [self._asset(name, data, kind) for name, data, kind in definitions]
        version = version or tag.lstrip("vV")
        manifest = {
            "format": self.update.UPDATE_FORMAT, "version": version,
            "channel": channel, "assets": assets,
        }
        canonical = self.update.canonical_manifest(manifest)
        signature = self.private_key.sign(canonical)
        manifest_url = "https://files.test/update-manifest.json"
        signature_url = "https://files.test/update-manifest.json.sig"
        release_assets = [
            {"name": asset["name"], "browser_download_url": asset["url"], "size": asset["size"]}
            for asset in assets
        ]
        release_assets.extend([
            {"name": "update-manifest.json", "browser_download_url": manifest_url,
             "size": len(canonical)},
            {"name": "update-manifest.json.sig", "browser_download_url": signature_url,
             "size": len(signature)},
        ])
        release = {
            "tag_name": tag, "body": "notes", "html_url": "https://example.test/release",
            "assets": release_assets,
        }
        routes = {
            f"{self.update.API}/repos/owner/takt/releases/latest": release,
            "https://updates.test/releases/latest": release,
            manifest_url: canonical,
            signature_url: signature,
        }
        for asset in assets:
            routes[asset["url"]] = dict(zip((d[0] for d in definitions), (d[1] for d in definitions)))[asset["name"]]
        return release, routes

    def _get(self, routes):
        def get(url, **kwargs):
            if url not in routes:
                raise AssertionError(f"Unexpected URL {url}")
            value = routes[url]
            if isinstance(value, FakeResponse):
                return value
            if isinstance(value, dict):
                value = json.dumps(value).encode()
            return FakeResponse(value, headers={"Content-Length": str(len(value))})
        return get

    def _check(self, routes, current="0.4.0", repo="owner/takt"):
        with patch.object(self.update.requests, "get", side_effect=self._get(routes)):
            return self.update.check_update(repo, current)

    def test_production_key_is_pinned(self):
        self.assertEqual(
            self.production_key,
            "0ed8d14055d57567054f3a4b89243ce9d5039636c7d13a6c4954591c8758727c",
        )

    def test_parse_version_and_prerelease_order(self):
        parse = self.update.parse_version
        self.assertEqual(parse("v0.2.0"), (0, 2, 0, 1, ()))
        self.assertGreater(parse("1.0.0"), parse("1.0.0-rc.1"))
        self.assertGreater(parse("1.0.1-beta"), parse("1.0.0"))
        self.assertGreater(parse("1.0.0-beta.11"), parse("1.0.0-beta.2"))
        self.assertGreater(parse("1.0.0-beta"), parse("1.0.0-alpha.9"))
        self.assertEqual(parse("1.0.0+one"), parse("1.0.0+two"))
        for invalid in ("1.0", "01.0.0", "1.0.0-01", "1.0.0.1", "release-1"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse(invalid)

    def test_check_update_found_and_setup_preferred(self):
        _, routes = self._signed_release("v9.9.9", [
            ("Takt-Code.zip", b"code", "android-code"),
            ("Takt-Portable.exe", b"portable", "windows-portable"),
            ("Takt-Setup.exe", b"setup", "windows-setup"),
        ])
        info = self._check(routes, current="0.2.0")
        self.assertEqual(info.version, "9.9.9")
        self.assertEqual(self.update.choose_asset(info.assets)["name"], "Takt-Setup.exe")

    def test_http_feed_is_rejected_before_request(self):
        with patch.object(self.update.requests, "get") as get:
            with self.assertRaisesRegex(self.update.UpdateError, "HTTPS"):
                self.update.check_update("http://updates.test", "0.4.0")
        get.assert_not_called()

    def test_downgrade_and_equal_version_are_rejected(self):
        for tag, current in (("v0.4.0", "0.4.0"), ("v0.3.9", "0.4.0"),
                             ("v0.4.0-rc.1", "0.4.0")):
            with self.subTest(tag=tag):
                routes = {
                    f"{self.update.API}/repos/owner/takt/releases/latest":
                        {"tag_name": tag, "assets": []},
                }
                self.assertIsNone(self._check(routes, current))

    def test_legacy_unsigned_release_is_rejected(self):
        routes = {
            f"{self.update.API}/repos/owner/takt/releases/latest":
                {"tag_name": "v9.9.9", "assets": []},
        }
        with self.assertRaisesRegex(self.update.UpdateError, "update-manifest.json"):
            self._check(routes)

    def test_tampered_manifest_is_rejected(self):
        _, routes = self._signed_release("v9.9.9", [("Takt-Setup.exe", b"setup", "windows-setup")])
        manifest = json.loads(routes["https://files.test/update-manifest.json"])
        manifest["version"] = "9.9.8"
        routes["https://files.test/update-manifest.json"] = json.dumps(manifest).encode()
        with self.assertRaisesRegex(self.update.UpdateError, "Подпись"):
            self._check(routes)

    def test_manifest_version_must_match_release_tag(self):
        _, routes = self._signed_release(
            "v9.9.9", [("Takt-Setup.exe", b"setup", "windows-setup")], version="9.9.8")
        with self.assertRaisesRegex(self.update.UpdateError, "tag_name"):
            self._check(routes)

    def test_manifest_schema_and_asset_allowlist(self):
        good = self._asset("Takt-Setup.exe", b"setup", "windows-setup")
        cases = [
            ({**good, "kind": "windows-code"}, "kind"),
            ({**good, "name": "../Takt-Setup.exe"}, "имя"),
            ({**good, "name": "Takt-Code.zip"}, "kind"),
            ({**good, "size": self.update.MAX_ASSET_SIZE + 1}, "размер"),
            ({**good, "url": "http://files.test/Takt-Setup.exe"}, "HTTPS"),
            ({**good, "sha256": "0" * 64, "extra": True}, "схема"),
        ]
        for asset, message in cases:
            manifest = {
                "format": self.update.UPDATE_FORMAT, "version": "9.9.9",
                "channel": "stable", "assets": [asset],
            }
            with self.subTest(asset=asset), self.assertRaisesRegex(self.update.UpdateError, message):
                self.update.validate_manifest(manifest)

    def test_check_update_404(self):
        routes = {f"{self.update.API}/repos/owner/takt/releases/latest": FakeResponse(status_code=404)}
        with self.assertRaises(self.update.UpdateError):
            self._check(routes)

    def test_https_redirect_is_checked(self):
        routes = {
            "https://files.test/start": FakeResponse(
                status_code=302, headers={"Location": "http://files.test/asset"}),
        }
        with patch.object(self.update.requests, "get", side_effect=self._get(routes)):
            with self.assertRaisesRegex(self.update.UpdateError, "HTTPS"):
                self.update._https_get("https://files.test/start")

    def test_redirect_count_is_limited(self):
        calls = []

        def get(url, **kwargs):
            calls.append(url)
            return FakeResponse(status_code=302, headers={"Location": "https://files.test/loop"})
        with patch.object(self.update.requests, "get", side_effect=get):
            with self.assertRaisesRegex(self.update.UpdateError, "редиректов"):
                self.update._https_get("https://files.test/loop")
        self.assertEqual(len(calls), self.update.MAX_REDIRECTS + 1)

    def test_takt_code_zip_is_never_a_windows_update(self):
        assets = [
            {"name": "Takt-Code.zip", "kind": "android-code"},
            {"name": "Takt-Portable.exe", "kind": "windows-portable"},
            {"name": "Takt-Setup.exe", "kind": "windows-setup"},
        ]
        self.assertEqual(self.update.choose_asset(assets)["name"], "Takt-Setup.exe")
        self.assertEqual(self.update.choose_asset(assets[:2])["name"], "Takt-Portable.exe")
        self.assertIsNone(self.update.choose_asset(assets[:1]))
        self.assertIsNone(self.update.choose_asset([{"name": "Takt-Setup.exe"}]))

    def test_download_verifies_hash_size_and_replaces_atomically(self):
        data = b"verified update" * 1000
        digest = hashlib.sha256(data).hexdigest()
        routes = {"https://files.test/asset": data}
        progress = []
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "Takt-Setup.exe"
            dest.write_bytes(b"old")
            with patch.object(self.update.requests, "get", side_effect=self._get(routes)):
                result = self.update.download_asset(
                    "https://files.test/asset", dest, progress.append,
                    expected_size=len(data), expected_sha256=digest,
                )
            self.assertEqual(result, dest)
            self.assertEqual(dest.read_bytes(), data)
            self.assertEqual(progress, sorted(progress))
            self.assertEqual(progress[-1], 1.0)
            self.assertEqual(list(Path(tmp).glob("*.part")), [])

    def test_tampered_asset_is_rejected_and_temp_removed(self):
        routes = {"https://files.test/asset": b"tampered"}
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "Takt-Setup.exe"
            dest.write_bytes(b"old")
            with patch.object(self.update.requests, "get", side_effect=self._get(routes)):
                with self.assertRaisesRegex(self.update.UpdateError, "SHA-256"):
                    self.update.download_asset(
                        "https://files.test/asset", dest,
                        expected_size=8, expected_sha256=hashlib.sha256(b"original").hexdigest(),
                    )
            self.assertEqual(dest.read_bytes(), b"old")
            self.assertEqual(list(Path(tmp).glob("*.part")), [])

    def test_content_length_and_stream_size_mismatch(self):
        for content, headers in (
            (b"data", {"Content-Length": "5"}),
            (b"too much data", {"Content-Length": "4"}),
        ):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as tmp:
                dest = Path(tmp) / "Takt-Setup.exe"
                response = FakeResponse(content, headers=headers)
                with patch.object(self.update.requests, "get", return_value=response):
                    with self.assertRaises(self.update.UpdateError):
                        self.update.download_asset(
                            "https://files.test/asset", dest,
                            expected_size=4, expected_sha256=hashlib.sha256(b"data").hexdigest(),
                        )
                self.assertFalse(dest.exists())
                self.assertEqual(list(Path(tmp).glob("*.part")), [])

    def test_cancel_cleans_temp(self):
        cancel = threading.Event()
        routes = {
            "https://files.test/asset": FakeResponse(
                b"data", headers={"Content-Length": "4"}, on_iter=cancel.set),
        }
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "Takt-Setup.exe"
            with patch.object(self.update.requests, "get", side_effect=self._get(routes)):
                with self.assertRaises(self.update.UpdateCancelled):
                    self.update.download_asset(
                        "https://files.test/asset", dest, cancel_event=cancel,
                        expected_size=4, expected_sha256=hashlib.sha256(b"data").hexdigest(),
                    )
            self.assertFalse(dest.exists())
            self.assertEqual(list(Path(tmp).glob("*.part")), [])

    def test_expired_deadline_cleans_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "Takt-Setup.exe"
            with self.assertRaisesRegex(self.update.UpdateError, "время"):
                self.update.download_asset(
                    "https://files.test/asset", dest, expected_size=4,
                    expected_sha256=hashlib.sha256(b"data").hexdigest(),
                    deadline=time.monotonic() - 1,
                )
            self.assertEqual(list(Path(tmp).glob("*.part")), [])

    def test_setup_args_portable_and_random_exe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            setup = root / "Takt-Setup.exe"
            setup.write_bytes(b"setup")
            app_dir = root / "installed"
            app_dir.mkdir()
            with patch.object(self.update.subprocess, "Popen") as popen:
                self.update.launch_update(setup, kind="windows-setup", app_dir=app_dir)
            self.assertEqual(popen.call_args.args[0], [str(setup.resolve()), "/S", f"/DIR={app_dir.resolve()}"])
            for name, kind in (("Takt-Portable.exe", "windows-portable"),
                               ("random.exe", "windows-setup"), ("Takt-Code.zip", "android-code")):
                path = root / name
                path.write_bytes(b"x")
                with self.subTest(name=name), patch.object(self.update.subprocess, "Popen") as popen:
                    with self.assertRaises(self.update.UpdateError):
                        self.update.launch_update(path, kind=kind, app_dir=app_dir)
                    popen.assert_not_called()

    def test_signing_cli_roundtrip(self):
        from tools import sign_update
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key_path = root / "private.key"
            key_path.write_bytes(self.private_key.private_bytes_raw())
            release = root / "release"
            release.mkdir()
            setup = release / "Takt-Setup.exe"
            setup.write_bytes(b"signed setup")
            output = root / "output"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(sign_update.main([
                    "--key", str(key_path), "--version", "1.2.3-rc.1", "--channel", "beta",
                    "--base-url", "https://files.test/releases/v1.2.3-rc.1",
                    "--output-dir", str(output), "--asset", "windows-setup", str(setup),
                ]), 0)
            manifest = self.update.verify_manifest(
                (output / "update-manifest.json").read_bytes(),
                (output / "update-manifest.json.sig").read_bytes(),
            )
            self.assertEqual(manifest["version"], "1.2.3-rc.1")
            self.assertEqual(manifest["assets"][0]["sha256"], hashlib.sha256(b"signed setup").hexdigest())
            self.assertEqual(len((output / "update-manifest.json.sig").read_bytes()), 64)
            self.assertNotIn(key_path.read_bytes(), (output / "update-manifest.json").read_bytes())


class TaskPage:
    def __init__(self):
        self.dialogs = []
        self.task_calls = 0
        self.window = SimpleNamespace(destroy=self.destroy)

    async def destroy(self):
        pass

    def update(self):
        pass

    def show_dialog(self, dialog):
        dialog.open = True
        self.dialogs.append(dialog)

    def run_task(self, handler, *args, **kwargs):
        self.task_calls += 1
        return asyncio.run(handler(*args, **kwargs))


class TestUpdateDialog(unittest.TestCase):
    def _info(self):
        from takt.update import UpdateInfo
        return UpdateInfo(
            version="9.9.9", tag="v9.9.9", notes="notes",
            assets=[{
                "name": "Takt-Setup.exe", "url": "https://files.test/setup",
                "size": 5, "sha256": hashlib.sha256(b"setup").hexdigest(),
                "kind": "windows-setup",
            }],
            url="https://example.test/release",
        )

    def test_cancel_cleans_temp_and_does_not_launch(self):
        from takt.ui import settings
        page = TaskPage()
        app = SimpleNamespace(page=page)
        started = threading.Event()
        cleaned = threading.Event()
        real_rmtree = settings.shutil.rmtree

        def download(url, dest, progress, cancel_event, **kwargs):
            started.set()
            progress(0.4)
            if not cancel_event.wait(2):
                raise AssertionError("Cancellation event was not set")
            raise settings.UpdateCancelled("cancelled")

        def cleanup(path, *args, **kwargs):
            real_rmtree(path, *args, **kwargs)
            cleaned.set()

        settings._show_update_dialog(app, self._info())
        dialog = page.dialogs[-1]
        with patch.object(settings, "is_android", return_value=False), \
                patch.object(settings, "download_asset", side_effect=download), \
                patch.object(settings, "launch_update") as launch, \
                patch.object(settings.shutil, "rmtree", side_effect=cleanup):
            dialog.actions[1].on_click(None)
            self.assertTrue(started.wait(2))
            dialog.actions[0].on_click(None)
            self.assertTrue(cleaned.wait(2))
            self.assertGreater(page.task_calls, 0)
            self.assertFalse(dialog.open)
            self.assertTrue(dialog.actions[1].disabled)
            launch.assert_not_called()

    def test_later_does_not_close_a_newer_dialog(self):
        import flet as ft
        from takt.ui import settings
        page = TaskPage()
        settings._show_update_dialog(SimpleNamespace(page=page), self._info())
        dialog = page.dialogs[-1]
        foreign = ft.AlertDialog(title=ft.Text("Other"))
        page.show_dialog(foreign)
        dialog.actions[0].on_click(None)
        self.assertFalse(dialog.open)
        self.assertTrue(foreign.open)

    def test_progress_never_moves_backwards(self):
        from takt.ui import settings
        page = TaskPage()
        settings._show_update_dialog(SimpleNamespace(page=page), self._info())
        dialog = page.dialogs[-1]
        status = dialog.content.content.controls[1].controls[1]
        cleaned = threading.Event()
        real_rmtree = settings.shutil.rmtree

        def download(url, dest, progress, cancel_event, **kwargs):
            progress(0.8)
            self.assertIn("80%", status.value)
            progress(0.2)
            self.assertIn("80%", status.value)

        def cleanup(path, *args, **kwargs):
            real_rmtree(path, *args, **kwargs)
            cleaned.set()

        with patch.object(settings, "is_android", return_value=False), \
                patch.object(settings, "download_asset", side_effect=download), \
                patch.object(settings, "launch_update") as launch, \
                patch.object(settings, "is_frozen", return_value=False), \
                patch.object(settings.shutil, "rmtree", side_effect=cleanup):
            dialog.actions[1].on_click(None)
            self.assertTrue(cleaned.wait(2))
            launch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
import zipfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

import installer
from takt import __version__


class FakeKey:
    def __init__(self, values):
        self.values = values

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def QueryValueEx(self, name):
        try:
            return self.values[name], 1
        except KeyError as ex:
            raise OSError(name) from ex


class InstallerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.system = self.root / "system"
        self.home.mkdir()
        self.system.mkdir()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {
            "SystemRoot": str(self.system),
            "WINDIR": str(self.system),
            "ProgramFiles": str(self.system / "Program Files"),
            "ProgramW6432": str(self.system / "Program Files"),
            "ProgramFiles(x86)": str(self.system / "Program Files (x86)"),
            "CommonProgramFiles": str(self.system / "Common Files"),
            "ProgramData": str(self.system / "ProgramData"),
            "LOCALAPPDATA": str(self.root / "localappdata"),
            "APPDATA": str(self.root / "appdata"),
        }))
        self.stack.enter_context(patch.object(installer.Path, "home", return_value=self.home))

    def make_archive(self, files=None):
        path = self.root / "app.zip"
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in (files or {"Takt.exe": b"new"}).items():
                archive.writestr(name, data)
        return path

    def make_installation(self, dest, *, exe=b"old", extra_files=()):
        dest.mkdir(parents=True)
        (dest / installer.EXE_NAME).write_bytes(exe)
        managed = [installer.EXE_NAME, installer.INSTALL_SENTINEL, *extra_files]
        for name in extra_files:
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"managed")
        (dest / installer.INSTALL_SENTINEL).write_text(json.dumps({
            "app": installer.APP_NAME,
            "version": "0.3.1",
            "files": managed,
        }), encoding="utf-8")
        return dest

    def fake_registry(self, values, *, open_error=None):
        key = FakeKey(values)
        fake = types.SimpleNamespace(
            HKEY_CURRENT_USER=object(),
            REG_SZ=1,
            REG_DWORD=4,
            OpenKey=Mock(return_value=key),
            QueryValueEx=lambda registry_key, name: registry_key.QueryValueEx(name),
            CloseKey=Mock(),
            DeleteKey=Mock(),
        )
        if open_error is not None:
            fake.OpenKey.side_effect = open_error
        return fake, key

    def install_mocks(self, archive):
        stack = ExitStack()
        stack.enter_context(patch.object(installer, "wait_app_close", return_value=True))
        stack.enter_context(patch.object(installer, "resource_zip", return_value=archive))
        stack.enter_context(patch.object(installer, "frozen", return_value=False))
        stack.enter_context(patch.object(installer, "shortcut_paths", return_value=(
            self.root / "start" / "Takt.lnk", self.root / "desktop" / "Takt.lnk")))
        stack.enter_context(patch.object(installer, "make_shortcut"))
        stack.enter_context(patch.object(installer, "register_uninstall", return_value=None))
        return stack


class TestVersionAndTimeout(InstallerTestCase):
    def test_version_uses_package(self):
        self.assertEqual(installer.VERSION, __version__)
        self.assertNotEqual(installer.VERSION, "0.3.1")

    def test_version_metadata_fallback(self):
        with patch.dict(sys.modules, {"takt": None}), patch(
                "importlib.metadata.version", return_value="9.8.7") as metadata:
            self.assertEqual(installer.application_version(), "9.8.7")
        metadata.assert_called_once_with("takt")

    def test_wait_app_close_returns_false_on_timeout(self):
        with patch.object(installer, "is_app_running", return_value=True), patch.object(
                installer.time, "monotonic", side_effect=[0.0, 0.0, 0.05]), patch.object(
                installer.time, "sleep") as sleep:
            self.assertFalse(installer.wait_app_close(0.01))
        sleep.assert_called_once_with(0.01)

    def test_wait_app_close_returns_true_when_closed(self):
        with patch.object(installer, "is_app_running", side_effect=[True, False]), patch.object(
                installer.time, "monotonic", return_value=0.0), patch.object(
                installer.time, "sleep") as sleep:
            self.assertTrue(installer.wait_app_close(1.0))
        sleep.assert_called_once_with(0.2)

    def test_install_aborts_before_extract_when_app_is_running(self):
        dest = self.make_installation(self.root / "install" / "Takt")
        with patch.object(installer, "wait_app_close", return_value=False), patch.object(
                installer, "extract") as extract:
            with self.assertRaises(installer.AppRunningError):
                installer.install(dest, launch=False)
        extract.assert_not_called()
        self.assertEqual((dest / installer.EXE_NAME).read_bytes(), b"old")

    def test_tasklist_failure_is_not_treated_as_closed(self):
        result = subprocess.CompletedProcess([], 1, "", "Access denied")
        with patch.object(installer.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(installer.AppRunningError, "Access denied"):
                installer.is_app_running()


class TestSafeInstall(InstallerTestCase):
    def test_staging_replaces_bundle_and_preserves_user_data(self):
        dest = self.make_installation(
            self.root / "install" / "Takt", extra_files=("_internal/stale.pyc",))
        (dest / "data" / "empty").mkdir(parents=True)
        (dest / "data" / "notes.txt").write_text("user data", encoding="utf-8")
        archive = self.make_archive({
            "Takt.exe": b"new", "resources/icon.png": b"icon",
        })
        with self.install_mocks(archive):
            installer.install(dest, desktop=True, launch=False)
        self.assertEqual((dest / installer.EXE_NAME).read_bytes(), b"new")
        self.assertFalse((dest / "_internal/stale.pyc").exists())
        self.assertEqual((dest / "data/notes.txt").read_text(encoding="utf-8"), "user data")
        self.assertTrue((dest / "data/empty").is_dir())
        manifest = json.loads((dest / installer.INSTALL_SENTINEL).read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], __version__)
        self.assertNotIn("data/notes.txt", manifest["files"])
        self.assertEqual(list(dest.parent.glob(".Takt.takt-install-*")), [])

    def test_failed_move_restores_backup(self):
        dest = self.make_installation(self.root / "install" / "Takt")
        archive = self.make_archive()
        original_rename = Path.rename

        def fail_stage(path, target):
            if path.name == "staging":
                raise OSError("replace failed")
            return original_rename(path, target)

        with self.install_mocks(archive), patch.object(installer.Path, "rename", new=fail_stage):
            with self.assertRaisesRegex(installer.InstallerError, "replace failed"):
                installer.install(dest, launch=False)
        self.assertEqual((dest / installer.EXE_NAME).read_bytes(), b"old")
        self.assertEqual(list(dest.parent.glob(".Takt.takt-install-*")), [])

    def test_shortcut_failure_rolls_back_replaced_installation(self):
        dest = self.make_installation(self.root / "install" / "Takt")
        archive = self.make_archive()
        with self.install_mocks(archive), patch.object(
                installer, "make_shortcut", side_effect=installer.ShortcutError("denied")):
            with self.assertRaisesRegex(installer.InstallerError, "denied"):
                installer.install(dest, launch=False)
        self.assertEqual((dest / installer.EXE_NAME).read_bytes(), b"old")
        self.assertEqual(list(dest.parent.glob(".Takt.takt-install-*")), [])

    def test_non_takt_directory_is_not_replaced(self):
        dest = self.root / "install" / "Other"
        dest.mkdir(parents=True)
        (dest / "important.txt").write_text("keep", encoding="utf-8")
        archive = self.make_archive()
        with self.install_mocks(archive):
            with self.assertRaises(installer.UnsafeInstallPathError):
                installer.install(dest, launch=False)
        self.assertEqual((dest / "important.txt").read_text(encoding="utf-8"), "keep")

    def test_extract_rejects_traversal(self):
        archive = self.make_archive({"../evil.txt": b"bad", "Takt.exe": b"new"})
        stage = self.root / "stage"
        with self.assertRaises(installer.InstallerError):
            installer.extract(archive, stage)
        self.assertFalse((self.root / "evil.txt").exists())

    def test_conflicting_user_file_rolls_back_without_losing_installation(self):
        dest = self.make_installation(self.root / "install" / "Takt")
        (dest / "resources").mkdir()
        (dest / "resources" / "user.txt").write_text("keep", encoding="utf-8")
        archive = self.make_archive({
            "Takt.exe": b"new", "resources/icon.png": b"icon",
            "resources/user.txt": b"replacement",
        })
        with self.install_mocks(archive):
            with self.assertRaisesRegex(
                    installer.InstallerError, "Пользовательский файл конфликтует"):
                installer.install(dest, launch=False)
        self.assertEqual((dest / installer.EXE_NAME).read_bytes(), b"old")
        self.assertEqual((dest / "resources" / "user.txt").read_text(encoding="utf-8"), "keep")
        self.assertEqual(list(dest.parent.glob(".Takt.takt-install-*")), [])

    def test_conflicting_user_directory_rolls_back_without_data_loss(self):
        dest = self.make_installation(self.root / "install" / "Takt")
        (dest / "resources" / "icon.png").mkdir(parents=True)
        archive = self.make_archive({
            "Takt.exe": b"new", "resources/icon.png": b"replacement",
        })
        with self.install_mocks(archive):
            with self.assertRaisesRegex(
                    installer.InstallerError, "Пользовательский каталог конфликтует"):
                installer.install(dest, launch=False)
        self.assertEqual((dest / installer.EXE_NAME).read_bytes(), b"old")
        self.assertTrue((dest / "resources" / "icon.png").is_dir())
        self.assertEqual(list(dest.parent.glob(".Takt.takt-install-*")), [])


class TestSafeUninstall(InstallerTestCase):
    def test_installed_dir_rejects_unsafe_registry_paths(self):
        unrelated = self.root / "Other"
        unrelated.mkdir()
        (unrelated / installer.EXE_NAME).write_bytes(b"foreign")
        empty_legacy = self.root / "legacy" / "Takt"
        empty_legacy.mkdir(parents=True)
        cases = [
            Path(self.root.anchor),
            self.home,
            self.system / "Takt",
            self.system / "ProgramData" / "Takt",
            unrelated,
            empty_legacy,
        ]
        for location in cases:
            with self.subTest(location=location):
                fake, _ = self.fake_registry({
                    "InstallLocation": str(location), "DisplayName": "Takt",
                })
                with patch.dict(sys.modules, {"winreg": fake}):
                    with self.assertRaises(installer.UnsafeInstallPathError):
                        installer.installed_dir()

    def test_installed_dir_rejects_relative_registry_path(self):
        dest = self.root / "custom"
        dest.mkdir()
        (dest / installer.INSTALL_SENTINEL).write_text("{}", encoding="utf-8")
        fake, _ = self.fake_registry({
            "InstallLocation": "custom", "DisplayName": "Takt",
        })
        # A relative value must be rejected before resolving it against cwd.
        with patch.dict(sys.modules, {"winreg": fake}):
            with self.assertRaises(installer.UnsafeInstallPathError):
                installer.installed_dir()

    def test_installed_dir_accepts_only_sentinel_or_strict_legacy_layout(self):
        legacy = self.root / "old" / "Takt"
        legacy.mkdir(parents=True)
        (legacy / installer.EXE_NAME).write_bytes(b"old")
        custom = self.root / "custom"
        custom.mkdir()
        (custom / installer.INSTALL_SENTINEL).write_text("{}", encoding="utf-8")
        for location in (legacy, custom):
            with self.subTest(location=location):
                fake, _ = self.fake_registry({
                    "InstallLocation": str(location), "DisplayName": "Takt",
                })
                with patch.dict(sys.modules, {"winreg": fake}):
                    self.assertEqual(installer.installed_dir(), location.resolve())

    def test_installed_dir_normalizes_path(self):
        custom = self.root / "custom"
        custom.mkdir()
        (custom / installer.INSTALL_SENTINEL).write_text("{}", encoding="utf-8")
        location = self.root / "missing" / ".." / "custom"
        fake, _ = self.fake_registry({
            "InstallLocation": str(location), "DisplayName": "Takt",
        })
        with patch.dict(sys.modules, {"winreg": fake}):
            self.assertEqual(installer.installed_dir(), custom.resolve())

    def test_missing_registry_does_not_fall_back_to_default(self):
        fake, _ = self.fake_registry({}, open_error=FileNotFoundError())
        with patch.dict(sys.modules, {"winreg": fake}):
            with self.assertRaises(installer.UninstallError):
                installer.installed_dir()

    def test_foreign_registry_entry_is_rejected(self):
        dest = self.root / "custom"
        dest.mkdir()
        (dest / installer.INSTALL_SENTINEL).write_text("{}", encoding="utf-8")
        fake, _ = self.fake_registry({
            "InstallLocation": str(dest), "DisplayName": "Other",
        })
        with patch.dict(sys.modules, {"winreg": fake}):
            with self.assertRaises(installer.UninstallError):
                installer.installed_dir()

    def test_uninstall_does_not_report_success_if_directory_remains(self):
        dest = self.make_installation(self.root / "install" / "Takt")
        messages = []
        with patch.object(installer, "installed_dir", return_value=dest), patch.object(
                installer, "wait_app_close", return_value=True), patch.object(
                installer, "shortcut_paths", return_value=(self.root / "a.lnk", self.root / "b.lnk")), patch.object(
                installer, "remove_shortcut"), patch.object(installer, "frozen", return_value=False), patch.object(
                installer.shutil, "rmtree"), patch.object(installer, "unregister_uninstall") as unregister:
            with self.assertRaisesRegex(installer.UninstallError, "всё ещё существует"):
                installer.uninstall(progress=messages.append)
        unregister.assert_not_called()
        self.assertNotIn("Удалено", messages)

    def test_uninstall_preserves_unmanaged_user_files(self):
        dest = self.make_installation(self.root / "install" / "Takt")
        (dest / "data").mkdir()
        (dest / "data/user.txt").write_text("keep", encoding="utf-8")
        with patch.object(installer, "installed_dir", return_value=dest), patch.object(
                installer, "wait_app_close", return_value=True), patch.object(
                installer, "shortcut_paths") as shortcuts, patch.object(
                installer.shutil, "rmtree") as remove, patch.object(
                installer, "unregister_uninstall") as unregister:
            with self.assertRaisesRegex(installer.UninstallError, "пользовательские файлы"):
                installer.uninstall()
        shortcuts.assert_not_called()
        remove.assert_not_called()
        unregister.assert_not_called()
        self.assertTrue((dest / "data/user.txt").exists())

    def test_uninstall_preserves_empty_user_directory(self):
        dest = self.make_installation(self.root / "install" / "Takt")
        (dest / "data" / "empty").mkdir(parents=True)
        with patch.object(installer, "installed_dir", return_value=dest), patch.object(
                installer, "wait_app_close", return_value=True), patch.object(
                installer.shutil, "rmtree") as remove:
            with self.assertRaisesRegex(installer.UninstallError, "пользовательские файлы"):
                installer.uninstall()
        remove.assert_not_called()
        self.assertTrue((dest / "data/empty").is_dir())

    def test_successful_uninstall_removes_directory_and_registry_entry(self):
        dest = self.make_installation(self.root / "install" / "Takt")
        messages = []
        with patch.object(installer, "installed_dir", return_value=dest), patch.object(
                installer, "wait_app_close", return_value=True), patch.object(
                installer, "shortcut_paths", return_value=(self.root / "a.lnk", self.root / "b.lnk")), patch.object(
                installer, "remove_shortcut") as remove_shortcut, patch.object(
                installer, "frozen", return_value=False), patch.object(
                installer, "unregister_uninstall") as unregister:
            installer.uninstall(progress=messages.append)
        self.assertFalse(dest.exists())
        self.assertEqual(remove_shortcut.call_count, 2)
        unregister.assert_called_once_with(dest)
        self.assertEqual(messages[-1], "Удалено")

    def test_self_uninstall_is_deferred_not_reported_as_success(self):
        dest = self.make_installation(
            self.root / "install" / "Takt", extra_files=("uninstall.exe",))
        with patch.object(installer, "installed_dir", return_value=dest), patch.object(
                installer, "wait_app_close", return_value=True), patch.object(
                installer, "shortcut_paths", return_value=(self.root / "a.lnk", self.root / "b.lnk")), patch.object(
                installer, "remove_shortcut"), patch.object(installer, "frozen", return_value=True), patch.object(
                installer.sys, "executable", str(dest / "uninstall.exe")), patch.object(
                installer, "_schedule_deferred_uninstall") as schedule, patch.object(
                installer.shutil, "rmtree") as remove, patch.object(
                installer, "unregister_uninstall") as unregister:
            with self.assertRaises(installer.UninstallDeferredError):
                installer.uninstall()
        schedule.assert_called_once_with(dest)
        remove.assert_not_called()
        unregister.assert_not_called()
        self.assertTrue(dest.exists())

    def test_unregister_refuses_foreign_or_mismatched_entry(self):
        dest = self.root / "Takt"
        for display, location in (("Other", str(dest)), ("Takt", str(self.root / "elsewhere"))):
            with self.subTest(display=display, location=location):
                fake, _ = self.fake_registry({
                    "DisplayName": display, "InstallLocation": location,
                })
                with patch.dict(sys.modules, {"winreg": fake}):
                    with self.assertRaises(installer.RegistryError):
                        installer.unregister_uninstall(dest)
                fake.DeleteKey.assert_not_called()

    def test_unregister_deletes_own_entry(self):
        dest = self.root / "Takt"
        fake, _ = self.fake_registry({
            "DisplayName": "Takt", "InstallLocation": str(dest),
        })
        with patch.dict(sys.modules, {"winreg": fake}):
            installer.unregister_uninstall(dest)
        fake.DeleteKey.assert_called_once_with(fake.HKEY_CURRENT_USER, installer.UNINST_KEY)


class TestRegistryRollback(InstallerTestCase):
    def test_snapshot_restore_removes_added_values_and_restores_originals(self):
        store = {
            "DisplayName": ("Takt", 1),
            "Custom": (9, 4),
            "AddedByFailedInstall": ("temporary", 1),
        }
        snapshot = {
            "DisplayName": ("Takt", 1),
            "Custom": (7, 4),
        }

        class StoreKey:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def enum_value(key, index):
            try:
                return list(store.items())[index]
            except IndexError as ex:
                raise OSError("no more values") from ex

        fake = types.SimpleNamespace(
            HKEY_CURRENT_USER=object(),
            CreateKey=lambda *args: StoreKey(),
            EnumValue=enum_value,
            DeleteValue=lambda key, name: store.pop(name),
            SetValueEx=lambda key, name, reserved, kind, value: store.__setitem__(
                name, (value, kind)),
        )
        with patch.dict(sys.modules, {"winreg": fake}):
            installer._restore_registry_snapshot(snapshot)
        self.assertEqual(store, snapshot)


class TestShortcutsAndExitCodes(InstallerTestCase):
    def test_shortcut_paths_are_not_concatenated_into_powershell(self):
        target = self.root / "O'Brien" / "Takt.exe"
        lnk = self.root / "Start's Menu" / "Takt.lnk"
        with patch.object(installer, "_shortcut_target", return_value=None), patch.object(
                installer, "_run_powershell", return_value=subprocess.CompletedProcess([], 0, "", "")) as powershell:
            installer.make_shortcut(target, lnk)
        script = powershell.call_args.args[0]
        env = powershell.call_args.kwargs["env"]
        self.assertNotIn(str(target), script)
        self.assertNotIn(str(lnk), script)
        self.assertEqual(env["TAKT_SHORTCUT_TARGET"], str(target.resolve()))
        self.assertEqual(env["TAKT_SHORTCUT_LINK"], str(lnk.resolve()))
        self.assertIn("WScript.Shell", script)
        self.assertIn(".Save()", script)

    def test_known_folders_include_redirected_locations(self):
        desktop = self.root / "OneDrive" / "Desktop"
        start = self.root / "OneDrive" / "Start Menu"
        result = subprocess.CompletedProcess(
            [], 0, json.dumps({"Desktop": str(desktop), "StartMenu": str(start)}), "")
        with patch.object(installer, "_run_powershell", return_value=result):
            actual_start, actual_desktop = installer.shortcut_paths()
        self.assertEqual(actual_start, start / "Programs" / "Takt.lnk")
        self.assertEqual(actual_desktop, desktop / "Takt.lnk")

    def test_powershell_failure_text_is_checked(self):
        result = subprocess.CompletedProcess([], 7, "", "Access denied")
        with patch.object(installer.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(installer.ShortcutError, "7.*Access denied"):
                installer._run_powershell("throw 'test'")

    def test_make_shortcut_refuses_foreign_existing_link(self):
        lnk = self.root / "Takt.lnk"
        lnk.write_bytes(b"foreign")
        with patch.object(installer, "_shortcut_target", return_value=self.root / "other.exe"), patch.object(
                installer, "_run_powershell") as powershell:
            with self.assertRaises(installer.ShortcutError):
                installer.make_shortcut(self.root / "Takt.exe", lnk)
        powershell.assert_not_called()
        self.assertEqual(lnk.read_bytes(), b"foreign")

    def test_remove_shortcut_removes_only_owned_target(self):
        lnk = self.root / "Takt.lnk"
        lnk.write_bytes(b"link")
        target = self.root / "Takt.exe"
        with patch.object(installer, "_shortcut_target", return_value=self.root / "other.exe"):
            self.assertFalse(installer.remove_shortcut(lnk, target))
        self.assertTrue(lnk.exists())
        with patch.object(installer, "_shortcut_target", return_value=target):
            self.assertTrue(installer.remove_shortcut(lnk, target))
        self.assertFalse(lnk.exists())

    def test_main_returns_honest_exit_codes(self):
        cases = [
            (installer.AppRunningError("running"), installer.EXIT_APP_RUNNING),
            (RuntimeError("failed"), installer.EXIT_ERROR),
        ]
        for error, expected in cases:
            with self.subTest(error=error), patch.object(
                    sys, "argv", ["setup", "/S", f"/DIR={self.root / 'Takt'}"]), patch.object(
                    installer, "install", side_effect=error), patch.object(sys, "stderr", io.StringIO()):
                self.assertEqual(installer.main(), expected)
        with patch.object(sys, "argv", ["setup", "/S"]), patch.object(
                installer, "install"), patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(installer.main(), installer.EXIT_OK)
        with patch.object(sys, "argv", ["uninstall", "/uninstall", "/S"]), patch.object(
                installer, "uninstall", side_effect=installer.UninstallDeferredError("later")), patch.object(
                sys, "stderr", io.StringIO()):
            self.assertEqual(installer.main(), installer.EXIT_DEFERRED)


if __name__ == "__main__":
    unittest.main()

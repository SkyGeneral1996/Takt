import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build


class BuildTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "takt").mkdir()
        (self.root / "assets").mkdir()
        (self.root / "takt" / "__init__.py").write_text('__version__ = "9.9.9"\n', encoding="utf-8")
        (self.root / "main.py").write_text("print('app')\n", encoding="utf-8")
        (self.root / "requirements.txt").write_text(
            "flet>=1.1.3\nrequests>=2.32\nqrcode>=8.0\n", encoding="utf-8")
        (self.root / "pyproject.toml").write_text(
            '[project]\nname = "takt"\nversion = "9.9.9"\n'
            'dependencies = ["flet>=1.1.3", "requests>=2.32"]\n', encoding="utf-8")
        (self.root / "assets" / "icon.png").write_bytes(b"png")
        (self.root / "assets" / "icon.ico").write_bytes(b"ico")

    def stamp(self):
        versions = {"flet": "1.1.3", "requests": "2.32.5", "qrcode": "8.2.1"}
        with patch.object(build.metadata, "version", side_effect=versions.__getitem__), patch.object(
                build.platform, "python_version", return_value="3.11.9"), patch.object(
                build.platform, "python_implementation", return_value="CPython"), patch.object(
                build.platform, "system", return_value="Windows"), patch.object(
                build.platform, "release", return_value="11"), patch.object(
                build.platform, "machine", return_value="AMD64"), patch.object(
                build.platform, "platform", return_value="Windows-11-AMD64"), patch.object(
                build.sysconfig, "get_config_var", return_value="cp311-win_amd64"):
            return build.compute_native_stamp(self.root, version="9.9.9")

    def test_native_stamp_contains_real_versions_abi_platform_and_sources(self):
        stamp = self.stamp()
        self.assertEqual(stamp["version"], "9.9.9")
        self.assertEqual(stamp["flet"], "1.1.3")
        self.assertEqual(stamp["dependencies"], {
            "flet": "1.1.3", "qrcode": "8.2.1", "requests": "2.32.5",
        })
        self.assertEqual(stamp["python"]["version"], "3.11.9")
        self.assertEqual(stamp["python"]["abi"], "cp311-win_amd64")
        self.assertEqual(stamp["platform"]["machine"], "AMD64")
        self.assertIn("main.py", stamp["sources"])
        self.assertIn("takt/__init__.py", stamp["sources"])
        self.assertIn("assets/icon.png", stamp["sources"])
        expected = hashlib.sha256((self.root / "main.py").read_bytes()).hexdigest()
        self.assertEqual(stamp["sources"]["main.py"], expected)
        self.assertEqual(len(stamp["native"]), 64)

    def test_native_stamp_changes_with_source_and_resource_content(self):
        original = self.stamp()["native"]
        (self.root / "takt" / "__init__.py").write_text("__version__ = '9.9.10'\n", encoding="utf-8")
        self.assertNotEqual(self.stamp()["native"], original)
        (self.root / "assets" / "icon.png").write_bytes(b"different png")
        self.assertNotEqual(self.stamp()["native"], original)

    def test_native_stamp_changes_with_dependency_version(self):
        original = self.stamp()["native"]
        versions = {"flet": "1.1.4", "requests": "2.32.5", "qrcode": "8.2.1"}
        with patch.object(build.metadata, "version", side_effect=versions.__getitem__), patch.object(
                build.platform, "python_version", return_value="3.11.9"), patch.object(
                build.platform, "python_implementation", return_value="CPython"), patch.object(
                build.platform, "system", return_value="Windows"), patch.object(
                build.platform, "release", return_value="11"), patch.object(
                build.platform, "machine", return_value="AMD64"), patch.object(
                build.platform, "platform", return_value="Windows-11-AMD64"), patch.object(
                build.sysconfig, "get_config_var", return_value="cp311-win_amd64"):
            changed = build.compute_native_stamp(self.root, version="9.9.9")
        self.assertNotEqual(changed["native"], original)

    def test_write_native_stamp_does_not_run_pack_and_is_deterministic(self):
        with patch.object(build.subprocess, "run") as run:
            first = build.write_native_stamp(self.root)
            second = build.compute_native_stamp(self.root)
        run.assert_not_called()
        self.assertEqual(first["native"], second["native"])
        saved = json.loads((self.root / "takt" / "native_stamp.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["native"], first["native"])
        self.assertEqual(list((self.root / "takt").glob(".native_stamp-*.tmp")), [])

    def test_copy_bundle_resources_copies_icons_and_stamp(self):
        (self.root / "takt" / "native_stamp.json").write_text('{"native":"abc"}', encoding="utf-8")
        bundle = self.root / "dist" / "Takt"
        bundle.mkdir(parents=True)
        build.copy_bundle_resources(bundle, self.root)
        self.assertEqual((bundle / "resources" / "icon.png").read_bytes(), b"png")
        self.assertEqual((bundle / "resources" / "icon.ico").read_bytes(), b"ico")
        self.assertEqual((bundle / "takt" / "native_stamp.json").read_text(encoding="utf-8"),
                         '{"native":"abc"}')

    def test_clear_staging_removes_only_named_build_artifact(self):
        stage = self.root / "release"
        old_bundle = stage / "Takt"
        old_bundle.mkdir(parents=True)
        (old_bundle / "stale.exe").write_bytes(b"old")
        (stage / "keep.txt").write_text("unrelated", encoding="utf-8")
        with patch.object(build, "STAGE", stage):
            build.clear_staging("Takt")
            with self.assertRaises(build.BuildError):
                build.clear_staging("../outside")
        self.assertFalse(old_bundle.exists())
        self.assertEqual((stage / "keep.txt").read_text(encoding="utf-8"), "unrelated")

    def test_clear_staging_refuses_reparse_release_root(self):
        with patch.object(build, "STAGE", self.root / "release"), patch.object(
                build, "_is_reparse_path", return_value=True), patch.object(
                build.shutil, "rmtree") as remove:
            with self.assertRaises(build.BuildError):
                build.clear_staging("Takt")
        remove.assert_not_called()

    def test_flet_paths_are_cross_platform(self):
        venv = self.root / ".venv"
        self.assertEqual(build._flet_in_venv(venv, windows=True), venv / "Scripts" / "flet.exe")
        self.assertEqual(build._flet_in_venv(venv, windows=False), venv / "bin" / "flet")

    def test_find_flet_searches_project_and_ancestors_not_cwd(self):
        project = self.root / "project"
        nested = project / "src"
        nested.mkdir(parents=True)
        executable = build._flet_in_venv(project / ".venv")
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"flet")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        previous = Path.cwd()
        try:
            os.chdir(elsewhere)
            self.assertEqual(build.find_flet(nested), executable)
        finally:
            os.chdir(previous)

    def test_find_flet_uses_virtual_env_fallback(self):
        project = self.root / "isolated" / "project"
        project.mkdir(parents=True)
        venv = self.root / "virtual"
        executable = build._flet_in_venv(venv)
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"flet")
        with patch.dict(os.environ, {"VIRTUAL_ENV": str(venv)}), patch.object(
                build.Path, "is_file", autospec=True, side_effect=lambda path: path == executable):
            self.assertEqual(build.find_flet(project), executable)


if __name__ == "__main__":
    unittest.main()

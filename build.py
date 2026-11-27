"""Сборка дистрибутивов Takt под Windows.

Результат в dist/:
  Takt-Portable.exe  — standalone, без установки
  Takt/              — one-folder бандл
  Takt-Setup.exe     — установщик (GUI + /S для автообновлений)

Важно: flet pack -y стирает build/ и dist/ перед каждой сборкой,
поэтому готовые артефакты между шагами хранятся в release/.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile
import tomllib
import zipfile
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
STAGE = ROOT / "release"
ICON = ROOT / "assets" / "icon.ico"

sys.path.insert(0, str(ROOT))
from takt import __version__ as VERSION


class BuildError(RuntimeError):
    """A distribution could not be prepared safely."""


def _flet_in_venv(venv: Path, *, windows: bool | None = None) -> Path:
    is_windows = os.name == "nt" if windows is None else windows
    if is_windows:
        return venv / "Scripts" / "flet.exe"
    return venv / "bin" / "flet"


def find_flet(project_root: Path | None = None) -> Path:
    """Find the nearest project/ancestor .venv independently of cwd."""
    start = (project_root or ROOT).resolve()
    for directory in (start, *start.parents):
        candidate = _flet_in_venv(directory / ".venv")
        if candidate.is_file():
            return candidate
    virtual_env = os.environ.get("VIRTUAL_ENV")
    if virtual_env:
        candidate = _flet_in_venv(Path(virtual_env))
        if candidate.is_file():
            return candidate
    raise BuildError(f"Не найден flet в .venv рядом с {start}")


def _dependency_names(project_root: Path) -> set[str]:
    names: set[str] = set()
    requirements = project_root / "requirements.txt"
    if requirements.is_file():
        for line in requirements.read_text(encoding="utf-8").splitlines():
            match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", line)
            if match:
                names.add(match.group(1))
    pyproject = project_root / "pyproject.toml"
    if pyproject.is_file():
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        for requirement in data.get("project", {}).get("dependencies", []):
            match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
            if match:
                names.add(match.group(1))
    return names


def _installed_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _source_hashes(project_root: Path) -> dict[str, str]:
    files: set[Path] = set()
    for name in ("main.py", "installer.py", "build.py", "requirements.txt", "pyproject.toml"):
        path = project_root / name
        if path.is_file():
            files.add(path)
    for name in ("takt", "assets"):
        base = project_root / name
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if (path.is_file() and "__pycache__" not in path.parts
                    and path.suffix not in {".pyc", ".pyo"}
                    and path.name != "native_stamp.json"):
                files.add(path)
    result = {}
    for path in sorted(files, key=lambda item: item.relative_to(project_root).as_posix()):
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        result[path.relative_to(project_root).as_posix()] = digest.hexdigest()
    return result


def compute_native_stamp(project_root: Path | None = None,
                         version: str | None = None) -> dict:
    """Compute the native fingerprint without invoking flet pack."""
    project_root = (project_root or ROOT).resolve()
    dependencies = {name: _installed_version(name)
                    for name in sorted(_dependency_names(project_root), key=str.casefold)}
    flet_version = _installed_version("flet")
    if flet_version is None:
        raise BuildError("Flet не установлен в интерпретаторе сборки")
    abi = sysconfig.get_config_var("SOABI") or sys.implementation.cache_tag
    payload = {
        "schema": 2,
        "version": str(version if version is not None else VERSION),
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "abi": abi,
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "platform": platform.platform(),
        },
        "flet": flet_version,
        "dependencies": dependencies,
        "sources": _source_hashes(project_root),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    payload["native"] = hashlib.sha256(canonical).hexdigest()
    return payload


def write_native_stamp(project_root: Path | None = None) -> dict:
    """Atomically write takt/native_stamp.json; safe to call from tests."""
    project_root = (project_root or ROOT).resolve()
    stamp = compute_native_stamp(project_root)
    target = project_root / "takt" / "native_stamp.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=target.parent,
                prefix=".native_stamp-", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            json.dump(stamp, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print("native stamp:", stamp["native"][:12], flush=True)
    return stamp


def copy_bundle_resources(bundle: Path, project_root: Path | None = None) -> None:
    """Ensure runtime resources and the current stamp are in the folder bundle."""
    project_root = (project_root or ROOT).resolve()
    bundle = bundle.resolve()
    resources = bundle / "resources"
    resources.mkdir(parents=True, exist_ok=True)
    for name in ("icon.png", "icon.ico"):
        source = project_root / "assets" / name
        if not source.is_file():
            raise BuildError(f"Не найден ресурс: {source}")
        shutil.copy2(source, resources / name)
    stamp = project_root / "takt" / "native_stamp.json"
    if not stamp.is_file():
        raise BuildError(f"Не найден native stamp: {stamp}")
    native_dir = bundle / "takt"
    native_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(stamp, native_dir / "native_stamp.json")


def _is_reparse_path(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (AttributeError, OSError):
        return False


def clear_staging(name: str) -> None:
    """Remove only a named build-owned entry, never the release root itself."""
    if not name or name in {".", ".."} or Path(name).name != name or "/" in name or "\\" in name:
        raise BuildError(f"Некорректное имя staging-артефакта: {name!r}")
    if _is_reparse_path(STAGE):
        raise BuildError(f"Release-каталог не должен быть ссылкой: {STAGE}")
    target = STAGE / name
    if _is_reparse_path(target):
        if target.is_symlink() or not target.is_dir():
            target.unlink()
        else:
            target.rmdir()
    elif target.is_file():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)


def run(*args):
    print(">>", " ".join(str(a) for a in args), flush=True)
    subprocess.run([str(a) for a in args], check=True, cwd=ROOT)


def main():
    if _is_reparse_path(STAGE):
        raise BuildError(f"Release-каталог не должен быть ссылкой: {STAGE}")
    if _is_reparse_path(DIST):
        raise BuildError(f"Dist-каталог не должен быть ссылкой: {DIST}")
    STAGE.mkdir(exist_ok=True)
    flet = find_flet()
    write_native_stamp()

    run(flet, "pack", "main.py", "-n", "Takt", "-D", "--distpath", DIST,
        "--product-name", "Takt", "--product-version", VERSION, "--icon", str(ICON), "-y")

    bundle = DIST / "Takt"
    if not bundle.is_dir():
        raise BuildError(f"Flet не создал bundle: {bundle}")
    copy_bundle_resources(bundle)

    app_zip = STAGE / "app.zip"
    clear_staging("app.zip")
    with zipfile.ZipFile(app_zip, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in bundle.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(bundle))
    print("app.zip:", app_zip.stat().st_size, "bytes", flush=True)
    clear_staging("Takt")
    shutil.move(str(bundle), str(STAGE / "Takt"))

    run(flet, "pack", "installer.py", "-n", "Takt-Setup", "--distpath", DIST,
        "--add-data", f"{app_zip};.",
        "--product-name", "Takt Setup", "--product-version", VERSION, "--icon", str(ICON), "-y",
        "--pyinstaller-build-args=--noconsole")
    clear_staging("Takt-Setup.exe")
    shutil.move(str(DIST / "Takt-Setup.exe"), str(STAGE / "Takt-Setup.exe"))

    run(flet, "pack", "main.py", "-n", "Takt-Portable", "--distpath", DIST,
        "--product-name", "Takt", "--product-version", VERSION, "--icon", str(ICON), "-y")
    clear_staging("Takt-Portable.exe")
    shutil.move(str(DIST / "Takt-Portable.exe"), str(STAGE / "Takt-Portable.exe"))

    shutil.rmtree(DIST, ignore_errors=True)
    DIST.mkdir()
    for name in ("Takt", "Takt-Setup.exe", "Takt-Portable.exe"):
        shutil.move(str(STAGE / name), str(DIST / name))
    app_zip.unlink(missing_ok=True)
    try:
        STAGE.rmdir()
    except OSError as ex:
        print(f"Предупреждение: release содержит посторонние файлы: {ex}", flush=True)

    print("\nГотово:", flush=True)
    for path in sorted(DIST.iterdir()):
        print(" ", path.name, flush=True)


if __name__ == "__main__":
    main()

"""Установщик Takt: GUI (tkinter) + тихий режим /S для автообновлений.

Режимы:
  Takt-Setup.exe            — GUI-установка
  Takt-Setup.exe /S         — тихая установка (используется механизмом обновлений)
  Takt-Setup.exe /S /DIR=X  — тихая установка в каталог X
  uninstall.exe /uninstall [/S] — удаление
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

APP_NAME = "Takt"
EXE_NAME = "Takt.exe"
UNINST_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Takt"
INSTALL_SENTINEL = ".takt-install"
CREATE_NO_WINDOW = 0x08000000
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_DEFERRED = 2
EXIT_APP_RUNNING = 3


class InstallerError(RuntimeError):
    """Base error for a failed installer operation."""


class AppRunningError(InstallerError):
    """The application could not be confirmed closed."""


class UnsafeInstallPathError(InstallerError):
    """A path is not safe to use as an installation directory."""


class ShortcutError(InstallerError):
    """A Windows shortcut could not be created, read, or removed."""


class RegistryError(InstallerError):
    """The uninstall registry entry could not be processed safely."""


class UninstallError(InstallerError):
    """Uninstall did not complete."""


class UninstallDeferredError(UninstallError):
    """A helper will finish uninstall after this process exits."""


def application_version() -> str:
    """Read the single project version, including in source checkouts."""
    try:
        from takt import __version__
    except ImportError:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version(APP_NAME.lower())
        except PackageNotFoundError as ex:
            raise InstallerError("Не удалось определить версию Takt") from ex
    return str(__version__)


VERSION = application_version()


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_zip() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / "app.zip"


def default_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "Programs" / APP_NAME


def _resolved(path: Path | str) -> Path:
    try:
        return Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as ex:
        raise UnsafeInstallPathError(f"Некорректный путь: {path!s}") from ex


def _system_dirs() -> list[Path]:
    result = []
    for name in ("SystemRoot", "WINDIR", "ProgramFiles", "ProgramW6432",
                 "ProgramFiles(x86)", "CommonProgramFiles", "ProgramData"):
        value = os.environ.get(name)
        if value:
            try:
                result.append(_resolved(value))
            except UnsafeInstallPathError:
                pass
    return result


def validate_install_path(path: Path | str, *, require_installation: bool = False) -> Path:
    """Normalize a path and refuse filesystem roots and protected folders."""
    if not str(path).strip():
        raise UnsafeInstallPathError("Пустой каталог установки")
    dest = _resolved(path)
    if dest.parent == dest:
        raise UnsafeInstallPathError("Нельзя использовать корень диска")
    if dest == _resolved(Path.home()):
        raise UnsafeInstallPathError("Нельзя использовать домашнюю папку")
    for system_dir in _system_dirs():
        if dest == system_dir or system_dir in dest.parents:
            raise UnsafeInstallPathError(f"Нельзя использовать системную папку: {dest}")
    if require_installation and not is_takt_installation(dest):
        raise UnsafeInstallPathError(
            f"В каталоге нет признаков установки Takt: {dest}")
    return dest


def is_takt_installation(path: Path) -> bool:
    """Require the new sentinel, or the strictly checked legacy layout."""
    return ((path / INSTALL_SENTINEL).is_file()
            or ((path / EXE_NAME).is_file() and path.name.casefold() == APP_NAME.casefold()))


def _read_manifest(dest: Path) -> dict | None:
    try:
        value = json.loads((dest / INSTALL_SENTINEL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _manifest_files(dest: Path) -> set[str] | None:
    manifest = _read_manifest(dest)
    files = manifest.get("files") if manifest else None
    if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
        return None
    return set(files)


def _write_manifest(stage: Path, files: set[str], version: str) -> None:
    files = set(files) | {INSTALL_SENTINEL}
    (stage / INSTALL_SENTINEL).write_text(json.dumps({
        "app": APP_NAME,
        "version": version,
        "files": sorted(files),
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def _safe_relative(name: str) -> PurePosixPath | None:
    normalized = name.replace("\\", "/")
    relative = PurePosixPath(normalized)
    if (not normalized or relative.is_absolute() or ".." in relative.parts
            or PureWindowsPath(normalized).drive or any(":" in part for part in relative.parts)):
        return None
    return relative


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (AttributeError, OSError):
        return False


def _walk_files(root: Path):
    def onerror(error):
        raise error

    for current, dirs, files in os.walk(root, followlinks=False, onerror=onerror):
        current_path = Path(current)
        for name in list(dirs):
            child = current_path / name
            if _is_reparse_point(child):
                dirs.remove(name)
                yield child, child.relative_to(root).as_posix()
        for name in files:
            child = current_path / name
            yield child, child.relative_to(root).as_posix()


def _walk_directories(root: Path):
    def onerror(error):
        raise error

    for current, dirs, _ in os.walk(root, followlinks=False, onerror=onerror):
        current_path = Path(current)
        for name in list(dirs):
            child = current_path / name
            if _is_reparse_point(child):
                dirs.remove(name)
            else:
                yield child, child.relative_to(root).as_posix()


_LEGACY_BUNDLE_ENTRIES = {
    EXE_NAME.casefold(), "uninstall.exe", "main.py", "_internal", "takt",
    "resources", "assets", "icon.png", "icon.ico", INSTALL_SENTINEL,
}


def _owned_directories(files: set[str]) -> set[str]:
    result = set()
    for relative in files:
        parts = PurePosixPath(relative).parts
        for index in range(1, len(parts)):
            result.add("/".join(parts[:index]))
    return result


def _unmanaged_files(dest: Path) -> list[str]:
    """Files and directories not owned by the installer must never be deleted."""
    managed = _manifest_files(dest)
    unknown = []
    for source, relative in _walk_files(dest):
        if _is_reparse_point(source):
            unknown.append(relative)
        elif managed is None:
            # Without a manifest only the conventional legacy bundle layout
            # is accepted. Unknown top-level files may belong to the user.
            if relative.split("/", 1)[0].casefold() not in _LEGACY_BUNDLE_ENTRIES:
                unknown.append(relative)
        elif relative not in managed:
            unknown.append(relative)
    if managed is not None:
        owned = _owned_directories(managed)
        for _, relative in _walk_directories(dest):
            if relative not in owned:
                unknown.append(relative + "/")
    else:
        # Empty user directories do not appear in os.walk's file list.
        for source in dest.iterdir():
            if (source.name.casefold() not in _LEGACY_BUNDLE_ENTRIES
                    and not any(item == source.name or item.startswith(source.name + "/")
                                for item in unknown)):
                unknown.append(source.name)
    return unknown


def _copy_entry(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if _is_reparse_point(source):
        if not source.is_symlink():
            raise InstallerError(f"Junction в пользовательских файлах не поддерживается: {source}")
        target.symlink_to(os.readlink(source), target_is_directory=source.is_dir())
    elif source.is_dir():
        for nested, _ in _walk_files(source):
            if _is_reparse_point(nested) and not nested.is_symlink():
                raise InstallerError(f"Junction в пользовательских файлах не поддерживается: {nested}")
        shutil.copytree(source, target, dirs_exist_ok=True, symlinks=True)
    else:
        shutil.copy2(source, target)


def _preserve_user_files(dest: Path, stage: Path) -> None:
    managed = _manifest_files(dest)
    if managed is not None:
        owned_directories = _owned_directories(managed)
        for source, relative in _walk_files(dest):
            if relative in managed:
                continue
            safe = _safe_relative(relative)
            if safe is None:
                raise InstallerError(f"Небезопасное имя пользовательского файла: {relative}")
            target = stage.joinpath(*safe.parts)
            if target.exists() or target.is_symlink():
                raise InstallerError(
                    f"Пользовательский файл конфликтует с новой версией: {relative}")
            _copy_entry(source, target)
        # os.walk reports files only; retain user-owned empty directories too.
        for _, relative in _walk_directories(dest):
            if relative in owned_directories:
                continue
            safe = _safe_relative(relative)
            if safe is None:
                raise InstallerError(f"Небезопасное имя пользовательского каталога: {relative}")
            target = stage.joinpath(*safe.parts)
            if target.exists() or target.is_symlink():
                if not target.is_dir() or target.is_symlink():
                    raise InstallerError(
                        f"Пользовательский каталог конфликтует с новой версией: {relative}")
                continue
            target.mkdir(parents=True, exist_ok=True)
        return

    # For legacy folders retain unknown top-level entries (including data).
    # Known bundle directories are replaced, not merged, to avoid stale code.
    for source in dest.iterdir():
        if _is_reparse_point(source):
            raise InstallerError(f"Ссылки в старой установке не поддерживаются: {source.name}")
        if source.name.casefold() in _LEGACY_BUNDLE_ENTRIES:
            continue
        target = stage / source.name
        if target.exists() or target.is_symlink():
            if source.name.casefold() in {"data", ".takt"} and source.is_dir():
                _copy_entry(source, target)
                continue
            raise InstallerError(f"Каталог конфликтует с новой версией: {source.name}")
        _copy_entry(source, target)


def extract(zip_path: Path, dest: Path) -> set[str]:
    """Extract only regular, relative ZIP members into a fresh staging folder."""
    dest.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(zip_path) as archive:
        files: set[str] = set()
        for member in archive.infolist():
            relative = _safe_relative(member.filename)
            if relative is None or stat.S_ISLNK(member.external_attr >> 16):
                raise InstallerError(f"Небезопасная запись в архиве: {member.filename}")
            if not member.is_dir():
                files.add(relative.as_posix())
        archive.extractall(dest)
    if EXE_NAME not in files:
        raise InstallerError("В архиве отсутствует Takt.exe")
    return files


def is_app_running() -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {EXE_NAME}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
    except (OSError, subprocess.TimeoutExpired) as ex:
        raise AppRunningError(f"Не удалось проверить процессы Takt: {ex}") from ex
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise AppRunningError(
            f"tasklist завершился с кодом {result.returncode}: {detail}")
    return any(row and row[0].strip().casefold() == EXE_NAME.casefold()
               for row in csv.reader(result.stdout.splitlines()))


def wait_app_close(timeout: float = 20.0) -> bool:
    """Return True only when Takt is closed; False exactly on timeout."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if not is_app_running():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.2, remaining))


def _run_powershell(script: str, *, env: dict | None = None,
                    timeout: float = 30) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, timeout=timeout, env=env,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
    except (OSError, subprocess.TimeoutExpired) as ex:
        raise ShortcutError(f"PowerShell завершился ошибкой: {ex}") from ex
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or "без текста ошибки"
        raise ShortcutError(
            f"PowerShell завершился с кодом {result.returncode}: {detail}")
    return result


def shortcut_paths() -> tuple[Path, Path]:
    """Ask Windows for redirected Desktop and Start Menu known folders."""
    script = """
$ErrorActionPreference = 'Stop'
$desktop = [Environment]::GetFolderPath('Desktop')
$start = [Environment]::GetFolderPath('StartMenu')
if (-not $desktop -or -not $start) { throw 'Known Folder не найдена' }
@{ Desktop = $desktop; StartMenu = $start } | ConvertTo-Json -Compress
"""
    result = _run_powershell(script)
    try:
        values = json.loads(result.stdout.lstrip("\ufeff"))
        start_menu = Path(values["StartMenu"]) / "Programs" / f"{APP_NAME}.lnk"
        desktop = Path(values["Desktop"]) / f"{APP_NAME}.lnk"
    except (ValueError, KeyError, TypeError) as ex:
        raise ShortcutError(f"PowerShell вернул некорректные пути ярлыков: {result.stdout!r}") from ex
    return start_menu, desktop


def _shortcut_target(lnk: Path) -> Path | None:
    if not lnk.exists() and not lnk.is_symlink():
        return None
    env = os.environ.copy()
    env["TAKT_SHORTCUT_LINK"] = str(lnk)
    script = """
$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($env:TAKT_SHORTCUT_LINK)
$shortcut.TargetPath
"""
    result = _run_powershell(script, env=env)
    value = result.stdout.strip()
    return Path(value) if value else None


def make_shortcut(target: Path, lnk: Path) -> None:
    target = _resolved(target)
    lnk = _resolved(lnk)
    existing = _shortcut_target(lnk)
    if lnk.exists() and existing is None:
        raise ShortcutError(f"Нельзя перезаписать нечитаемый ярлык: {lnk}")
    if existing is not None and _resolved(existing) != target:
        raise ShortcutError(f"Нельзя перезаписать чужой ярлык: {lnk}")
    lnk.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["TAKT_SHORTCUT_LINK"] = str(lnk)
    env["TAKT_SHORTCUT_TARGET"] = str(target)
    env["TAKT_SHORTCUT_WORKDIR"] = str(target.parent)
    # Paths travel through the environment, never through PowerShell source.
    script = """
$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($env:TAKT_SHORTCUT_LINK)
$shortcut.TargetPath = $env:TAKT_SHORTCUT_TARGET
$shortcut.WorkingDirectory = $env:TAKT_SHORTCUT_WORKDIR
$shortcut.IconLocation = $env:TAKT_SHORTCUT_TARGET + ',0'
$shortcut.Description = 'Takt'
$shortcut.Save()
"""
    _run_powershell(script, env=env)


def remove_shortcut(lnk: Path, target: Path) -> bool:
    """Remove only a shortcut which still points at this installation."""
    existing = _shortcut_target(lnk)
    if existing is None:
        return False
    if _resolved(existing) != _resolved(target):
        return False
    try:
        lnk.unlink()
    except OSError as ex:
        raise ShortcutError(f"Не удалось удалить ярлык {lnk}: {ex}") from ex
    return True


def _registry_snapshot() -> dict | None:
    import winreg

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINST_KEY)
    except FileNotFoundError:
        return None
    values = {}
    try:
        index = 0
        while True:
            try:
                name, value, value_type = winreg.EnumValue(key, index)
            except OSError:
                break
            values[name] = (value, value_type)
            index += 1
    finally:
        winreg.CloseKey(key)
    return values


def _write_registry_values(values: dict) -> None:
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINST_KEY) as key:
        for name, (value, value_type) in values.items():
            winreg.SetValueEx(key, name, 0, value_type, value)


def _restore_registry_snapshot(snapshot: dict | None) -> None:
    import winreg

    if snapshot is None:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINST_KEY)
        except FileNotFoundError:
            pass
        return
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINST_KEY) as key:
        existing = []
        index = 0
        while True:
            try:
                existing.append(winreg.EnumValue(key, index)[0])
            except OSError:
                break
            index += 1
        for name in existing:
            if name not in snapshot:
                winreg.DeleteValue(key, name)
        for name, (value, value_type) in snapshot.items():
            winreg.SetValueEx(key, name, 0, value_type, value)


def register_uninstall(dest: Path) -> dict | None:
    import winreg

    snapshot = _registry_snapshot()
    if snapshot is not None:
        display_name = snapshot.get("DisplayName")
        if display_name is None or str(display_name[0]).casefold() != APP_NAME.casefold():
            raise RegistryError("Существующая запись реестра принадлежит другому приложению")
    values = {
        "DisplayName": (APP_NAME, winreg.REG_SZ),
        "DisplayVersion": (VERSION, winreg.REG_SZ),
        "Publisher": (APP_NAME, winreg.REG_SZ),
        "InstallLocation": (str(dest), winreg.REG_SZ),
        "DisplayIcon": (str(dest / EXE_NAME), winreg.REG_SZ),
        "UninstallString": (f'"{dest / "uninstall.exe"}" /uninstall', winreg.REG_SZ),
        "NoModify": (1, winreg.REG_DWORD),
        "NoRepair": (1, winreg.REG_DWORD),
    }
    try:
        _write_registry_values(values)
    except Exception:
        try:
            _restore_registry_snapshot(snapshot)
        except Exception as restore_error:
            raise RegistryError(
                f"Не удалось записать и восстановить запись удаления: {restore_error}") from restore_error
        raise
    return snapshot


def unregister_uninstall(expected_dir: Path | None = None) -> None:
    import winreg

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINST_KEY)
    except FileNotFoundError:
        return
    try:
        display_name = winreg.QueryValueEx(key, "DisplayName")[0]
        location = winreg.QueryValueEx(key, "InstallLocation")[0]
        if str(display_name).casefold() != APP_NAME.casefold():
            raise RegistryError("Запись удаления принадлежит другому приложению")
        if expected_dir is not None and _resolved(location) != _resolved(expected_dir):
            raise RegistryError("Запись удаления указывает на другой каталог")
    except OSError as ex:
        raise RegistryError(f"Не удалось проверить запись удаления: {ex}") from ex
    finally:
        winreg.CloseKey(key)
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINST_KEY)
    except FileNotFoundError:
        pass
    except OSError as ex:
        raise RegistryError(f"Не удалось удалить запись из реестра: {ex}") from ex


def installed_dir() -> Path:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINST_KEY) as key:
            location = winreg.QueryValueEx(key, "InstallLocation")[0]
            if (not isinstance(location, str) or not location.strip()
                    or not Path(location.strip()).is_absolute()):
                raise UnsafeInstallPathError("Пустой, относительный или некорректный InstallLocation")
            dest = validate_install_path(location.strip(), require_installation=True)
            display_name = winreg.QueryValueEx(key, "DisplayName")[0]
    except FileNotFoundError as ex:
        raise UninstallError("Takt не зарегистрирован в установке и удалении программ") from ex
    except UnsafeInstallPathError:
        raise
    except (OSError, TypeError, ValueError) as ex:
        raise UninstallError(f"Некорректная запись InstallLocation: {ex}") from ex
    if str(display_name).casefold() != APP_NAME.casefold():
        raise UninstallError("Запись реестра принадлежит другому приложению")
    return dest


def _rollback_install(*, dest: Path, backup: Path,
                      failed: Path, old_moved: bool, new_moved: bool,
                      shortcuts: list[tuple[Path, bool]],
                      registry_written: bool, registry_snapshot: dict | None) -> list[str]:
    errors = []
    if new_moved and (dest.exists() or dest.is_symlink()):
        try:
            dest.rename(failed)
        except OSError as ex:
            errors.append(f"не удалось убрать новую установку: {ex}")
    if old_moved and backup.exists():
        try:
            backup.rename(dest)
        except OSError as ex:
            errors.append(f"не удалось восстановить резервную копию {backup}: {ex}")
    for lnk, existed_before in shortcuts:
        try:
            if existed_before:
                # It already pointed at the same executable path. After the
                # directory rollback it points at the restored installation.
                continue
            lnk.unlink(missing_ok=True)
        except OSError as ex:
            errors.append(f"не удалось убрать ярлык {lnk}: {ex}")
    if registry_written:
        try:
            _restore_registry_snapshot(registry_snapshot)
        except Exception as ex:
            errors.append(f"не удалось восстановить реестр: {ex}")
    return errors


def install(dest: Path, desktop: bool = True, launch: bool = True,
            progress=None, *, close_timeout: float = 20.0) -> None:
    def say(msg):
        if progress:
            progress(msg)

    say("Ожидание закрытия приложения…")
    if not wait_app_close(close_timeout):
        raise AppRunningError(
            "Takt не закрылся за отведённое время. Установка отменена.")
    dest = validate_install_path(dest)
    if dest.exists():
        if not dest.is_dir():
            raise UnsafeInstallPathError(f"Цель не является каталогом: {dest}")
        if any(dest.iterdir()) and not is_takt_installation(dest):
            raise UnsafeInstallPathError(
                f"Нельзя заменять чужой непустой каталог: {dest}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix=f".{dest.name}.takt-install-", dir=dest.parent))
    stage = workspace / "staging"
    backup = workspace / "backup"
    failed = workspace / "failed"
    old_moved = False
    new_moved = False
    shortcuts: list[tuple[Path, bool]] = []
    registry_written = False
    registry_snapshot = None
    try:
        say("Распаковка во временный каталог…")
        archive_files = extract(resource_zip(), stage)
        if dest.exists():
            _preserve_user_files(dest, stage)
        if frozen():
            shutil.copy2(sys.executable, stage / "uninstall.exe")
            archive_files.add("uninstall.exe")
        _write_manifest(stage, archive_files, VERSION)

        if dest.exists():
            dest.rename(backup)
            old_moved = True
        stage.rename(dest)
        new_moved = True

        say("Создание ярлыков…")
        start, desk = shortcut_paths()
        for lnk in ((start, desk) if desktop else (start,)):
            existed_before = lnk.exists() or lnk.is_symlink()
            shortcuts.append((lnk, existed_before))
            make_shortcut(dest / EXE_NAME, lnk)
        registry_snapshot = register_uninstall(dest)
        registry_written = True
    except Exception as ex:
        rollback_errors = _rollback_install(
            dest=dest, backup=backup, failed=failed,
            old_moved=old_moved, new_moved=new_moved, shortcuts=shortcuts,
            registry_written=registry_written, registry_snapshot=registry_snapshot)
        if not rollback_errors:
            try:
                shutil.rmtree(workspace)
            except OSError as cleanup_error:
                rollback_errors.append(f"временный каталог не очищен: {cleanup_error}")
        detail = f" Установка отменена: {ex}"
        if rollback_errors:
            detail += ". Проблемы отката: " + "; ".join(rollback_errors)
            detail += f". Резервная копия/временные файлы: {workspace}"
        raise InstallerError(detail) from ex

    try:
        shutil.rmtree(workspace)
    except OSError as ex:
        say(f"Предупреждение: не удалось очистить {workspace}: {ex}")
    say("Готово")
    if launch:
        try:
            subprocess.Popen([str(dest / EXE_NAME)], cwd=str(dest))
        except OSError as ex:
            say(f"Установлено, но запустить не удалось: {ex}")


_DEFERRED_UNINSTALL_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$dir = [IO.Path]::GetFullPath($env:TAKT_UNINSTALL_DIR)
$root = [IO.Path]::GetPathRoot($dir)
$home = [Environment]::GetFolderPath('UserProfile')
$protected = @([Environment]::GetFolderPath('Windows'), $env:ProgramFiles,
               $env:ProgramW6432, ${env:ProgramFiles(x86)},
               $env:CommonProgramFiles, $env:ProgramData,
               [Environment]::GetFolderPath('CommonApplicationData')) | Where-Object { $_ }
$code = 1
try {
    Wait-Process -Id ([int]$env:TAKT_UNINSTALL_PID) -ErrorAction SilentlyContinue
    if (-not $dir -or $dir -eq '') { throw 'Пустой путь удаления' }
    if ($dir.TrimEnd('\') -eq $root.TrimEnd('\')) { throw 'Корень диска запрещён' }
    if ($home -and $dir -eq $home) { throw 'Домашняя папка запрещена' }
    foreach ($folder in $protected) {
        $full = [IO.Path]::GetFullPath($folder).TrimEnd('\')
        if ($dir.TrimEnd('\') -eq $full -or
            $dir.TrimEnd('\').StartsWith($full + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Системная папка запрещена'
        }
    }
    $sentinel = Join-Path $dir '.takt-install'
    $legacy = (Test-Path -LiteralPath (Join-Path $dir 'Takt.exe') -PathType Leaf) -and
              ([IO.Path]::GetFileName($dir) -ieq 'Takt')
    if (-not ((Test-Path -LiteralPath $sentinel -PathType Leaf) -or $legacy)) {
        throw 'Нет признаков установки Takt'
    }
    if (Test-Path -LiteralPath $sentinel -PathType Leaf) {
        try { $manifest = Get-Content -LiteralPath $sentinel -Raw | ConvertFrom-Json } catch { $manifest = $null }
        if ($null -ne $manifest -and $null -ne $manifest.files) {
            $managed = @($manifest.files)
            $ownedDirectories = @{}
            foreach ($relative in $managed) {
                if ($relative -isnot [string]) { throw 'Некорректный манифест установки' }
                $parts = $relative.Replace('\', '/').Split('/')
                for ($i = 1; $i -lt $parts.Length; $i++) {
                    $ownedDirectories[($parts[0..($i - 1)] -join '/')] = $true
                }
            }
            $pending = New-Object 'System.Collections.Generic.Stack[string]'
            $pending.Push($dir)
            while ($pending.Count -gt 0) {
                $current = $pending.Pop()
                foreach ($entry in Get-ChildItem -LiteralPath $current -Force) {
                    if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                        throw "Ссылка в установке: $($entry.FullName)"
                    }
                    $relative = $entry.FullName.Substring($dir.Length).TrimStart('\').Replace('\', '/')
                    if ($entry.PSIsContainer) {
                        if (-not $ownedDirectories.ContainsKey($relative)) {
                            throw "Пользовательский каталог: $relative"
                        }
                        $pending.Push($entry.FullName)
                    } elseif ($managed -notcontains $relative) {
                        throw "Пользовательский файл: $relative"
                    }
                }
            }
        } else {
            $allowed = @('Takt.exe', 'uninstall.exe', 'main.py', '_internal', 'takt',
                         'resources', 'assets', 'icon.png', 'icon.ico', '.takt-install')
            foreach ($entry in Get-ChildItem -LiteralPath $dir -Force) {
                if ($allowed -notcontains $entry.Name) { throw "Пользовательский файл: $($entry.Name)" }
                if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                    throw "Ссылка в установке: $($entry.FullName)"
                }
            }
        }
    }
    [IO.Directory]::Delete($dir, $true)
    if (Test-Path -LiteralPath $dir) { throw 'Каталог удаления остался' }
    $keyPath = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\Takt'
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($keyPath, $true)
    if ($null -ne $key) {
        $removeEntry = $false
        try {
            $display = $key.GetValue('DisplayName')
            $location = [string]$key.GetValue('InstallLocation')
            if ($display -eq 'Takt' -and $location) {
                $location = [IO.Path]::GetFullPath($location).TrimEnd('\')
                $removeEntry = ($location -eq $dir.TrimEnd('\'))
            }
        } finally { $key.Dispose() }
        if ($removeEntry) {
            [Microsoft.Win32.Registry]::CurrentUser.DeleteSubKey($keyPath, $false)
        }
    }
    Set-Content -LiteralPath (Join-Path $env:TAKT_UNINSTALL_STATUS_DIR 'status.txt') -Value 'OK'
    $code = 0
} catch {
    Set-Content -LiteralPath (Join-Path $env:TAKT_UNINSTALL_STATUS_DIR 'status.txt') -Value $_
    $code = 1
} finally {
    Remove-Item -LiteralPath $env:TAKT_UNINSTALL_STATUS_DIR -Recurse -Force -ErrorAction SilentlyContinue
}
exit $code
"""


def _schedule_deferred_uninstall(dest: Path) -> None:
    status_dir = Path(tempfile.mkdtemp(prefix=".takt-uninstall-", dir=dest.parent))
    env = os.environ.copy()
    env["TAKT_UNINSTALL_DIR"] = str(dest)
    env["TAKT_UNINSTALL_PID"] = str(os.getpid())
    env["TAKT_UNINSTALL_STATUS_DIR"] = str(status_dir)
    try:
        subprocess.Popen(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
             "-Command", _DEFERRED_UNINSTALL_SCRIPT],
            env=env, close_fds=True,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
    except OSError as ex:
        shutil.rmtree(status_dir, ignore_errors=True)
        raise UninstallError(f"Не удалось запустить отложенное удаление: {ex}") from ex


def uninstall(progress=None, *, close_timeout: float = 20.0) -> None:
    def say(msg):
        if progress:
            progress(msg)

    dest = installed_dir()
    say("Ожидание закрытия приложения…")
    if not wait_app_close(close_timeout):
        raise AppRunningError(
            "Takt не закрылся за отведённое время. Удаление отменено.")
    unmanaged = _unmanaged_files(dest)
    if unmanaged:
        shown = ", ".join(unmanaged[:5])
        raise UninstallError(
            "В каталоге есть пользовательские файлы; удаление отменено: " + shown)

    say("Удаление ярлыков…")
    start, desk = shortcut_paths()
    for lnk in (start, desk):
        remove_shortcut(lnk, dest / EXE_NAME)

    say("Удаление файлов…")
    try:
        current_exe = _resolved(sys.executable) if frozen() else None
    except UnsafeInstallPathError:
        current_exe = None
    if current_exe is not None and current_exe.parent == dest:
        _schedule_deferred_uninstall(dest)
        raise UninstallDeferredError(
            "Удаление начнётся после закрытия этого окна. Приложение ещё не удалено.")

    try:
        shutil.rmtree(dest)
    except OSError as ex:
        raise UninstallError(f"Не удалось удалить {dest}: {ex}") from ex
    if dest.exists() or dest.is_symlink():
        raise UninstallError(f"Каталог всё ещё существует: {dest}")
    unregister_uninstall(dest)
    say("Удалено")


def run_gui() -> int:
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title(f"Установка {APP_NAME} {VERSION}")
    root.geometry("460x260")
    root.resizable(False, False)
    exit_code = EXIT_ERROR

    frm = ttk.Frame(root, padding=16)
    frm.pack(fill="both", expand=True)
    ttk.Label(frm, text=f"{APP_NAME} {VERSION} — менеджер задач",
              font=("Segoe UI", 11, "bold")).pack(anchor="w")
    ttk.Label(frm, text="Каталог установки:").pack(anchor="w", pady=(12, 2))
    dir_var = tk.StringVar(value=str(default_dir()))
    ttk.Entry(frm, textvariable=dir_var).pack(fill="x")
    desktop_var = tk.BooleanVar(value=True)
    launch_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(frm, text="Ярлык на рабочем столе",
                    variable=desktop_var).pack(anchor="w", pady=(8, 0))
    ttk.Checkbutton(frm, text="Запустить после установки",
                    variable=launch_var).pack(anchor="w")
    status = ttk.Label(frm, text="")
    status.pack(anchor="w", pady=(8, 0))
    btn = ttk.Button(frm, text="Установить")
    btn.pack(anchor="e", pady=(8, 0))

    def do_install():
        btn.state(["disabled"])

        def progress(msg):
            status.config(text=msg)
            root.update_idletasks()

        def worker():
            nonlocal exit_code
            try:
                install(Path(dir_var.get()), desktop_var.get(), launch_var.get(), progress)
                exit_code = EXIT_OK
                status.config(text="Установка завершена")
                root.after(1200, root.destroy)
            except Exception as ex:
                exit_code = EXIT_APP_RUNNING if isinstance(ex, AppRunningError) else EXIT_ERROR
                status.config(text=f"Ошибка: {ex}")
                btn.state(["!disabled"])

        import threading
        threading.Thread(target=worker, daemon=True).start()

    btn.config(command=do_install)
    root.mainloop()
    return exit_code


def _run_uninstall_gui() -> int:
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.withdraw()
    try:
        if not messagebox.askyesno(f"Удаление {APP_NAME}",
                                   f"Удалить {APP_NAME} с этого компьютера?"):
            return EXIT_OK
        try:
            uninstall()
        except UninstallDeferredError as ex:
            messagebox.showwarning(APP_NAME, str(ex))
            return EXIT_DEFERRED
        except Exception as ex:
            messagebox.showerror(APP_NAME, f"Ошибка: {ex}")
            return EXIT_APP_RUNNING if isinstance(ex, AppRunningError) else EXIT_ERROR
        messagebox.showinfo(APP_NAME, "Приложение удалено")
        return EXIT_OK
    finally:
        root.destroy()


def main() -> int:
    args = sys.argv[1:]
    silent = "/S" in args or "/s" in args
    dir_arg = next((a[5:] for a in args if a.startswith("/DIR=")), None)
    try:
        if "/uninstall" in args:
            if silent:
                uninstall()
            else:
                return _run_uninstall_gui()
            return EXIT_OK
        dest = Path(dir_arg) if dir_arg else default_dir()
        if silent:
            install(dest, desktop=True, launch=False)
        else:
            return run_gui()
        return EXIT_OK
    except UninstallDeferredError as ex:
        print(ex, file=sys.stderr)
        return EXIT_DEFERRED
    except AppRunningError as ex:
        print(ex, file=sys.stderr)
        return EXIT_APP_RUNNING
    except Exception as ex:
        print(f"Ошибка: {ex}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())

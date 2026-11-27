from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import __version__

API = "https://api.github.com"
UPDATE_FORMAT = "takt-update-1"
UPDATE_PUBLIC_KEY_HEX = "0ed8d14055d57567054f3a4b89243ce9d5039636c7d13a6c4954591c8758727c"
MANIFEST_NAME = "update-manifest.json"
MANIFEST_SIG_NAME = "update-manifest.json.sig"
MAX_REDIRECTS = 5
MAX_MANIFEST_SIZE = 1024 * 1024
MAX_RELEASE_SIZE = 2 * 1024 * 1024
MAX_ASSET_SIZE = 512 * 1024 * 1024
MAX_ASSETS = 16
_DOWNLOAD_TIMEOUT = 60.0

_ASSET_EXTENSIONS = {
    "windows-setup": ".exe",
    "windows-portable": ".exe",
    "android-apk": ".apk",
    "android-code": ".zip",
}
_ASSET_NAME_PATTERNS = {
    "windows-setup": re.compile(r"takt-setup(?:[-_][0-9a-z._-]+)?\.exe", re.IGNORECASE),
    "windows-portable": re.compile(r"takt-portable(?:[-_][0-9a-z._-]+)?\.exe", re.IGNORECASE),
    "android-apk": re.compile(r"takt(?:[-_][0-9a-z._-]+)?\.apk", re.IGNORECASE),
    "android-code": re.compile(r"takt-code\.zip", re.IGNORECASE),
}
_SAFE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_SEMVER_IDENTIFIER = r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
_SEMVER_RE = re.compile(
    rf"[vV]?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    rf"(?:-({_SEMVER_IDENTIFIER}(?:\.{_SEMVER_IDENTIFIER})*))?"
    rf"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?\Z"
)


class UpdateError(Exception):
    pass


class UpdateCancelled(UpdateError):
    """Загрузка отменена пользователем."""


@dataclass
class UpdateInfo:
    version: str
    tag: str
    notes: str
    assets: list[dict]
    url: str


def parse_version(text: str) -> tuple:
    """Строгая SemVer: (major, minor, patch, stable, prerelease_order).

    Build metadata не влияет на порядок. Stable новее prerelease той же версии.
    """
    if not isinstance(text, str):
        raise ValueError("Версия должна быть строкой")
    match = _SEMVER_RE.fullmatch(text.strip())
    if not match:
        raise ValueError(f"Некорректная SemVer: {text!r}")
    major, minor, patch, prerelease, _build = match.groups()
    if not prerelease:
        precedence = 1
        identifiers: tuple = ()
    else:
        precedence = 0
        identifiers = tuple(
            (0, int(part)) if part.isdigit() else (1, part)
            for part in prerelease.split(".")
        )
    return int(major), int(minor), int(patch), precedence, identifiers


def current_version() -> str:
    return __version__


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def _validate_https_url(url: str) -> str:
    if not isinstance(url, str) or not url or any(ord(ch) <= 32 or ord(ch) == 127 for ch in url):
        raise UpdateError("Некорректный URL обновления")
    if "\\" in url:
        raise UpdateError("Некорректный URL обновления")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as ex:
        raise UpdateError("Некорректный URL обновления") from ex
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise UpdateError("Обновления разрешены только по HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise UpdateError("URL обновления не должен содержать учётные данные")
    if port is not None and not 1 <= port <= 65535:
        raise UpdateError("Некорректный порт URL обновления")
    return url


def _remaining_time(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise UpdateError("Превышено время ожидания обновления")
    return remaining


def _check_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise UpdateCancelled("Загрузка отменена")


def _https_get(url: str, *, stream: bool = False, timeout: float = 15.0,
               deadline: float | None = None, headers: dict | None = None):
    """GET с ручными редиректами: каждый адрес HTTPS, не более MAX_REDIRECTS."""
    current = _validate_https_url(url)
    if timeout <= 0:
        raise UpdateError("Тайм-аут обновления должен быть положительным")
    if deadline is None:
        deadline = time.monotonic() + timeout
    for redirects in range(MAX_REDIRECTS + 1):
        remaining = _remaining_time(deadline)
        request_headers = {"Accept-Encoding": "identity"}
        request_headers.update(headers or {})
        try:
            response = requests.get(
                current, headers=request_headers, stream=stream,
                timeout=min(remaining, timeout), allow_redirects=False,
            )
        except requests.RequestException as ex:
            raise UpdateError(f"Ошибка сети: {ex}") from ex
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location")
            response.close()
            if redirects >= MAX_REDIRECTS:
                raise UpdateError("Слишком много редиректов при обновлении")
            if not location:
                raise UpdateError("Редирект обновления не содержит Location")
            current = _validate_https_url(urljoin(current, location))
            continue
        return response, deadline
    raise UpdateError("Слишком много редиректов при обновлении")


def _content_length(response, *, required: bool = False) -> int | None:
    value = response.headers.get("Content-Length")
    if value is None:
        if required:
            raise UpdateError("Сервер не передал Content-Length")
        return None
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]+", text):
        raise UpdateError("Некорректный Content-Length")
    return int(text)


def _read_limited(response, limit: int, deadline: float,
                  cancel_event: threading.Event | None = None) -> bytes:
    declared = _content_length(response)
    if declared is not None and declared > limit:
        raise UpdateError("Ответ сервера обновления слишком велик")
    chunks = []
    done = 0
    try:
        for chunk in response.iter_content(chunk_size=64 * 1024):
            _remaining_time(deadline)
            _check_cancelled(cancel_event)
            if not chunk:
                continue
            done += len(chunk)
            if done > limit:
                raise UpdateError("Ответ сервера обновления слишком велик")
            chunks.append(chunk)
    except requests.RequestException as ex:
        raise UpdateError(f"Ошибка сети: {ex}") from ex
    if declared is not None and done != declared:
        raise UpdateError("Неполный ответ сервера обновления")
    return b"".join(chunks)


def _fetch_bytes(url: str, limit: int, *, timeout: float = 15.0) -> bytes:
    response, deadline = _https_get(url, stream=True, timeout=timeout)
    try:
        if response.status_code != 200:
            raise UpdateError(f"Сервер обновления ответил HTTP {response.status_code}")
        return _read_limited(response, limit, deadline)
    finally:
        response.close()


def canonical_manifest(manifest: dict) -> bytes:
    return json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _validate_asset_name(name: str, kind: str) -> None:
    if not isinstance(name, str) or not _SAFE_NAME_RE.fullmatch(name):
        raise UpdateError("Некорректное имя файла обновления")
    if name.endswith((".", " ")) or name.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        raise UpdateError("Небезопасное имя файла обновления")
    pattern = _ASSET_NAME_PATTERNS.get(kind)
    if pattern is None or not pattern.fullmatch(name):
        raise UpdateError(f"Имя файла не соответствует kind={kind}")
    if Path(name).suffix.lower() != _ASSET_EXTENSIONS[kind]:
        raise UpdateError(f"Некорректное расширение файла для kind={kind}")


def validate_manifest(manifest: dict) -> dict:
    if not isinstance(manifest, dict):
        raise UpdateError("Manifest обновления должен быть объектом JSON")
    if set(manifest) != {"format", "version", "channel", "assets"}:
        raise UpdateError("Некорректная схема manifest обновления")
    if manifest["format"] != UPDATE_FORMAT:
        raise UpdateError("Неподдерживаемый формат manifest обновления")
    version = manifest["version"]
    if not isinstance(version, str) or version != version.strip() or version[:1] in ("v", "V"):
        raise UpdateError("Некорректная версия в manifest")
    try:
        parse_version(version)
    except ValueError as ex:
        raise UpdateError(str(ex)) from ex
    channel = manifest["channel"]
    if not isinstance(channel, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", channel):
        raise UpdateError("Некорректный канал в manifest")
    assets = manifest["assets"]
    if not isinstance(assets, list) or not 1 <= len(assets) <= MAX_ASSETS:
        raise UpdateError("Некорректный список файлов в manifest")
    names = set()
    required = {"name", "url", "size", "sha256", "kind"}
    for asset in assets:
        if not isinstance(asset, dict) or set(asset) != required:
            raise UpdateError("Некорректная схема файла в manifest")
        kind = asset["kind"]
        if not isinstance(kind, str) or kind not in _ASSET_EXTENSIONS:
            raise UpdateError("Недопустимый kind файла обновления")
        _validate_asset_name(asset["name"], kind)
        folded_name = asset["name"].casefold()
        if folded_name in names:
            raise UpdateError("В manifest есть повторяющиеся имена файлов")
        names.add(folded_name)
        _validate_https_url(asset["url"])
        size = asset["size"]
        if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= MAX_ASSET_SIZE:
            raise UpdateError("Некорректный или слишком большой размер файла обновления")
        digest = asset["sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise UpdateError("Некорректный SHA-256 файла обновления")
    return manifest


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Повторяющийся ключ JSON")
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValueError(f"Недопустимое значение JSON: {value}")


def verify_manifest(manifest_bytes: bytes, signature: bytes) -> dict:
    if len(signature) != 64:
        raise UpdateError("Некорректная подпись manifest")
    try:
        manifest = json.loads(
            manifest_bytes.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
        canonical = canonical_manifest(manifest)
    except (UnicodeDecodeError, ValueError, TypeError) as ex:
        raise UpdateError("Некорректный JSON manifest обновления") from ex
    try:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(UPDATE_PUBLIC_KEY_HEX))
        public_key.verify(signature, canonical)
    except (InvalidSignature, ValueError, TypeError) as ex:
        raise UpdateError("Подпись manifest недействительна") from ex
    return validate_manifest(manifest)


def _release_file(assets: list, name: str, max_size: int) -> dict:
    matches = [
        asset for asset in assets
        if isinstance(asset, dict) and asset.get("name") == name
    ]
    if len(matches) != 1:
        raise UpdateError(f"Релиз должен содержать ровно один файл {name}")
    asset = matches[0]
    _validate_https_url(asset.get("browser_download_url"))
    declared_size = asset.get("size")
    if declared_size is not None:
        if (isinstance(declared_size, bool) or not isinstance(declared_size, int)
                or not 1 <= declared_size <= max_size):
            raise UpdateError(f"Некорректный размер файла {name}")
    return asset


def check_update(repo: str, current: str | None = None) -> UpdateInfo | None:
    """repo: owner/name (GitHub) или HTTPS URL сервера с /releases/latest.

    Принимается только подписанный manifest takt-update-1. None — если версия
    релиза не новее установленной.
    """
    repo = repo.strip()
    if repo.lower().startswith(("http://", "https://")):
        _validate_https_url(repo)
        url = repo.rstrip("/") + "/releases/latest"
    else:
        repo = repo.strip("/")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise UpdateError("Репозиторий должен быть в формате owner/name или HTTPS URL")
        url = f"{API}/repos/{repo}/releases/latest"
    try:
        installed = parse_version(current or current_version())
    except ValueError as ex:
        raise UpdateError(str(ex)) from ex

    response, deadline = _https_get(
        url, stream=True, timeout=15.0,
        headers={"Accept": "application/vnd.github+json"},
    )
    try:
        if response.status_code == 404:
            raise UpdateError("Репозиторий или релизы не найдены. "
                              "Создайте релиз на GitHub после публикации репозитория.")
        if response.status_code != 200:
            raise UpdateError(f"GitHub ответил HTTP {response.status_code}")
        raw_release = _read_limited(response, MAX_RELEASE_SIZE, deadline)
    finally:
        response.close()
    try:
        data = json.loads(raw_release.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as ex:
        raise UpdateError("Сервер вернул некорректный JSON релиза") from ex
    if not isinstance(data, dict):
        raise UpdateError("Некорректное описание релиза")
    tag = data.get("tag_name")
    if not isinstance(tag, str):
        raise UpdateError("У релиза отсутствует корректный tag_name")
    try:
        release_version = parse_version(tag)
    except ValueError as ex:
        raise UpdateError(str(ex)) from ex
    if release_version <= installed:
        return None

    release_assets = data.get("assets")
    if not isinstance(release_assets, list):
        raise UpdateError("У релиза отсутствует список файлов")
    manifest_asset = _release_file(release_assets, MANIFEST_NAME, MAX_MANIFEST_SIZE)
    signature_asset = _release_file(release_assets, MANIFEST_SIG_NAME, 64)
    manifest_bytes = _fetch_bytes(manifest_asset["browser_download_url"], MAX_MANIFEST_SIZE)
    signature = _fetch_bytes(signature_asset["browser_download_url"], 64)
    manifest = verify_manifest(manifest_bytes, signature)
    normalized_tag = tag.strip()[1:] if tag.strip()[:1] in ("v", "V") else tag.strip()
    if manifest["version"] != normalized_tag:
        raise UpdateError("Версия manifest не совпадает с tag_name релиза")
    notes = data.get("body") or ""
    if not isinstance(notes, str):
        notes = ""
    return UpdateInfo(
        version=manifest["version"], tag=tag, notes=notes[:2000],
        assets=manifest["assets"], url=data.get("html_url", "") or "",
    )


def choose_asset(assets: list[dict]) -> dict | None:
    """Только проверенные Windows EXE; Setup приоритетнее Portable."""
    candidates = []
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        kind = asset.get("kind")
        name = asset.get("name")
        if kind not in ("windows-setup", "windows-portable") or not isinstance(name, str):
            continue
        try:
            _validate_asset_name(name, kind)
        except UpdateError:
            continue
        candidates.append(asset)
    for kind in ("windows-setup", "windows-portable"):
        for asset in candidates:
            if asset["kind"] == kind:
                return asset
    return None


def download_asset(url: str, dest: Path, progress=None, cancel_event: threading.Event | None = None,
                   *, expected_size: int, expected_sha256: str, timeout: float = _DOWNLOAD_TIMEOUT,
                   deadline: float | None = None) -> Path:
    """Потоковая загрузка с проверкой размера/hash и атомарной заменой dest."""
    _validate_https_url(url)
    _check_cancelled(cancel_event)
    if (isinstance(expected_size, bool) or not isinstance(expected_size, int)
            or not 1 <= expected_size <= MAX_ASSET_SIZE):
        raise UpdateError("Некорректный ожидаемый размер файла обновления")
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise UpdateError("Некорректный ожидаемый SHA-256 файла обновления")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".part", dir=dest.parent)
    temp_path = Path(temp_name)
    hasher = hashlib.sha256()
    done = 0
    last_progress = 0.0
    try:
        response, request_deadline = _https_get(
            url, stream=True, timeout=timeout, deadline=deadline,
            headers={"Accept": "application/octet-stream"},
        )
        with response:
            if response.status_code != 200:
                raise UpdateError(f"Сервер обновления ответил HTTP {response.status_code}")
            declared = _content_length(response, required=True)
            if declared > MAX_ASSET_SIZE:
                raise UpdateError("Файл обновления слишком велик")
            if declared != expected_size:
                raise UpdateError("Content-Length не совпадает с размером из manifest")
            with os.fdopen(fd, "wb") as output:
                fd = None
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    _remaining_time(request_deadline)
                    _check_cancelled(cancel_event)
                    if not chunk:
                        continue
                    done += len(chunk)
                    if done > expected_size or done > MAX_ASSET_SIZE:
                        raise UpdateError("Скачанный файл обновления слишком велик")
                    output.write(chunk)
                    hasher.update(chunk)
                    fraction = min(done / expected_size, 1.0)
                    if progress is not None and fraction > last_progress:
                        last_progress = fraction
                        progress(fraction)
                output.flush()
                os.fsync(output.fileno())
        _check_cancelled(cancel_event)
        if done != expected_size:
            raise UpdateError("Размер скачанного файла не совпадает с manifest")
        if not hmac.compare_digest(hasher.hexdigest(), expected_sha256):
            raise UpdateError("SHA-256 скачанного файла не совпадает с manifest")
        _check_cancelled(cancel_event)
        os.replace(temp_path, dest)
        return dest
    except UpdateError:
        raise
    except requests.RequestException as ex:
        raise UpdateError(f"Ошибка сети: {ex}") from ex
    finally:
        if fd is not None:
            os.close(fd)
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def launch_update(asset_path: Path, *, kind: str | None = None,
                  app_dir: Path | None = None) -> None:
    """Запускает только ожидаемый Setup EXE; Portable не запускается вслепую."""
    path = Path(asset_path)
    if path.suffix.lower() == ".zip":
        raise UpdateError("ZIP-архивы нельзя запускать как обновление Windows")
    if path.is_symlink() or not path.is_file():
        raise UpdateError("Файл обновления не найден или небезопасен")
    if kind is None:
        kind = next(
            (candidate for candidate, pattern in _ASSET_NAME_PATTERNS.items()
             if candidate.startswith("windows-") and pattern.fullmatch(path.name)),
            None,
        )
    try:
        _validate_asset_name(path.name, kind or "")
    except UpdateError as ex:
        raise UpdateError("Недопустимое имя исполняемого файла обновления") from ex
    if kind not in ("windows-setup", "windows-portable"):
        raise UpdateError("Ожидался проверенный Setup или Portable EXE")
    _validate_asset_name(path.name, kind)
    path = path.resolve()
    if kind == "windows-portable":
        raise UpdateError("Для обновления Portable нужен Takt-Setup.exe")
    if app_dir is None:
        app_dir = Path(sys.executable).parent if is_frozen() else Path(sys.argv[0]).resolve().parent
    app_dir = Path(app_dir).resolve()
    if not app_dir.is_dir():
        raise UpdateError("Не найден каталог текущего приложения")
    args = [str(path), "/S", f"/DIR={app_dir}"]
    subprocess.Popen(args, cwd=str(path.parent), close_fds=True)


def is_android() -> bool:
    return bool(os.environ.get("ANDROID_ROOT")) or sys.platform == "android"


def android_base() -> Path:
    """Каталог serious_python: <filesDir>/flet (рядом с <filesDir>/data)."""
    data = os.environ.get("FLET_APP_STORAGE_DATA")
    if not data:
        raise UpdateError("Не найдено хранилище приложения Android")
    return Path(data).parent / "flet"


def installed_apk_version() -> str | None:
    try:
        key = (android_base() / ".key").read_text(encoding="utf-8")
    except (OSError, UpdateError):
        return None
    if not key.startswith("app:"):
        return None
    return key[4:].split("+", 1)[0] or None


def _stamp_from_text(text: str) -> str:
    try:
        return json.loads(text).get("native", "")
    except ValueError:
        return ""


def native_stamp() -> str:
    """Отпечаток нативной оболочки (requirements, flet, иконки, манифест)."""
    try:
        return _stamp_from_text(
            (Path(__file__).parent / "native_stamp.json").read_text(encoding="utf-8"))
    except OSError:
        return ""


def zip_native_stamp(zip_path: Path) -> str:
    with zipfile.ZipFile(zip_path) as z:
        return _stamp_from_text(z.read("takt/native_stamp.json").decode("utf-8"))


def android_assets(assets: list[dict]) -> tuple[dict | None, dict | None]:
    code = None
    apk = None
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        kind = asset.get("kind")
        name = asset.get("name")
        if not isinstance(name, str):
            continue
        try:
            _validate_asset_name(name, kind or "")
        except UpdateError:
            continue
        if kind == "android-code" and code is None:
            code = asset
        elif kind == "android-apk" and apk is None:
            apk = asset
    return code, apk


def apply_code_update(zip_path: Path) -> None:
    """Заменяет распакованный код приложения; .key не трогает, чтобы
    serious_python не перетёр обновление кодом из APK при следующем запуске."""
    base = android_base()
    app_dir = base / "app"
    staging = base / "app.new"
    backup = base / "app.old"
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        if not {"main.pyc", "takt/ui/app.pyc", "takt/native_stamp.json"} <= set(names):
            raise UpdateError("Архив обновления повреждён")
        if any(n.startswith(("/", "..")) or ".." in n.split("/") for n in names):
            raise UpdateError("Архив обновления повреждён")
        if staging.exists():
            shutil.rmtree(staging)
        z.extractall(staging)
    if backup.exists():
        shutil.rmtree(backup)
    app_dir.rename(backup)
    try:
        staging.rename(app_dir)
    except OSError:
        backup.rename(app_dir)
        raise UpdateError("Не удалось применить обновление")
    shutil.rmtree(backup, ignore_errors=True)


def _android_activity():
    from jnius import autoclass
    return autoclass("com.flet.serious_python_android.PythonActivity").mActivity


def ensure_notification_permission() -> None:
    """Однократный запрос POST_NOTIFICATIONS (Android 13+) для перезапуска после OTA."""
    if not is_android():
        return
    try:
        from jnius import autoclass, cast
        if autoclass("android.os.Build$VERSION").SDK_INT < 33:
            return
        activity = _android_activity()
        if activity is None:
            return
        ctx = activity.getApplicationContext()
        nm = cast("android.app.NotificationManager",
                  ctx.getSystemService(ctx.NOTIFICATION_SERVICE))
        if nm.areNotificationsEnabled():
            return
        activity.requestPermissions(
            ["android.permission.POST_NOTIFICATIONS"], 1001)
    except Exception:
        pass


def _post_restart_notification(ctx, pi) -> None:
    from jnius import autoclass, cast
    nm = cast("android.app.NotificationManager",
              ctx.getSystemService(ctx.NOTIFICATION_SERVICE))
    channel_id = "takt_updates"
    channel = autoclass("android.app.NotificationChannel")(
        channel_id, "Обновления",
        autoclass("android.app.NotificationManager").IMPORTANCE_HIGH)
    nm.createNotificationChannel(channel)
    builder = autoclass("android.app.Notification$Builder")(ctx, channel_id)
    builder.setContentTitle("Takt обновлён")
    builder.setContentText("Нажмите, чтобы перезапустить приложение")
    builder.setSmallIcon(ctx.getApplicationInfo().icon)
    builder.setContentIntent(pi)
    builder.setAutoCancel(True)
    builder.setPriority(1)
    nm.notify(1001, builder.build())


def android_restart_mode() -> str:
    """Способ перезапуска после OTA: auto (<14), notification (14+ и уведомления
    разрешены), manual (14+ без уведомлений — пользователь открывает приложение сам)."""
    try:
        from jnius import autoclass, cast
        sdk = autoclass("android.os.Build$VERSION").SDK_INT
        if sdk < 34:
            return "auto"
        activity = _android_activity()
        ctx = activity.getApplicationContext()
        nm = cast("android.app.NotificationManager",
                  ctx.getSystemService(ctx.NOTIFICATION_SERVICE))
        return "notification" if nm.areNotificationsEnabled() else "manual"
    except Exception:
        return "manual"


def restart_android() -> None:
    """Перезапуск после OTA: будильник (Android <14) либо уведомление (Android 14+).

    На Android 14+ запуск activity из будильника блокируется системой (BAL),
    поэтому публикуем уведомление: тап по нему отправляет PendingIntent от имени
    системы, что разрешает запуск нового процесса с обновлённым кодом.
    """
    try:
        from jnius import autoclass, cast
    except ImportError:
        raise UpdateError("Обновление применено. Перезапустите приложение вручную.")
    activity = _android_activity()
    ctx = activity.getApplicationContext()
    intent = ctx.getPackageManager().getLaunchIntentForPackage(ctx.getPackageName())
    # FLAG_ACTIVITY_NEW_TASK | FLAG_ACTIVITY_CLEAR_TASK
    intent.addFlags(0x10000000 | 0x00008000)
    pending = autoclass("android.app.PendingIntent")
    # FLAG_IMMUTABLE | FLAG_UPDATE_CURRENT
    flags = 0x4000000 | 0x10000000
    sdk = autoclass("android.os.Build$VERSION").SDK_INT
    if sdk >= 35:
        opts = autoclass("android.app.ActivityOptions").makeBasic()
        opts.setPendingIntentCreatorBackgroundActivityStartMode(1)
        pi = pending.getActivity(ctx, 0, intent, flags, opts.toBundle())
    else:
        pi = pending.getActivity(ctx, 0, intent, flags)
    if android_restart_mode() == "notification":
        try:
            _post_restart_notification(ctx, pi)
        except Exception:
            pass
    am = cast("android.app.AlarmManager",
              ctx.getSystemService(ctx.ALARM_SERVICE))
    system = autoclass("java.lang.System")
    am.set(autoclass("android.app.AlarmManager").RTC,
           system.currentTimeMillis() + 400, pi)
    system.exit(0)


def install_apk_android(apk_path: Path) -> None:
    """Системный установщик через FileProvider из шаблона Flet."""
    from jnius import autoclass
    ctx = autoclass("android.app.ActivityThread") \
        .currentApplication().getApplicationContext()
    uri = autoclass("androidx.core.content.FileProvider").getUriForFile(
        ctx, ctx.getPackageName() + ".provider",
        autoclass("java.io.File")(str(apk_path)))
    intent = autoclass("android.content.Intent")(
        autoclass("android.content.Intent").ACTION_VIEW)
    intent.setDataAndType(uri, "application/vnd.android.package-archive")
    # FLAG_ACTIVITY_NEW_TASK | FLAG_ACTIVITY_GRANT_READ_URI_PERMISSION
    intent.setFlags(0x10000000 | 0x1)
    ctx.startActivity(intent)

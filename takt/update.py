from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import requests

from . import __version__

API = "https://api.github.com"


class UpdateError(Exception):
    pass


@dataclass
class UpdateInfo:
    version: str
    tag: str
    notes: str
    assets: list[dict]
    url: str


def parse_version(text: str) -> tuple:
    text = text.strip().lstrip("vV")
    parts = []
    for chunk in text.split(".")[:4]:
        num = ""
        for ch in chunk:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num or 0))
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def current_version() -> str:
    return __version__


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def check_update(repo: str, current: str | None = None) -> UpdateInfo | None:
    """repo: owner/name (GitHub) или URL сервера обновлений с /releases/latest.
    None, если обновлений нет."""
    repo = repo.strip()
    if repo.startswith(("http://", "https://")):
        url = repo.rstrip("/") + "/releases/latest"
    else:
        repo = repo.strip("/")
        if repo.count("/") != 1:
            raise UpdateError("Репозиторий должен быть в формате owner/name")
        url = f"{API}/repos/{repo}/releases/latest"
    resp = requests.get(
        url, headers={"Accept": "application/vnd.github+json"}, timeout=15)
    if resp.status_code == 404:
        raise UpdateError("Репозиторий или релизы не найдены. "
                          "Создайте релиз на GitHub после публикации репозитория.")
    if resp.status_code != 200:
        raise UpdateError(f"GitHub ответил HTTP {resp.status_code}")
    data = resp.json()
    tag = data.get("tag_name", "")
    if parse_version(tag) <= parse_version(current or current_version()):
        return None
    return UpdateInfo(
        version=tag.lstrip("vV"), tag=tag,
        notes=(data.get("body") or "")[:2000],
        assets=[{"name": a["name"], "url": a["browser_download_url"],
                 "size": a.get("size", 0)} for a in data.get("assets", [])],
        url=data.get("html_url", ""))


def choose_asset(assets: list[dict]) -> dict | None:
    exe = [a for a in assets if a["name"].lower().endswith(".exe")]
    for a in exe:
        if "setup" in a["name"].lower():
            return a
    zips = [a for a in assets if a["name"].lower().endswith(".zip")]
    if zips:
        return zips[0]
    return exe[0] if exe else None


def download_asset(url: str, dest: Path, progress=None) -> Path:
    with requests.get(url, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length", 0))
        done = 0
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=256 * 1024):
                f.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(done / total)
    return dest


def launch_update(asset_path: Path) -> None:
    """Установщик запускаем и выходим; portable/zip — запускаем новую копию."""
    path = Path(asset_path)
    if path.suffix.lower() == ".zip":
        target = Path(tempfile.mkdtemp(prefix="takt-update-"))
        with zipfile.ZipFile(path) as z:
            z.extractall(target)
        exes = list(target.rglob("*.exe"))
        if not exes:
            raise UpdateError("В архиве обновления не найден .exe")
        path = exes[0]
    subprocess.Popen([str(path)], close_fds=True)


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
    code = next((a for a in assets if a["name"] == "Takt-Code.zip"), None)
    apk = next((a for a in assets if a["name"].lower().endswith(".apk")), None)
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
    # FLAG_ACTIVITY_NEW_TASK | FLAG_GRANT_READ_URI_PERMISSION
    intent.setFlags(0x10000000 | 0x1)
    ctx.startActivity(intent)
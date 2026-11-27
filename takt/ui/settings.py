from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import flet as ft

from .. import __version__
from ..sync import YandexDiskProvider, SyncEngine
from .editor import open_category_manager
from .helpers import dialog_height, dialog_width
from ..transfer import (
    TransferServer, qr_png_base64, fetch_payload, load_payload_file,
    build_payload, save_payload_file, diff_payload, apply_payload,
)
from ..update import (
    check_update, choose_asset, download_asset, launch_update, UpdateError,
    UpdateCancelled, is_frozen, is_android, android_assets, zip_native_stamp,
    native_stamp, apply_code_update, restart_android, install_apk_android,
    android_restart_mode,
)

DEFAULT_REPO = "SkyGeneral1996/Takt"
ANDROID_APK_NAME = "takt-update.apk"


def _run_ui(page, callback):
    """Выполняет изменение контролов в UI-потоке, включая вызовы из worker."""
    async def apply():
        callback()
    return page.run_task(apply)


def _dismiss_dialog(page, dialog) -> None:
    """Закрывает только свой диалог, не затрагивая более новый dialog stack."""
    if getattr(dialog, "open", True):
        dialog.open = False
        page.update()


async def _destroy_window(page) -> None:
    await page.window.destroy()


def open_settings(app) -> None:
    store = app.store
    page = app.page

    def mode_text() -> str:
        if store.get_meta("yandex_token"):
            return "Режим: синхронизация через Яндекс.Диск"
        return "Режим: локальный (без синхронизации). Данные хранятся только на этом устройстве."

    mode_t = ft.Text(mode_text(), size=12, weight=ft.FontWeight.BOLD)
    device_t = ft.Text(f"Устройство: {store.device_id}", size=12, color=ft.Colors.GREY)
    last = store.get_meta("last_sync")
    last_t = ft.Text(f"Последняя синхронизация: {last or 'ещё не выполнялась'}",
                     size=12, color=ft.Colors.GREY)

    token_f = ft.TextField(
        label="OAuth-токен Яндекс.Диска", password=True, can_reveal_password=True,
        value=store.get_meta("yandex_token", ""))
    folder_f = ft.TextField(label="Папка на Диске",
                            value=store.get_meta("sync_folder", "Takt"))
    sync_status = ft.Text("", size=12)

    repo_f = ft.TextField(label="GitHub-репозиторий (owner/name)",
                          value=store.get_meta("github_repo", DEFAULT_REPO),
                          hint_text="owner/name или URL сервера обновлений")
    auto_cb = ft.Checkbox(value=store.get_meta("auto_update") == "1")
    auto_row = ft.Row([auto_cb, ft.Text("Проверять обновления при запуске",
                                        size=13, expand=True)], spacing=4)
    upd_status = ft.Text("", size=12)

    def save_settings(e=None):
        store.set_meta("yandex_token", (token_f.value or "").strip())
        store.set_meta("sync_folder", (folder_f.value or "Takt").strip())
        store.set_meta("github_repo", (repo_f.value or "").strip())
        store.set_meta("auto_update", "1" if auto_cb.value else "0")
        mode_t.value = mode_text()
        page.update()

    def do_sync(e):
        save_settings()
        token = store.get_meta("yandex_token", "")
        if not token:
            sync_status.value = "Укажите OAuth-токен (как получить — в README.md)"
            page.update()
            return
        sync_status.value = "Синхронизация…"
        page.update()

        def worker():
            try:
                provider = YandexDiskProvider(token)
                engine = SyncEngine(store, provider, store.get_meta("sync_folder", "Takt"))
                result = engine.sync()
            except Exception as ex:
                message = f"Ошибка: {ex}"
                last_value = None
            else:
                message = (f"Готово: отправлено {result.pushed}, "
                           f"получено {result.applied} изменений")
                last_value = f"Последняя синхронизация: {result.at}"

            def publish():
                sync_status.value = message
                if last_value:
                    last_t.value = last_value
                app.refresh()
                page.update()
            _run_ui(page, publish)

        threading.Thread(target=worker, daemon=True).start()

    def do_check_updates(e):
        save_settings()
        repo = store.get_meta("github_repo", DEFAULT_REPO)
        if not repo:
            upd_status.value = "Укажите репозиторий после его создания на GitHub"
            page.update()
            return
        upd_status.value = "Проверка обновлений…"
        page.update()

        def publish_result(message: str, info=None):
            def apply():
                upd_status.value = message
                page.update()
                if info is not None:
                    _show_update_dialog(app, info)
            _run_ui(page, apply)

        def worker():
            try:
                info = check_update(repo)
                if info is None:
                    publish_result(f"Установлена последняя версия ({__version__})")
                else:
                    publish_result("Найдено обновление", info)
            except Exception as ex:
                publish_result(str(ex) if isinstance(ex, UpdateError) else f"Ошибка: {ex}")

        threading.Thread(target=worker, daemon=True).start()

    backup_picker = ft.FilePicker()
    page.services.append(backup_picker)
    backup_status = ft.Text("", size=12)

    def cleanup_picker():
        if backup_picker in page.services:
            page.services.remove(backup_picker)

    def close_settings(e=None):
        cleanup_picker()
        page.pop_dialog()

    def publish_backup(message: str):
        def apply():
            backup_status.value = message
            page.update()
        _run_ui(page, apply)

    async def do_save_backup(e):
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
        path = await backup_picker.save_file(
            dialog_title="Сохранить резервную копию Takt",
            file_name=f"takt-backup-{stamp}.json",
            allowed_extensions=["json"])
        if not path:
            return
        backup_status.value = "Создание резервной копии…"
        page.update()

        def worker():
            try:
                save_payload_file(path, build_payload(store))
                publish_backup("Резервная копия сохранена")
            except Exception as ex:
                publish_backup(f"Ошибка: {ex}")

        threading.Thread(target=worker, daemon=True).start()

    async def do_restore_backup(e):
        files = await backup_picker.pick_files(
            dialog_title="Выберите резервную копию Takt",
            allowed_extensions=["json"])
        if not files:
            return
        try:
            payload = load_payload_file(files[0].path)
        except Exception as ex:
            backup_status.value = f"Ошибка: {ex}"
            page.update()
            return
        close_settings()
        _show_diff(app, payload, title="Восстановление резервной копии",
                   apply_label="Восстановить")

    dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Настройки"),
        content=ft.Container(
            width=dialog_width(page, 460),
            height=dialog_height(page, 620),
            content=ft.Column([
                mode_t, device_t, last_t,
                ft.OutlinedButton("Категории", icon=ft.Icons.CATEGORY,
                                  on_click=lambda e: open_category_manager(app)),
                ft.Divider(),
                ft.Text("Яндекс.Диск", weight=ft.FontWeight.BOLD),
                token_f, folder_f, sync_status,
                ft.Row([
                    ft.FilledButton("Синхронизировать", icon=ft.Icons.SYNC,
                                    on_click=do_sync),
                ]),
                ft.Divider(),
                ft.Text("Перенос данных между устройствами (без облака)",
                        weight=ft.FontWeight.BOLD),
                ft.Text("По QR-коду через локальную сеть (Wi-Fi). Перед применением "
                        "показывается разница данных.", size=12, color=ft.Colors.GREY),
                ft.Row([
                    ft.OutlinedButton("Передать (QR)", icon=ft.Icons.QR_CODE,
                                      on_click=lambda e: open_send(app)),
                    ft.OutlinedButton("Принять", icon=ft.Icons.DOWNLOAD,
                                      on_click=lambda e: open_receive(app)),
                ], wrap=True, spacing=8),
                ft.Divider(),
                ft.Text("Резервная копия", weight=ft.FontWeight.BOLD),
                ft.Text("Сохраняет задачи, списки, привычки, заметки и историю "
                        "выполнений в JSON-файл. Восстановление объединяет данные "
                        "и сохраняет более новые правки.", size=12, color=ft.Colors.GREY),
                ft.Row([
                    ft.OutlinedButton("Скачать копию", icon=ft.Icons.BACKUP,
                                      on_click=do_save_backup),
                    ft.OutlinedButton("Восстановить", icon=ft.Icons.RESTORE,
                                      on_click=do_restore_backup),
                ], wrap=True, spacing=8),
                backup_status,
                ft.Divider(),
                ft.Text("Обновления", weight=ft.FontWeight.BOLD),
                ft.Text(f"Версия: {__version__}", size=12, color=ft.Colors.GREY),
                repo_f, auto_row, upd_status,
                ft.Row([
                    ft.OutlinedButton("Проверить обновления", icon=ft.Icons.SYSTEM_UPDATE,
                                      on_click=do_check_updates),
                ]),
            ], scroll=ft.ScrollMode.AUTO, tight=True, spacing=10, expand=True),
        ),
        actions=[
            ft.TextButton("Закрыть", on_click=close_settings),
            ft.FilledButton("Сохранить", on_click=save_settings),
        ],
        on_dismiss=lambda e: cleanup_picker(),
    )
    page.show_dialog(dialog)


def open_send(app, payload: dict | None = None, title: str = "Передать данные") -> None:
    page = app.page
    server = TransferServer(app.store, payload=payload)
    server.start()
    state = {"closed": False}

    status_t = ft.Text("Ожидание получения… (сервер активен до 5 минут)", size=12)

    def close(e=None):
        state["closed"] = True
        server.stop()
        page.pop_dialog()

    def watcher():
        while not server.received.is_set() and not state["closed"]:
            server.received.wait(0.5)
        if state["closed"]:
            return
        status_t.value = "Данные получены вторым устройством!"
        page.update()
        time.sleep(1.5)
        if not state["closed"]:
            server.stop()
            page.pop_dialog()
            app.notify("Перенос завершён")

    threading.Thread(target=watcher, daemon=True).start()

    dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text(title),
        content=ft.Container(
            width=dialog_width(page, 340),
            content=ft.Column([
                ft.Text("Отсканируйте QR камерой телефона (оба устройства в одной "
                        "Wi-Fi сети), скачайте файл и примите его в приложении: "
                        "Настройки → Принять → «Из файла». Либо вставьте ссылку "
                        "в поле «По ссылке».", size=12),
                ft.Image(src=qr_png_base64(server.url), width=240, height=240),
                ft.Text(server.url, size=11, selectable=True, color=ft.Colors.GREY),
                status_t,
            ], tight=True, spacing=10,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER),
        ),
        actions=[ft.TextButton("Отмена", on_click=close)],
    )
    page.show_dialog(dialog)


def open_receive(app) -> None:
    page = app.page
    store = app.store
    url_f = ft.TextField(label="Ссылка из QR-кода", hint_text="http://192.168.…/t/…")
    error_t = ft.Text("", size=12, color=ft.Colors.RED)
    picker = ft.FilePicker()
    page.services.append(picker)

    def close(e=None):
        if picker in page.services:
            page.services.remove(picker)
        page.pop_dialog()

    def got_payload(payload):
        close()
        _show_diff(app, payload)

    def by_url(e):
        url = (url_f.value or "").strip()
        if not url:
            error_t.value = "Вставьте ссылку из QR-кода"
            page.update()
            return
        error_t.value = "Получение данных…"
        page.update()

        def worker():
            try:
                payload = fetch_payload(url)
                got_payload(payload)
            except Exception as ex:
                error_t.value = f"Не удалось получить данные: {ex}"
                page.update()

        threading.Thread(target=worker, daemon=True).start()

    async def by_file(e):
        files = await picker.pick_files(
            dialog_title="Файл переноса Takt", allowed_extensions=["json"])
        if not files:
            return
        try:
            payload = load_payload_file(files[0].path)
            got_payload(payload)
        except Exception as ex:
            error_t.value = str(ex)
            page.update()

    dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Принять данные"),
        content=ft.Container(
            width=dialog_width(page, 420),
            content=ft.Column([
                ft.Text("Данные с другого устройства: по ссылке из QR-кода "
                        "(устройства в одной сети) или из файла, скачанного "
                        "через браузер после сканирования QR.", size=12),
                url_f,
                ft.Row([
                    ft.FilledButton("Получить по ссылке", icon=ft.Icons.LINK,
                                    on_click=by_url),
                    ft.OutlinedButton("Из файла", icon=ft.Icons.FILE_OPEN,
                                      on_click=by_file),
                ]),
                error_t,
            ], tight=True, spacing=10),
        ),
        actions=[ft.TextButton("Отмена", on_click=close)],
    )
    page.show_dialog(dialog)


def _show_diff(app, payload: dict, *, title: str = "Разница данных",
               apply_label: str = "Применить") -> None:
    page = app.page
    d = diff_payload(app.store, payload)
    samples = ft.Column(
        [ft.Text(s, size=12) for s in d.samples], tight=True, spacing=2)
    note = ft.Text("", size=12, color=ft.Colors.GREY)
    if d.older:
        note.value = (f"Пропущено более старых версий: {d.older} — "
                      f"ваши более новые правки сохранятся.")

    def apply(e):
        n = apply_payload(app.store, payload)
        page.pop_dialog()
        app.refresh()
        app.notify(f"Перенос применён: {n} изменений")

    dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text(title),
        content=ft.Container(
            width=dialog_width(page, 420),
            height=dialog_height(page, 480),
            content=ft.Column([
                ft.Text(d.describe(), weight=ft.FontWeight.BOLD),
                samples, note,
            ], tight=True, spacing=8, scroll=ft.ScrollMode.AUTO, expand=True),
        ),
        actions=[
            ft.TextButton("Отмена", on_click=lambda e: page.pop_dialog()),
            ft.FilledButton(apply_label, on_click=apply),
        ],
    )
    page.show_dialog(dialog)


def _update_android(info, status_t, page, cancel_event, publish, tmp: Path) -> None:
    code, apk = android_assets(info.assets)
    last_progress = 0.0

    def say(text: str):
        def apply():
            status_t.value = text
            page.update()
        publish(apply)

    def ensure_active():
        if cancel_event.is_set():
            raise UpdateCancelled("Загрузка отменена")

    def prog(frac):
        nonlocal last_progress
        fraction = min(max(float(frac), 0.0), 1.0)
        if fraction <= last_progress:
            return
        last_progress = fraction

        def apply():
            status_t.value = f"Загрузка… {int(fraction * 100)}%"
            page.update()
        publish(apply)

    if code:
        say("Загрузка обновления…")
        dest = tmp / "Takt-Code.zip"
        download_asset(
            code["url"], dest, prog, cancel_event,
            expected_size=code["size"], expected_sha256=code["sha256"],
        )
        ensure_active()
        if zip_native_stamp(dest) == native_stamp():
            say("Установка обновления…")
            ensure_active()
            apply_code_update(dest)
            mode = android_restart_mode()
            if mode == "notification":
                say("Обновление установлено ✓ Нажмите уведомление для перезапуска.")
            elif mode == "manual":
                say("Обновление установлено ✓ Закройте и откройте приложение снова.")
            else:
                say("Обновление установлено ✓ Перезапуск…")
            if mode == "manual" and cancel_event.wait(2.5):
                raise UpdateCancelled("Загрузка отменена")
            ensure_active()
            restart_android()
            return
        if apk is None:
            raise UpdateError("Нативная оболочка изменилась — нужен APK обновления")

    if apk:
        say("Загрузка APK (обновилась нативная часть)…")
        cache = Path(os.environ.get("FLET_APP_STORAGE_CACHE", str(tmp / "apk-cache")))
        cache.mkdir(parents=True, exist_ok=True)
        # Фиксированное имя: имя из подписанного manifest не используется как путь.
        dest = cache / ANDROID_APK_NAME
        downloaded = False
        try:
            download_asset(
                apk["url"], dest, prog, cancel_event,
                expected_size=apk["size"], expected_sha256=apk["sha256"],
            )
            downloaded = True
            ensure_active()
            say("Подтвердите установку в системном окне")
            ensure_active()
            install_apk_android(dest)
        except Exception:
            # После успешного запуска системного установщика файл должен остаться.
            if downloaded:
                try:
                    dest.unlink()
                except FileNotFoundError:
                    pass
            raise
        return
    raise UpdateError("В релизе нет файлов для Android")


def _show_update_dialog(app, info) -> None:
    page = app.page
    progress = ft.ProgressRing(visible=False, width=20, height=20)
    status_t = ft.Text("", size=12, expand=True)
    update_button = ft.FilledButton("Обновить", icon=ft.Icons.SYSTEM_UPDATE)
    cancel_event = threading.Event()
    state = {"busy": False, "closed": False, "progress": 0.0}

    def publish(callback):
        def apply():
            if not state["closed"]:
                callback()
        _run_ui(page, apply)

    def say(text: str):
        def apply():
            status_t.value = text
            page.update()
        publish(apply)

    def cancel_update(e=None):
        if state["closed"]:
            return
        state["closed"] = True
        cancel_event.set()
        _dismiss_dialog(page, dialog)

    def do_update(e):
        if state["busy"] or state["closed"] or cancel_event.is_set():
            return
        state["busy"] = True
        state["progress"] = 0.0
        progress.visible = True
        update_button.disabled = True
        page.update()

        def worker():
            tmp = Path(tempfile.mkdtemp(prefix="takt-update-"))
            try:
                if cancel_event.is_set():
                    return
                if is_android():
                    _update_android(info, status_t, page, cancel_event, publish, tmp)
                    return
                asset = choose_asset(info.assets)
                if not asset:
                    raise UpdateError("В релизе нет файлов для Windows (.exe)")
                say(f"Загрузка {asset['name']}…")
                dest = tmp / asset["name"]

                def prog(frac):
                    def apply():
                        state["progress"] = max(state["progress"], min(max(float(frac), 0.0), 1.0))
                        status_t.value = (f"Загрузка {asset['name']}… "
                                          f"{int(state['progress'] * 100)}%")
                        page.update()
                    publish(apply)

                download_asset(
                    asset["url"], dest, prog, cancel_event,
                    expected_size=asset["size"], expected_sha256=asset["sha256"],
                )
                if cancel_event.is_set():
                    return
                say("Запуск обновления…")
                if cancel_event.is_set():
                    return
                launch_update(dest, kind=asset["kind"])
                if is_frozen():
                    page.run_task(_destroy_window, page)
                else:
                    say("Обновление запущено (dev-режим: приложение не закрывается)")
            except Exception as ex:
                if cancel_event.is_set():
                    return
                message = f"Ошибка обновления: {ex}"

                def failed():
                    if state["closed"]:
                        return
                    status_t.value = message
                    state["busy"] = False
                    progress.visible = False
                    update_button.disabled = False
                    page.update()
                _run_ui(page, failed)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

        threading.Thread(target=worker, daemon=True).start()

    update_button.on_click = do_update
    dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text(f"Доступно обновление {info.version}"),
        content=ft.Container(
            width=dialog_width(page, 420),
            height=dialog_height(page, 460),
            content=ft.Column([
                ft.Text(info.notes or "Описание релиза отсутствует.", size=12),
                ft.Row([progress, status_t]),
            ], tight=True, spacing=10, scroll=ft.ScrollMode.AUTO, expand=True),
        ),
        actions=[
            ft.TextButton("Позже", on_click=cancel_update),
            update_button,
        ],
        on_dismiss=cancel_update,
    )
    page.show_dialog(dialog)


def check_updates_silent(app) -> None:
    repo = app.store.get_meta("github_repo", DEFAULT_REPO)
    if not repo or app.store.get_meta("auto_update") != "1":
        return

    def worker():
        try:
            info = check_update(repo)
            if info:
                _run_ui(app.page, lambda: _show_update_dialog(app, info))
        except Exception:
            pass

    threading.Thread(target=worker, daemon=True).start()
from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

import flet as ft

from .. import __version__
from ..sync import YandexDiskProvider, SyncEngine
from ..transfer import (
    TransferServer, qr_png_base64, fetch_payload, load_payload_file,
    diff_payload, apply_payload,
)
from ..update import (
    check_update, choose_asset, download_asset, launch_update, UpdateError, is_frozen,
)

DEFAULT_REPO = "SkyGeneral1996/Takt"


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
                          hint_text="например: SkyGeneral1996/Takt")
    auto_cb = ft.Checkbox(label="Проверять обновления при запуске",
                          value=store.get_meta("auto_update") == "1")
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
                sync_status.value = (f"Готово: отправлено {result.pushed}, "
                                     f"получено {result.applied} изменений")
                last_t.value = f"Последняя синхронизация: {result.at}"
                app.refresh()
            except Exception as ex:
                sync_status.value = f"Ошибка: {ex}"
            page.update()

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

        def worker():
            try:
                info = check_update(repo)
                if info is None:
                    upd_status.value = f"Установлена последняя версия ({__version__})"
                    page.update()
                else:
                    page.update()
                    _show_update_dialog(app, info)
            except UpdateError as ex:
                upd_status.value = str(ex)
                page.update()
            except Exception as ex:
                upd_status.value = f"Ошибка: {ex}"
                page.update()

        threading.Thread(target=worker, daemon=True).start()

    dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Настройки"),
        content=ft.Container(
            width=460,
            content=ft.Column([
                mode_t, device_t, last_t,
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
                ]),
                ft.Divider(),
                ft.Text("Обновления", weight=ft.FontWeight.BOLD),
                ft.Text(f"Версия: {__version__}", size=12, color=ft.Colors.GREY),
                repo_f, auto_cb, upd_status,
                ft.Row([
                    ft.OutlinedButton("Проверить обновления", icon=ft.Icons.SYSTEM_UPDATE,
                                      on_click=do_check_updates),
                ]),
            ], scroll=ft.ScrollMode.AUTO, tight=True, spacing=10),
        ),
        actions=[
            ft.TextButton("Закрыть", on_click=lambda e: page.pop_dialog()),
            ft.FilledButton("Сохранить", on_click=save_settings),
        ],
    )
    page.show_dialog(dialog)


def open_send(app) -> None:
    page = app.page
    server = TransferServer(app.store)
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
        title=ft.Text("Передать данные"),
        content=ft.Container(
            width=340,
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
            width=420,
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


def _show_diff(app, payload: dict) -> None:
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
        title=ft.Text("Разница данных"),
        content=ft.Container(
            width=420,
            content=ft.Column([
                ft.Text(d.describe(), weight=ft.FontWeight.BOLD),
                samples, note,
            ], tight=True, spacing=8, scroll=ft.ScrollMode.AUTO),
        ),
        actions=[
            ft.TextButton("Отмена", on_click=lambda e: page.pop_dialog()),
            ft.FilledButton("Применить", on_click=apply),
        ],
    )
    page.show_dialog(dialog)


def _show_update_dialog(app, info) -> None:
    page = app.page
    progress = ft.ProgressRing(visible=False, width=20, height=20)
    status_t = ft.Text("", size=12)
    state = {"busy": False}

    def do_update(e):
        if state["busy"]:
            return
        asset = choose_asset(info.assets)
        if not asset:
            status_t.value = "В релизе нет файлов для Windows (.exe / .zip)"
            page.update()
            return
        state["busy"] = True
        progress.visible = True
        status_t.value = f"Загрузка {asset['name']}…"
        page.update()

        def worker():
            try:
                dest = Path(tempfile.mkdtemp(prefix="takt-update-")) / asset["name"]

                def prog(frac):
                    status_t.value = f"Загрузка {asset['name']}… {int(frac * 100)}%"
                    page.update()

                download_asset(asset["url"], dest, prog)
                status_t.value = "Запуск обновления…"
                page.update()
                launch_update(dest)
                if is_frozen():
                    page.run_task(page.window.destroy)
                else:
                    status_t.value = "Обновление запущено (dev-режим: приложение не закрывается)"
                    page.update()
            except Exception as ex:
                status_t.value = f"Ошибка обновления: {ex}"
                state["busy"] = False
                progress.visible = False
                page.update()

        threading.Thread(target=worker, daemon=True).start()

    dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text(f"Доступно обновление {info.version}"),
        content=ft.Container(
            width=420,
            content=ft.Column([
                ft.Text(info.notes or "Описание релиза отсутствует.", size=12),
                ft.Row([progress, status_t]),
            ], tight=True, spacing=10, scroll=ft.ScrollMode.AUTO),
        ),
        actions=[
            ft.TextButton("Позже", on_click=lambda e: page.pop_dialog()),
            ft.FilledButton("Обновить", icon=ft.Icons.SYSTEM_UPDATE, on_click=do_update),
        ],
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
                _show_update_dialog(app, info)
        except Exception:
            pass

    threading.Thread(target=worker, daemon=True).start()
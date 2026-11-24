from __future__ import annotations

import threading

import flet as ft

from ..sync import YandexDiskProvider, YandexDiskError, SyncEngine


def open_settings(app) -> None:
    store = app.store
    page = app.page

    token_f = ft.TextField(
        label="OAuth-токен Яндекс.Диска", password=True, can_reveal_password=True,
        value=store.get_meta("yandex_token", ""))
    folder_f = ft.TextField(label="Папка на Диске",
                            value=store.get_meta("sync_folder", "TaskSync"))
    status_t = ft.Text("", size=12)
    device_t = ft.Text(f"Устройство: {store.device_id}", size=12,
                       color=ft.Colors.GREY)
    last = store.get_meta("last_sync")
    last_t = ft.Text(f"Последняя синхронизация: {last or 'ещё не выполнялась'}",
                     size=12, color=ft.Colors.GREY)

    def save_settings(e):
        store.set_meta("yandex_token", (token_f.value or "").strip())
        store.set_meta("sync_folder", (folder_f.value or "TaskSync").strip())
        status_t.value = "Настройки сохранены"
        page.update()

    def do_sync(e):
        save_settings(e)
        token = store.get_meta("yandex_token", "")
        if not token:
            status_t.value = "Укажите OAuth-токен (как получить — в README.md)"
            page.update()
            return
        status_t.value = "Синхронизация…"
        page.update()

        def worker():
            try:
                provider = YandexDiskProvider(token)
                engine = SyncEngine(store, provider, store.get_meta("sync_folder", "TaskSync"))
                result = engine.sync()
                status_t.value = (f"Готово: отправлено {result.pushed}, "
                                  f"получено {result.applied} изменений")
                last_t.value = f"Последняя синхронизация: {result.at}"
                app.refresh()
            except Exception as ex:
                status_t.value = f"Ошибка: {ex}"
            page.update()

        threading.Thread(target=worker, daemon=True).start()

    dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Настройки и синхронизация"),
        content=ft.Container(
            width=440,
            content=ft.Column([
                token_f, folder_f, device_t, last_t, status_t,
            ], tight=True, spacing=10),
        ),
        actions=[
            ft.TextButton("Закрыть", on_click=lambda e: page.pop_dialog()),
            ft.TextButton("Сохранить", on_click=save_settings),
            ft.FilledButton("Синхронизировать", icon=ft.Icons.SYNC, on_click=do_sync),
        ],
    )
    page.show_dialog(dialog)
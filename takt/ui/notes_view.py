from __future__ import annotations

import flet as ft

from ..core.models import Note


def build(app) -> ft.Control:
    query = getattr(app, "notes_search", "") or ""
    notes = app.store.list_notes()
    if query:
        notes = [n for n in notes
                 if query in " ".join([n.title, n.text, " ".join(n.tags)]).lower()]

    search_f = ft.TextField(hint_text="Поиск по заметкам…", prefix_icon=ft.Icons.SEARCH,
                            dense=True, value=query,
                            on_change=lambda e: _set_search(app, e.control.value),
                            on_submit=lambda e: _set_search(app, e.control.value),
                            on_blur=lambda e: _set_search(app, e.control.value))
    cards: list[ft.Control] = [search_f]
    if not notes:
        cards.append(ft.Text("Нет заметок. Нажмите +, чтобы создать.",
                             color=ft.Colors.GREY))
    for n in notes:
        preview = (n.text or "").strip().replace("\n", " ")
        if len(preview) > 120:
            preview = preview[:120] + "…"
        meta = [ft.Text(f"#{t}", size=11, color=ft.Colors.INDIGO_300) for t in n.tags]
        cards.append(ft.Card(content=ft.Container(
            padding=ft.Padding.symmetric(horizontal=10, vertical=6),
            content=ft.Row([
                ft.IconButton(
                    ft.Icons.PUSH_PIN if n.pinned else ft.Icons.PUSH_PIN_OUTLINED,
                    icon_size=16, tooltip="Закрепить",
                    on_click=lambda e, note=n: _toggle_pin(app, note)),
                ft.Column([
                    ft.Text(n.title or "Без названия", size=15, weight=ft.FontWeight.W_500),
                    ft.Text(preview, size=12, color=ft.Colors.GREY),
                    ft.Row(meta, spacing=6, wrap=True) if meta else ft.Container(),
                ], spacing=2, expand=True, tight=True),
                ft.IconButton(ft.Icons.EDIT, icon_size=18,
                              on_click=lambda e, note=n: open_note_editor(app, note)),
            ], vertical_alignment=ft.CrossAxisAlignment.START),
            on_click=lambda e, note=n: open_note_editor(app, note), ink=True,
        )))
    return ft.Column(cards, spacing=6, scroll=ft.ScrollMode.AUTO, expand=True)


def _set_search(app, value: str) -> None:
    app.notes_search = (value or "").lower().strip()
    app.refresh()


def _toggle_pin(app, note: Note) -> None:
    note.pinned = not note.pinned
    app.store.save_note(note)
    app.refresh()


def open_note_editor(app, note: Note | None = None) -> None:
    page = app.page
    editing = note is not None
    note = note or Note()

    title_f = ft.TextField(label="Заголовок", value=note.title, autofocus=not editing)
    text_f = ft.TextField(label="Текст", value=note.text, multiline=True,
                          min_lines=5, max_lines=12)
    tags_f = ft.TextField(label="Теги (через запятую)", value=", ".join(note.tags))
    pin_cb = ft.Checkbox(label="Закрепить", value=note.pinned)

    def save(e):
        note.title = (title_f.value or "").strip()
        note.text = text_f.value or ""
        note.tags = [t.strip() for t in (tags_f.value or "").split(",") if t.strip()]
        note.pinned = pin_cb.value
        if not note.title and not note.text:
            app.notify("Заметка пуста")
            return
        app.store.save_note(note)
        page.pop_dialog()
        app.refresh()

    def delete(e):
        app.store.delete_note(note.id)
        page.pop_dialog()
        app.refresh()

    actions = [ft.TextButton("Закрыть", on_click=lambda e: page.pop_dialog()),
               ft.FilledButton("Сохранить", on_click=save)]
    if editing:
        actions.insert(0, ft.TextButton("Удалить", on_click=delete))

    page.show_dialog(ft.AlertDialog(
        modal=True,
        title=ft.Text("Заметка" if editing else "Новая заметка"),
        content=ft.Container(width=440, content=ft.Column(
            [title_f, text_f, tags_f, pin_cb], tight=True, spacing=10,
            scroll=ft.ScrollMode.AUTO)),
        actions=actions,
    ))
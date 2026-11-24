from __future__ import annotations

from typing import Protocol


class SyncProvider(Protocol):
    """Интерфейс облачного провайдера. Новые облака (Dropbox, WebDAV, своё
    серверное API) реализуют эти четыре метода без изменения движка."""

    def ensure_folder(self, path: str) -> None: ...

    def list_files(self, folder: str) -> list[str]:
        """Имена файлов в папке (без пути)."""
        ...

    def upload_text(self, remote_path: str, text: str) -> None: ...

    def download_text(self, remote_path: str) -> str | None:
        """None, если файл не существует."""
        ...
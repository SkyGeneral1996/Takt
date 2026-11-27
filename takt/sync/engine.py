from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from ..core.models import now_iso
from ..core.store import Store
from .base import SyncProvider

MAX_REMOTE_JOURNAL_BYTES = 32 * 1024 * 1024
MAX_REMOTE_EVENTS = 100000
MAX_REMOTE_LINE_BYTES = 1024 * 1024
_DEVICE_FILE = re.compile(r"[A-Za-z0-9_-]{1,128}\.jsonl\Z")


@dataclass
class SyncResult:
    pushed: int
    applied: int
    at: str
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


class SyncEngine:
    """Обмен журналами через отдельный файл каждого устройства."""

    def __init__(self, store: Store, provider: SyncProvider, root_folder: str = "TaskSync"):
        self.store = store
        self.provider = provider
        self.root = root_folder.strip("/")
        if not self.root or any(part in {"", ".", ".."} for part in self.root.split("/")):
            raise ValueError("Некорректная папка синхронизации")
        self.devices_folder = f"{self.root}/devices"

    def sync(self) -> SyncResult:
        with self.store.sync_lock:
            return self._sync_locked()

    def _sync_locked(self) -> SyncResult:
        self.provider.ensure_folder(self.devices_folder)
        events, sequence = self.store.journal_batch()
        text = "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        own_name = f"{self.store.device_id}.jsonl"
        own_path = f"{self.devices_folder}/{own_name}"
        pushed = 0
        if self.store.get_meta("last_push_hash") != digest:
            self.provider.upload_text(own_path, text)
            self.store.set_meta("last_push_hash", digest)
            self.store.compact_changes(sequence)
            pushed = len(events)

        applied = 0
        skipped = 0
        errors: list[str] = []
        try:
            names = sorted(self.provider.list_files(self.devices_folder))
        except Exception as exc:
            names = []
            errors.append(f"Не удалось получить список журналов: {exc}")
        for name in names:
            if name == own_name or not _DEVICE_FILE.fullmatch(name):
                continue
            remote_path = f"{self.devices_folder}/{name}"
            try:
                remote_text = self.provider.download_text(remote_path)
            except Exception as exc:
                skipped += 1
                errors.append(f"{name}: {exc}")
                continue
            if not remote_text:
                continue
            if len(remote_text.encode("utf-8")) > MAX_REMOTE_JOURNAL_BYTES:
                skipped += 1
                errors.append(f"{name}: журнал превышает лимит размера")
                continue
            remote_events = []
            lines = remote_text.splitlines()
            if len(lines) > MAX_REMOTE_EVENTS:
                skipped += len(lines) - MAX_REMOTE_EVENTS
                lines = lines[:MAX_REMOTE_EVENTS]
                errors.append(f"{name}: журнал превышает лимит событий")
            for line in lines:
                if not line.strip():
                    continue
                if len(line.encode("utf-8")) > MAX_REMOTE_LINE_BYTES:
                    skipped += 1
                    continue
                try:
                    remote_events.append(json.loads(line))
                except (ValueError, TypeError):
                    skipped += 1
            if not remote_events:
                continue
            try:
                applied += self.store.apply_remote_events(remote_events)
            except ValueError:
                for remote_event in remote_events:
                    try:
                        applied += self.store.apply_remote_events([remote_event])
                    except (ValueError, TypeError, KeyError) as exc:
                        skipped += 1
                        if len(errors) < 20:
                            errors.append(f"{name}: {exc}")
            except (TypeError, KeyError):
                skipped += len(remote_events)
                if len(errors) < 20:
                    errors.append(f"{name}: повреждённая структура журнала")

        at = now_iso()
        self.store.set_meta("last_sync", at)
        return SyncResult(pushed=pushed, applied=applied, at=at,
                          skipped=skipped, errors=errors)

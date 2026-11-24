from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from ..core.models import now_iso
from ..core.store import Store
from .base import SyncProvider


@dataclass
class SyncResult:
    pushed: int
    applied: int
    at: str


class SyncEngine:
    """Обмен журналами изменений: своё устройство загружает полный лог,
    чужие логи скачивает и применяет события (идемпотентно, LWW)."""

    def __init__(self, store: Store, provider: SyncProvider, root_folder: str = "TaskSync"):
        self.store = store
        self.provider = provider
        self.root = root_folder.strip("/")
        self.devices_folder = f"{self.root}/devices"

    def sync(self) -> SyncResult:
        self.provider.ensure_folder(self.devices_folder)

        events = self.store.local_events()
        text = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        own_path = f"{self.devices_folder}/{self.store.device_id}.jsonl"
        pushed = 0
        if self.store.get_meta("last_push_hash") != digest:
            self.provider.upload_text(own_path, text)
            self.store.set_meta("last_push_hash", digest)
            pushed = len(events)

        applied = 0
        for name in self.provider.list_files(self.devices_folder):
            if not name.endswith(".jsonl") or name == f"{self.store.device_id}.jsonl":
                continue
            remote_text = self.provider.download_text(f"{self.devices_folder}/{name}")
            if not remote_text:
                continue
            remote_events = [json.loads(line) for line in remote_text.splitlines() if line.strip()]
            applied += self.store.apply_remote_events(remote_events)

        at = now_iso()
        self.store.set_meta("last_sync", at)
        return SyncResult(pushed=pushed, applied=applied, at=at)
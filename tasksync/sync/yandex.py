from __future__ import annotations

import requests

API = "https://cloud-api.yandex.net/v1/disk"


class YandexDiskError(Exception):
    pass


class YandexDiskProvider:
    """Хранилище на Яндекс.Диске через REST API с OAuth-токеном.

    Каждое устройство пишет только свой файл devices/{device_id}.jsonl,
    поэтому конфликтов записи на уровне облака не возникает.
    """

    def __init__(self, token: str, timeout: float = 30.0):
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"OAuth {token}"
        self._timeout = timeout

    def _check(self, resp: requests.Response, ok: tuple[int, ...]) -> requests.Response:
        if resp.status_code not in ok:
            try:
                detail = resp.json().get("message", resp.text[:200])
            except ValueError:
                detail = resp.text[:200]
            raise YandexDiskError(f"HTTP {resp.status_code}: {detail}")
        return resp

    def ensure_folder(self, path: str) -> None:
        parts = path.strip("/").split("/")
        for i in range(1, len(parts) + 1):
            sub = "/".join(parts[:i])
            resp = self._session.put(
                f"{API}/resources", params={"path": sub}, timeout=self._timeout)
            if resp.status_code not in (201, 409):  # 409 = уже существует
                self._check(resp, (201,))

    def list_files(self, folder: str) -> list[str]:
        resp = self._session.get(
            f"{API}/resources",
            params={"path": folder, "limit": 1000, "fields": "_embedded.items.name,_embedded.items.type"},
            timeout=self._timeout)
        if resp.status_code == 404:
            return []
        self._check(resp, (200,))
        items = resp.json().get("_embedded", {}).get("items", [])
        return [it["name"] for it in items if it.get("type") == "file"]

    def upload_text(self, remote_path: str, text: str) -> None:
        resp = self._session.get(
            f"{API}/resources/upload",
            params={"path": remote_path, "overwrite": "true"},
            timeout=self._timeout)
        self._check(resp, (200,))
        href = resp.json()["href"]
        up = requests.put(href, data=text.encode("utf-8"), timeout=self._timeout)
        if up.status_code not in (201, 202):
            raise YandexDiskError(f"Загрузка не удалась: HTTP {up.status_code}")

    def download_text(self, remote_path: str) -> str | None:
        resp = self._session.get(
            f"{API}/resources/download", params={"path": remote_path}, timeout=self._timeout)
        if resp.status_code == 404:
            return None
        self._check(resp, (200,))
        href = resp.json()["href"]
        down = requests.get(href, timeout=self._timeout)
        if down.status_code == 404:
            return None
        if down.status_code != 200:
            raise YandexDiskError(f"Скачивание не удалось: HTTP {down.status_code}")
        down.encoding = "utf-8"
        return down.text
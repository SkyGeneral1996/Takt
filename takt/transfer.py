from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
import struct
import tempfile
import threading
import time
import zlib
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import qrcode
import requests

from .core.store import Store

FORMAT = "takt-transfer-1"
MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
MAX_EVENTS = 100000


def lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def build_payload(store: Store, only: set | None = None) -> dict:
    events = []
    for row in store.export_state():
        if only is not None and (row["entity"], row["entity_id"]) not in only:
            continue
        identity = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        event_id = hashlib.sha256(f"{store.device_id}:{identity}".encode("utf-8")).hexdigest()
        events.append({
            "event_id": event_id, "device_id": store.device_id,
            "entity": row["entity"], "entity_id": row["entity_id"],
            "op": "delete" if row["payload"].get("deleted") else "upsert",
            "payload": row["payload"], "updated_at": row["updated_at"],
        })
    return {"format": FORMAT, "device_id": store.device_id, "events": events}


def validate_payload(data: dict) -> dict:
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError("Это не файл переноса Takt")
    events = data.get("events")
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise ValueError("Повреждённый или слишком большой файл переноса")
    if len(json.dumps(data, ensure_ascii=False).encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError("Файл переноса превышает лимит размера")
    normalized = []
    seen = set()
    for event in events:
        item = Store.validate_event(event)
        if item["event_id"] in seen:
            raise ValueError("В файле есть повторяющиеся события")
        seen.add(item["event_id"])
        normalized.append(item)
    result = dict(data)
    result["events"] = normalized
    return result


def _validate_url(url: str) -> None:
    if urlsplit(url).scheme not in {"http", "https"}:
        raise ValueError("Недопустимая ссылка переноса")


def fetch_payload(url: str, timeout: float = 15.0) -> dict:
    _validate_url(url)
    deadline = time.monotonic() + timeout
    for _ in range(6):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Превышено время загрузки")
        response = requests.get(url, timeout=(min(5.0, remaining), remaining),
                                stream=True, allow_redirects=False)
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise ValueError("Некорректный редирект")
            url = urljoin(url, location)
            _validate_url(url)
            continue
        try:
            response.raise_for_status()
            length = response.headers.get("Content-Length")
            if length:
                try:
                    declared = int(length)
                except ValueError as exc:
                    raise ValueError("Некорректный размер ответа") from exc
                if declared > MAX_PAYLOAD_BYTES:
                    raise ValueError("Файл переноса превышает лимит размера")
            data = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                data.extend(chunk)
                if len(data) > MAX_PAYLOAD_BYTES:
                    raise ValueError("Файл переноса превышает лимит размера")
                if time.monotonic() > deadline:
                    raise TimeoutError("Превышено время загрузки")
            try:
                parsed = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError) as exc:
                raise ValueError("Повреждённый файл переноса") from exc
            return validate_payload(parsed)
        finally:
            response.close()
    raise ValueError("Слишком много редиректов")


def load_payload_file(path: str) -> dict:
    file_path = Path(path)
    if file_path.stat().st_size > MAX_PAYLOAD_BYTES:
        raise ValueError("Файл переноса превышает лимит размера")
    with file_path.open("r", encoding="utf-8") as file:
        try:
            return validate_payload(json.load(file))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise ValueError("Повреждённый файл переноса") from exc


def save_payload_file(path: str | Path, payload: dict) -> Path:
    destination = Path(path)
    validated = validate_payload(payload)
    data = json.dumps(validated, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")
    if len(data) > MAX_PAYLOAD_BYTES:
        raise ValueError("Файл переноса превышает лимит размера")
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.",
                                     suffix=".part", dir=destination.parent)
    temp_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, destination)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
    return destination


@dataclass
class DiffSummary:
    new: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0
    older: int = 0
    samples: list[str] = field(default_factory=list)

    @property
    def total_changes(self) -> int:
        return self.new + self.updated + self.deleted

    def describe(self) -> str:
        return (f"Новых: {self.new}, изменённых: {self.updated}, "
                f"удалённых: {self.deleted}, без изменений: {self.unchanged}")


def diff_payload(store: Store, payload: dict) -> DiffSummary:
    summary = DiffSummary()
    labels = {"new": "новая", "updated": "изменение", "deleted": "удаление"}
    for event, action in store.preview_remote_events(payload["events"]):
        setattr(summary, action, getattr(summary, action) + 1)
        if action not in labels or len(summary.samples) >= 12:
            continue
        incoming = event["payload"]
        title = incoming.get("name") or incoming.get("title") or event["entity_id"][:8]
        summary.samples.append(f"{labels[action]}: {title}")
    return summary


def apply_payload(store: Store, payload: dict) -> int:
    return store.apply_remote_events(payload["events"])


def qr_png_base64(text: str) -> str:
    qr = qrcode.QRCode(border=4)
    qr.add_data(text)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    scale = 8
    size = len(matrix) * scale
    raw = bytearray()
    for row in matrix:
        line = b"".join(bytes([0 if value else 255]) * scale for value in row)
        for _ in range(scale):
            raw += b"\x00" + line

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 0, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
           + chunk(b"IEND", b""))
    return base64.b64encode(png).decode("ascii")


_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Takt — перенос данных</title>
<style>body{font-family:system-ui,sans-serif;max-width:32em;margin:3em auto;padding:0 1em;
text-align:center}a.btn{display:inline-block;background:#3f51b5;color:#fff;padding:.8em 1.6em;
border-radius:8px;text-decoration:none;margin:1em 0}p{color:#555}</style></head><body>
<h2>Takt</h2><p>Данные готовы к переносу.</p>
<a class="btn" href="/data.json?token=__TOKEN__">Скачать файл данных</a>
<p>Затем в приложении Takt на этом устройстве:<br>
Настройки → Перенос данных → Принять → «Из файла».</p>
<p>Или скопируйте ссылку и вставьте её в поле «По ссылке» в приложении.</p>
<p>Используйте эту ссылку только в доверенной локальной сети.</p>
</body></html>"""


class TransferServer:
    """Временный LAN-сервер с одноразовым токеном и ограниченным временем жизни."""

    def __init__(self, store: Store, ttl_sec: float = 300.0, payload: dict | None = None):
        body = payload if payload is not None else build_payload(store)
        self.payload_json = json.dumps(body, ensure_ascii=False)
        if len(self.payload_json.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise ValueError("Файл переноса превышает лимит размера")
        self.token = secrets.token_urlsafe(16)
        self.received = threading.Event()
        self._token_lock = threading.Lock()
        self._consumed = False
        self._stop_lock = threading.Lock()
        self._stopped = False
        self._thread: threading.Thread | None = None
        server = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(10.0)

            def log_message(self, *args):
                pass

            def _send(self, code: int, body: bytes, ctype: str):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == f"/t/{server.token}":
                    self._send(200, _PAGE.replace("__TOKEN__", server.token).encode("utf-8"),
                               "text/html; charset=utf-8")
                elif self.path == f"/data.json?token={server.token}" and server._claim_token():
                    self._send(200, server.payload_json.encode("utf-8"),
                               "application/json; charset=utf-8")
                    server.received.set()
                    timer = threading.Timer(0.5, server.stop)
                    timer.daemon = True
                    timer.start()
                else:
                    self._send(404, b"not found", "text/plain")

        self.httpd = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        host = lan_ip()
        self.url = f"http://{host}:{self.port}/t/{self.token}"
        self.data_url = f"http://{host}:{self.port}/data.json?token={self.token}"
        self._ttl = threading.Timer(ttl_sec, self.stop)
        self._ttl.daemon = True

    def _claim_token(self) -> bool:
        with self._token_lock:
            if self._consumed or self._stopped:
                return False
            self._consumed = True
            return True

    def start(self) -> None:
        if self._stopped:
            raise RuntimeError("Сервер переноса уже остановлен")
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True,
                                        name="takt-transfer")
        self._thread.start()
        self._ttl.start()

    def stop(self) -> None:
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
        self._ttl.cancel()
        if self._thread and self._thread.is_alive():
            self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5.0)

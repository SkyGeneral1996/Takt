from __future__ import annotations

import base64
import hashlib
import json
import secrets
import socket
import struct
import threading
import zlib
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import qrcode
import requests

from .core.store import Store

FORMAT = "takt-transfer-1"


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def build_payload(store: Store, only: set | None = None) -> dict:
    events = []
    for row in store.export_state():
        if only is not None and (row["entity"], row["entity_id"]) not in only:
            continue
        eid = hashlib.sha1(
            f"{store.device_id}:{row['entity']}:{row['entity_id']}:{row['updated_at']}"
            .encode("utf-8")).hexdigest()
        events.append({
            "event_id": eid, "device_id": store.device_id,
            "entity": row["entity"], "entity_id": row["entity_id"],
            "op": "upsert", "payload": row["payload"], "updated_at": row["updated_at"],
        })
    return {"format": FORMAT, "device_id": store.device_id, "events": events}


def validate_payload(data: dict) -> dict:
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError("Это не файл переноса Takt")
    if not isinstance(data.get("events"), list):
        raise ValueError("Повреждённый файл переноса")
    return data


def fetch_payload(url: str, timeout: float = 15.0) -> dict:
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return validate_payload(resp.json())


def load_payload_file(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return validate_payload(json.load(f))


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
    local = {(r["entity"], r["entity_id"]): r for r in store.export_state()}
    s = DiffSummary()
    for ev in payload["events"]:
        key = (ev["entity"], ev["entity_id"])
        incoming = ev["payload"]
        title = incoming.get("name") or incoming.get("title") or ev["entity_id"][:8]
        cur = local.get(key)
        if cur is None or (cur["payload"].get("deleted") and not incoming.get("deleted")):
            s.new += 1
            action = "новая"
        elif incoming.get("deleted") and not cur["payload"].get("deleted"):
            s.deleted += 1
            action = "удаление"
        elif incoming.get("updated_at", "") > cur["updated_at"]:
            s.updated += 1
            action = "изменение"
        elif incoming.get("updated_at", "") < cur["updated_at"]:
            s.older += 1
            continue
        else:
            s.unchanged += 1
            continue
        if len(s.samples) < 12:
            s.samples.append(f"{action}: {title}")
    return s


def apply_payload(store: Store, payload: dict) -> int:
    return store.apply_remote_events(payload["events"])


def qr_png_base64(text: str) -> str:
    # PNG без pillow/pypng:serious_python на Android их не собирает
    qr = qrcode.QRCode(border=4)
    qr.add_data(text)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    scale = 8
    size = len(matrix) * scale
    raw = bytearray()
    for row in matrix:
        line = b"".join(bytes([0 if v else 255]) * scale for v in row)
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
</body></html>"""


class TransferServer:
    """Временный HTTP-сервер в локальной сети: отдаёт payload по одноразовому
    токену. QR ведёт на страницу /t/<token>, данные — /data.json?token=."""

    def __init__(self, store: Store, ttl_sec: float = 300.0, payload: dict | None = None):
        self.payload_json = json.dumps(
            payload if payload is not None else build_payload(store), ensure_ascii=False)
        self.token = secrets.token_urlsafe(12)
        self.received = threading.Event()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code: int, body: bytes, ctype: str):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == f"/t/{server.token}":
                    self._send(200, _PAGE.replace("__TOKEN__", server.token).encode("utf-8"),
                               "text/html; charset=utf-8")
                elif self.path == f"/data.json?token={server.token}":
                    self._send(200, server.payload_json.encode("utf-8"),
                               "application/json; charset=utf-8")
                    server.received.set()
                    t = threading.Timer(1.0, server.stop)
                    t.daemon = True
                    t.start()
                else:
                    self._send(404, b"not found", "text/plain")

        self.httpd = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.url = f"http://{lan_ip()}:{self.port}/t/{self.token}"
        self.data_url = f"http://{lan_ip()}:{self.port}/data.json?token={self.token}"
        self._ttl = threading.Timer(ttl_sec, self.stop)
        self._ttl.daemon = True

    def start(self) -> None:
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self._ttl.start()

    def stop(self) -> None:
        self._ttl.cancel()
        threading.Thread(target=self.httpd.shutdown, daemon=True).start()
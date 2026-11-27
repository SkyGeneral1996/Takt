import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from takt import transfer
from takt.core.models import Task
from takt.core.store import Store


class FakeResponse:
    def __init__(self, chunks=(), headers=None, status_code=200):
        self._chunks = chunks
        self.headers = headers or {}
        self.status_code = status_code
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def iter_content(self, chunk_size):
        yield from self._chunks

    def close(self):
        self.closed = True


class TestTransferSecurity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "data.db", device_id="devA")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_payload_rejects_id_mismatch(self):
        task = Task(title="Подмена")
        payload = task.to_dict()
        event = {"event_id": "event", "device_id": "remote", "entity": "task",
                 "entity_id": "other", "op": "upsert", "payload": payload,
                 "updated_at": payload["updated_at"]}
        with self.assertRaises(ValueError):
            transfer.validate_payload({"format": transfer.FORMAT, "events": [event]})

    def test_fetch_rejects_non_http_scheme(self):
        with self.assertRaises(ValueError):
            transfer.fetch_payload("file:///tmp/data.json")

    def test_fetch_enforces_declared_size(self):
        response = FakeResponse(headers={"Content-Length": str(transfer.MAX_PAYLOAD_BYTES + 1)})
        with patch.object(transfer.requests, "get", return_value=response):
            with self.assertRaises(ValueError):
                transfer.fetch_payload("http://127.0.0.1/data")
        self.assertTrue(response.closed)

    def test_fetch_enforces_streamed_size_and_closes_response(self):
        response = FakeResponse([b"x" * (transfer.MAX_PAYLOAD_BYTES + 1)])
        with patch.object(transfer.requests, "get", return_value=response):
            with self.assertRaises(ValueError):
                transfer.fetch_payload("http://127.0.0.1/data")
        self.assertTrue(response.closed)

    def test_server_token_is_single_use_and_socket_closes(self):
        self.store.save_task(Task(title="Одноразовый"))
        server = transfer.TransferServer(self.store, ttl_sec=30)
        server.start()
        local = server.data_url.replace(transfer.lan_ip(), "127.0.0.1")
        try:
            first = requests.get(local, timeout=5)
            self.assertEqual(first.status_code, 200)
            second = requests.get(local, timeout=5)
            self.assertEqual(second.status_code, 404)
        finally:
            server.stop()
        with self.assertRaises(requests.RequestException):
            requests.get(local, timeout=1)


if __name__ == "__main__":
    unittest.main()

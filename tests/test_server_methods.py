import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace

from stock_tracker.api.handlers import AppContext
from stock_tracker.api.server import APIServer
from stock_tracker.core.store import MarketStore
from stock_tracker.storage.db import close_all
from stock_tracker.storage.repository import Repository


class TestServerMethods(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.ctx = AppContext(
            bundle=SimpleNamespace(),
            store=MarketStore(),
            repo=Repository(os.path.join(cls.tmp.name, "methods.db")),
            router=SimpleNamespace(),
            signal_manager=None,
            sse_hub=SimpleNamespace(),
            web_root=cls.tmp.name,
        )
        cls.server = APIServer("127.0.0.1", 0, cls.ctx, None)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown_wait()
        cls.thread.join(timeout=5)
        close_all()
        cls.tmp.cleanup()

    def call(self, method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method
        )
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            with exc:
                raw = exc.read().decode()
                return exc.code, json.loads(raw) if raw.startswith("{") else raw
        with response:
            return response.status, json.loads(response.read().decode())

    def test_unknown_put_patch_delete_are_404(self):
        for method in ("PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                status, body = self.call(method, "/api/not-found", {})
                self.assertEqual((status, body["error"]["code"]), (404, "NOT_FOUND"))

    def test_existing_post_malformed_json_compatibility(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/watch",
            data=b"{",
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=5)
        try:
            self.assertEqual(raised.exception.code, 400)
        finally:
            raised.exception.close()

    def test_static_path_traversal_is_forbidden(self):
        status, _ = self.call("GET", "/../../outside")
        self.assertIn(status, (403, 404))


if __name__ == "__main__":
    unittest.main()

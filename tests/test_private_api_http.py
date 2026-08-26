from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from stock_tracker.api.handlers import AppContext
from stock_tracker.api.server import APIServer
from stock_tracker.core.config import load_configs
from stock_tracker.core.store import MarketStore
from stock_tracker.storage.db import close_all
from stock_tracker.storage.repository import Repository

_STRONG_ACCESS = "stage1-private-access-value-0123456789abcdef"


class TestPrivateAPIHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.ctx = AppContext(
            bundle=load_configs("config"),
            store=MarketStore(),
            repo=Repository(os.path.join(cls.tmp.name, "private-http.db")),
            router=SimpleNamespace(health_list=list),
            signal_manager=None,
            sse_hub=SimpleNamespace(),
            web_root=cls.tmp.name,
        )
        cls.server = APIServer("127.0.0.1", 0, cls.ctx, None)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(
            target=cls.server.serve_forever,
            daemon=True,
        )
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown_wait()
        cls.thread.join(timeout=5)
        close_all()
        cls.tmp.cleanup()

    def request(
        self,
        *,
        host: str,
        authorization: str | None = None,
        origin: str | None = None,
        sec_fetch_site: str | None = None,
        forwarded_for: str | None = None,
    ) -> tuple[int, dict, dict[str, str]]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.putrequest("GET", "/api/portfolio", skip_host=True)
            connection.putheader("Host", host)
            connection.putheader("Accept", "application/json")
            if authorization is not None:
                connection.putheader("Authorization", authorization)
            if origin is not None:
                connection.putheader("Origin", origin)
            if sec_fetch_site is not None:
                connection.putheader("Sec-Fetch-Site", sec_fetch_site)
            if forwarded_for is not None:
                connection.putheader("X-Forwarded-For", forwarded_for)
            connection.endheaders()
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            headers = {key.lower(): value for key, value in response.getheaders()}
            return response.status, payload, headers
        finally:
            connection.close()

    def assert_no_wildcard_cors(self, headers: dict[str, str]) -> None:
        self.assertNotIn("access-control-allow-origin", headers)

    def test_public_host_without_server_access_value_fails_closed(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STOCK_TRACKER_PRIVATE_ACCESS", None)
            status, body, headers = self.request(host="stock.example")
        self.assertEqual(
            (status, body["error"]["code"]),
            (503, "PRIVATE_API_DISABLED"),
        )
        self.assert_no_wildcard_cors(headers)

    def test_short_server_access_value_is_reported_as_misconfigured(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"STOCK_TRACKER_PRIVATE_ACCESS": "too-short"},
            clear=False,
        ):
            status, body, headers = self.request(
                host="stock.example",
                authorization="Bearer too-short",
            )
        self.assertEqual(
            (status, body["error"]["code"]),
            (503, "PRIVATE_API_MISCONFIGURED"),
        )
        self.assert_no_wildcard_cors(headers)

    def test_public_host_requires_exact_strong_bearer_value(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"STOCK_TRACKER_PRIVATE_ACCESS": _STRONG_ACCESS},
            clear=False,
        ):
            status, body, _ = self.request(host="stock.example")
            self.assertEqual(
                (status, body["error"]["code"]),
                (401, "PRIVATE_API_AUTH_REQUIRED"),
            )
            status, body, _ = self.request(
                host="stock.example",
                authorization="Bearer wrong-value",
            )
            self.assertEqual(
                (status, body["error"]["code"]),
                (401, "PRIVATE_API_AUTH_REQUIRED"),
            )
            status, body, headers = self.request(
                host="stock.example",
                authorization="Bearer " + _STRONG_ACCESS,
            )
        self.assertEqual(status, 200)
        self.assertEqual(body["schema_version"], "stage1-v1")
        self.assertIsNone(body["profile"])
        self.assert_no_wildcard_cors(headers)

    def test_cross_site_or_forwarded_loopback_request_does_not_bypass(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STOCK_TRACKER_PRIVATE_ACCESS", None)
            cases = (
                {"origin": "https://evil.example"},
                {"sec_fetch_site": "cross-site"},
                {"forwarded_for": "198.51.100.20"},
            )
            for case in cases:
                with self.subTest(case=case):
                    status, body, _ = self.request(
                        host=f"127.0.0.1:{self.port}",
                        **case,
                    )
                    expected = (
                        (403, "CORS_ORIGIN_DENIED")
                        if "origin" in case
                        else (503, "PRIVATE_API_DISABLED")
                    )
                    self.assertEqual(
                        (status, body["error"]["code"]),
                        expected,
                    )

    def test_direct_same_origin_loopback_remains_available(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STOCK_TRACKER_PRIVATE_ACCESS", None)
            status, body, headers = self.request(
                host=f"127.0.0.1:{self.port}",
                origin=f"http://127.0.0.1:{self.port}",
                sec_fetch_site="same-origin",
            )
        self.assertEqual(status, 200)
        self.assertEqual(body["positions"], [])
        self.assert_no_wildcard_cors(headers)


if __name__ == "__main__":
    unittest.main()

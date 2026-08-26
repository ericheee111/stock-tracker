from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from stock_tracker.api.handlers import AppContext
from stock_tracker.api.runtime import build_runtime_health
from stock_tracker.api.server import APIServer
from stock_tracker.api.sse import SSEHub
from stock_tracker.core import types as T
from stock_tracker.core.config import ConfigError, load_app, load_configs
from stock_tracker.core.network import InvalidOriginError, normalize_http_origin
from stock_tracker.core.store import MarketStore
from stock_tracker.storage.db import close_all
from stock_tracker.storage.repository import Repository

_ALLOWED_ORIGIN = "https://app.example"
_STRONG_ACCESS = "h1-h2-test-only-" + ("x" * 32)


class _Bus:
    def subscribe(self, callback) -> None:
        self.callback = callback


class _AliveThread:
    def is_alive(self) -> bool:
        return True


class _Router:
    def health_list(self) -> list[T.ProviderHealth]:
        return [
            T.ProviderHealth(
                provider="fixture",
                circuit_state=T.CircuitState.CLOSED,
                last_success_at=datetime.now(timezone.utc),
            )
        ]


class TestRuntimeConfigContract(unittest.TestCase):
    def test_origin_normalization_is_exact_and_bounded(self) -> None:
        self.assertEqual(
            normalize_http_origin("HTTPS://APP.Example:443/"),
            "https://app.example",
        )
        self.assertEqual(
            normalize_http_origin("http://127.0.0.1:8080"),
            "http://127.0.0.1:8080",
        )
        invalid = (
            "null",
            "*",
            "ftp://app.example",
            "http://app.example",
            "https://user@app.example",
            "https://app.example/api",
            "https://app.example?x=1",
            " https://app.example",
            "https://app.example\\evil",
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(InvalidOriginError):
                normalize_http_origin(value)

    def test_runtime_config_normalizes_deduplicates_and_validates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.toml"
            path.write_text(
                """
[runtime]
deployment_mode = "HYBRID_PRIVATE"
engine_id = "fixture-engine"
commit_id = "abc123"
api_major = 2
cors_allowed_origins = ["HTTPS://APP.Example:443/", "https://app.example"]
cors_max_age_sec = 300
""".strip(),
                encoding="utf-8",
            )
            runtime = load_app(str(path), root_dir=directory).runtime
            self.assertEqual(runtime.cors_allowed_origins, [_ALLOWED_ORIGIN])
            self.assertEqual(runtime.engine_id, "fixture-engine")
            self.assertEqual(runtime.commit_id, "abc123")
            self.assertEqual(runtime.api_major, 2)
            self.assertEqual(runtime.cors_max_age_sec, 300)

    def test_runtime_config_rejects_unsafe_shapes(self) -> None:
        invalid_documents = (
            '[runtime]\ncors_allowed_origins = "https://app.example"\n',
            '[runtime]\ncors_allowed_origins = ["null"]\n',
            '[runtime]\ndeployment_mode = "UNKNOWN"\n',
            '[runtime]\napi_major = true\n',
            '[runtime]\ncors_max_age_sec = 86401\n',
            '[runtime]\nengine_id = " bad"\n',
            '[runtime]\nprivate_access = "forbidden"\n',
        )
        with tempfile.TemporaryDirectory() as directory:
            for index, document in enumerate(invalid_documents):
                path = Path(directory) / f"app-{index}.toml"
                path.write_text(document, encoding="utf-8")
                with self.subTest(document=document), self.assertRaises(ConfigError):
                    load_app(str(path), root_dir=directory)


class TestHybridH1H2HTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(prefix="hybrid-h1-h2-")
        cls.bundle = load_configs("config")
        cls.bundle.app.runtime.engine_id = "fixture-engine"
        cls.bundle.app.runtime.commit_id = "fixture-commit"
        cls.bundle.app.runtime.cors_allowed_origins = [_ALLOWED_ORIGIN]
        cls.bundle.app.runtime.cors_max_age_sec = 321

        now = datetime.now(timezone.utc)
        cls.store = MarketStore()
        cls.store.update_quote(
            T.Quote(
                symbol="600000.SH",
                market=T.Market.A,
                timestamp=now,
                name="浦发银行",
                open=10.0,
                high=10.2,
                low=9.9,
                close=10.1,
                last=10.1,
                prev_close=10.0,
                source="hybrid-h1-h2-fixture",
                received_at=now,
                computed_at=now,
                displayed_at=now,
                observed_age_ms=50,
                data_status=T.DataStatus.LIVE,
            )
        )
        cls.repo = Repository(os.path.join(cls.tmp.name, "hybrid.db"))
        cls.ctx = AppContext(
            bundle=cls.bundle,
            store=cls.store,
            repo=cls.repo,
            router=_Router(),
            signal_manager=SimpleNamespace(_portfolio_heat=lambda: 0.0),
            sse_hub=SSEHub(_Bus()),
            web_root=cls.tmp.name,
            scheduler=SimpleNamespace(
                _stop=threading.Event(),
                _threads=[_AliveThread()],
            ),
        )
        cls.server = APIServer("127.0.0.1", 0, cls.ctx, None)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown_wait()
        cls.thread.join(timeout=5)
        close_all()
        cls.tmp.cleanup()

    def request(
        self,
        method: str,
        path: str,
        *,
        origin: str | None = None,
        authorization: str | None = None,
        body: dict | None = None,
        request_method: str | None = None,
        request_headers: str | None = None,
        host: str | None = None,
    ) -> tuple[int, bytes, dict[str, str]]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = None if body is None else json.dumps(body).encode("utf-8")
        try:
            connection.putrequest(method, path, skip_host=True)
            connection.putheader("Host", host or f"127.0.0.1:{self.port}")
            connection.putheader("Accept", "application/json")
            if origin is not None:
                connection.putheader("Origin", origin)
            if authorization is not None:
                connection.putheader("Authorization", authorization)
            if request_method is not None:
                connection.putheader("Access-Control-Request-Method", request_method)
            if request_headers is not None:
                connection.putheader("Access-Control-Request-Headers", request_headers)
            if payload is not None:
                connection.putheader("Content-Type", "application/json; charset=utf-8")
                connection.putheader("Content-Length", str(len(payload)))
            connection.endheaders(payload)
            response = connection.getresponse()
            raw = response.read()
            headers = {key.lower(): value for key, value in response.getheaders()}
            return response.status, raw, headers
        finally:
            connection.close()

    @staticmethod
    def json_body(raw: bytes) -> dict:
        return json.loads(raw.decode("utf-8"))

    def test_runtime_health_is_public_exact_cors_and_contains_no_private_facts(self) -> None:
        status, raw, headers = self.request(
            "GET",
            "/api/runtime/health",
            origin=_ALLOWED_ORIGIN,
        )
        body = self.json_body(raw)
        self.assertEqual(status, 200)
        self.assertEqual(headers["access-control-allow-origin"], _ALLOWED_ORIGIN)
        self.assertEqual(headers["vary"], "Origin")
        self.assertNotEqual(headers["access-control-allow-origin"], "*")
        self.assertEqual(body["schema_version"], "hybrid-runtime-v1")
        self.assertEqual(body["status"], "ONLINE")
        self.assertEqual(body["engine_id"], "fixture-engine")
        self.assertEqual(body["commit_id"], "fixture-commit")
        self.assertEqual(body["api_major"], 1)
        self.assertEqual(body["scheduler_state"], "RUNNING")
        self.assertEqual(body["database_state"], "READY")
        self.assertEqual(body["data_status"], "LIVE")
        self.assertIsNotNone(body["data_as_of"])
        serialized = json.dumps(body, ensure_ascii=False).lower()
        for forbidden in (
            "private_access",
            "authorization",
            "account_equity",
            "available_cash",
            "average_cost",
            "positions",
            self.repo.db_path.lower(),
        ):
            self.assertNotIn(forbidden, serialized)

    def test_health_without_quotes_is_honestly_degraded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ctx = AppContext(
                bundle=load_configs("config"),
                store=MarketStore(),
                repo=Repository(os.path.join(directory, "empty.db")),
                router=_Router(),
                signal_manager=None,
                sse_hub=SSEHub(_Bus()),
            )
            health = build_runtime_health(ctx)
            close_all()
        self.assertEqual(health["status"], "DEGRADED")
        self.assertEqual(health["data_status"], "UNKNOWN")
        self.assertIsNone(health["data_as_of"])
        self.assertIsNone(health["last_collection_at"])

    def test_runtime_health_recomputes_aged_live_quote_as_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = load_configs("config")
            store = MarketStore()
            old_utc = datetime.now(timezone.utc) - timedelta(hours=2)
            market_offset = timedelta(hours=bundle.markets.a.utc_offset_hours)
            old_market = (old_utc + market_offset).replace(tzinfo=None)
            store.update_quote(
                T.Quote(
                    symbol="600001.SH",
                    market=T.Market.A,
                    timestamp=old_market,
                    received_at=old_utc,
                    computed_at=old_utc,
                    displayed_at=old_utc,
                    last=10.0,
                    prev_close=9.9,
                    source="stale-runtime-fixture",
                    data_status=T.DataStatus.LIVE,
                )
            )
            ctx = AppContext(
                bundle=bundle,
                store=store,
                repo=Repository(os.path.join(directory, "stale.db")),
                router=_Router(),
                signal_manager=None,
                sse_hub=SSEHub(_Bus()),
                scheduler=SimpleNamespace(
                    _stop=threading.Event(),
                    _threads=[_AliveThread()],
                ),
            )
            health = build_runtime_health(ctx)
            close_all()
        self.assertEqual(health["status"], "STALE")
        self.assertEqual(health["data_status"], "STALE")
        reported = datetime.fromisoformat(health["data_as_of"])
        self.assertLess(abs((reported - old_utc).total_seconds()), 2)

    def test_runtime_health_without_provider_health_is_degraded(self) -> None:
        original = self.ctx.router
        self.ctx.router = SimpleNamespace(health_list=list)
        try:
            health = build_runtime_health(self.ctx)
        finally:
            self.ctx.router = original
        self.assertEqual(health["status"], "DEGRADED")
        self.assertEqual(health["provider_summary"]["count"], 0)

    def test_same_origin_loopback_remains_compatible_without_cors_headers(self) -> None:
        status, raw, headers = self.request(
            "GET",
            "/api/runtime/health",
            origin=f"http://127.0.0.1:{self.port}",
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.json_body(raw)["engine_id"], "fixture-engine")
        self.assertNotIn("access-control-allow-origin", headers)

    def test_disallowed_null_and_malformed_origins_fail_before_auth(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STOCK_TRACKER_PRIVATE_ACCESS", None)
            cases = (
                ("https://evil.example", "CORS_ORIGIN_DENIED"),
                ("null", "CORS_ORIGIN_INVALID"),
                ("https://user@app.example", "CORS_ORIGIN_INVALID"),
            )
            for origin, expected in cases:
                with self.subTest(origin=origin):
                    status, raw, headers = self.request(
                        "GET",
                        "/api/portfolio",
                        origin=origin,
                    )
                    self.assertEqual(status, 403)
                    self.assertEqual(self.json_body(raw)["error"]["code"], expected)
                    self.assertNotIn("access-control-allow-origin", headers)

    def test_malformed_host_cannot_fake_same_origin(self) -> None:
        status, raw, headers = self.request(
            "GET",
            "/api/portfolio",
            origin="https://evil.example",
            host="user@evil.example",
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.json_body(raw)["error"]["code"], "CORS_ORIGIN_DENIED")
        self.assertNotIn("access-control-allow-origin", headers)

    def test_preflight_is_exact_and_does_not_grant_actual_private_access(self) -> None:
        status, raw, headers = self.request(
            "OPTIONS",
            "/api/portfolio/profile",
            origin=_ALLOWED_ORIGIN,
            request_method="PUT",
            request_headers="Authorization, Content-Type, Accept",
        )
        self.assertEqual(status, 204)
        self.assertEqual(raw, b"")
        self.assertEqual(headers["access-control-allow-origin"], _ALLOWED_ORIGIN)
        self.assertEqual(
            headers["access-control-allow-methods"],
            "GET, POST, PUT, PATCH, DELETE, OPTIONS",
        )
        self.assertEqual(
            headers["access-control-allow-headers"],
            "Authorization, Content-Type, Accept",
        )
        self.assertEqual(headers["access-control-max-age"], "321")
        self.assertNotEqual(headers["access-control-allow-origin"], "*")

        with mock.patch.dict(
            os.environ,
            {"STOCK_TRACKER_PRIVATE_ACCESS": _STRONG_ACCESS},
            clear=False,
        ):
            status, raw, headers = self.request(
                "GET",
                "/api/portfolio",
                origin=_ALLOWED_ORIGIN,
            )
        self.assertEqual(status, 401)
        self.assertEqual(
            self.json_body(raw)["error"]["code"],
            "PRIVATE_API_AUTH_REQUIRED",
        )
        self.assertEqual(headers["access-control-allow-origin"], _ALLOWED_ORIGIN)

    def test_preflight_rejects_unknown_method_and_headers(self) -> None:
        cases = (
            ({"request_method": "TRACE"}, "CORS_PREFLIGHT_METHOD_DENIED"),
            (
                {
                    "request_method": "GET",
                    "request_headers": "Authorization, X-Unsafe",
                },
                "CORS_PREFLIGHT_HEADERS_DENIED",
            ),
        )
        for kwargs, expected in cases:
            with self.subTest(expected=expected):
                status, raw, headers = self.request(
                    "OPTIONS",
                    "/api/portfolio",
                    origin=_ALLOWED_ORIGIN,
                    **kwargs,
                )
                self.assertEqual(status, 403)
                self.assertEqual(self.json_body(raw)["error"]["code"], expected)
                self.assertEqual(headers["access-control-allow-origin"], _ALLOWED_ORIGIN)

    def test_allowed_origin_private_crud_requires_exact_bearer(self) -> None:
        authorization = "Bearer " + _STRONG_ACCESS
        with mock.patch.dict(
            os.environ,
            {"STOCK_TRACKER_PRIVATE_ACCESS": _STRONG_ACCESS},
            clear=False,
        ):
            status, raw, headers = self.request(
                "POST",
                "/api/portfolio/positions",
                origin=_ALLOWED_ORIGIN,
                authorization=authorization,
                body={
                    "symbol": "600000.SH",
                    "market": "A",
                    "shares": 100,
                    "average_cost": 10.0,
                    "added_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            self.assertEqual(status, 201)
            position_id = self.json_body(raw)["id"]
            self.assertEqual(headers["access-control-allow-origin"], _ALLOWED_ORIGIN)

            status, _, headers = self.request(
                "PATCH",
                f"/api/portfolio/positions/{position_id}",
                origin=_ALLOWED_ORIGIN,
                authorization=authorization,
                body={"shares": 50},
            )
            self.assertEqual(status, 200)
            self.assertEqual(headers["access-control-allow-origin"], _ALLOWED_ORIGIN)

            status, _, headers = self.request(
                "DELETE",
                f"/api/portfolio/positions/{position_id}",
                origin=_ALLOWED_ORIGIN,
                authorization=authorization,
            )
            self.assertEqual(status, 200)
            self.assertEqual(headers["access-control-allow-origin"], _ALLOWED_ORIGIN)

    def test_allowed_origin_sse_has_exact_cors_and_bearer(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        with mock.patch.dict(
            os.environ,
            {"STOCK_TRACKER_PRIVATE_ACCESS": _STRONG_ACCESS},
            clear=False,
        ):
            try:
                connection.putrequest("GET", "/api/stream", skip_host=True)
                connection.putheader("Host", f"127.0.0.1:{self.port}")
                connection.putheader("Origin", _ALLOWED_ORIGIN)
                connection.putheader("Accept", "text/event-stream")
                connection.putheader("Authorization", "Bearer " + _STRONG_ACCESS)
                connection.endheaders()
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                headers = {key.lower(): value for key, value in response.getheaders()}
                self.assertEqual(headers["access-control-allow-origin"], _ALLOWED_ORIGIN)
                self.assertEqual(response.readline(), b": connected\n")
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()

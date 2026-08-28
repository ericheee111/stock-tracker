from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from unittest import mock
from urllib import error as urllib_error
from urllib import request as urllib_request

from sidecars.xtp import official
from sidecars.xtp.contracts import (
    ENV_SIDECAR_ACCESS,
    EventEnvelope,
    XtpSidecarContractError,
    canonical_json_bytes,
    strict_json_loads,
    trading_day_for,
)
from sidecars.xtp.runtime import SidecarRuntime, SimulatorBackend
from sidecars.xtp.server import XtpSidecarHTTPServer
from stock_tracker.collector.xtp_sidecar import (
    XtpSidecarClient,
    XtpSidecarClientError,
    load_xtp_sidecar_config,
)

ROOT = Path(__file__).resolve().parents[1]
_ACCESS = "test-sidecar-access-" + ("x" * 40)


class TestXtpConfigurationAndAbi(unittest.TestCase):
    def test_committed_config_is_disabled_read_only_and_secret_free(self) -> None:
        path = ROOT / "config" / "xtp_sidecar.toml"
        text = path.read_text(encoding="utf-8")
        config = load_xtp_sidecar_config(path)
        self.assertFalse(config.enabled)
        self.assertEqual(config.backend, "simulator")
        self.assertEqual(config.bind_host, "127.0.0.1")
        self.assertLessEqual(config.max_symbols, 20)
        self.assertEqual(config.expected_api_version, "2.2.50.8")
        self.assertEqual(config.expected_python_series, "3.9")
        self.assertTrue(config.read_only)
        self.assertFalse(config.allow_live_decision)
        self.assertFalse(config.allow_model_training)
        self.assertFalse(config.allow_public_redistribution)
        self.assertFalse(config.auto_trade)
        for forbidden in (
            "quote_user =",
            "quote_access =",
            "quote_server =",
            "client_id =",
            "sidecar_access =",
            "algorithm_account =",
        ):
            self.assertNotIn(forbidden, text.lower())

    def test_official_loader_requires_python39_and_never_imports_trader(self) -> None:
        with (
            mock.patch.object(official, "_runtime_python_series", return_value=(3, 14)),
            self.assertRaisesRegex(XtpSidecarContractError, "CPython 3.9"),
        ):
            official.load_quote_module()
        with mock.patch.object(official, "_runtime_python_series", return_value=(3, 9)):
            for name in ("xtptraderapi", "xtp_algo_api", "order_gateway"):
                with self.subTest(name=name), self.assertRaisesRegex(
                    XtpSidecarContractError,
                    "quote module",
                ):
                    official.load_quote_module(name)
        source = (ROOT / "sidecars" / "xtp" / "official.py").read_text(encoding="utf-8")
        self.assertNotIn("import xtptrader", source.lower())
        self.assertNotIn("create_order", source.lower())
        self.assertNotIn("insert_order", source.lower())

    def test_official_capability_probe_fails_closed_on_unsafe_or_missing_surface(self) -> None:
        safe = ModuleType("xtpquoteapi")
        safe.__file__ = "fixture/xtpquoteapi.pyd"
        safe.QuoteApi = object()
        safe.OrderBook = object()
        capabilities = official.quote_module_capabilities(safe)
        self.assertTrue(capabilities["quote_factory_present"])
        self.assertFalse(capabilities["forbidden_surface_detected"])

        unsafe = ModuleType("xtpquoteapi")
        unsafe.__file__ = "fixture/xtpquoteapi.pyd"
        unsafe.QuoteApi = object()
        unsafe.insertOrder = object()
        with self.assertRaisesRegex(XtpSidecarContractError, "forbidden"):
            official.quote_module_capabilities(unsafe)

        missing = ModuleType("xtpquoteapi")
        missing.__file__ = "fixture/xtpquoteapi.pyd"
        with self.assertRaisesRegex(XtpSidecarContractError, "quote factory"):
            official.quote_module_capabilities(missing)

    def test_official_callback_bridge_enforces_price_and_cumulative_units(self) -> None:
        runtime = SidecarRuntime(["600519.SH"], backend="xtp")
        bridge = official.QuoteCallbackBridge(runtime, feed_mode="LEVEL1")
        now = datetime.now(timezone.utc)
        raw = {
            "last": 100.0,
            "open": 99.0,
            "high": 101.0,
            "low": 98.0,
            "prev_close": 99.5,
            "volume": 1000,
            "amount": 100000.0,
        }
        event = bridge.on_market_data(
            raw,
            symbol="600519.SH",
            provider_timestamp=now - timedelta(milliseconds=5),
            received_at=now,
        )
        self.assertEqual(event.payload["volume"], 1000)
        self.assertEqual(event.payload["amount"], 100000.0)

        cases = (
            ("volume", -1, "non-negative"),
            ("volume", 1.5, "integer"),
            ("amount", -1.0, "non-negative"),
            ("last", float("nan"), "finite"),
        )
        for field, value, message in cases:
            malformed = dict(raw)
            malformed[field] = value
            with self.subTest(field=field, value=value), self.assertRaisesRegex(
                XtpSidecarContractError,
                message,
            ):
                bridge.on_market_data(
                    malformed,
                    symbol="600519.SH",
                    provider_timestamp=now - timedelta(milliseconds=5),
                    received_at=now,
                )

        bridge.on_disconnected("account=fixture-secret")
        metrics = runtime.metrics()
        self.assertEqual(metrics["last_error_code"], "XTP_DISCONNECTED")
        self.assertNotIn("fixture-secret", json.dumps(metrics, sort_keys=True))
        bridge.on_disconnected(42)
        self.assertEqual(runtime.metrics()["last_error_code"], "XTP_DISCONNECTED_42")

    def test_official_environment_repr_and_safe_dict_hide_connection_values(self) -> None:
        environment = official.OfficialQuoteEnvironment(
            user_present=True,
            credential_present=True,
            server="198.51.100.24",
            port=6001,
            client_id=17,
        )
        rendered = repr(environment)
        safe = json.dumps(environment.as_safe_dict(), sort_keys=True)
        for value in ("198.51.100.24", "port=6001", "client_id=17"):
            self.assertNotIn(value, rendered)
        self.assertNotIn("198.51.100.24", safe)
        self.assertNotIn('"port": 6001', safe)
        self.assertNotIn('"client_id": 17', safe)
        self.assertEqual(environment.as_safe_dict()["protocol"], "TCP")
        self.assertFalse(environment.as_safe_dict()["contains_account_value"])
        self.assertFalse(environment.as_safe_dict()["contains_server_value"])

    def test_official_environment_requires_tcp_without_exposing_connection_values(self) -> None:
        fixture_values = (
            "fixture-user",
            "fixture-password-value",
            "198.51.100.24",
            "6001",
            "TCP",
            "17",
        )
        with mock.patch.object(
            official,
            "_visible_environment",
            side_effect=fixture_values,
        ):
            environment = official.OfficialQuoteEnvironment.from_environ()
        safe = environment.as_safe_dict()
        rendered = repr(environment) + json.dumps(safe, sort_keys=True)
        self.assertTrue(environment.user_present)
        self.assertTrue(environment.credential_present)
        self.assertEqual(environment.protocol, "TCP")
        self.assertEqual(safe["protocol"], "TCP")
        for value in fixture_values[:4] + fixture_values[5:]:
            self.assertNotIn(value, rendered)

        with (
            mock.patch.object(
                official,
                "_visible_environment",
                side_effect=(*fixture_values[:4], "UDP", fixture_values[5]),
            ),
            self.assertRaisesRegex(XtpSidecarContractError, "requires TCP"),
        ):
            official.OfficialQuoteEnvironment.from_environ()

    def test_invalid_config_cannot_enable_trading_or_public_bind(self) -> None:
        source = (ROOT / "config" / "xtp_sidecar.toml").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "xtp.toml"
            for old, new in (
                ('bind_host = "127.0.0.1"', 'bind_host = "0.0.0.0"'),
                ('bind_host = "127.0.0.1"', 'bind_host = "::1"'),
                ("read_only = true", "read_only = false"),
                ("auto_trade = false", "auto_trade = true"),
                ("allow_live_decision = false", "allow_live_decision = true"),
                ("max_symbols = 20", "max_symbols = 21"),
                (
                    'metadata_db = "data/market_events.db"',
                    'metadata_db = "data/stock_tracker.db"',
                ),
                (
                    'database = "data/monitor.db"',
                    'database = "data/stock_tracker.db"',
                ),
                (
                    'database = "data/monitor.db"',
                    'database = "data/market_events.db"',
                ),
                (
                    'quarantine_root = "data/market-events-quarantine"',
                    'quarantine_root = "data/market-events"',
                ),
            ):
                path.write_text(source.replace(old, new, 1), encoding="utf-8")
                with self.subTest(replacement=(old, new)), self.assertRaises(
                    XtpSidecarClientError
                ):
                    load_xtp_sidecar_config(path)


class TestXtpEventContract(unittest.TestCase):
    def _event(self, **overrides) -> EventEnvelope:
        now = datetime.now(timezone.utc)
        values = {
            "feed_mode": "SIMULATOR",
            "symbol": "600519.SH",
            "event_type": "MARKET_DATA",
            "trading_day": trading_day_for(now - timedelta(milliseconds=8)),
            "exchange_timestamp": now - timedelta(milliseconds=8),
            "provider_timestamp": now - timedelta(milliseconds=7),
            "received_at": now,
            "session_id": "session-fixture",
            "callback_seq": 1,
            "provider_seq": 10,
            "payload": {
                "last": 100.0,
                "open": 99.0,
                "high": 101.0,
                "low": 98.0,
                "prev_close": 99.5,
                "volume": 1000,
                "amount": 100000.0,
            },
        }
        values.update(overrides)
        return EventEnvelope.create(**values)

    def test_round_trip_and_identity_are_tamper_evident(self) -> None:
        event = self._event()
        restored = EventEnvelope.from_dict(event.as_dict())
        self.assertEqual(restored, event)
        tampered = event.as_dict()
        tampered["payload"]["last"] = 101.0
        with self.assertRaisesRegex(XtpSidecarContractError, "hash mismatch"):
            EventEnvelope.from_dict(tampered)
        tampered = event.as_dict()
        tampered["callback_seq"] = 2
        with self.assertRaisesRegex(XtpSidecarContractError, "event_id mismatch"):
            EventEnvelope.from_dict(tampered)
        with self.assertRaisesRegex(XtpSidecarContractError, "event_id mismatch"):
            replace(event, callback_seq=2)

    def test_payload_is_deeply_isolated_and_revalidated_at_runtime_boundary(self) -> None:
        payload = {
            "last": 100.0,
            "open": 99.0,
            "high": 101.0,
            "low": 98.0,
            "prev_close": 99.5,
            "volume": 1000,
            "amount": 100000.0,
            "levels": [{"price": 100.0, "volume": 10}],
        }
        event = self._event(payload=payload)
        payload["levels"][0]["price"] = 999.0
        self.assertEqual(event.payload["levels"][0]["price"], 100.0)

        exported = event.as_dict()
        exported["levels"] = []
        exported["payload"]["levels"][0]["price"] = 888.0
        self.assertEqual(event.payload["levels"][0]["price"], 100.0)

        event.payload["last"] = 101.0
        runtime = SidecarRuntime(["600519.SH"], backend="simulator")
        with self.assertRaisesRegex(XtpSidecarContractError, "hash mismatch"):
            runtime.append(event)

        mismatched_feed = self._event(
            session_id=runtime.session_id,
            feed_mode="LEVEL1",
        )
        with self.assertRaisesRegex(XtpSidecarContractError, "feed_mode"):
            runtime.append(mismatched_feed)

    def test_trading_day_and_signed_integer_bounds_fail_closed(self) -> None:
        with self.assertRaisesRegex(XtpSidecarContractError, "trading_day"):
            self._event(trading_day="2000-01-01")
        with self.assertRaisesRegex(XtpSidecarContractError, "signed 64-bit"):
            self._event(payload={"value": 1 << 63})
        with self.assertRaisesRegex(XtpSidecarContractError, "object key"):
            canonical_json_bytes({"bad\nkey": 1})

    def test_duplicate_keys_nonfinite_bool_sequence_and_future_time_fail(self) -> None:
        with self.assertRaisesRegex(XtpSidecarContractError, "duplicate"):
            strict_json_loads(b'{"a":1,"a":2}')
        with self.assertRaisesRegex(XtpSidecarContractError, "non-finite"):
            strict_json_loads(b'{"a":NaN}')
        with self.assertRaises(XtpSidecarContractError):
            self._event(callback_seq=True)
        with self.assertRaises(XtpSidecarContractError):
            self._event(received_at=datetime.now(timezone.utc) + timedelta(minutes=10))
        with self.assertRaises(XtpSidecarContractError):
            self._event(symbol="AAPL.US")

    def test_canonical_json_refuses_nonfinite_and_unsupported_objects(self) -> None:
        with self.assertRaises(XtpSidecarContractError):
            canonical_json_bytes({"x": float("inf")})
        with self.assertRaises(XtpSidecarContractError):
            canonical_json_bytes({"x": object()})


class TestXtpSidecarHttp(unittest.TestCase):
    def setUp(self) -> None:
        self.base = load_xtp_sidecar_config(ROOT / "config" / "xtp_sidecar.toml")
        self.runtime = SidecarRuntime(
            ["600519.SH", "000001.SZ"],
            backend="simulator",
        )
        self.backend = SimulatorBackend(self.runtime, interval_sec=0.02)
        self.server = XtpSidecarHTTPServer(
            "127.0.0.1",
            0,
            self.runtime,
            access_value=_ACCESS,
            health_public=True,
        )
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.backend.start()
        self.thread.start()
        self.config = replace(self.base, enabled=True, bind_port=self.port)
        self.client = XtpSidecarClient(
            self.config,
            access_provider=lambda: _ACCESS,
        )

    def tearDown(self) -> None:
        self.server.shutdown()
        self.backend.stop()
        self.server.server_close()
        self.thread.join(timeout=2.0)

    def test_public_health_private_endpoints_and_get_only_boundary(self) -> None:
        health = self.client.health()
        self.assertTrue(health["read_only"])
        self.assertFalse(health["auto_trade"])
        session = self.client.session()
        self.assertEqual(session["algorithm_account_used"], False)
        self.assertIn("callback_count", self.client.metrics())
        events, cursor, _ = self.client.events(
            limit=100,
            expected_session_id=session["session_id"],
            expected_feed_mode=session["feed_mode"],
            expected_symbols=tuple(session["symbols"]),
        )
        self.assertTrue(events)
        self.assertGreater(cursor, 0)

        origin = f"http://127.0.0.1:{self.port}"
        with urllib_request.urlopen(origin + "/v1/health", timeout=2) as response:
            self.assertEqual(response.status, 200)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            connection.request("GET", origin + "/v1/health")
            absolute_response = connection.getresponse()
            absolute_response.read()
        finally:
            connection.close()
        self.assertEqual(absolute_response.status, 400)
        with self.assertRaises(urllib_error.HTTPError) as unauthorized:
            urllib_request.urlopen(origin + "/v1/session", timeout=2)
        self.assertEqual(unauthorized.exception.code, 401)
        request = urllib_request.Request(origin + "/v1/events", data=b"{}", method="POST")
        with self.assertRaises(urllib_error.HTTPError) as read_only:
            urllib_request.urlopen(request, timeout=2)
        self.assertEqual(read_only.exception.code, 405)

    def test_wrong_access_redirect_proxy_and_unbounded_cursor_fail(self) -> None:
        wrong = XtpSidecarClient(self.config, access_provider=lambda: "z" * 40)
        with self.assertRaisesRegex(XtpSidecarClientError, "HTTP 401"):
            wrong.session()
        with self.assertRaises(XtpSidecarClientError):
            self.client.events(after=-1)
        with self.assertRaises(XtpSidecarClientError):
            self.client.events(limit=501)
        with self.assertRaisesRegex(XtpSidecarClientError, "session changed"):
            self.client.events(expected_session_id="different-session")
        with self.assertRaisesRegex(XtpSidecarClientError, "expected_feed_mode"):
            self.client.events(expected_feed_mode="INVALID")
        with self.assertRaisesRegex(XtpSidecarClientError, "expected_symbols"):
            self.client.events(expected_symbols=("600519.SH", "600519.SH"))

        now = datetime.now(timezone.utc)
        mismatched_feed = EventEnvelope.create(
            feed_mode="LEVEL1",
            symbol="600519.SH",
            event_type="MARKET_DATA",
            trading_day=trading_day_for(now),
            received_at=now,
            session_id=self.runtime.session_id,
            callback_seq=1,
            payload={"last": 100.0},
        )
        response = {
            "schema": "stock-tracker-xtp-events-response-v1",
            "session_id": self.runtime.session_id,
            "after": 0,
            "oldest_cursor": 1,
            "next_cursor": 1,
            "cursor_lost": False,
            "has_more": False,
            "events": [mismatched_feed.as_dict()],
        }
        with (
            mock.patch.object(self.client, "_request", return_value=response),
            self.assertRaisesRegex(XtpSidecarClientError, "feed_mode"),
        ):
            self.client.events(expected_feed_mode="SIMULATOR")

        unsubscribed = EventEnvelope.create(
            feed_mode="SIMULATOR",
            symbol="000001.SZ",
            event_type="MARKET_DATA",
            trading_day=trading_day_for(now),
            received_at=now,
            session_id=self.runtime.session_id,
            callback_seq=1,
            payload={"last": 100.0},
        )
        response["events"] = [unsubscribed.as_dict()]
        with (
            mock.patch.object(self.client, "_request", return_value=response),
            self.assertRaisesRegex(XtpSidecarClientError, "session subscription"),
        ):
            self.client.events(expected_symbols=("600519.SH",))

        mismatched_backend = XtpSidecarClient(
            replace(self.config, backend="xtp"),
            access_provider=lambda: _ACCESS,
        )
        with self.assertRaisesRegex(XtpSidecarClientError, "backend"):
            mismatched_backend.health()

        class ChangedUrlResponse:
            status = 200

            def __init__(self) -> None:
                self.headers = {"Content-Type": "application/json"}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback) -> None:
                del exc_type, exc, traceback

            def geturl(self) -> str:
                return "http://127.0.0.1:1/v1/health"

            def read(self, limit: int) -> bytes:
                del limit
                return b"{}"

        class ChangedUrlOpener:
            @staticmethod
            def open(request, timeout):
                del request, timeout
                return ChangedUrlResponse()

        changed_url_client = XtpSidecarClient(
            self.config,
            access_provider=lambda: _ACCESS,
            opener=ChangedUrlOpener(),
        )
        with self.assertRaisesRegex(XtpSidecarClientError, "URL changed"):
            changed_url_client.health()

        origin = f"http://127.0.0.1:{self.port}"
        request = urllib_request.Request(
            origin + "/v1/events?a=1&b=2&c=3&d=4&e=5",
            headers={"Authorization": "Bearer " + _ACCESS},
            method="GET",
        )
        with self.assertRaises(urllib_error.HTTPError) as too_many_fields:
            urllib_request.urlopen(request, timeout=2)
        self.assertEqual(too_many_fields.exception.code, 400)
        payload = json.loads(too_many_fields.exception.read().decode("utf-8"))
        self.assertEqual(payload["error"]["code"], "TOO_MANY_QUERY_FIELDS")

    def test_reconnect_and_sequence_metrics_are_honest(self) -> None:
        before = self.runtime.session()
        with self.assertRaisesRegex(XtpSidecarContractError, "feed mode"):
            self.runtime.mark_connected(feed_mode="INVALID")
        self.assertEqual(self.runtime.session(), before)

        self.backend.inject_reconnect()
        metrics = self.client.metrics()
        self.assertGreaterEqual(metrics["reconnect_count"], 1)
        self.assertGreaterEqual(metrics["disconnect_count"], 1)
        self.assertEqual(metrics["callback_gap_count"], 0)
        self.assertEqual(metrics["provider_gap_count"], 0)

    def test_metadata_timestamps_and_error_pair_fail_closed(self) -> None:
        health = self.runtime.health()
        health["started_at"] = "not-a-time"
        with (
            mock.patch.object(self.client, "_request", return_value=health),
            self.assertRaisesRegex(XtpSidecarClientError, "started_at"),
        ):
            self.client.health()

        session = self.runtime.session()
        session["connected_at"] = None
        with (
            mock.patch.object(self.client, "_request", return_value=session),
            self.assertRaisesRegex(XtpSidecarClientError, "connected_at"),
        ):
            self.client.session()

        metrics = self.runtime.metrics()
        metrics["last_error_at"] = datetime.now(timezone.utc).isoformat()
        with (
            mock.patch.object(self.client, "_request", return_value=metrics),
            self.assertRaisesRegex(XtpSidecarClientError, "requires last_error_code"),
        ):
            self.client.metrics()

    def test_access_value_is_not_serialized(self) -> None:
        rendered = json.dumps(
            {
                "health": self.client.health(),
                "session": self.client.session(),
                "metrics": self.client.metrics(),
            },
            ensure_ascii=False,
        )
        self.assertNotIn(_ACCESS, rendered)
        self.assertNotIn(os.environ.get(ENV_SIDECAR_ACCESS, "not-configured"), rendered)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import http.client
import json
import os
import queue
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlencode

from stock_tracker.api.audit import RemoteAuditLogger
from stock_tracker.api.handlers import AppContext
from stock_tracker.api.server import APIServer
from stock_tracker.api.sse import SSE_OVERFLOW_TOPIC, SSEHub
from stock_tracker.collector.xtp_sidecar import load_xtp_sidecar_config
from stock_tracker.core.config import load_configs
from stock_tracker.core.eventbus import EventBus
from stock_tracker.core.store import MarketStore
from stock_tracker.monitor import (
    MonitorCondition,
    MonitorExpression,
    MonitorRule,
    MonitorScope,
    MonitorService,
    MonitorSeverity,
    RuleLogic,
    RuleOperator,
    ScopeKind,
)
from stock_tracker.storage.db import close_all
from stock_tracker.storage.repository import Repository

ROOT = Path(__file__).resolve().parents[1]
_PRIVATE_ACCESS = "monitor-private-access-" + ("x" * 40)


def _rule_payload(rule_id: str = "api-latency") -> dict:
    now = datetime.now(timezone.utc)
    rule = MonitorRule(
        rule_id=rule_id,
        name="<b>链路延迟</b>",
        expression=MonitorExpression(
            RuleLogic.AND,
            (
                MonitorCondition(
                    "market_event.latency_p95_ms",
                    RuleOperator.GE,
                    10.0,
                ),
                MonitorCondition(
                    "market_event.connection_state",
                    RuleOperator.EQ,
                    "CONNECTED",
                ),
            ),
        ),
        scope=MonitorScope(
            ScopeKind.SYMBOLS,
            symbols=("600519.SH",),
            max_symbols=1,
        ),
        severity=MonitorSeverity.WARNING,
        cooldown_sec=0,
        duplicate_window_sec=0,
        notification_channels=("BROWSER",),
        created_at=now,
        updated_at=now,
    )
    payload = rule.as_dict()
    payload.pop("created_at")
    payload.pop("updated_at")
    return payload


class TestMonitorAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        bundle = load_configs(str(ROOT / "config"))
        bundle.app.runtime.cors_allowed_origins = ["https://ui.example"]
        repo = Repository(str(cls.root / "runtime.sqlite3"))
        sidecar = load_xtp_sidecar_config(ROOT / "config" / "xtp_sidecar.toml")
        sidecar = replace(
            sidecar,
            event_root="events",
            metadata_db="events/catalog.sqlite3",
            quarantine_root="quarantine",
            monitor_db="monitor.sqlite3",
        )
        bus = EventBus()
        cls.bus = bus
        monitor = MonitorService(
            sidecar,
            project_root=cls.root,
            publisher=bus.publish,
        )
        cls.ctx = AppContext(
            bundle=bundle,
            store=MarketStore(),
            repo=repo,
            router=SimpleNamespace(health_list=list),
            signal_manager=None,
            sse_hub=SSEHub(bus),
            web_root=str(cls.root),
            monitor_service=monitor,
        )
        cls.audit_path = cls.root / "remote-monitor-audit.jsonl"
        cls.audit = RemoteAuditLogger(
            cls.audit_path,
            enabled=True,
            max_bytes=1024 * 1024,
            backup_count=1,
        )
        cls.audit.ensure_ready()
        cls.server = APIServer(
            "127.0.0.1",
            0,
            cls.ctx,
            None,
            audit_logger=cls.audit,
        )
        cls.port = int(cls.server.server_address[1])
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown_wait()
        cls.thread.join(timeout=5)
        close_all()
        cls.temp.cleanup()

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict | bytes | None = None,
        remote: bool = False,
        authorization: str | None = None,
        origin: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict | None, dict[str, str]]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.putrequest(method, path, skip_host=True)
            connection.putheader(
                "Host",
                "monitor.example" if remote else f"127.0.0.1:{self.port}",
            )
            connection.putheader("Accept", "application/json")
            if authorization is not None:
                connection.putheader("Authorization", authorization)
            if origin is not None:
                connection.putheader("Origin", origin)
            raw: bytes | None
            if isinstance(body, dict):
                raw = json.dumps(
                    body,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            else:
                raw = body
            if raw is not None:
                connection.putheader("Content-Type", "application/json")
                connection.putheader("Content-Length", str(len(raw)))
            for key, value in (headers or {}).items():
                connection.putheader(key, value)
            connection.endheaders(raw)
            response = connection.getresponse()
            response_raw = response.read()
            response_headers = {
                key.lower(): value for key, value in response.getheaders()
            }
            payload = json.loads(response_raw.decode("utf-8")) if response_raw else None
            return response.status, payload, response_headers
        finally:
            connection.close()

    def test_rule_crud_summary_and_strict_json(self) -> None:
        payload = _rule_payload("api-crud")
        payload["version"] = 999999
        status, body, headers = self.request(
            "POST",
            "/api/monitor/rules",
            body=payload,
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["rule"]["rule_id"], "api-crud")
        self.assertEqual(body["rule"]["version"], 1)
        self.assertIn("x-request-id", headers)
        self.assertEqual(headers["cache-control"], "no-store")

        status, body, _ = self.request("GET", "/api/monitor/rules")
        self.assertEqual(status, 200)
        self.assertTrue(any(item["rule_id"] == "api-crud" for item in body["rules"]))

        payload["name"] = "更新后的规则"
        status, body, _ = self.request(
            "PUT",
            "/api/monitor/rules/api-crud",
            body=payload,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["rule"]["name"], "更新后的规则")
        self.assertEqual(body["rule"]["version"], 2)

        duplicate = b'{"rule_id":"one","rule_id":"two"}'
        status, body, _ = self.request(
            "POST",
            "/api/monitor/rules",
            body=duplicate,
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_JSON")

        status, body, _ = self.request(
            "GET",
            "/api/monitor/summary?unexpected=1",
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "UNKNOWN_QUERY_FIELD")

        too_many_query_fields = "&".join(f"field{index}=1" for index in range(17))
        status, body, _ = self.request(
            "GET",
            "/api/monitor/inbox?" + too_many_query_fields,
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "TOO_MANY_QUERY_FIELDS")

        status, body, _ = self.request(
            "DELETE",
            "/api/monitor/rules/api-crud",
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["removed"])

    def test_inbox_transition_and_replay_are_private_contracts(self) -> None:
        payload = _rule_payload("api-inbox")
        status, _, _ = self.request("POST", "/api/monitor/rules", body=payload)
        self.assertEqual(status, 201)
        rule = self.ctx.monitor_service.repository.get_rule("api-inbox")
        evaluation = self.ctx.monitor_service.engine.evaluate_rule(
            rule,
            symbol="600519.SH",
            market="A",
            facts={
                "action_state": "WATCH",
                "signal_state": "WATCH",
                "data_status": "DELAYED",
                "data_quality": {"status": "DEGRADED", "score": 60.0},
                "blocker_codes": [],
                "market_regime": {"state": "ROTATION", "score": 50.0},
                "features": {},
                "market_event": {
                    "connection_state": "CONNECTED",
                    "feed_mode": "SIMULATOR",
                    "latency_p50_ms": 8.0,
                    "latency_p95_ms": 15.0,
                    "duplicate_count": 0,
                    "callback_gap_count": 0,
                    "provider_gap_count": 0,
                    "out_of_order_count": 0,
                    "ingestion_lag_ms": 12,
                    "last_price": 10.0,
                    "change_pct": 0.0,
                },
            },
        )
        inbox_id = evaluation.inbox["inbox_id"]
        status, body, _ = self.request(
            "POST",
            f"/api/monitor/inbox/{inbox_id}/transition",
            body={"state": "ACKNOWLEDGED", "reason": "API review"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["inbox"]["state"], "ACKNOWLEDGED")

        status, body, _ = self.request(
            "GET",
            "/api/monitor/inbox?state=ACKNOWLEDGED&limit=10",
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["inbox"][0]["inbox_id"], inbox_id)

        query = urlencode(
            {
                "symbol": "600519.SH",
                "start": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
                "end": datetime.now(timezone.utc).isoformat(),
                "backend": "python",
                "limit": 100,
            }
        )
        with closing(
            sqlite3.connect(self.ctx.monitor_service.event_store.metadata_db)
        ) as connection:
            replay_runs_before = int(
                connection.execute("SELECT COUNT(*) FROM replay_runs").fetchone()[0]
            )
        status, body, _ = self.request("GET", "/api/monitor/replay?" + query)
        self.assertEqual(status, 200)
        self.assertEqual(body["row_count"], 0)
        self.assertFalse(body["production_database_modified"])
        with closing(
            sqlite3.connect(self.ctx.monitor_service.event_store.metadata_db)
        ) as connection:
            replay_runs_after = int(
                connection.execute("SELECT COUNT(*) FROM replay_runs").fetchone()[0]
            )
        self.assertEqual(replay_runs_after, replay_runs_before)

        oversized_query = urlencode(
            {
                "symbol": "600519.SH",
                "start": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
                "end": datetime.now(timezone.utc).isoformat(),
                "backend": "python",
                "limit": 5001,
            }
        )
        status, body, _ = self.request(
            "GET",
            "/api/monitor/replay?" + oversized_query,
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_QUERY_FIELD")

    def test_remote_auth_cors_and_metadata_only_audit(self) -> None:
        payload = _rule_payload("remote-rule")
        with mock.patch.dict(
            os.environ,
            {"STOCK_TRACKER_PRIVATE_ACCESS": _PRIVATE_ACCESS},
            clear=False,
        ):
            status, body, _ = self.request(
                "POST",
                "/api/monitor/rules",
                body=payload,
                remote=True,
                origin="https://ui.example",
            )
            self.assertEqual(status, 401)
            self.assertEqual(body["error"]["code"], "PRIVATE_API_AUTH_REQUIRED")

            status, _, headers = self.request(
                "OPTIONS",
                "/api/monitor/rules",
                remote=True,
                origin="https://ui.example",
                headers={
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "Authorization, Content-Type",
                },
            )
            self.assertEqual(status, 204)
            self.assertEqual(
                headers["access-control-allow-origin"],
                "https://ui.example",
            )

            status, body, headers = self.request(
                "POST",
                "/api/monitor/rules",
                body=payload,
                remote=True,
                origin="https://ui.example",
                authorization="Bearer " + _PRIVATE_ACCESS,
            )
        self.assertEqual(status, 201)
        self.assertEqual(headers["access-control-allow-origin"], "https://ui.example")
        self.assertNotEqual(headers["access-control-allow-origin"], "*")
        audit_text = self.audit_path.read_text(encoding="utf-8")
        self.assertIn('/api/monitor/rules', audit_text)
        self.assertNotIn("remote-rule", audit_text)
        self.assertNotIn("600519.SH", audit_text)
        self.assertNotIn(_PRIVATE_ACCESS, audit_text)

    def test_sse_hub_forwards_monitor_topics_but_not_internal_monitor_facts(self) -> None:
        events: queue.Queue = queue.Queue()
        self.ctx.sse_hub.add_client(events)
        try:
            inbox_payload = {"schema": "fixture-monitor-inbox", "inbox": {"id": "one"}}
            self.bus.publish("monitor.inbox", inbox_payload)
            topic, payload = events.get(timeout=1)
            self.assertEqual((topic, payload), ("monitor.inbox", inbox_payload))

            notification_payload = {
                "schema": "fixture-monitor-notification",
                "payload": {"id": "two"},
            }
            self.bus.publish("monitor.notification", notification_payload)
            topic, payload = events.get(timeout=1)
            self.assertEqual(
                (topic, payload),
                ("monitor.notification", notification_payload),
            )

            self.bus.publish(
                "monitor_facts",
                {"schema": "internal-monitor-facts", "symbol": "600519.SH"},
            )
            with self.assertRaises(queue.Empty):
                events.get(timeout=0.05)
        finally:
            self.ctx.sse_hub.remove_client(events)

    def test_sse_hub_disconnects_a_slow_client_on_bounded_queue_overflow(self) -> None:
        events: queue.Queue = queue.Queue(maxsize=1)
        self.ctx.sse_hub.add_client(events)
        self.bus.publish("monitor.inbox", {"id": "first"})
        self.bus.publish("monitor.inbox", {"id": "second"})

        topic, payload = events.get(timeout=1)
        self.assertEqual(topic, SSE_OVERFLOW_TOPIC)
        self.assertEqual(payload, {})
        self.assertEqual(self.ctx.sse_hub.client_count(), 0)

        self.bus.publish("monitor.inbox", {"id": "third"})
        with self.assertRaises(queue.Empty):
            events.get(timeout=0.05)

    def test_monitor_data_link_never_exposes_account_or_access_values(self) -> None:
        status, body, _ = self.request("GET", "/api/monitor/data-link")
        self.assertEqual(status, 200)
        rendered = json.dumps(body, ensure_ascii=False)
        self.assertEqual(body["status"], "DISABLED")
        self.assertFalse(body["contains_account_value"])
        self.assertFalse(body["contains_sidecar_access"])
        self.assertNotIn("QUOTE_ACCESS", rendered)
        self.assertNotIn("QUOTE_PASSWORD", rendered)
        self.assertNotIn("SIDECAR_ACCESS", rendered)


if __name__ == "__main__":
    unittest.main()

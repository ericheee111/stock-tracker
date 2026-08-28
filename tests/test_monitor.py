from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stock_tracker.collector.xtp_sidecar import load_xtp_sidecar_config
from stock_tracker.monitor import (
    InboxState,
    MonitorCondition,
    MonitorEngine,
    MonitorExpression,
    MonitorRepository,
    MonitorRule,
    MonitorScope,
    MonitorService,
    MonitorSeverity,
    MonitorValidationError,
    RuleLogic,
    RuleOperator,
    ScopeKind,
)
from stock_tracker.monitor.notifications import (
    NotificationDeliveryError,
    NotificationDispatcher,
)
from stock_tracker.monitor.repository import MonitorRepositoryError
from stock_tracker.monitor.service import MonitorServiceError

ROOT = Path(__file__).resolve().parents[1]
_BASE_TIME = datetime(2026, 8, 27, 2, 0, tzinfo=timezone.utc)


def _facts(**overrides):
    value = {
        "action_state": "WATCH",
        "signal_state": "WATCH",
        "data_status": "DELAYED",
        "data_quality": {"status": "DEGRADED", "score": 72.0},
        "blocker_codes": ["XTP_NOT_PROMOTED_TO_LIVE_DECISION"],
        "market_regime": {"state": "ROTATION", "score": 55.0},
        "features": {
            "trend_strength": 64.0,
            "momentum": 51.0,
            "volatility": 22.0,
            "volume_ratio": 1.4,
            "crowding": 37.0,
            "breadth": 58.0,
        },
        "market_event": {
            "connection_state": "CONNECTED",
            "feed_mode": "SIMULATOR",
            "latency_p50_ms": 8.0,
            "latency_p95_ms": 15.0,
            "duplicate_count": 0,
            "callback_gap_count": 0,
            "provider_gap_count": 0,
            "out_of_order_count": 0,
            "ingestion_lag_ms": 20,
            "last_price": 10.2,
            "change_pct": 1.2,
        },
    }
    value.update(overrides)
    return value


def _rule(
    *,
    rule_id: str = "latency-watch",
    logic: RuleLogic = RuleLogic.AND,
    conditions: tuple[MonitorCondition, ...] | None = None,
    scope: MonitorScope | None = None,
    cooldown_sec: int = 60,
    duplicate_window_sec: int = 120,
    channels: tuple[str, ...] = ("BROWSER",),
    expires_at: datetime | None = None,
) -> MonitorRule:
    return MonitorRule(
        rule_id=rule_id,
        name="链路延迟观察",
        expression=MonitorExpression(
            logic=logic,
            conditions=conditions
            or (
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
        scope=scope
        or MonitorScope(
            ScopeKind.SYMBOLS,
            symbols=("600519.SH",),
            max_symbols=1,
        ),
        severity=MonitorSeverity.WARNING,
        enabled=True,
        cooldown_sec=cooldown_sec,
        duplicate_window_sec=duplicate_window_sec,
        expires_at=expires_at,
        notification_channels=channels,
        created_at=_BASE_TIME,
        updated_at=_BASE_TIME,
    )


class TestMonitorContracts(unittest.TestCase):
    def test_rule_round_trip_is_strict_and_non_eval(self) -> None:
        rule = _rule()
        restored = MonitorRule.from_dict(rule.as_dict())
        self.assertEqual(restored, rule)
        malformed = rule.as_dict()
        malformed["expression"]["conditions"][0]["fact"] = "__import__('os').system"
        with self.assertRaisesRegex(MonitorValidationError, "unsupported monitor fact"):
            MonitorRule.from_dict(malformed)
        malformed = rule.as_dict()
        malformed["expression"]["conditions"][0]["operator"] = "EVAL"
        with self.assertRaisesRegex(MonitorValidationError, "unsupported condition operator"):
            MonitorRule.from_dict(malformed)
        malformed = rule.as_dict()
        malformed["unexpected"] = True
        with self.assertRaisesRegex(MonitorValidationError, "invalid field set"):
            MonitorRule.from_dict(malformed)

    def test_broad_scope_requires_acknowledgement_and_bound(self) -> None:
        with self.assertRaisesRegex(MonitorValidationError, "explicit acknowledgement"):
            MonitorScope(
                ScopeKind.ALL_MARKET,
                max_symbols=100,
                all_market_acknowledged=False,
            )
        with self.assertRaisesRegex(MonitorValidationError, "between 1 and 5000"):
            MonitorScope(
                ScopeKind.MARKET,
                max_symbols=5001,
                all_market_acknowledged=True,
            )
        scope = MonitorScope(
            ScopeKind.MARKET,
            max_symbols=20,
            all_market_acknowledged=True,
        )
        self.assertEqual(scope.as_dict()["max_symbols"], 20)

    def test_in_contains_and_numeric_operators_reject_bool_confusion(self) -> None:
        expression = MonitorExpression(
            RuleLogic.AND,
            (
                MonitorCondition("blocker_codes", RuleOperator.CONTAINS, "X"),
                MonitorCondition("signal_state", RuleOperator.IN, ("WATCH", "ACTIVE")),
                MonitorCondition("data_quality.score", RuleOperator.GT, 50.0),
            ),
        )
        rule = _rule(conditions=expression.conditions)
        self.assertEqual(rule.expression.conditions[1].as_dict()["value"], ["WATCH", "ACTIVE"])
        with self.assertRaises(MonitorValidationError):
            MonitorCondition("data_quality.score", RuleOperator.GT, object())
        with self.assertRaisesRegex(MonitorValidationError, "signed 64-bit"):
            MonitorCondition("data_quality.score", RuleOperator.EQ, 1 << 63)

    def test_v2_repository_migrates_rule_snapshot_as_inexact_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "monitor-v2.sqlite3"
            legacy_rule = _rule(rule_id="legacy-rule").as_dict()
            legacy_rule.pop("version")
            connection = sqlite3.connect(database)
            try:
                connection.executescript(
                    """
                    PRAGMA foreign_keys=ON;
                    CREATE TABLE monitor_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    INSERT INTO monitor_meta(key,value)
                    VALUES('schema','stock-tracker-monitor-store-v2');
                    CREATE TABLE monitor_rules (
                        rule_id TEXT PRIMARY KEY,
                        rule_json TEXT NOT NULL,
                        enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE monitor_inbox (
                        inbox_id TEXT PRIMARY KEY,
                        rule_id TEXT NOT NULL REFERENCES monitor_rules(rule_id),
                        symbol TEXT NOT NULL,
                        market TEXT NOT NULL,
                        severity TEXT NOT NULL,
                        state TEXT NOT NULL,
                        title TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        evidence_json TEXT NOT NULL,
                        dedup_key TEXT NOT NULL,
                        first_triggered_at TEXT NOT NULL,
                        last_triggered_at TEXT NOT NULL,
                        trigger_count INTEGER NOT NULL,
                        snoozed_until TEXT,
                        updated_at TEXT NOT NULL
                    );
                    """
                )
                rule_json = json.dumps(
                    legacy_rule,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                connection.execute(
                    "INSERT INTO monitor_rules VALUES(?,?,?,?,?)",
                    (
                        "legacy-rule",
                        rule_json,
                        1,
                        _BASE_TIME.isoformat(),
                        _BASE_TIME.isoformat(),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO monitor_inbox VALUES(
                        ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                    )
                    """,
                    (
                        "legacy-inbox",
                        "legacy-rule",
                        "600519.SH",
                        "A",
                        "WARNING",
                        "NEW",
                        "Legacy event",
                        "Legacy event summary",
                        '{"schema":"legacy-evidence-v1"}',
                        "d" * 64,
                        _BASE_TIME.isoformat(),
                        _BASE_TIME.isoformat(),
                        1,
                        None,
                        _BASE_TIME.isoformat(),
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            repository = MonitorRepository(database)
            migrated = repository.get_inbox("legacy-inbox")
            self.assertEqual(migrated["rule_version"], 1)
            self.assertFalse(migrated["rule_snapshot"]["historical_exact"])
            self.assertEqual(
                migrated["rule_snapshot"]["migration_source"],
                "V2_CURRENT_RULE",
            )
            self.assertEqual(migrated["rule_snapshot"]["rule"]["version"], 1)
            connection = sqlite3.connect(database)
            try:
                schema = connection.execute(
                    "SELECT value FROM monitor_meta WHERE key='schema'"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(schema, "stock-tracker-monitor-store-v3")


class TestMonitorEngineAndRepository(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = MonitorRepository(Path(self.temp.name) / "monitor.sqlite3")
        self.published: list[tuple[str, dict]] = []
        self.engine = MonitorEngine(
            self.repo,
            publisher=lambda topic, payload: self.published.append((topic, payload)),
        )
        self.rule = _rule()
        self.repo.upsert_rule(self.rule)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_boolean_numeric_equality_confusion_fails_closed(self) -> None:
        for operator in (RuleOperator.EQ, RuleOperator.NE):
            with self.subTest(operator=operator.value):
                rule = _rule(
                    rule_id=f"bool-confusion-{operator.value.lower()}",
                    conditions=(
                        MonitorCondition("data_quality.score", operator, True),
                    ),
                )
                result = self.engine.evaluate_rule(
                    rule,
                    symbol="600519.SH",
                    market="A",
                    facts=_facts(),
                    now=_BASE_TIME,
                )
                self.assertFalse(result.matched)
                self.assertEqual(result.reason, "NO_MATCH")

    def test_match_cooldown_repeat_and_terminal_reopen(self) -> None:
        first = self.engine.evaluate_rule(
            self.rule,
            symbol="600519.SH",
            market="A",
            facts=_facts(),
            now=_BASE_TIME + timedelta(seconds=1),
        )
        self.assertTrue(first.matched)
        self.assertFalse(first.suppressed)
        self.assertEqual(first.inbox["trigger_count"], 1)
        first_id = first.inbox["inbox_id"]

        suppressed = self.engine.evaluate_rule(
            self.rule,
            symbol="600519.SH",
            market="A",
            facts=_facts(market_event={**_facts()["market_event"], "latency_p95_ms": 19.0}),
            now=_BASE_TIME + timedelta(seconds=30),
        )
        self.assertTrue(suppressed.matched)
        self.assertTrue(suppressed.suppressed)
        self.assertEqual(len(self.repo.list_inbox()), 1)

        repeated = self.engine.evaluate_rule(
            self.rule,
            symbol="600519.SH",
            market="A",
            facts=_facts(market_event={**_facts()["market_event"], "latency_p95_ms": 22.0}),
            now=_BASE_TIME + timedelta(seconds=180),
        )
        self.assertEqual(repeated.inbox["inbox_id"], first_id)
        self.assertEqual(repeated.inbox["trigger_count"], 2)

        resolved = self.repo.transition(
            first_id,
            InboxState.RESOLVED,
            reason="fixture resolved",
        )
        self.assertEqual(resolved["state"], InboxState.RESOLVED.value)
        reopened = self.engine.evaluate_rule(
            self.rule,
            symbol="600519.SH",
            market="A",
            facts=_facts(),
            now=_BASE_TIME + timedelta(seconds=400),
        )
        self.assertNotEqual(reopened.inbox["inbox_id"], first_id)
        self.assertEqual(len(self.repo.list_inbox()), 2)

    def test_concurrent_first_triggers_share_one_active_inbox(self) -> None:
        rule = _rule(
            rule_id="concurrent-first-trigger",
            cooldown_sec=0,
            duplicate_window_sec=0,
        )
        self.repo.upsert_rule(rule)
        barrier = threading.Barrier(3)
        results = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                barrier.wait(timeout=5)
                results.append(
                    self.repo.record_trigger(
                        rule=rule,
                        symbol="600519.SH",
                        market="A",
                        title="Concurrent fixture",
                        summary="Concurrent first trigger must remain atomic.",
                        evidence={"schema": "concurrent-fixture-v1"},
                        dedup_key="c" * 64,
                        triggered_at=_BASE_TIME,
                    )
                )
            except BaseException as exc:  # noqa: BLE001 - test captures worker failures
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertTrue(all(not item.suppressed for item in results))
        inbox_ids = {
            item.inbox["inbox_id"]
            for item in results
            if item.inbox is not None
        }
        self.assertEqual(len(inbox_ids), 1)
        inbox = self.repo.list_inbox()
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]["trigger_count"], 2)
        self.assertEqual(len(self.repo.outbox()), 2)

    def test_concurrent_engine_evaluations_apply_cooldown_atomically(self) -> None:
        rule = _rule(
            rule_id="concurrent-cooldown",
            cooldown_sec=60,
            duplicate_window_sec=120,
        )
        self.repo.upsert_rule(rule)
        barrier = threading.Barrier(3)
        results = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                barrier.wait(timeout=5)
                results.append(
                    self.engine.evaluate_rule(
                        rule,
                        symbol="600519.SH",
                        market="A",
                        facts=_facts(),
                        now=_BASE_TIME,
                    )
                )
            except BaseException as exc:  # noqa: BLE001 - test captures worker failures
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(1 for item in results if item.suppressed), 1)
        self.assertEqual(sum(1 for item in results if item.inbox is not None), 1)
        inbox = self.repo.list_inbox()
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]["trigger_count"], 1)
        self.assertEqual(len(self.repo.outbox()), 1)
        self.assertEqual(len(self.published), 1)

    def test_snooze_expiry_and_invalid_transition(self) -> None:
        event = self.engine.evaluate_rule(
            self.rule,
            symbol="600519.SH",
            market="A",
            facts=_facts(),
            now=_BASE_TIME + timedelta(seconds=1),
        ).inbox
        inbox_id = event["inbox_id"]
        acknowledged = self.repo.transition(
            inbox_id,
            InboxState.ACKNOWLEDGED,
            reason="reviewed",
        )
        self.assertEqual(acknowledged["state"], InboxState.ACKNOWLEDGED.value)
        snoozed_until = datetime.now(timezone.utc) + timedelta(seconds=90)
        snoozed = self.repo.transition(
            inbox_id,
            InboxState.SNOOZED,
            reason="quiet period",
            snoozed_until=snoozed_until,
        )
        self.assertEqual(snoozed["state"], InboxState.SNOOZED.value)
        changed = self.repo.expire_due(snoozed_until + timedelta(seconds=1))
        self.assertEqual(changed, 1)
        self.assertEqual(self.repo.get_inbox(inbox_id)["state"], InboxState.NEW.value)
        self.repo.transition(inbox_id, InboxState.INVALIDATED, reason="bad fixture")
        with self.assertRaisesRegex(MonitorRepositoryError, "invalid monitor transition"):
            self.repo.transition(inbox_id, InboxState.ACKNOWLEDGED, reason="cannot reopen")

    def test_missing_fact_cannot_satisfy_not_equal_condition(self) -> None:
        rule = _rule(
            rule_id="missing-fact",
            conditions=(
                MonitorCondition("data_status", RuleOperator.NE, "STALE"),
            ),
        )
        self.repo.upsert_rule(rule)
        result = self.engine.evaluate_rule(
            rule,
            symbol="600519.SH",
            market="A",
            facts={},
            now=_BASE_TIME,
        )
        self.assertFalse(result.matched)
        self.assertEqual(result.reason, "NO_MATCH")
        self.assertFalse(result.evidence["conditions"][0]["present"])
        self.assertIsNone(result.evidence["conditions"][0]["actual"])
        self.assertEqual(self.repo.list_inbox(), [])

    def test_duplicate_conditions_remain_distinct_in_evidence(self) -> None:
        rule = _rule(
            rule_id="duplicate-fact",
            conditions=(
                MonitorCondition("market_event.last_price", RuleOperator.GE, 10.0),
                MonitorCondition("market_event.last_price", RuleOperator.LE, 11.0),
            ),
        )
        self.repo.upsert_rule(rule)
        result = self.engine.evaluate_rule(
            rule,
            symbol="600519.SH",
            market="A",
            facts=_facts(),
            now=_BASE_TIME + timedelta(seconds=1),
        )
        conditions = result.evidence["conditions"]
        self.assertEqual(len(conditions), 2)
        self.assertEqual([item["operator"] for item in conditions], ["GE", "LE"])

    def test_scope_is_bounded_and_does_not_mutate_facts(self) -> None:
        broad = _rule(
            rule_id="broad",
            scope=MonitorScope(
                ScopeKind.ALL_MARKET,
                max_symbols=2,
                all_market_acknowledged=True,
            ),
        )
        original = _facts()
        snapshot = copy.deepcopy(original)
        missed = self.engine.evaluate_rule(
            broad,
            symbol="600519.SH",
            market="A",
            facts=original,
            all_market_universe=frozenset({"600519.SH", "000001.SZ", "300750.SZ"}),
            now=_BASE_TIME,
        )
        self.assertEqual(missed.reason, "SCOPE_MISS")
        self.assertEqual(original, snapshot)

    def test_rule_with_history_cannot_be_deleted(self) -> None:
        event = self.engine.evaluate_rule(
            self.rule,
            symbol="600519.SH",
            market="A",
            facts=_facts(),
            now=_BASE_TIME,
        )
        self.assertIsNotNone(event.inbox)
        with self.assertRaisesRegex(MonitorRepositoryError, "immutable inbox history"):
            self.repo.delete_rule(self.rule.rule_id)


class _WebhookResponse:
    status = 204

    def __init__(self, url: str) -> None:
        self._url = url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback

    def geturl(self) -> str:
        return self._url

    def read(self, limit: int) -> bytes:
        del limit
        return b""


class _WebhookOpener:
    def __init__(self, response_url: str | None = None) -> None:
        self.requests = []
        self.response_url = response_url

    def open(self, request, timeout: float):
        self.requests.append((request, timeout))
        return _WebhookResponse(self.response_url or request.full_url)


class TestMonitorNotifications(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = MonitorRepository(Path(self.temp.name) / "monitor.sqlite3")
        self.rule = _rule(channels=("BROWSER", "WEBHOOK"))
        self.repo.upsert_rule(self.rule)
        self.event = self.repo.create_inbox(
            rule=self.rule,
            symbol="600519.SH",
            market="A",
            title="测试通知",
            summary="仅元数据通知",
            evidence={"safe": True},
            dedup_key="d" * 64,
            triggered_at=datetime.now(timezone.utc),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_browser_delivery_and_disabled_webhook_are_honest(self) -> None:
        published = []
        dispatcher = NotificationDispatcher(
            self.repo,
            webhook_enabled=False,
            browser_publisher=lambda topic, payload: published.append((topic, payload)),
        )
        result = dispatcher.dispatch_pending()
        self.assertEqual(result["delivered"], 1)
        self.assertEqual(result["disabled"], 1)
        self.assertEqual(len(published), 1)
        states = {item["channel"]: item["state"] for item in self.repo.outbox()}
        self.assertEqual(states, {"BROWSER": "DELIVERED", "WEBHOOK": "DISABLED"})

    def test_concurrent_dispatchers_claim_each_outbox_row_once(self) -> None:
        published: list[tuple[str, dict]] = []
        dispatcher = NotificationDispatcher(
            self.repo,
            webhook_enabled=False,
            browser_publisher=lambda topic, payload: published.append((topic, payload)),
        )
        barrier = threading.Barrier(3)
        results: list[dict] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                barrier.wait(timeout=5)
                results.append(dispatcher.dispatch_pending())
            except BaseException as exc:  # noqa: BLE001 - test captures worker failures
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertEqual(sum(item["delivered"] for item in results), 1)
        self.assertEqual(sum(item["disabled"] for item in results), 1)
        self.assertEqual(len(published), 1)
        states = {item["channel"]: item["state"] for item in self.repo.outbox()}
        self.assertEqual(states, {"BROWSER": "DELIVERED", "WEBHOOK": "DISABLED"})

    def test_expired_outbox_lease_is_recovered(self) -> None:
        now = datetime.now(timezone.utc)
        claimed = self.repo.claim_pending_outbox(now=now, lease_seconds=5)
        self.assertEqual(len(claimed), 2)
        self.assertEqual(
            self.repo.claim_pending_outbox(
                now=now + timedelta(seconds=1),
                lease_seconds=5,
            ),
            [],
        )
        recovered = self.repo.claim_pending_outbox(
            now=now + timedelta(seconds=6),
            lease_seconds=5,
        )
        self.assertEqual({item["outbox_id"] for item in recovered}, {item["outbox_id"] for item in claimed})

    def test_browser_publisher_failure_is_bounded_and_retryable(self) -> None:
        def fail_publisher(topic, payload) -> None:
            del topic, payload
            raise RuntimeError("fixture publisher failure")

        dispatcher = NotificationDispatcher(
            self.repo,
            webhook_enabled=False,
            browser_publisher=fail_publisher,
        )
        result = dispatcher.dispatch_pending()
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["disabled"], 1)
        browser = next(item for item in self.repo.outbox() if item["channel"] == "BROWSER")
        self.assertEqual(browser["state"], "PENDING")
        self.assertEqual(browser["attempts"], 1)

    def test_https_allowlist_signing_and_no_redirect_contract(self) -> None:
        opener = _WebhookOpener()
        dispatcher = NotificationDispatcher(
            self.repo,
            webhook_enabled=True,
            webhook_allowed_origins=("https://hooks.example",),
            browser_publisher=lambda topic, payload: None,
            opener=opener,
            environ={
                "STOCK_TRACKER_MONITOR_WEBHOOK_URL": "https://hooks.example/stock-monitor",
                "STOCK_TRACKER_MONITOR_WEBHOOK_SIGNING_KEY": "s" * 48,
            },
        )
        result = dispatcher.dispatch_pending()
        self.assertEqual(result["delivered"], 2)
        self.assertEqual(len(opener.requests), 1)
        request, timeout = opener.requests[0]
        self.assertEqual(timeout, 5.0)
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertIn("x-stock-tracker-signature", headers)
        self.assertNotIn("s" * 48, request.data.decode("utf-8"))

    def test_webhook_response_url_must_match_exact_request_path(self) -> None:
        opener = _WebhookOpener("https://hooks.example/changed-path")
        dispatcher = NotificationDispatcher(
            self.repo,
            webhook_enabled=True,
            webhook_allowed_origins=("https://hooks.example",),
            browser_publisher=lambda topic, payload: None,
            opener=opener,
            environ={
                "STOCK_TRACKER_MONITOR_WEBHOOK_URL": "https://hooks.example/stock-monitor",
                "STOCK_TRACKER_MONITOR_WEBHOOK_SIGNING_KEY": "p" * 48,
            },
        )
        result = dispatcher.dispatch_pending()
        self.assertEqual(result["failed"], 1)
        webhook = next(item for item in self.repo.outbox() if item["channel"] == "WEBHOOK")
        self.assertEqual(webhook["state"], "PENDING")
        self.assertEqual(webhook["attempts"], 1)

    def test_unallowlisted_webhook_fails_with_bounded_retry(self) -> None:
        dispatcher = NotificationDispatcher(
            self.repo,
            webhook_enabled=True,
            webhook_allowed_origins=("https://approved.example",),
            browser_publisher=lambda topic, payload: None,
            environ={
                "STOCK_TRACKER_MONITOR_WEBHOOK_URL": "https://blocked.example/hook",
                "STOCK_TRACKER_MONITOR_WEBHOOK_SIGNING_KEY": "k" * 48,
            },
        )
        result = dispatcher.dispatch_pending()
        self.assertEqual(result["failed"], 1)
        webhook = next(item for item in self.repo.outbox() if item["channel"] == "WEBHOOK")
        self.assertEqual(webhook["state"], "PENDING")
        self.assertEqual(webhook["attempts"], 1)
        with self.assertRaises(NotificationDeliveryError):
            NotificationDispatcher(
                self.repo,
                webhook_enabled="yes",  # type: ignore[arg-type]
            )


class TestMonitorServiceEventBus(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        base = load_xtp_sidecar_config(ROOT / "config" / "xtp_sidecar.toml")
        config = replace(
            base,
            event_root="event-data",
            metadata_db="event-data/catalog.sqlite3",
            quarantine_root="event-quarantine",
            monitor_db="monitor.sqlite3",
        )
        self.published: list[tuple[str, dict]] = []
        self.service = MonitorService(
            config,
            project_root=root,
            publisher=lambda topic, payload: self.published.append((topic, payload)),
        )
        self.rule = _rule(
            rule_id="runtime-signal",
            conditions=(
                MonitorCondition("signal_state", RuleOperator.EQ, "WATCH"),
                MonitorCondition("data_status", RuleOperator.EQ, "DELAYED"),
                MonitorCondition("scores.opportunity", RuleOperator.GE, 75),
                MonitorCondition("features.rsi14", RuleOperator.GE, 50.0),
            ),
        )
        self.service.repository.upsert_rule(self.rule)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_eventbus_observation_is_read_only_and_bounded(self) -> None:
        payload = {
            "schema": "stock-tracker-monitor-facts-v1",
            "symbol": "600519.SH",
            "market": "A",
            "strategy_id": "BASE",
            "action_state": "WATCH",
            "signal_state": "WATCH",
            "data_status": "DELAYED",
            "data_quality": {"status": "VALID", "score": 88},
            "blocker_codes": [],
            "market_regime": {"state": "ROTATION", "score": 55.0},
            "scores": {
                "opportunity": 80,
                "timing": 65,
                "risk": 25,
                "confidence": 70,
            },
            "features": {
                "rsi14": 58.0,
                "roc20": 4.0,
                "roc60": 8.0,
                "ann_vol": 22.0,
                "volume_ratio": 1.4,
                "pos52w": 0.7,
                "amplitude": 2.2,
                "bar_count": 80,
            },
            "market_event": {
                "connection_state": "NOT_APPLICABLE",
                "feed_mode": "RUNTIME_PROVIDER",
                "latency_p50_ms": None,
                "latency_p95_ms": None,
                "duplicate_count": None,
                "callback_gap_count": None,
                "provider_gap_count": None,
                "out_of_order_count": None,
                "ingestion_lag_ms": None,
                "last_price": 10.2,
                "change_pct": 2.0,
            },
            "has_position": False,
            "action_state_mutated": False,
            "score_mutated": False,
            "order_created": False,
        }
        original = copy.deepcopy(payload)
        result = self.service.observe_eventbus(
            "monitor_facts",
            payload,
            universe=("600519.SH",),
        )
        self.assertEqual(payload, original)
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["matched"])
        self.assertEqual(
            self.service.observe_eventbus("signal", payload),
            [],
        )
        malformed = copy.deepcopy(payload)
        malformed["action_state_mutated"] = True
        self.assertEqual(
            self.service.observe_eventbus("monitor_facts", malformed),
            [],
        )

        self.assertTrue(self.service.start_notification_worker(interval_sec=0.1))
        try:
            for _ in range(30):
                if all(
                    item["state"] == "DELIVERED"
                    for item in self.service.repository.outbox()
                ):
                    break
                threading.Event().wait(0.02)
        finally:
            self.service.stop_notification_worker()
        self.assertTrue(
            any(topic == "monitor.notification" for topic, _ in self.published)
        )
        self.assertTrue(
            all(
                item["state"] == "DELIVERED"
                for item in self.service.repository.outbox()
            )
        )
        summary = self.service.summary()
        self.assertFalse(summary["notification_worker"]["running"])
        self.assertFalse(summary["auto_trade"])
        self.assertFalse(summary["allow_live_decision"])
        self.assertFalse(summary["allow_model_training"])
        self.assertFalse(summary["account_value_exposed"])

    def test_runtime_event_worker_is_bounded_nonblocking_and_snapshot_isolated(self) -> None:
        regime_payload = {"regime": "ROTATION", "market_score": 55.0}
        for _ in range(1024):
            self.assertTrue(self.service.enqueue_runtime_event("regime", regime_payload))
        self.assertFalse(self.service.enqueue_runtime_event("regime", regime_payload))
        initial = self.service.summary()["runtime_event_worker"]
        self.assertEqual(initial["queue_size"], 1024)
        self.assertEqual(initial["dropped"], 1)
        self.assertFalse(self.service.enqueue_runtime_event("signal", {}))

        self.assertTrue(self.service.start_runtime_event_worker())
        try:
            for _ in range(100):
                status = self.service.summary()["runtime_event_worker"]
                if status["processed"] >= 1024 and status["queue_size"] == 0:
                    break
                threading.Event().wait(0.01)

            payload = {
                "schema": "stock-tracker-monitor-facts-v1",
                "symbol": "600519.SH",
                "market": "A",
                "strategy_id": "BASE",
                "action_state": "WATCH",
                "signal_state": "WATCH",
                "data_status": "DELAYED",
                "data_quality": {"status": "VALID", "score": 88},
                "blocker_codes": [],
                "market_regime": {"state": "ROTATION", "score": 55.0},
                "scores": {
                    "opportunity": 80,
                    "timing": 65,
                    "risk": 25,
                    "confidence": 70,
                },
                "features": {
                    "rsi14": 58.0,
                    "roc20": 4.0,
                    "roc60": 8.0,
                    "ann_vol": 22.0,
                    "volume_ratio": 1.4,
                    "pos52w": 0.7,
                    "amplitude": 2.2,
                    "bar_count": 80,
                },
                "market_event": {
                    "connection_state": "NOT_APPLICABLE",
                    "feed_mode": "RUNTIME_PROVIDER",
                    "latency_p50_ms": None,
                    "latency_p95_ms": None,
                    "duplicate_count": None,
                    "callback_gap_count": None,
                    "provider_gap_count": None,
                    "out_of_order_count": None,
                    "ingestion_lag_ms": None,
                    "last_price": 10.2,
                    "change_pct": 2.0,
                },
                "has_position": False,
                "action_state_mutated": False,
                "score_mutated": False,
                "order_created": False,
            }
            self.assertTrue(
                self.service.enqueue_runtime_event(
                    "monitor_facts",
                    payload,
                    universe=("600519.SH",),
                )
            )
            payload["signal_state"] = "COLD"
            for _ in range(100):
                if self.service.repository.list_inbox(limit=10):
                    break
                threading.Event().wait(0.01)
        finally:
            self.service.stop_runtime_event_worker()

        inbox = self.service.repository.list_inbox(limit=10)
        self.assertEqual(len(inbox), 1)
        signal_evidence = next(
            item
            for item in inbox[0]["evidence"]["facts"]["conditions"]
            if item["fact"] == "signal_state"
        )
        self.assertEqual(signal_evidence["actual"], "WATCH")
        final = self.service.summary()["runtime_event_worker"]
        self.assertFalse(final["running"])
        self.assertEqual(final["enqueued"], 1025)
        self.assertEqual(final["processed"], 1025)
        self.assertEqual(final["dropped"], 1)

    def test_disabled_sidecar_status_does_not_require_credentials(self) -> None:
        status = self.service.data_link()
        self.assertEqual(status["status"], "DISABLED")
        self.assertFalse(status["contains_account_value"])
        self.assertFalse(status["contains_sidecar_access"])
        self.assertFalse(status["sidecar"]["algorithm_account_used"])

    def test_transition_rejects_snooze_duration_for_non_snoozed_state(self) -> None:
        event = self.service.repository.create_inbox(
            rule=self.rule,
            symbol="600519.SH",
            market="A",
            title="Transition fixture",
            summary="Transition contract fixture.",
            evidence={"schema": "transition-fixture-v1"},
            dedup_key="t" * 64,
            triggered_at=datetime.now(timezone.utc),
        )
        with self.assertRaisesRegex(MonitorServiceError, "only valid for SNOOZED"):
            self.service.transition(
                event["inbox_id"],
                "ACKNOWLEDGED",
                reason="fixture acknowledgement",
                snooze_sec=60,
            )

    def test_concurrent_rule_updates_allocate_distinct_versions(self) -> None:
        draft = _rule(rule_id="concurrent-versioned-rule").as_dict()
        created = self.service.create_or_update_rule(draft)
        self.assertEqual(created["version"], 1)
        barrier = threading.Barrier(3)
        results: list[dict] = []
        errors: list[BaseException] = []

        def worker(name: str) -> None:
            try:
                payload = dict(draft)
                payload["name"] = name
                payload["version"] = 999999
                barrier.wait(timeout=5)
                results.append(self.service.create_or_update_rule(payload))
            except BaseException as exc:  # noqa: BLE001 - test captures worker failures
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=("并发版本 A",)),
            threading.Thread(target=worker, args=("并发版本 B",)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertEqual({result["version"] for result in results}, {2, 3})
        self.assertEqual(
            self.service.repository.get_rule("concurrent-versioned-rule").version,
            3,
        )

    def test_service_versions_rules_and_preserves_trigger_snapshots(self) -> None:
        draft = _rule(
            rule_id="versioned-rule",
            cooldown_sec=0,
            duplicate_window_sec=0,
        ).as_dict()
        draft["version"] = 999999
        created = self.service.create_or_update_rule(draft)
        self.assertEqual(created["version"], 1)
        rule_v1 = self.service.repository.get_rule("versioned-rule")
        first = self.service.engine.evaluate_rule(
            rule_v1,
            symbol="600519.SH",
            market="A",
            facts=_facts(),
            now=_BASE_TIME + timedelta(seconds=1),
        ).inbox
        self.assertEqual(first["rule_version"], 1)
        self.assertTrue(first["rule_snapshot"]["historical_exact"])
        self.assertEqual(first["rule_snapshot"]["rule"]["version"], 1)
        self.assertEqual(first["evidence"]["rule_version"], 1)
        self.assertEqual(len(first["evidence"]["rule_snapshot_sha256"]), 64)

        draft["name"] = "更新后的版本规则"
        draft["version"] = 999999
        updated = self.service.create_or_update_rule(draft)
        self.assertEqual(updated["version"], 2)
        rule_v2 = self.service.repository.get_rule("versioned-rule")
        second = self.service.engine.evaluate_rule(
            rule_v2,
            symbol="600519.SH",
            market="A",
            facts=_facts(),
            now=_BASE_TIME + timedelta(seconds=2),
        ).inbox
        self.assertNotEqual(first["inbox_id"], second["inbox_id"])
        self.assertEqual(second["rule_version"], 2)
        self.assertEqual(second["rule_snapshot"]["rule"]["version"], 2)
        persisted = {
            item["rule_version"]: item
            for item in self.service.repository.list_inbox()
            if item["rule_id"] == "versioned-rule"
        }
        self.assertEqual(set(persisted), {1, 2})
        self.assertEqual(
            persisted[1]["rule_snapshot"]["rule"]["name"],
            "链路延迟观察",
        )
        self.assertEqual(
            persisted[2]["rule_snapshot"]["rule"]["name"],
            "更新后的版本规则",
        )

        connection = sqlite3.connect(self.service.repository.database)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE monitor_inbox SET rule_version=99 WHERE inbox_id=?",
                    (first["inbox_id"],),
                )
            connection.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE monitor_inbox SET rule_snapshot_json=NULL WHERE inbox_id=?",
                    (first["inbox_id"],),
                )
            connection.rollback()
            tampered_evidence = dict(persisted[1]["evidence"])
            tampered_evidence["rule_snapshot_sha256"] = "0" * 64
            connection.execute(
                "UPDATE monitor_inbox SET evidence_json=? WHERE inbox_id=?",
                (
                    json.dumps(tampered_evidence, separators=(",", ":"), sort_keys=True),
                    first["inbox_id"],
                ),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(MonitorRepositoryError, "evidence hash"):
            self.service.repository.get_inbox(first["inbox_id"])


if __name__ == "__main__":
    unittest.main()

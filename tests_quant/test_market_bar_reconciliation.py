from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

from stock_tracker.collector.eastmoney import EastmoneyProvider
from stock_tracker.collector.tencent import TencentProvider
from stock_tracker.core.config import ProviderConfig
from stock_tracker.core.types import Bar, Market
from stock_tracker.quant.data import (
    DataTrustTier,
    ManifestContractError,
    MarketBarCandidateState,
    MarketBarComparisonState,
    MarketBarField,
    MarketBarGoldenError,
    MarketBarLicenseStatus,
    MarketBarParserBinding,
    MarketBarPoint,
    MarketBarReconciliationError,
    MarketBarReconciliationPolicy,
    MarketBarSeriesEvidence,
    capture_market_bars,
    load_captured_market_bars,
    load_market_bar_golden_pack,
    materialize_golden_case,
    reconcile_market_bars,
    validate_captured_market_bars,
    validate_market_bar_report_payload,
    write_market_bar_reconciliation_json,
    write_market_bar_reconciliation_markdown,
)

_FIXTURE_MANIFEST = (
    Path(__file__).parent / "fixtures" / "market_bar_golden" / "v1" / "manifest.json"
)
_COMPARABLE_FIELDS = tuple(
    sorted(
        (
            MarketBarField.OPEN,
            MarketBarField.HIGH,
            MarketBarField.LOW,
            MarketBarField.CLOSE,
            MarketBarField.VOLUME,
        ),
        key=lambda item: item.value,
    )
)


class MarketBarReconciliationFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.eastmoney = EastmoneyProvider(
            ProviderConfig(
                name="eastmoney",
                cls="EastmoneyProvider",
                markets=["a", "hk", "us"],
                max_rps=100,
            )
        )
        self.tencent = TencentProvider(
            ProviderConfig(
                name="tencent",
                cls="TencentProvider",
                markets=["a", "hk", "us"],
                max_rps=100,
            )
        )
        self.registry = {
            "eastmoney": MarketBarParserBinding(
                source="eastmoney",
                schema_version=self.eastmoney.KLINE_SCHEMA_VERSION,
                parser_version=self.eastmoney.KLINE_ADAPTER_VERSION,
                parser=self.eastmoney.parse_bars_strict,
            ),
            "tencent": MarketBarParserBinding(
                source="tencent",
                schema_version=self.tencent.KLINE_SCHEMA_VERSION,
                parser_version=self.tencent.KLINE_ADAPTER_VERSION,
                parser=self.tencent.parse_bars_strict,
            ),
        }

    @staticmethod
    def _bar(
        *,
        source: str,
        session: date,
        symbol: str = "600519.SH",
        market: Market = Market.A,
        open_price: float = 100.0,
        high: float = 110.0,
        low: float = 95.0,
        close: float = 105.0,
        volume: int = 1_200_000,
        amount: float = 150_000_000.0,
    ) -> Bar:
        return Bar(
            symbol=symbol,
            market=market,
            timestamp=datetime.combine(session, datetime.min.time()),
            interval="1d",
            open=open_price,
            high=high,
            low=low,
            close=close,
            volume=volume,
            amount=amount,
            turnover=0.0,
            source=source,
            adjustment_factor=1.0,
        )

    def _capture(
        self,
        root: str | Path,
        *,
        source: str,
        bars: tuple[Bar, ...],
        synthetic_fixture: bool = True,
        retrieved_at: datetime = datetime(2025, 1, 5, tzinfo=timezone.utc),
        adjustment: str = "qfq",
    ):
        raw = json.dumps(
            {
                "source": source,
                "rows": [
                    {
                        "symbol": bar.symbol,
                        "date": bar.timestamp.date().isoformat(),
                        "open": bar.open,
                        "high": bar.high,
                        "low": bar.low,
                        "close": bar.close,
                        "volume": bar.volume,
                        "amount": bar.amount,
                    }
                    for bar in bars
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

        def parser(raw_bytes: bytes, symbol: str, market: Market, interval: str):
            self.assertEqual(raw_bytes, raw)
            self.assertEqual(symbol, bars[0].symbol)
            self.assertIs(market, bars[0].market)
            self.assertEqual(interval, "1d")
            return bars

        return capture_market_bars(
            root,
            raw_bytes=raw,
            parser=parser,
            symbol=bars[0].symbol,
            market=bars[0].market,
            interval="1d",
            retrieved_at=retrieved_at,
            source=source,
            source_dataset=f"{source}-synthetic-bars",
            provider_version="synthetic-provider-v1",
            schema_version=f"{source}-synthetic-schema-v1",
            parser_version=f"{source}-synthetic-parser-v1",
            request_parameters={
                "adjustment": adjustment,
                "requested_start": bars[0].timestamp.date().isoformat(),
                "requested_end": bars[-1].timestamp.date().isoformat(),
                "endpoint": f"https://{source}.invalid/bars",
                "interval": "1d",
                "synthetic_fixture": synthetic_fixture,
            },
            known_at_policy="synthetic-retrieved-at",
            revision_policy="synthetic-content-addressed-v1",
            source_note="SYNTHETIC test payload",
        )

    @staticmethod
    def _evidence(captured, *, synthetic_fixture: bool = True):
        return MarketBarSeriesEvidence(
            captured=captured,
            source_family=captured.artifact.source,
            adjustment=str(captured.request_parameters["adjustment"]),
            comparable_fields=_COMPARABLE_FIELDS,
            license_status=MarketBarLicenseStatus.PENDING,
            synthetic_fixture=synthetic_fixture,
        )

    def _two_series(
        self,
        root: str | Path,
        *,
        second_close: float = 105.0,
        second_sessions: tuple[date, ...] = (date(2024, 1, 2),),
        second_symbol: str = "600519.SH",
        same_source: bool = False,
    ) -> tuple[MarketBarSeriesEvidence, MarketBarSeriesEvidence]:
        first_capture = self._capture(
            root,
            source="source_one",
            bars=(
                self._bar(source="source_one", session=date(2024, 1, 2)),
            ),
        )
        second_source = "source_one" if same_source else "source_two"
        second_bars = tuple(
            self._bar(
                source=second_source,
                session=session,
                symbol=second_symbol,
                close=second_close,
                high=max(110.0, second_close + 1.0),
            )
            for session in second_sessions
        )
        second_capture = self._capture(
            root,
            source=second_source,
            bars=second_bars,
        )
        return self._evidence(first_capture), self._evidence(second_capture)


class TestGoldenMarketBarPack(MarketBarReconciliationFixture):
    def test_all_markets_materialize_and_remain_trust_blocked(self) -> None:
        pack = load_market_bar_golden_pack(_FIXTURE_MANIFEST)
        self.assertTrue(pack.synthetic_fixture)
        self.assertEqual({item.case_name for item in pack.cases}, {"A_600519", "HK_00700", "US_AAPL"})
        with tempfile.TemporaryDirectory() as directory:
            for case_name in ("A_600519", "HK_00700", "US_AAPL"):
                with self.subTest(case_name=case_name):
                    loaded_pack, case, report = materialize_golden_case(
                        manifest_path=_FIXTURE_MANIFEST,
                        case_name=case_name,
                        artifact_root=directory,
                        parser_registry=self.registry,
                    )
                    self.assertEqual(loaded_pack.pack_id, pack.pack_id)
                    self.assertEqual(case.case_name, case_name)
                    self.assertIs(
                        report.candidate_state,
                        MarketBarCandidateState.STRUCTURALLY_CONSTRUCTIBLE,
                    )
                    self.assertEqual(report.finding_counts["HARD_BLOCK"], 0)
                    self.assertEqual(len(report.coverage.fully_observed_sessions), 3)
                    self.assertTrue(
                        all(
                            item.state is MarketBarComparisonState.MATCH
                            for item in report.comparisons
                        )
                    )
                    self.assertIn("T3_NOT_REACHED", report.open_blockers)
                    self.assertIn("LICENSE_PENDING", report.open_blockers)
                    self.assertIn(
                        "SYNTHETIC_MARKET_BAR_EVIDENCE",
                        report.open_blockers,
                    )
                    self.assertNotIn("trust_tier", json.dumps(report.as_dict()))

    def test_input_order_and_output_writes_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, case, report = materialize_golden_case(
                manifest_path=_FIXTURE_MANIFEST,
                case_name="A_600519",
                artifact_root=directory,
                parser_registry=self.registry,
            )
            reversed_report = reconcile_market_bars(
                as_of=report.as_of,
                calendar_snapshot_id=case.calendar_snapshot_id,
                expected_open_sessions=case.expected_open_sessions,
                series=reversed(report.series),
                policy=report.policy,
            )
            self.assertEqual(reversed_report.report_id, report.report_id)
            json_path = Path(directory) / "report.json"
            markdown_path = Path(directory) / "report.md"
            write_market_bar_reconciliation_json(report, json_path)
            write_market_bar_reconciliation_markdown(report, markdown_path)
            self.assertEqual(json.loads(json_path.read_text(encoding="utf-8"))["report_id"], report.report_id)
            markdown = markdown_path.read_text(encoding="utf-8")
            self.assertIn(report.report_id, markdown)
            self.assertIn("T3_NOT_REACHED", markdown)
            write_market_bar_reconciliation_json(report, json_path)
            json_path.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(
                MarketBarReconciliationError,
                "immutable report path",
            ):
                write_market_bar_reconciliation_json(report, json_path)

    def test_manifest_and_raw_tamper_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "pack"
            shutil.copytree(_FIXTURE_MANIFEST.parent, copied)
            raw_path = copied / "a" / "600519_eastmoney.json"
            raw_path.write_bytes(raw_path.read_bytes() + b" ")
            with self.assertRaisesRegex(MarketBarGoldenError, "SHA-256 mismatch"):
                load_market_bar_golden_pack(copied / "manifest.json")

    def test_synthetic_pack_cannot_be_relabelled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "pack"
            shutil.copytree(_FIXTURE_MANIFEST.parent, copied)
            manifest = copied / "manifest.json"
            value = json.loads(manifest.read_text(encoding="utf-8"))
            value["synthetic_fixture"] = False
            manifest.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(MarketBarGoldenError, "synthetic-only"):
                load_market_bar_golden_pack(manifest)

    def test_manifest_rejects_duplicate_keys_and_identity_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            duplicate = Path(directory) / "duplicate.json"
            duplicate.write_text('{"schema":"one","schema":"two"}', encoding="utf-8")
            with self.assertRaisesRegex(MarketBarGoldenError, "duplicate JSON keys"):
                load_market_bar_golden_pack(duplicate)

            copied = Path(directory) / "pack"
            shutil.copytree(_FIXTURE_MANIFEST.parent, copied)
            manifest = copied / "manifest.json"
            value = json.loads(manifest.read_text(encoding="utf-8"))
            value["cases"][0]["sources"][0]["source_id"] = "0" * 64
            manifest.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(MarketBarGoldenError, "source identity mismatch"):
                load_market_bar_golden_pack(manifest)

    def test_parser_binding_version_mismatch_is_rejected(self) -> None:
        registry = dict(self.registry)
        registry["tencent"] = replace(
            registry["tencent"],
            parser_version="unexpected-parser-v999",
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(MarketBarGoldenError, "parser version mismatch"),
        ):
            materialize_golden_case(
                manifest_path=_FIXTURE_MANIFEST,
                case_name="A_600519",
                artifact_root=directory,
                parser_registry=registry,
            )


class TestMarketBarReconciliation(MarketBarReconciliationFixture):
    @staticmethod
    def _calendar_id() -> str:
        return hashlib.sha256(b"synthetic-calendar-test-v1").hexdigest()

    def test_price_conflict_is_a_hard_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first, second = self._two_series(directory, second_close=108.0)
            report = reconcile_market_bars(
                as_of=datetime(2025, 1, 5, tzinfo=timezone.utc),
                calendar_snapshot_id=self._calendar_id(),
                expected_open_sessions=(date(2024, 1, 2),),
                series=(first, second),
            )
            self.assertIs(report.candidate_state, MarketBarCandidateState.HARD_BLOCKED)
            self.assertIn(
                "MARKET_BAR_CLOSE_CONFLICT",
                {item.code for item in report.findings},
            )

    def test_missing_and_closed_session_rows_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first, second = self._two_series(directory)
            missing = reconcile_market_bars(
                as_of=datetime(2025, 1, 5, tzinfo=timezone.utc),
                calendar_snapshot_id=self._calendar_id(),
                expected_open_sessions=(date(2024, 1, 2), date(2024, 1, 3)),
                series=(first, second),
            )
            self.assertIn(
                "MARKET_BAR_OPEN_SESSION_COVERAGE_GAP",
                {item.code for item in missing.findings},
            )
            self.assertIs(missing.candidate_state, MarketBarCandidateState.HARD_BLOCKED)

            first, second = self._two_series(
                directory,
                second_sessions=(date(2024, 1, 2), date(2024, 1, 3)),
            )
            unexpected = reconcile_market_bars(
                as_of=datetime(2025, 1, 5, tzinfo=timezone.utc),
                calendar_snapshot_id=self._calendar_id(),
                expected_open_sessions=(date(2024, 1, 2),),
                series=(first, second),
            )
            self.assertIn(
                "MARKET_BAR_ON_CALENDAR_CLOSED_SESSION",
                {item.code for item in unexpected.findings},
            )
            self.assertIs(unexpected.candidate_state, MarketBarCandidateState.HARD_BLOCKED)

    def test_identity_mismatch_is_a_hard_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first, second = self._two_series(directory, second_symbol="000001.SZ")
            report = reconcile_market_bars(
                as_of=datetime(2025, 1, 5, tzinfo=timezone.utc),
                calendar_snapshot_id=self._calendar_id(),
                expected_open_sessions=(date(2024, 1, 2),),
                series=(first, second),
            )
            self.assertIn(
                "MARKET_BAR_SERIES_IDENTITY_MISMATCH",
                {item.code for item in report.findings},
            )
            self.assertIs(report.candidate_state, MarketBarCandidateState.HARD_BLOCKED)

    def test_same_source_is_not_independent_corroboration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first, second = self._two_series(
                directory,
                same_source=True,
                second_close=105.01,
            )
            report = reconcile_market_bars(
                as_of=datetime(2025, 1, 5, tzinfo=timezone.utc),
                calendar_snapshot_id=self._calendar_id(),
                expected_open_sessions=(date(2024, 1, 2),),
                series=(first, second),
            )
            codes = {item.code for item in report.findings}
            self.assertIn("DUPLICATE_SOURCE_FAMILY_NOT_INDEPENDENT", codes)
            self.assertIn("INSUFFICIENT_INDEPENDENT_MARKET_BAR_SOURCES", codes)
            self.assertIn("SOURCE_FAMILY_INDEPENDENCE_UNVERIFIED", codes)

    def test_one_series_never_becomes_a_false_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capture = self._capture(
                directory,
                source="source_one",
                bars=(self._bar(source="source_one", session=date(2024, 1, 2)),),
            )
            report = reconcile_market_bars(
                as_of=datetime(2025, 1, 5, tzinfo=timezone.utc),
                calendar_snapshot_id=self._calendar_id(),
                expected_open_sessions=(date(2024, 1, 2),),
                series=(self._evidence(capture),),
            )
            self.assertTrue(
                all(
                    item.state is MarketBarComparisonState.NOT_COMPARABLE
                    for item in report.comparisons
                )
            )

    def test_synthetic_and_license_labels_cannot_self_promote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            captured = self._capture(
                directory,
                source="source_one",
                bars=(self._bar(source="source_one", session=date(2024, 1, 2)),),
                synthetic_fixture=True,
            )
            with self.assertRaisesRegex(MarketBarReconciliationError, "must match"):
                self._evidence(captured, synthetic_fixture=False)
            with self.assertRaisesRegex(MarketBarReconciliationError, "licence clearance"):
                MarketBarSeriesEvidence(
                    captured=captured,
                    source_family="source_one",
                    adjustment="qfq",
                    comparable_fields=_COMPARABLE_FIELDS,
                    license_status=MarketBarLicenseStatus.CLEARED_FOR_INTERNAL_RESEARCH,
                    synthetic_fixture=True,
                )

    def test_capture_mutation_cannot_change_frozen_series_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first_capture = self._capture(
                directory,
                source="source_one",
                bars=(self._bar(source="source_one", session=date(2024, 1, 2)),),
            )
            second_capture = self._capture(
                directory,
                source="source_two",
                bars=(self._bar(source="source_two", session=date(2024, 1, 2)),),
            )
            first = self._evidence(first_capture)
            second = self._evidence(second_capture)
            report = reconcile_market_bars(
                as_of=datetime(2025, 1, 5, tzinfo=timezone.utc),
                calendar_snapshot_id=self._calendar_id(),
                expected_open_sessions=(date(2024, 1, 2),),
                series=(first, second),
            )
            original_id = report.report_id
            first_capture.request_parameters["adjustment"] = "raw"
            first_capture.bars[0].close = 999.0
            repeated = reconcile_market_bars(
                as_of=report.as_of,
                calendar_snapshot_id=report.calendar_snapshot_id,
                expected_open_sessions=report.expected_open_sessions,
                series=report.series,
                policy=report.policy,
            )
            self.assertEqual(repeated.report_id, original_id)
            with self.assertRaises(ManifestContractError):
                validate_captured_market_bars(first_capture)

    def test_policy_cannot_disable_coverage_or_license_gates(self) -> None:
        for overrides, expected in (
            ({"require_all_open_sessions": False}, "open-session coverage"),
            ({"require_license_clearance": False}, "licence clearance"),
        ):
            with self.subTest(overrides=overrides), self.assertRaisesRegex(
                MarketBarReconciliationError,
                expected,
            ):
                MarketBarReconciliationPolicy(**overrides)

    def test_derived_report_fields_cannot_be_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first, second = self._two_series(directory)
            report = reconcile_market_bars(
                as_of=datetime(2025, 1, 5, tzinfo=timezone.utc),
                calendar_snapshot_id=self._calendar_id(),
                expected_open_sessions=(date(2024, 1, 2),),
                series=(first, second),
            )
            with self.assertRaises(TypeError):
                replace(report, findings=())
            changed_policy = MarketBarReconciliationPolicy(price_tolerance_bps=0)
            changed = replace(report, policy=changed_policy)
            self.assertNotEqual(changed.report_id, report.report_id)

    def test_future_calendar_session_is_a_hard_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            retrieved = datetime(2024, 1, 1, 12, tzinfo=timezone.utc)
            first_capture = self._capture(
                directory,
                source="source_one",
                bars=(self._bar(source="source_one", session=date(2024, 1, 2)),),
                retrieved_at=retrieved,
            )
            second_capture = self._capture(
                directory,
                source="source_two",
                bars=(self._bar(source="source_two", session=date(2024, 1, 2)),),
                retrieved_at=retrieved,
            )
            report = reconcile_market_bars(
                as_of=datetime(2024, 1, 1, 13, tzinfo=timezone.utc),
                calendar_snapshot_id=self._calendar_id(),
                expected_open_sessions=(date(2024, 1, 2),),
                series=(self._evidence(first_capture), self._evidence(second_capture)),
            )
            self.assertIn(
                "CALENDAR_SESSION_NOT_FINAL_AS_OF",
                {item.code for item in report.findings},
            )
            self.assertIs(report.candidate_state, MarketBarCandidateState.HARD_BLOCKED)

    def test_same_day_daily_bar_is_not_final_as_of(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            retrieved = datetime(2024, 1, 2, 1, tzinfo=timezone.utc)
            first_capture = self._capture(
                directory,
                source="source_one",
                bars=(self._bar(source="source_one", session=date(2024, 1, 2)),),
                retrieved_at=retrieved,
            )
            second_capture = self._capture(
                directory,
                source="source_two",
                bars=(self._bar(source="source_two", session=date(2024, 1, 2)),),
                retrieved_at=retrieved,
            )
            report = reconcile_market_bars(
                as_of=datetime(2024, 1, 2, 4, tzinfo=timezone.utc),
                calendar_snapshot_id=self._calendar_id(),
                expected_open_sessions=(date(2024, 1, 2),),
                series=(self._evidence(first_capture), self._evidence(second_capture)),
            )
            self.assertIn(
                "CALENDAR_SESSION_NOT_FINAL_AS_OF",
                {item.code for item in report.findings},
            )
            self.assertIs(report.candidate_state, MarketBarCandidateState.HARD_BLOCKED)

    def test_promotion_fields_are_rejected_recursively(self) -> None:
        for payload in (
            {"trust_tier": "T3"},
            {"nested": [{"verified": False}]},
            {"research_grade": False},
        ):
            with (
                self.subTest(payload=payload),
                self.assertRaisesRegex(
                    MarketBarReconciliationError,
                    "promotion fields",
                ),
            ):
                validate_market_bar_report_payload(payload)

    def test_market_bar_point_rejects_datetime_as_date(self) -> None:
        with self.assertRaisesRegex(MarketBarReconciliationError, "session_date"):
            MarketBarPoint(
                session_date=datetime(2024, 1, 2, tzinfo=timezone.utc),
                open="100",
                high="110",
                low="95",
                close="105",
                volume=100,
                amount="1000",
                turnover="0",
            )

    def test_report_rejects_datetime_as_calendar_date(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first, second = self._two_series(directory)
            with self.assertRaisesRegex(
                MarketBarReconciliationError,
                "must contain date values",
            ):
                reconcile_market_bars(
                    as_of=datetime(2025, 1, 5, tzinfo=timezone.utc),
                    calendar_snapshot_id=self._calendar_id(),
                    expected_open_sessions=(
                        datetime(2024, 1, 2, tzinfo=timezone.utc),
                    ),
                    series=(first, second),
                )


class TestTencentRawBarBoundary(MarketBarReconciliationFixture):
    def test_eastmoney_strict_parser_rejects_duplicate_keys_and_nonfinite_rows(self) -> None:
        payloads = (
            b'{"rc":0,"rc":0}',
            json.dumps(
                {
                    "rc": 0,
                    "data": {
                        "klines": [
                            "2024-01-02,NaN,105,110,95,12000,150000000,2.3"
                        ]
                    },
                }
            ).encode("utf-8"),
        )
        for payload in payloads:
            with self.subTest(payload=payload[:40]), self.assertRaises(ValueError):
                self.eastmoney.parse_bars_strict(
                    payload,
                    "600519.SH",
                    Market.A,
                    "1d",
                )

    def test_exact_raw_capability_and_adjustment_contract(self) -> None:
        self.assertTrue(self.tencent.supports_raw_bars())
        self.assertTrue(self.tencent.supports_adjustment("qfq"))
        self.assertFalse(self.tencent.supports_adjustment("raw"))

    def test_strict_parser_rejects_duplicate_keys_nonfinite_and_duplicate_dates(self) -> None:
        bad_payloads = (
            b'{"code":0,"code":0}',
            b'{"code":0,"data":{"sh600519":{"qfqday":[["2024-01-02",NaN,105,110,95,12000]]}}}',
            json.dumps(
                {
                    "code": 0,
                    "data": {
                        "sh600519": {
                            "qfqday": [
                                ["2024-01-02", 100, 105, 110, 95, 12000],
                                ["2024-01-02", 100, 105, 110, 95, 12000],
                            ]
                        }
                    },
                }
            ).encode("utf-8"),
        )
        for payload in bad_payloads:
            with self.subTest(payload=payload[:40]), self.assertRaises(ValueError):
                self.tencent.parse_bars_strict(
                    payload,
                    "600519.SH",
                    Market.A,
                    "1d",
                )

    def test_qfq_parser_never_silently_falls_back_to_unadjusted_day(self) -> None:
        payload = json.dumps(
            {
                "code": 0,
                "data": {
                    "sh600519": {
                        "day": [["2024-01-02", 100, 105, 110, 95, 12000]]
                    }
                },
            }
        ).encode("utf-8")
        with self.assertRaisesRegex(ValueError, "qfqday"):
            self.tencent.parse_bars_strict(
                payload,
                "600519.SH",
                Market.A,
                "1d",
            )
        self.assertEqual(
            self.tencent.parse_bars(payload, "600519.SH", Market.A, "1d"),
            [],
        )

    def test_capture_descriptor_rejects_duplicate_json_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            captured = self._capture(
                directory,
                source="source_one",
                bars=(self._bar(source="source_one", session=date(2024, 1, 2)),),
            )
            descriptor = Path(directory) / Path(captured.descriptor_key)
            descriptor.write_text(
                '{"schema":"captured-market-bars-v1","schema":"captured-market-bars-v1"}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ManifestContractError, "duplicate JSON keys"):
                load_captured_market_bars(
                    directory,
                    descriptor_key=captured.descriptor_key,
                    parser=lambda raw, symbol, market, interval: captured.bars,
                )

    def test_capture_validation_rejects_recomputed_dataclass_replace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            captured = self._capture(
                directory,
                source="source_one",
                bars=(self._bar(source="source_one", session=date(2024, 1, 2)),),
            )
            forged = replace(captured, trust_tier=DataTrustTier.RESEARCH_GRADE)
            with self.assertRaisesRegex(ManifestContractError, "cannot self-promote"):
                validate_captured_market_bars(forged)


if __name__ == "__main__":
    unittest.main()

"""Run deterministic synthetic smoke checks for the quant foundation."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from stock_tracker.core.types import Bar, DataStatus, Market
from stock_tracker.quant.backtest import (
    CostSchedule,
    CostScheduleBook,
    ExecutionBar,
    ExecutionEngine,
    MarketRule,
    MarketRuleBook,
)
from stock_tracker.quant.config import load_quant_config
from stock_tracker.quant.core.calendar import (
    CalendarCoverage,
    CalendarDay,
    CalendarStatus,
    InstrumentSessionState,
    InstrumentSessionStatus,
    SessionKind,
    TradingCalendar,
)
from stock_tracker.quant.core.fingerprint import fingerprint
from stock_tracker.quant.data import (
    DataFormat,
    DataKind,
    DataSnapshotManifest,
    ManifestContractError,
    RawDataArtifact,
)
from stock_tracker.quant.features import default_qlib_audit
from stock_tracker.quant.labels import (
    CalendarAwareTripleBarrierLabeler,
    SameBarPolicy,
    TripleBarrierConfig,
    TripleBarrierLabeler,
)
from stock_tracker.quant.research import (
    ProbabilityAdvisory,
    assess_negative_controls,
    risk_gated_action,
)
from stock_tracker.quant.storage import apply_database

UTC = timezone.utc
A_TZ = ZoneInfo("Asia/Shanghai")


def utc_datetime(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


def local_datetime(session_date: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(session_date, time(hour, minute), tzinfo=A_TZ)


def bar(
    session_date: date,
    *,
    open_price: float,
    high: float,
    low: float,
    close: float,
    volume: int = 10_000,
) -> Bar:
    return Bar(
        symbol="600000.SH",
        market=Market.A,
        timestamp=local_datetime(session_date, 15),
        interval="1d",
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
        amount=close * volume,
        turnover=1.0,
        source="synthetic-contract-smoke",
        adjustment_factor=1.0,
        quality_status=DataStatus.LIVE,
    )


def execution_engine() -> ExecutionEngine:
    rule = MarketRule(
        rule_id="A-synthetic-smoke-rule",
        market=Market.A,
        effective_from=date(2000, 1, 1),
        effective_to=None,
        currency="CNY",
        lot_size=100,
        settlement_days=1,
        sell_t_plus_one=True,
        price_limit_state_required=True,
        verified=True,
        source_note="synthetic smoke fixture only",
    )
    costs = CostSchedule(
        schedule_id="A-synthetic-smoke-cost",
        market=Market.A,
        effective_from=date(2000, 1, 1),
        effective_to=None,
        commission_bps=0.0,
        minimum_commission=0.0,
        sell_tax_bps=0.0,
        exchange_fee_bps=0.0,
        transfer_fee_bps=0.0,
        half_spread_bps=0.0,
        slippage_bps=0.0,
        impact_coefficient=0.0,
        max_participation_rate=1.0,
        verified=True,
        source_note="synthetic smoke fixture only",
    )
    return ExecutionEngine(MarketRuleBook((rule,)), CostScheduleBook((costs,)))


def calendar_day(session_date: date, status: CalendarStatus) -> CalendarDay:
    return CalendarDay(
        market=Market.A,
        session_date=session_date,
        status=status,
        open_time=(local_datetime(session_date, 9, 30) if status is CalendarStatus.OPEN else None),
        close_time=(local_datetime(session_date, 15) if status is CalendarStatus.OPEN else None),
        session_kind=SessionKind.REGULAR,
        known_at=utc_datetime(2025, 1, 1),
        source="synthetic-calendar",
        revision=1,
        calendar_version="synthetic-calendar-v1",
        verified=True,
        source_note="synthetic smoke fixture only",
    )


def calendar_horizon_evidence() -> dict[str, object]:
    start = date(2025, 1, 2)
    end = date(2025, 1, 6)
    raw_bars = (
        bar(start, open_price=10.0, high=10.2, low=9.8, close=10.0),
        bar(end, open_price=11.2, high=11.5, low=11.0, close=11.3),
    )
    config = TripleBarrierConfig(
        take_profit_atr=1.0,
        stop_loss_atr=1.0,
        horizon_sessions=2,
        entry_delay_sessions=0,
        same_bar_policy=SameBarPolicy.MARK_AMBIGUOUS,
    )
    base = TripleBarrierLabeler(execution_engine(), config)
    unsafe = base.label(
        tuple(
            ExecutionBar(
                item,
                locked_limit_up=False,
                locked_limit_down=False,
            )
            for item in raw_bars
        ),
        signal_index=0,
        atr=1.0,
        requested_quantity=100,
    )
    coverage = CalendarCoverage(
        market=Market.A,
        start_date=start,
        end_date=end,
        source="synthetic-calendar",
        calendar_version="synthetic-calendar-v1",
        known_at=utc_datetime(2025, 1, 1),
        revision=1,
        verified=True,
        source_note="synthetic smoke fixture only",
    )
    days = (
        calendar_day(date(2025, 1, 2), CalendarStatus.OPEN),
        calendar_day(date(2025, 1, 3), CalendarStatus.CLOSED),
        calendar_day(date(2025, 1, 4), CalendarStatus.CLOSED),
        calendar_day(date(2025, 1, 5), CalendarStatus.OPEN),
        calendar_day(date(2025, 1, 6), CalendarStatus.OPEN),
    )
    status = InstrumentSessionStatus(
        symbol="600000.SH",
        market=Market.A,
        session_date=date(2025, 1, 5),
        status=InstrumentSessionState.SUSPENDED,
        known_at=utc_datetime(2025, 1, 5),
        source="synthetic-status",
        revision=1,
        reference_price=10.0,
        share_factor=1.0,
        verified=True,
        source_note="synthetic smoke fixture only",
    )
    aligned = TradingCalendar((coverage,), days, (status,)).align_bars(
        symbol="600000.SH",
        market=Market.A,
        bars=raw_bars,
        start=start,
        end=end,
        as_of=utc_datetime(2025, 1, 7),
    )
    safe = CalendarAwareTripleBarrierLabeler(base).label(
        aligned,
        signal_index=0,
        atr=1.0,
        requested_quantity=100,
        limit_states={
            date(2025, 1, 2): (False, False),
            date(2025, 1, 5): (False, False),
            date(2025, 1, 6): (False, False),
        },
    )
    return {
        "unsafe_raw_bar_label": unsafe.outcome.value,
        "calendar_aware_label": safe.outcome.value,
        "calendar_fix_prevents_horizon_drift": (
            unsafe.outcome.value == "TP_FIRST" and safe.outcome.value == "TIMEOUT"
        ),
        "calendar_snapshot_id": aligned.calendar_snapshot_id,
        "status_snapshot_id": aligned.instrument_status_snapshot_id,
        "session_dates": [value.isoformat() for value in aligned.session_dates],
        "placeholder_dates": [value.isoformat() for value in aligned.placeholder_dates],
    }


def manifest_evidence() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        payload = root / "raw" / "a" / "bars.csv"
        payload.parent.mkdir(parents=True)
        payload.write_bytes(b"date,close\n2025-01-02,10\n")
        artifact = RawDataArtifact.from_file(
            root,
            storage_key="raw/a/bars.csv",
            kind=DataKind.MARKET_BARS,
            format=DataFormat.CSV,
            market=Market.A,
            source="synthetic-provider",
            source_dataset="synthetic-bars",
            row_count=1,
            content_start=utc_datetime(2025, 1, 2),
            content_end=utc_datetime(2025, 1, 2),
            retrieved_at=utc_datetime(2025, 1, 5),
            provider_version="synthetic-provider-v1",
            schema_version="synthetic-schema-v1",
            adapter_version="synthetic-adapter-v1",
            known_at_policy="provider-published-at",
            revision_policy="append-new-artifact",
            verified=True,
            source_note="synthetic smoke fixture only",
        )
        manifest = DataSnapshotManifest(
            name="synthetic-smoke-snapshot",
            as_of=utc_datetime(2025, 1, 2),
            created_at=utc_datetime(2025, 1, 6),
            config_hash=fingerprint({"synthetic": True}),
            code_version="local-workspace",
            artifacts=(artifact,),
            calendar_snapshot_ids=("c" * 64,),
            universe_snapshot_id="e" * 64,
        )
        manifest_path = root / "manifest.json"
        manifest.write_json(manifest_path)
        round_trip = DataSnapshotManifest.read_json(manifest_path)
        original_size = payload.stat().st_size
        payload.write_bytes(b"date,close\n2025-01-02,99\n")
        tamper_detected = False
        try:
            artifact.verify_file(root)
        except ManifestContractError:
            tamper_detected = True
        return {
            "artifact_id": artifact.artifact_id,
            "snapshot_id": manifest.snapshot_id,
            "round_trip_verified": round_trip.snapshot_id == manifest.snapshot_id,
            "same_size_tamper": payload.stat().st_size == original_size,
            "tamper_detected": tamper_detected,
        }


def migration_evidence() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        database = Path(directory) / "quant-smoke.db"
        plan = apply_database(database)
        return {
            "database_created": database.exists(),
            "migration_count": len(plan.applied),
            "pending_count": len(plan.pending),
            "versions": [migration.version for migration in plan.applied],
            "production_database_modified": False,
        }


def governance_evidence() -> dict[str, object]:
    labels = tuple(index % 2 for index in range(100))
    negative = assess_negative_controls(
        y_true=labels,
        baseline_probabilities=tuple(0.45 if value == 0 else 0.55 for value in labels),
        future_feature_probabilities=tuple(
            0.001 if value == 0 else 0.999 for value in labels
        ),
    )
    blocked = risk_gated_action(
        ProbabilityAdvisory(
            probability=0.95,
            calibrated=True,
            calibration_id="a" * 64,
            model_id="synthetic-model",
        ),
        rule_signal_allowed=True,
        risk_gate_allowed=False,
        data_quality_allowed=True,
        minimum_probability=0.6,
    )
    return {
        "future_feature_brier": negative.future_feature_brier,
        "future_feature_flagged": negative.future_feature_flagged,
        "suspicious_advantage_detected": negative.suspicious_advantage_detected,
        "high_probability_bypasses_risk_gate": blocked.actionable,
        "risk_gate_reasons": list(blocked.reasons),
    }


def build_result() -> dict[str, object]:
    config = load_quant_config()
    qlib = default_qlib_audit()
    result = {
        "schema": "stock-tracker-quant-contract-smoke-v1",
        "synthetic_fixture_only": True,
        "investment_performance_claim": False,
        "config_hash": config.config_hash,
        "safety": {
            "auto_apply_sql": config.safety.auto_apply_sql,
            "auto_promote_models": config.safety.auto_promote_models,
            "probability_advisory_only": config.safety.probability_advisory_only,
        },
        "calendar_horizon": calendar_horizon_evidence(),
        "manifest": manifest_evidence(),
        "migrations": migration_evidence(),
        "governance": governance_evidence(),
        "qlib_audit": {
            "may_claim_numerical_equivalence": qlib.may_claim_numerical_equivalence,
            "blockers": list(qlib.blockers),
            "audit_id": qlib.audit_id,
        },
    }
    calendar_ok = result["calendar_horizon"]["calendar_fix_prevents_horizon_drift"]
    manifest_ok = result["manifest"]["tamper_detected"]
    migration_ok = result["migrations"]["pending_count"] == 0
    governance_ok = (
        result["governance"]["future_feature_flagged"]
        and not result["governance"]["high_probability_bypasses_risk_gate"]
    )
    result["passed"] = bool(calendar_ok and manifest_ok and migration_ok and governance_ok)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", help="Optional JSON evidence output path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = build_result()
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

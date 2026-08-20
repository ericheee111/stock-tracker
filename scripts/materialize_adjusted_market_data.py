from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from stock_tracker.core.types import Bar, Market
from stock_tracker.quant.core.corporate_actions import (
    AdjustmentBasis,
    AdjustmentConvention,
    CorporateActionBook,
    CorporateActionCoverage,
    CorporateActionFact,
    CorporateActionLifecycle,
    build_adjustment_series,
)
from stock_tracker.quant.core.universe import (
    InstrumentIdentityFact,
    SecurityType,
)
from stock_tracker.quant.data.adjusted_market_data import (
    AdjustedMarketDataError,
    AdjustedMarketDataPolicy,
    CalendarMaterializationSnapshot,
    RawBarSnapshot,
    SessionGapPolicy,
    materialize_adjusted_market_data,
    write_adjusted_market_data_dataset,
)

_SCHEMA = "stage2f-adjusted-market-data-request-v1"
_TOP_FIELDS = frozenset(
    {
        "schema",
        "synthetic_fixture",
        "as_of",
        "start_date",
        "end_date",
        "raw_artifact_id",
        "identity",
        "calendar",
        "corporate_action",
        "bars",
        "basis",
        "convention",
        "policy",
        "explicit_gap_sessions",
    }
)
_IDENTITY_FIELDS = frozenset(
    {
        "instrument_id",
        "symbol",
        "market",
        "exchange",
        "security_type",
        "effective_from",
        "effective_to",
        "known_at",
        "revision",
    }
)
_CALENDAR_FIELDS = frozenset({"open_sessions"})
_ACTION_FIELDS = frozenset(
    {
        "action_id",
        "ex_date",
        "record_date",
        "payment_date",
        "share_listing_date",
        "automatic_share_ratio",
        "cash_dividend_per_share",
        "rights_entitlement_ratio",
        "rights_subscription_price",
        "currency",
        "reference_price",
        "reference_price_snapshot_id",
        "known_at",
        "usable_from",
        "revision",
    }
)
_BAR_FIELDS = frozenset(
    {
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "turnover",
    }
)
_POLICY_FIELDS = frozenset({"policy_version", "session_gap_policy"})


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise AdjustedMarketDataError(
            f"{name} must be a non-empty trimmed string"
        )
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise AdjustedMarketDataError(f"{name} must be lowercase SHA-256")
    return text


def _datetime(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AdjustedMarketDataError(
            f"{name} must be ISO-8601 datetime"
        ) from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise AdjustedMarketDataError(f"{name} must include timezone")
    return result


def _date(value: object, name: str, *, optional: bool = False) -> date | None:
    if value is None and optional:
        return None
    try:
        return date.fromisoformat(_text(value, name))
    except ValueError as exc:
        raise AdjustedMarketDataError(
            f"{name} must be YYYY-MM-DD"
        ) from exc


def _decimal(value: object, name: str, *, optional: bool = False) -> Decimal | None:
    if value is None and optional:
        return None
    text = _text(value, name)
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise AdjustedMarketDataError(f"{name} is not decimal text") from exc
    if not result.is_finite():
        raise AdjustedMarketDataError(f"{name} must be finite")
    canonical = format(result, "f")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    if result == 0:
        canonical = "0"
    if canonical != text:
        raise AdjustedMarketDataError(
            f"{name} must be canonical decimal text"
        )
    return result


def _dict(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise AdjustedMarketDataError(f"{name} must be a JSON object")
    return value


def _strict_dict(
    value: object,
    name: str,
    fields: frozenset[str],
) -> dict[str, object]:
    result = _dict(value, name)
    unknown = sorted(set(result) - fields)
    missing = sorted(fields - set(result))
    if unknown or missing:
        raise AdjustedMarketDataError(
            f"{name} field mismatch; unknown={unknown}, missing={missing}"
        )
    return result


def _strict_json(text: str) -> object:
    def reject_constant(value: str) -> object:
        raise AdjustedMarketDataError(
            f"non-finite JSON constant {value!r} is forbidden"
        )

    def reject_duplicates(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise AdjustedMarketDataError(
                    f"duplicate JSON field is forbidden: {key}"
                )
            result[key] = value
        return result

    try:
        return json.loads(
            text,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except json.JSONDecodeError as exc:
        raise AdjustedMarketDataError("input is unreadable JSON") from exc


def _list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise AdjustedMarketDataError(f"{name} must be a JSON array")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Offline synthetic Stage 2F adjusted-market-data materialization. "
            "No network, database, backtest, model, trust, or promotion operation "
            "is available."
        )
    )
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-root", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        input_path = Path(args.input).expanduser().resolve(strict=True)
        output_root = Path(args.output_root).expanduser().resolve(strict=False)
        if not input_path.is_file() or input_path.is_symlink():
            raise AdjustedMarketDataError(
                "--input must be a regular non-symlink file"
            )
        try:
            input_path.relative_to(output_root)
        except ValueError:
            pass
        else:
            raise AdjustedMarketDataError(
                "input cannot be inside the output root"
            )
        try:
            request = _strict_dict(
                _strict_json(input_path.read_text(encoding="utf-8")),
                "request",
                _TOP_FIELDS,
            )
        except (OSError, UnicodeError) as exc:
            raise AdjustedMarketDataError("input is unreadable JSON") from exc
        if request["schema"] != _SCHEMA:
            raise AdjustedMarketDataError("unsupported Stage 2F request schema")
        if request.get("synthetic_fixture") is not True:
            raise AdjustedMarketDataError(
                "Stage 2F CLI accepts synthetic_fixture=true only"
            )
        as_of = _datetime(request.get("as_of"), "as_of")
        start_date = _date(request["start_date"], "start_date")
        end_date = _date(request["end_date"], "end_date")
        if start_date is None or end_date is None:
            raise AdjustedMarketDataError(
                "start_date and end_date cannot be null"
            )

        identity_value = _strict_dict(
            request["identity"],
            "identity",
            _IDENTITY_FIELDS,
        )
        identity_effective_from = _date(
            identity_value["effective_from"],
            "identity.effective_from",
        )
        if identity_effective_from is None:
            raise AdjustedMarketDataError(
                "identity.effective_from cannot be null"
            )
        identity = InstrumentIdentityFact(
            instrument_id=_text(
                identity_value.get("instrument_id"),
                "identity.instrument_id",
            ),
            symbol=_text(identity_value.get("symbol"), "identity.symbol"),
            market=Market(_text(identity_value.get("market"), "identity.market")),
            exchange=_text(
                identity_value.get("exchange"),
                "identity.exchange",
            ),
            security_type=SecurityType(
                _text(
                    identity_value.get("security_type"),
                    "identity.security_type",
                )
            ),
            effective_from=identity_effective_from,
            effective_to=_date(
                identity_value.get("effective_to"),
                "identity.effective_to",
                optional=True,
            ),
            known_at=_datetime(
                identity_value.get("known_at"),
                "identity.known_at",
            ),
            usable_from=_datetime(
                identity_value.get("known_at"),
                "identity.known_at",
            ),
            source="stage2f-cli-synthetic-identity",
            revision=_text(
                identity_value.get("revision"),
                "identity.revision",
            ),
            verified=True,
            source_note="synthetic Stage 2F CLI identity",
        )

        action_value = _strict_dict(
            request["corporate_action"],
            "corporate_action",
            _ACTION_FIELDS,
        )
        automatic = _decimal(
            action_value.get("automatic_share_ratio"),
            "automatic_share_ratio",
        )
        cash = _decimal(
            action_value.get("cash_dividend_per_share"),
            "cash_dividend_per_share",
        )
        rights = _decimal(
            action_value["rights_entitlement_ratio"],
            "rights_entitlement_ratio",
        )
        if automatic is None or cash is None or rights is None:
            raise AdjustedMarketDataError(
                "core corporate-action terms cannot be null"
            )
        action = CorporateActionFact(
            action_id=_text(action_value.get("action_id"), "action_id"),
            instrument_id=identity.instrument_id,
            identity_fact_id=identity.fact_id,
            symbol=identity.symbol,
            market=identity.market,
            ex_date=_date(action_value.get("ex_date"), "ex_date"),
            record_date=_date(
                action_value.get("record_date"),
                "record_date",
                optional=True,
            ),
            payment_date=_date(
                action_value.get("payment_date"),
                "payment_date",
                optional=True,
            ),
            share_listing_date=_date(
                action_value.get("share_listing_date"),
                "share_listing_date",
                optional=True,
            ),
            lifecycle=CorporateActionLifecycle.EFFECTIVE,
            automatic_share_ratio=automatic,
            cash_dividend_per_share=cash,
            rights_entitlement_ratio=rights,
            rights_subscription_price=_decimal(
                action_value.get("rights_subscription_price"),
                "rights_subscription_price",
                optional=True,
            ),
            currency=(
                None
                if action_value.get("currency") is None
                else _text(action_value.get("currency"), "currency")
            ),
            reference_price=_decimal(
                action_value.get("reference_price"),
                "reference_price",
                optional=True,
            ),
            reference_price_snapshot_id=(
                None
                if action_value.get("reference_price_snapshot_id") is None
                else _sha(
                    action_value.get("reference_price_snapshot_id"),
                    "reference_price_snapshot_id",
                )
            ),
            known_at=_datetime(action_value.get("known_at"), "action.known_at"),
            usable_from=_datetime(
                action_value.get("usable_from"),
                "action.usable_from",
            ),
            source="stage2f-cli-synthetic-actions",
            action_version="stage2f-cli-v1",
            revision=_text(action_value.get("revision"), "action.revision"),
            supersedes_revision=None,
            verified=True,
            source_note="synthetic Stage 2F CLI action",
        )
        coverage = CorporateActionCoverage(
            instrument_id=identity.instrument_id,
            market=identity.market,
            start_date=start_date,
            end_date=end_date,
            source=action.source,
            action_version=action.action_version,
            known_at=as_of,
            usable_from=as_of,
            revision="coverage-r1",
            supersedes_revision=None,
            verified=True,
            complete=True,
            source_note="synthetic complete Stage 2F CLI coverage",
        )
        snapshot = CorporateActionBook(
            (coverage,),
            (action,),
            (identity,),
        ).snapshot(
            identity.instrument_id,
            identity.market,
            start_date,
            end_date,
            as_of,
        )
        series = build_adjustment_series(
            snapshot,
            basis=AdjustmentBasis(_text(request.get("basis"), "basis")),
            convention=AdjustmentConvention(
                _text(request.get("convention"), "convention")
            ),
        )

        bars: list[Bar] = []
        for index, raw_bar in enumerate(_list(request["bars"], "bars")):
            bar = _strict_dict(
                raw_bar,
                f"bars[{index}]",
                _BAR_FIELDS,
            )
            open_price = _decimal(bar.get("open"), "bar.open")
            high = _decimal(bar.get("high"), "bar.high")
            low = _decimal(bar.get("low"), "bar.low")
            close = _decimal(bar.get("close"), "bar.close")
            amount = _decimal(bar.get("amount"), "bar.amount")
            turnover = _decimal(bar["turnover"], "bar.turnover")
            if None in (open_price, high, low, close, amount, turnover):
                raise AdjustedMarketDataError(
                    "bar price/amount/turnover fields cannot be null"
                )
            volume = bar.get("volume")
            if isinstance(volume, bool) or not isinstance(volume, int):
                raise AdjustedMarketDataError("bar.volume must be integer")
            bars.append(
                Bar(
                    symbol=identity.symbol,
                    market=identity.market,
                    timestamp=_datetime(bar.get("timestamp"), "bar.timestamp"),
                    interval="1d",
                    open=float(open_price),
                    high=float(high),
                    low=float(low),
                    close=float(close),
                    volume=volume,
                    amount=float(amount),
                    turnover=float(turnover),
                    source="stage2f-cli-raw-fixture",
                    adjustment_factor=1.0,
                )
            )
        raw_snapshot = RawBarSnapshot(
            raw_artifact_id=_sha(
                request.get("raw_artifact_id"),
                "raw_artifact_id",
            ),
            instrument_id=identity.instrument_id,
            identity_fact_id=identity.fact_id,
            symbol=identity.symbol,
            market=identity.market,
            start_date=start_date,
            end_date=end_date,
            as_of=as_of,
            bars=tuple(bars),
            source_note="synthetic Stage 2F CLI raw snapshot",
        )
        calendar_value = _strict_dict(
            request["calendar"],
            "calendar",
            _CALENDAR_FIELDS,
        )
        open_sessions = tuple(
            _date(value, "open_session")
            for value in _list(
                calendar_value.get("open_sessions"),
                "open_sessions",
            )
        )
        if any(item is None for item in open_sessions):
            raise AdjustedMarketDataError("open sessions cannot be null")
        calendar = CalendarMaterializationSnapshot(
            market=identity.market,
            start_date=start_date,
            end_date=end_date,
            as_of=as_of,
            open_sessions=tuple(item for item in open_sessions if item is not None),
            verified=True,
            complete=True,
            source_note="synthetic verified complete Stage 2F CLI Calendar",
        )
        policy_value = _strict_dict(
            request["policy"],
            "policy",
            _POLICY_FIELDS,
        )
        policy = AdjustedMarketDataPolicy(
            policy_version=_text(
                policy_value.get("policy_version"),
                "policy_version",
            ),
            session_gap_policy=SessionGapPolicy(
                _text(
                    policy_value.get("session_gap_policy"),
                    "session_gap_policy",
                )
            ),
        )
        explicit_gaps = tuple(
            item
            for item in (
                _date(value, "explicit_gap_session")
                for value in _list(
                    request.get("explicit_gap_sessions"),
                    "explicit_gap_sessions",
                )
            )
            if item is not None
        )
        dataset = materialize_adjusted_market_data(
            raw_snapshot=raw_snapshot,
            calendar_snapshot=calendar,
            identity=identity,
            series=series,
            policy=policy,
            explicit_gap_sessions=explicit_gaps,
        )
        artifact = write_adjusted_market_data_dataset(
            output_root,
            dataset=dataset,
        )
        result = {
            "schema": "stage2f-adjusted-market-data-result-v1",
            "dataset_id": dataset.dataset_id,
            "descriptor_key": artifact.descriptor_key,
            "data_key": artifact.data_key,
            "row_count": artifact.row_count,
            "gaps": list(dataset.gaps),
            "evidence_boundary": (
                "SYNTHETIC_CONTRACT_ONLY / LICENSE_PENDING / T3_NOT_REACHED"
            ),
        }
        print(
            json.dumps(
                result,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        )
        return 0
    except (AdjustedMarketDataError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

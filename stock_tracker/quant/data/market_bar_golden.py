"""Versioned synthetic golden raw payload packs for Stage 2G.

The committed pack mimics documented vendor envelopes so parser and
reconciliation contracts can be tested offline.  The loader forces every case
to remain synthetic and licence-pending; it cannot be relabelled as live or T3
evidence by editing one boolean and recomputing an ID.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from stock_tracker.core.types import Bar, Market, market_from_symbol

from ..core.fingerprint import fingerprint
from ..core.time import ensure_aware, to_utc
from .bar_artifact import capture_market_bars
from .manifest import safe_artifact_path, validate_storage_key
from .market_bar_reconciliation import (
    MarketBarField,
    MarketBarLicenseStatus,
    MarketBarReconciliationPolicy,
    MarketBarReconciliationReport,
    MarketBarSeriesEvidence,
    reconcile_market_bars,
)

PACK_SCHEMA = "stage2g-market-bar-golden-pack-v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
Parser = Callable[[bytes, str, Market, str], list[Bar]]


class MarketBarGoldenError(ValueError):
    """Raised when a golden fixture pack is malformed, changed, or relabelled."""


@dataclass(frozen=True, slots=True)
class MarketBarParserBinding:
    source: str
    schema_version: str
    parser_version: str
    parser: Parser = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        _text(self.source, "parser binding source")
        _text(self.schema_version, "parser binding schema_version")
        _text(self.parser_version, "parser binding parser_version")
        if not callable(self.parser):
            raise MarketBarGoldenError("parser binding parser must be callable")

    @property
    def binding_id(self) -> str:
        return fingerprint(
            {
                "schema": "stage2g-market-bar-parser-binding-v1",
                "source": self.source,
                "schema_version": self.schema_version,
                "parser_version": self.parser_version,
            }
        )


def _strict_json(raw: bytes) -> object:
    if not isinstance(raw, bytes) or not raw:
        raise MarketBarGoldenError("golden manifest must be non-empty bytes")

    def pairs_hook(pairs):
        output = {}
        for key, value in pairs:
            if key in output:
                raise MarketBarGoldenError("golden manifest contains duplicate JSON keys")
            output[key] = value
        return output

    def reject_constant(value: str):
        raise MarketBarGoldenError(f"golden manifest contains non-finite token: {value}")

    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except UnicodeDecodeError as exc:
        raise MarketBarGoldenError("golden manifest must use UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise MarketBarGoldenError("golden manifest is invalid JSON") from exc


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MarketBarGoldenError(f"{name} must be an object")
    return value


def _fields(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown:
        raise MarketBarGoldenError(f"{name} contains unknown field: {unknown[0]}")
    if missing:
        raise MarketBarGoldenError(f"{name} is missing field: {missing[0]}")


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise MarketBarGoldenError(f"{name} must be a non-empty trimmed string")
    if len(value) > 2048 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise MarketBarGoldenError(f"{name} contains invalid characters")
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name)
    if _SHA256.fullmatch(text) is None:
        raise MarketBarGoldenError(f"{name} must be lowercase SHA-256")
    return text


def _bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise MarketBarGoldenError(f"{name} must be boolean")
    return value


def _datetime(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MarketBarGoldenError(f"{name} must be ISO-8601") from exc
    return to_utc(ensure_aware(parsed, name))


def _date(value: object, name: str) -> date:
    text = _text(value, name)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise MarketBarGoldenError(f"{name} must use YYYY-MM-DD") from exc


@dataclass(frozen=True, slots=True)
class GoldenMarketBarSource:
    source: str
    source_family: str
    raw_file: str
    raw_sha256: str
    source_dataset: str
    provider_version: str
    schema_version: str
    parser_version: str
    endpoint: str
    comparable_fields: tuple[MarketBarField, ...]
    license_status: MarketBarLicenseStatus
    source_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "source",
            "source_family",
            "source_dataset",
            "provider_version",
            "schema_version",
            "parser_version",
            "endpoint",
        ):
            _text(getattr(self, name), name)
        if self.source_family != self.source:
            raise MarketBarGoldenError(
                "Stage 2G golden source_family must equal the captured source identity"
            )
        validate_storage_key(self.raw_file)
        _sha256(self.raw_sha256, "raw_sha256")
        if not self.comparable_fields or any(
            not isinstance(item, MarketBarField) for item in self.comparable_fields
        ):
            raise MarketBarGoldenError("comparable_fields is invalid")
        canonical_fields = tuple(sorted(set(self.comparable_fields), key=lambda item: item.value))
        if self.comparable_fields != canonical_fields:
            raise MarketBarGoldenError("comparable_fields must be sorted and unique")
        if self.license_status not in {
            MarketBarLicenseStatus.UNKNOWN,
            MarketBarLicenseStatus.PENDING,
        }:
            raise MarketBarGoldenError(
                "synthetic golden source cannot claim licence clearance"
            )
        object.__setattr__(
            self,
            "source_id",
            fingerprint(
                {
                    "schema": "stage2g-golden-market-bar-source-v1",
                    "source": self.source,
                    "source_family": self.source_family,
                    "raw_file": self.raw_file,
                    "raw_sha256": self.raw_sha256,
                    "source_dataset": self.source_dataset,
                    "provider_version": self.provider_version,
                    "schema_version": self.schema_version,
                    "parser_version": self.parser_version,
                    "endpoint": self.endpoint,
                    "comparable_fields": self.comparable_fields,
                    "license_status": self.license_status,
                }
            ),
        )

    @classmethod
    def from_mapping(cls, value: object) -> GoldenMarketBarSource:
        payload = _object(value, "golden source")
        expected = {
            "source",
            "source_family",
            "raw_file",
            "raw_sha256",
            "source_dataset",
            "provider_version",
            "schema_version",
            "parser_version",
            "endpoint",
            "comparable_fields",
            "license_status",
            "source_id",
        }
        _fields(payload, expected, "golden source")
        fields = payload["comparable_fields"]
        if not isinstance(fields, list) or any(type(item) is not str for item in fields):
            raise MarketBarGoldenError("comparable_fields must be a string array")
        try:
            comparable = tuple(MarketBarField(item) for item in fields)
            license_status = MarketBarLicenseStatus(payload["license_status"])
        except ValueError as exc:
            raise MarketBarGoldenError("golden source enum value is invalid") from exc
        source = cls(
            source=payload["source"],
            source_family=payload["source_family"],
            raw_file=payload["raw_file"],
            raw_sha256=payload["raw_sha256"],
            source_dataset=payload["source_dataset"],
            provider_version=payload["provider_version"],
            schema_version=payload["schema_version"],
            parser_version=payload["parser_version"],
            endpoint=payload["endpoint"],
            comparable_fields=comparable,
            license_status=license_status,
        )
        if _sha256(payload["source_id"], "source_id") != source.source_id:
            raise MarketBarGoldenError("golden source identity mismatch")
        return source

    def identity_payload(self) -> dict[str, object]:
        return {
            "source": self.source,
            "source_family": self.source_family,
            "raw_file": self.raw_file,
            "raw_sha256": self.raw_sha256,
            "source_dataset": self.source_dataset,
            "provider_version": self.provider_version,
            "schema_version": self.schema_version,
            "parser_version": self.parser_version,
            "endpoint": self.endpoint,
            "comparable_fields": [item.value for item in self.comparable_fields],
            "license_status": self.license_status.value,
            "source_id": self.source_id,
        }


@dataclass(frozen=True, slots=True)
class GoldenMarketBarCase:
    case_name: str
    market: Market
    symbol: str
    interval: str
    adjustment: str
    calendar_snapshot_id: str
    expected_open_sessions: tuple[date, ...]
    sources: tuple[GoldenMarketBarSource, ...]
    case_id: str = field(init=False)

    def __post_init__(self) -> None:
        _text(self.case_name, "case_name")
        if not isinstance(self.market, Market):
            raise MarketBarGoldenError("market is invalid")
        _text(self.symbol, "symbol")
        try:
            inferred_market = market_from_symbol(self.symbol)
        except ValueError as exc:
            raise MarketBarGoldenError("golden case symbol is invalid") from exc
        if inferred_market is not self.market:
            raise MarketBarGoldenError("golden case symbol/market mismatch")
        if self.interval != "1d":
            raise MarketBarGoldenError("Stage 2G golden cases currently support 1d only")
        if self.adjustment not in {"qfq", "hfq", "raw"}:
            raise MarketBarGoldenError("golden case adjustment is invalid")
        _sha256(self.calendar_snapshot_id, "calendar_snapshot_id")
        if any(type(item) is not date for item in self.expected_open_sessions):
            raise MarketBarGoldenError("expected_open_sessions must contain dates")
        if self.expected_open_sessions != tuple(
            sorted(set(self.expected_open_sessions))
        ) or not self.expected_open_sessions:
            raise MarketBarGoldenError(
                "expected_open_sessions must be non-empty, sorted and unique"
            )
        if len(self.sources) < 2 or any(
            not isinstance(item, GoldenMarketBarSource) for item in self.sources
        ):
            raise MarketBarGoldenError(
                "golden case requires at least two GoldenMarketBarSource values"
            )
        if self.sources != tuple(sorted(self.sources, key=lambda item: item.source_id)):
            raise MarketBarGoldenError("sources must be sorted by source_id")
        if len({item.source_family for item in self.sources}) != len(self.sources):
            raise MarketBarGoldenError("golden case sources must be independent families")
        object.__setattr__(
            self,
            "case_id",
            fingerprint(
                {
                    "schema": "stage2g-golden-market-bar-case-v1",
                    "case_name": self.case_name,
                    "market": self.market,
                    "symbol": self.symbol,
                    "interval": self.interval,
                    "adjustment": self.adjustment,
                    "calendar_snapshot_id": self.calendar_snapshot_id,
                    "expected_open_sessions": self.expected_open_sessions,
                    "source_ids": [item.source_id for item in self.sources],
                }
            ),
        )

    @classmethod
    def from_mapping(cls, value: object) -> GoldenMarketBarCase:
        payload = _object(value, "golden case")
        expected = {
            "case_name",
            "market",
            "symbol",
            "interval",
            "adjustment",
            "calendar_snapshot_id",
            "expected_open_sessions",
            "sources",
            "case_id",
        }
        _fields(payload, expected, "golden case")
        sessions = payload["expected_open_sessions"]
        sources = payload["sources"]
        if not isinstance(sessions, list):
            raise MarketBarGoldenError("expected_open_sessions must be an array")
        if not isinstance(sources, list):
            raise MarketBarGoldenError("sources must be an array")
        try:
            market = Market(payload["market"])
        except ValueError as exc:
            raise MarketBarGoldenError("golden case market is invalid") from exc
        parsed_sources = tuple(
            sorted(
                (GoldenMarketBarSource.from_mapping(item) for item in sources),
                key=lambda item: item.source_id,
            )
        )
        case = cls(
            case_name=payload["case_name"],
            market=market,
            symbol=payload["symbol"],
            interval=payload["interval"],
            adjustment=payload["adjustment"],
            calendar_snapshot_id=payload["calendar_snapshot_id"],
            expected_open_sessions=tuple(
                _date(item, "expected_open_session") for item in sessions
            ),
            sources=parsed_sources,
        )
        if _sha256(payload["case_id"], "case_id") != case.case_id:
            raise MarketBarGoldenError("golden case identity mismatch")
        return case

    def identity_payload(self) -> dict[str, object]:
        return {
            "case_name": self.case_name,
            "market": self.market.value,
            "symbol": self.symbol,
            "interval": self.interval,
            "adjustment": self.adjustment,
            "calendar_snapshot_id": self.calendar_snapshot_id,
            "expected_open_sessions": [
                item.isoformat() for item in self.expected_open_sessions
            ],
            "sources": [item.identity_payload() for item in self.sources],
            "case_id": self.case_id,
        }


@dataclass(frozen=True, slots=True)
class GoldenMarketBarPack:
    pack_version: str
    retrieved_at: datetime
    synthetic_fixture: bool
    cases: tuple[GoldenMarketBarCase, ...]
    pack_id: str = field(init=False)

    def __post_init__(self) -> None:
        _text(self.pack_version, "pack_version")
        retrieved = to_utc(ensure_aware(self.retrieved_at, "retrieved_at"))
        _bool(self.synthetic_fixture, "synthetic_fixture")
        if self.synthetic_fixture is not True:
            raise MarketBarGoldenError(
                "committed golden pack is synthetic-only and cannot be relabelled"
            )
        if self.cases != tuple(sorted(self.cases, key=lambda item: item.case_id)):
            raise MarketBarGoldenError("cases must be sorted by case_id")
        if not self.cases or len({item.case_id for item in self.cases}) != len(self.cases):
            raise MarketBarGoldenError("golden pack cases must be non-empty and unique")
        object.__setattr__(self, "retrieved_at", retrieved)
        object.__setattr__(
            self,
            "pack_id",
            fingerprint(
                {
                    "schema": PACK_SCHEMA,
                    "pack_version": self.pack_version,
                    "retrieved_at": retrieved,
                    "synthetic_fixture": True,
                    "cases": [item.identity_payload() for item in self.cases],
                }
            ),
        )

    def case(self, name: str) -> GoldenMarketBarCase:
        matches = [item for item in self.cases if item.case_name == name]
        if len(matches) != 1:
            raise MarketBarGoldenError(f"expected exactly one golden case named {name}")
        return matches[0]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": PACK_SCHEMA,
            "pack_version": self.pack_version,
            "retrieved_at": self.retrieved_at.isoformat().replace("+00:00", "Z"),
            "synthetic_fixture": True,
            "cases": [item.identity_payload() for item in self.cases],
            "pack_id": self.pack_id,
        }


def load_market_bar_golden_pack(
    manifest_path: str | Path,
) -> GoldenMarketBarPack:
    path = Path(manifest_path)
    if not path.is_file() or path.is_symlink():
        raise MarketBarGoldenError("golden manifest is missing or is a symlink")
    payload = _object(_strict_json(path.read_bytes()), "golden pack")
    expected = {
        "schema",
        "pack_version",
        "retrieved_at",
        "synthetic_fixture",
        "cases",
        "pack_id",
    }
    _fields(payload, expected, "golden pack")
    if payload["schema"] != PACK_SCHEMA:
        raise MarketBarGoldenError("golden pack schema is invalid")
    cases_value = payload["cases"]
    if not isinstance(cases_value, list):
        raise MarketBarGoldenError("golden pack cases must be an array")
    cases = tuple(
        sorted(
            (GoldenMarketBarCase.from_mapping(item) for item in cases_value),
            key=lambda item: item.case_id,
        )
    )
    pack = GoldenMarketBarPack(
        pack_version=payload["pack_version"],
        retrieved_at=_datetime(payload["retrieved_at"], "retrieved_at"),
        synthetic_fixture=_bool(payload["synthetic_fixture"], "synthetic_fixture"),
        cases=cases,
    )
    if _sha256(payload["pack_id"], "pack_id") != pack.pack_id:
        raise MarketBarGoldenError("golden pack identity mismatch")
    root = path.parent
    for case in pack.cases:
        for source in case.sources:
            raw_path = safe_artifact_path(root, source.raw_file)
            if not raw_path.is_file() or raw_path.is_symlink():
                raise MarketBarGoldenError("golden raw file is missing or is a symlink")
            digest = hashlib.sha256(raw_path.read_bytes()).hexdigest()
            if digest != source.raw_sha256:
                raise MarketBarGoldenError("golden raw file SHA-256 mismatch")
    return pack


def materialize_golden_case(
    *,
    manifest_path: str | Path,
    case_name: str,
    artifact_root: str | Path,
    parser_registry: Mapping[str, MarketBarParserBinding],
    policy: MarketBarReconciliationPolicy | None = None,
) -> tuple[GoldenMarketBarPack, GoldenMarketBarCase, MarketBarReconciliationReport]:
    pack = load_market_bar_golden_pack(manifest_path)
    case = pack.case(case_name)
    fixture_root = Path(manifest_path).parent
    evidence: list[MarketBarSeriesEvidence] = []
    for source in case.sources:
        binding = parser_registry.get(source.source)
        if binding is None or not isinstance(binding, MarketBarParserBinding):
            raise MarketBarGoldenError(
                f"no parser binding registered for golden source {source.source}"
            )
        if binding.source != source.source:
            raise MarketBarGoldenError("parser binding source identity mismatch")
        if binding.schema_version != source.schema_version:
            raise MarketBarGoldenError("parser binding schema version mismatch")
        if binding.parser_version != source.parser_version:
            raise MarketBarGoldenError("parser binding parser version mismatch")
        raw_path = safe_artifact_path(fixture_root, source.raw_file)
        raw = raw_path.read_bytes()
        captured = capture_market_bars(
            artifact_root,
            raw_bytes=raw,
            parser=binding.parser,
            symbol=case.symbol,
            market=case.market,
            interval=case.interval,
            retrieved_at=pack.retrieved_at,
            source=source.source,
            source_dataset=source.source_dataset,
            provider_version=source.provider_version,
            schema_version=source.schema_version,
            parser_version=source.parser_version,
            request_parameters={
                "adjustment": case.adjustment,
                "requested_start": case.expected_open_sessions[0].isoformat(),
                "requested_end": case.expected_open_sessions[-1].isoformat(),
                "endpoint": source.endpoint,
                "interval": case.interval,
                "synthetic_fixture": True,
            },
            known_at_policy="synthetic-fixture-retrieved-at",
            revision_policy=f"synthetic-golden-{pack.pack_version}",
            source_note=(
                "SYNTHETIC vendor-shaped golden payload; parser/reconciliation contract only"
            ),
        )
        evidence.append(
            MarketBarSeriesEvidence(
                captured=captured,
                source_family=source.source_family,
                adjustment=case.adjustment,
                comparable_fields=source.comparable_fields,
                license_status=source.license_status,
                synthetic_fixture=True,
            )
        )
    report = reconcile_market_bars(
        as_of=pack.retrieved_at,
        calendar_snapshot_id=case.calendar_snapshot_id,
        expected_open_sessions=case.expected_open_sessions,
        series=evidence,
        policy=policy,
    )
    return pack, case, report


__all__ = [
    "PACK_SCHEMA",
    "GoldenMarketBarCase",
    "GoldenMarketBarPack",
    "GoldenMarketBarSource",
    "MarketBarGoldenError",
    "MarketBarParserBinding",
    "load_market_bar_golden_pack",
    "materialize_golden_case",
]

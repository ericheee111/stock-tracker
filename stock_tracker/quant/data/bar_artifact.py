"""Immutable capture of exact market-bar responses before normalization.

This module deliberately sits outside the operational SQLite bar cache. It
persists provider bytes first, then parses those exact bytes and emits a
content-addressed :class:`RawDataArtifact` descriptor. A raw capture is capped
at BEST_EFFORT; higher trust tiers require a separate verifier and a higher-level
snapshot that binds calendars, universe membership, corporate actions and
market/cost rules.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from stock_tracker.core.types import Bar, Market

from ..core.fingerprint import canonical_json, fingerprint, hash_file
from ..core.time import ensure_aware, market_timezone
from .manifest import (
    DataFormat,
    DataKind,
    ManifestContractError,
    RawDataArtifact,
    safe_artifact_path,
    validate_storage_key,
)

BarParser = Callable[[bytes, str, Market, str], Sequence[Bar]]
_SYMBOL_TOKEN = re.compile(r"^[A-Za-z0-9._-]+$")
_FORMAT_SUFFIX = {
    DataFormat.CSV: "csv",
    DataFormat.JSON: "json",
    DataFormat.JSONL: "jsonl",
    DataFormat.PARQUET: "parquet",
    DataFormat.SQLITE: "sqlite",
    DataFormat.TOML: "toml",
    DataFormat.BINARY: "bin",
}


class DataTrustTier(StrEnum):
    UNKNOWN = "UNKNOWN"
    BEST_EFFORT = "BEST_EFFORT"
    OPERATIONAL_VERIFIED = "OPERATIONAL_VERIFIED"
    RESEARCH_GRADE = "RESEARCH_GRADE"
    FROZEN_HOLDOUT = "FROZEN_HOLDOUT"


@dataclass(frozen=True, slots=True)
class CapturedBarArtifact:
    artifact: RawDataArtifact
    bars: tuple[Bar, ...]
    trust_tier: DataTrustTier
    parser_version: str
    request_parameters: dict[str, Any]
    normalized_dataset_id: str
    descriptor_key: str
    capture_id: str


def _expect_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ManifestContractError(f"{name} must be a boolean")
    return value


def _expect_str(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestContractError(f"{name} must be a non-empty string")
    return value


def _normalize_request_parameters(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ManifestContractError(
            "request_parameters must be a JSON object with string keys"
        )
    try:
        normalized = json.loads(canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise ManifestContractError("request_parameters are not canonical") from exc
    if not isinstance(normalized, dict):
        raise ManifestContractError("request_parameters must normalize to an object")
    required = {"adjustment", "requested_start", "requested_end", "endpoint"}
    missing = sorted(required - set(normalized))
    if missing:
        raise ManifestContractError(
            "request_parameters missing fields: " + ", ".join(missing)
        )
    _expect_str(normalized.get("adjustment"), "request_parameters.adjustment")
    _expect_str(normalized.get("endpoint"), "request_parameters.endpoint")
    parsed_dates: dict[str, date] = {}
    for name in ("requested_start", "requested_end"):
        item = normalized.get(name)
        if item is None:
            continue
        if not isinstance(item, str):
            raise ManifestContractError(
                f"request_parameters.{name} must be an ISO date or null"
            )
        try:
            parsed_dates[name] = date.fromisoformat(item)
        except ValueError as exc:
            raise ManifestContractError(
                f"request_parameters.{name} must be an ISO date or null"
            ) from exc
    if (
        "requested_start" in parsed_dates
        and "requested_end" in parsed_dates
        and parsed_dates["requested_end"] < parsed_dates["requested_start"]
    ):
        raise ManifestContractError(
            "request_parameters.requested_end cannot precede requested_start"
        )
    return cast(dict[str, Any], normalized)


def _descriptor_key(
    *,
    artifact_id: str,
    symbol: str,
    market: Market,
    interval: str,
    parser_version: str,
    request_parameters: dict[str, Any],
) -> str:
    token = fingerprint(
        {
            "schema": "captured-market-bars-descriptor-key-v1",
            "artifact_id": artifact_id,
            "symbol": symbol,
            "market": market.value,
            "interval": interval,
            "parser_version": parser_version,
            "request_parameters": request_parameters,
        }
    )
    return validate_storage_key(f"manifests/market-bars/{token}.json")


def _validate_capture_tier(
    trust_tier: DataTrustTier,
    *,
    verified: bool,
    calendar_snapshot_id: str | None,
) -> None:
    if trust_tier not in {DataTrustTier.UNKNOWN, DataTrustTier.BEST_EFFORT}:
        raise ManifestContractError(
            "raw capture cannot self-promote above BEST_EFFORT"
        )
    if verified or calendar_snapshot_id is not None:
        raise ManifestContractError(
            "raw capture cannot self-declare verification or calendar binding"
        )


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise ManifestContractError(
                f"immutable artifact path already contains different bytes: {path.name}"
            )
        return

    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.tmp-",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    _atomic_write_bytes(path, encoded)


def _normalized_rows(
    bars: Sequence[Bar],
    *,
    symbol: str,
    market: Market,
    interval: str,
    source: str,
) -> list[dict[str, Any]]:
    if not bars:
        raise ManifestContractError("captured market-bar response produced no rows")

    rows: list[dict[str, Any]] = []
    previous: datetime | None = None
    for index, bar in enumerate(bars):
        if not isinstance(bar, Bar):
            raise ManifestContractError(f"parser row {index} is not Bar")
        if bar.symbol != symbol or bar.market is not market or bar.interval != interval:
            raise ManifestContractError("parsed bar identity differs from capture request")
        if bar.source != source:
            raise ManifestContractError("parsed bar source differs from capture source")
        if previous is not None and bar.timestamp <= previous:
            raise ManifestContractError("parsed bars must be strictly chronological")
        prices = (bar.open, bar.high, bar.low, bar.close)
        if any(not math.isfinite(value) or value <= 0 for value in prices):
            raise ManifestContractError("parsed bar OHLC must be finite and positive")
        if bar.low > min(bar.open, bar.close, bar.high):
            raise ManifestContractError("parsed bar low is inconsistent with OHLC")
        if bar.high < max(bar.open, bar.close, bar.low):
            raise ManifestContractError("parsed bar high is inconsistent with OHLC")
        numeric_nonnegative = (bar.amount, bar.turnover, bar.adjustment_factor)
        if bar.volume < 0 or any(
            not math.isfinite(value) or value < 0 for value in numeric_nonnegative
        ):
            raise ManifestContractError("parsed bar volume/amount metadata is invalid")
        if bar.adjustment_factor <= 0:
            raise ManifestContractError("parsed bar adjustment_factor must be positive")
        previous = bar.timestamp
        rows.append(
            {
                "symbol": bar.symbol,
                "market": bar.market.value,
                "timestamp": bar.timestamp.isoformat(),
                "session_date": bar.timestamp.date().isoformat(),
                "interval": bar.interval,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                "amount": bar.amount,
                "turnover": bar.turnover,
                "source": bar.source,
                "adjustment_factor": bar.adjustment_factor,
            }
        )
    return rows


def _content_bounds(bars: Sequence[Bar], market: Market) -> tuple[datetime, datetime]:
    zone = market_timezone(market)
    first = datetime.combine(bars[0].timestamp.date(), time.min, tzinfo=zone)
    last = datetime.combine(bars[-1].timestamp.date(), time.max, tzinfo=zone)
    return first, last


def _capture_payload(
    *,
    artifact: RawDataArtifact,
    symbol: str,
    market: Market,
    interval: str,
    trust_tier: DataTrustTier,
    parser_version: str,
    request_parameters: dict[str, Any],
    normalized_dataset_id: str,
    descriptor_key: str,
) -> dict[str, Any]:
    return {
        "schema": "captured-market-bars-v1",
        "artifact": artifact.as_dict(),
        "symbol": symbol,
        "market": market.value,
        "interval": interval,
        "trust_tier": trust_tier.value,
        "parser_version": parser_version,
        "request_parameters": request_parameters,
        "normalized_dataset_id": normalized_dataset_id,
        "descriptor_key": descriptor_key,
    }


def capture_market_bars(
    root: str | Path,
    *,
    raw_bytes: bytes,
    parser: BarParser,
    symbol: str,
    market: Market,
    interval: str,
    retrieved_at: datetime,
    source: str,
    source_dataset: str,
    provider_version: str,
    schema_version: str,
    parser_version: str,
    request_parameters: dict[str, Any],
    raw_format: DataFormat = DataFormat.JSON,
    known_at_policy: str = "retrieved-at",
    revision_policy: str = "content-addressed-immutable",
    verified: bool = False,
    source_note: str = "best-effort public source; not yet research grade",
    calendar_snapshot_id: str | None = None,
    trust_tier: DataTrustTier = DataTrustTier.BEST_EFFORT,
) -> CapturedBarArtifact:
    """Persist exact response bytes and bind deterministic normalized rows.

    Raw bytes are written before the parser is invoked.  If parsing fails, the
    content-addressed raw payload remains available for quarantine/forensics but
    no descriptor is emitted.
    """

    ensure_aware(retrieved_at, "retrieved_at")
    _expect_bool(verified, "verified")
    if not isinstance(raw_bytes, bytes) or not raw_bytes:
        raise ManifestContractError("raw_bytes must be non-empty bytes")
    if not _SYMBOL_TOKEN.fullmatch(symbol):
        raise ManifestContractError("symbol contains unsupported storage-key characters")
    if not interval or not source or not source_dataset:
        raise ManifestContractError("interval/source/source_dataset must be non-empty")
    if not provider_version or not schema_version or not parser_version:
        raise ManifestContractError("provider/schema/parser versions must be non-empty")
    if not isinstance(raw_format, DataFormat):
        raise ManifestContractError("raw_format must be DataFormat")
    if not isinstance(trust_tier, DataTrustTier):
        raise ManifestContractError("trust_tier must be DataTrustTier")
    _validate_capture_tier(
        trust_tier,
        verified=verified,
        calendar_snapshot_id=calendar_snapshot_id,
    )
    normalized_request_parameters = _normalize_request_parameters(
        request_parameters
    )

    root_path = Path(root)
    root_path.mkdir(parents=True, exist_ok=True)
    raw_hash = hashlib.sha256(raw_bytes).hexdigest()
    suffix = _FORMAT_SUFFIX[raw_format]
    storage_key = validate_storage_key(
        f"raw/market-bars/{market.value.lower()}/{symbol}/{raw_hash}.{suffix}"
    )
    raw_path = safe_artifact_path(root_path, storage_key)
    _atomic_write_bytes(raw_path, raw_bytes)

    bars = tuple(parser(raw_bytes, symbol, market, interval))
    rows = _normalized_rows(
        bars,
        symbol=symbol,
        market=market,
        interval=interval,
        source=source,
    )
    content_start, content_end = _content_bounds(bars, market)
    artifact = RawDataArtifact.from_file(
        root_path,
        storage_key=storage_key,
        kind=DataKind.MARKET_BARS,
        format=raw_format,
        market=market,
        source=source,
        source_dataset=source_dataset,
        row_count=len(rows),
        content_start=content_start,
        content_end=content_end,
        retrieved_at=retrieved_at,
        provider_version=provider_version,
        schema_version=schema_version,
        adapter_version=parser_version,
        known_at_policy=known_at_policy,
        revision_policy=revision_policy,
        verified=verified,
        source_note=source_note,
        calendar_snapshot_id=calendar_snapshot_id,
    )
    normalized_dataset_id = fingerprint(
        {
            "schema": "normalized-market-bars-v1",
            "artifact_id": artifact.artifact_id,
            "parser_version": parser_version,
            "rows": rows,
        }
    )
    descriptor_key = _descriptor_key(
        artifact_id=artifact.artifact_id,
        symbol=symbol,
        market=market,
        interval=interval,
        parser_version=parser_version,
        request_parameters=normalized_request_parameters,
    )
    payload = _capture_payload(
        artifact=artifact,
        symbol=symbol,
        market=market,
        interval=interval,
        trust_tier=trust_tier,
        parser_version=parser_version,
        request_parameters=normalized_request_parameters,
        normalized_dataset_id=normalized_dataset_id,
        descriptor_key=descriptor_key,
    )
    capture_id = fingerprint(payload)
    payload["capture_id"] = capture_id
    descriptor_path = safe_artifact_path(root_path, descriptor_key)
    _atomic_write_json(descriptor_path, payload)
    return CapturedBarArtifact(
        artifact=artifact,
        bars=bars,
        trust_tier=trust_tier,
        parser_version=parser_version,
        request_parameters=dict(normalized_request_parameters),
        normalized_dataset_id=normalized_dataset_id,
        descriptor_key=descriptor_key,
        capture_id=capture_id,
    )


def load_captured_market_bars(
    root: str | Path,
    *,
    descriptor_key: str,
    parser: BarParser,
) -> CapturedBarArtifact:
    """Re-verify descriptor identity, raw bytes and deterministic parsing."""

    root_path = Path(root)
    descriptor_path = safe_artifact_path(root_path, descriptor_key)
    value = json.loads(descriptor_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ManifestContractError("capture descriptor must be a JSON object")
    payload = cast(dict[str, Any], value)
    allowed = {
        "schema",
        "artifact",
        "symbol",
        "market",
        "interval",
        "trust_tier",
        "parser_version",
        "request_parameters",
        "normalized_dataset_id",
        "descriptor_key",
        "capture_id",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ManifestContractError(
            "capture descriptor contains unknown fields: " + ", ".join(unknown)
        )
    if payload.get("schema") != "captured-market-bars-v1":
        raise ManifestContractError("unsupported capture descriptor schema")
    expected_capture_id = payload.get("capture_id")
    identity_payload = dict(payload)
    identity_payload.pop("capture_id", None)
    if not isinstance(expected_capture_id, str) or fingerprint(identity_payload) != expected_capture_id:
        raise ManifestContractError("capture_id does not match descriptor content")
    if payload.get("descriptor_key") != descriptor_key:
        raise ManifestContractError("descriptor_key does not match requested descriptor")

    artifact_value = payload.get("artifact")
    if not isinstance(artifact_value, dict):
        raise ManifestContractError("capture descriptor artifact must be an object")
    artifact = RawDataArtifact.from_dict(cast(dict[str, Any], artifact_value))
    artifact.verify_file(root_path)

    try:
        market = Market(_expect_str(payload.get("market"), "market"))
        trust_tier = DataTrustTier(
            _expect_str(payload.get("trust_tier"), "trust_tier")
        )
        symbol = _expect_str(payload.get("symbol"), "symbol")
        interval = _expect_str(payload.get("interval"), "interval")
        parser_version = _expect_str(
            payload.get("parser_version"),
            "parser_version",
        )
    except ValueError as exc:
        raise ManifestContractError("capture descriptor identity is invalid") from exc
    request_parameters = _normalize_request_parameters(
        payload.get("request_parameters")
    )
    expected_descriptor_key = _descriptor_key(
        artifact_id=artifact.artifact_id,
        symbol=symbol,
        market=market,
        interval=interval,
        parser_version=parser_version,
        request_parameters=request_parameters,
    )
    if expected_descriptor_key != descriptor_key:
        raise ManifestContractError("descriptor path does not match descriptor identity")
    _validate_capture_tier(
        trust_tier,
        verified=artifact.verified,
        calendar_snapshot_id=artifact.calendar_snapshot_id,
    )

    raw_path = safe_artifact_path(root_path, artifact.storage_key)
    if hash_file(raw_path) != artifact.sha256:
        raise ManifestContractError("raw artifact hash changed")
    bars = tuple(parser(raw_path.read_bytes(), symbol, market, interval))
    rows = _normalized_rows(
        bars,
        symbol=symbol,
        market=market,
        interval=interval,
        source=artifact.source,
    )
    normalized_dataset_id = fingerprint(
        {
            "schema": "normalized-market-bars-v1",
            "artifact_id": artifact.artifact_id,
            "parser_version": parser_version,
            "rows": rows,
        }
    )
    expected_dataset_id = _expect_str(
        payload.get("normalized_dataset_id"),
        "normalized_dataset_id",
    )
    if normalized_dataset_id != expected_dataset_id:
        raise ManifestContractError("normalized dataset identity changed")
    return CapturedBarArtifact(
        artifact=artifact,
        bars=bars,
        trust_tier=trust_tier,
        parser_version=parser_version,
        request_parameters=dict(request_parameters),
        normalized_dataset_id=normalized_dataset_id,
        descriptor_key=descriptor_key,
        capture_id=expected_capture_id,
    )

"""Offline corporate-action extraction and Stage 2A identity binding.

Stage 2D is deliberately evidence-first.  It accepts an immutable Stage 2C raw
capture, extracts source-native rows, and only then binds those rows to stable
``InstrumentIdentityFact`` values supplied by the caller.  Source rows are never
allowed to self-assert ``instrument_id`` or ``identity_fact_id``.

The module supports two deterministic extraction lanes:

* a frozen synthetic/offline HTML table contract parsed with ``html.parser``;
* a strict structured extraction document for PDF/XLS/XLSX/manual review.

Neither lane promotes trust, completeness, T2/T3, or research-grade status.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from html.parser import HTMLParser
from pathlib import Path

from stock_tracker.core.types import Market

from ..core.calendar import select_superseding_revision
from ..core.fingerprint import fingerprint
from ..core.point_in_time import PITConflictError
from ..core.time import ensure_aware, to_utc
from ..core.universe import InstrumentIdentityFact
from .corporate_action_adapter import (
    CandidateCorporateAction,
    CandidateCorporateActionLifecycle,
    CandidateCorporateActionType,
    CorporateActionAdapterError,
    CorporateActionRawCapture,
    SourcePublishedGranularity,
    load_corporate_action_raw,
)
from .manifest import safe_artifact_path, validate_storage_key

HTML_EXTRACTION_SCHEMA = "stage2d-corporate-action-html-v1"
STRUCTURED_EXTRACTION_SCHEMA = "stage2d-corporate-action-extraction-v1"
EXTRACTION_DESCRIPTOR_SCHEMA = "stage2d-corporate-action-extraction-descriptor-v1"
IDENTITY_BINDING_POLICY_VERSION = "stage2d-identity-binding-v1"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_SYMBOL_SUFFIXES = {
    Market.A: frozenset({"SH", "SZ"}),
    Market.HK: frozenset({"HK"}),
    Market.US: frozenset({"US"}),
}

_HTML_HEADERS = (
    "source_event_id",
    "source_security_id",
    "symbol",
    "market",
    "exchange",
    "action_type",
    "lifecycle",
    "source_published_at",
    "source_published_granularity",
    "ex_date",
    "record_date",
    "payment_date",
    "share_listing_date",
    "effective_date",
    "automatic_share_ratio",
    "cash_dividend_per_share",
    "rights_entitlement_ratio",
    "rights_subscription_price",
    "currency",
    "reference_price",
    "reference_price_snapshot_id",
    "revision_id",
    "supersedes_revision_id",
    "source_locator",
)

_ROW_FIELDS = frozenset(_HTML_HEADERS)
_TOP_FIELDS = frozenset(
    {
        "schema",
        "synthetic_fixture",
        "extractor_name",
        "extractor_version",
        "reviewer_note",
        "rows",
    }
)


class CorporateActionExtractionError(ValueError):
    """Raised when extraction or identity binding is ambiguous or unsafe."""


class ExtractionMethod(StrEnum):
    FROZEN_HTML_TABLE = "FROZEN_HTML_TABLE"
    STRUCTURED_MANUAL = "STRUCTURED_MANUAL"
    STRUCTURED_EXTERNAL_TOOL = "STRUCTURED_EXTERNAL_TOOL"


class IdentityBindingStatus(StrEnum):
    BOUND = "BOUND"
    UNBOUND = "UNBOUND"
    AMBIGUOUS = "AMBIGUOUS"
    FUTURE = "FUTURE"
    INACTIVE = "INACTIVE"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"


class SourceMappingStatus(StrEnum):
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


def _require_text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise CorporateActionExtractionError(
            f"{name} must be a non-empty trimmed string"
        )
    return value


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise CorporateActionExtractionError(f"{name} must be a boolean")
    return value


def _require_sha256(value: object, name: str) -> str:
    text = _require_text(value, name)
    if _SHA256.fullmatch(text) is None:
        raise CorporateActionExtractionError(f"{name} must be lowercase SHA-256")
    return text


def _parse_datetime(value: object, name: str) -> datetime:
    text = _require_text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CorporateActionExtractionError(
            f"{name} must be ISO-8601 datetime"
        ) from exc
    ensure_aware(parsed, name)
    return to_utc(parsed)


def _parse_optional_date(value: object, name: str) -> date | None:
    if value in (None, ""):
        return None
    text = _require_text(value, name)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise CorporateActionExtractionError(
            f"{name} must be YYYY-MM-DD or null"
        ) from exc


def _parse_optional_decimal(value: object, name: str) -> Decimal | None:
    if value in (None, ""):
        return None
    if type(value) is not str:
        raise CorporateActionExtractionError(
            f"{name} must be a canonical decimal string or null"
        )
    if value != value.strip() or not value:
        raise CorporateActionExtractionError(
            f"{name} must be a canonical decimal string"
        )
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise CorporateActionExtractionError(f"{name} is not a decimal") from exc
    if not parsed.is_finite():
        raise CorporateActionExtractionError(f"{name} must be finite")
    canonical = format(parsed, "f")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    if parsed == 0:
        canonical = "0"
    if value != canonical:
        raise CorporateActionExtractionError(
            f"{name} must use canonical decimal text {canonical!r}"
        )
    return parsed


def _parse_publication(
    value: object,
    granularity: SourcePublishedGranularity,
) -> date | datetime | None:
    if granularity is SourcePublishedGranularity.UNKNOWN:
        if value not in (None, ""):
            raise CorporateActionExtractionError(
                "UNKNOWN publication granularity requires null publication"
            )
        return None
    if granularity is SourcePublishedGranularity.DATE:
        if value in (None, ""):
            raise CorporateActionExtractionError(
                "DATE publication requires YYYY-MM-DD"
            )
        return _parse_optional_date(value, "source_published_at")
    if value in (None, ""):
        raise CorporateActionExtractionError(
            "SECOND publication requires timezone-aware datetime"
        )
    return _parse_datetime(value, "source_published_at")


def _published_text(value: date | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return to_utc(value).isoformat().replace("+00:00", "Z")
    return value.isoformat()


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if value == 0 else text


def _strict_json_loads(payload: bytes, name: str) -> object:
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CorporateActionExtractionError(f"{name} must be strict UTF-8") from exc

    def reject_constant(value: str) -> object:
        raise CorporateActionExtractionError(
            f"non-finite JSON constant {value!r} is forbidden"
        )

    try:
        return json.loads(text, parse_constant=reject_constant)
    except json.JSONDecodeError as exc:
        raise CorporateActionExtractionError(f"{name} is not valid JSON") from exc


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise CorporateActionExtractionError(
                f"immutable extraction path contains different bytes: {path.name}"
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


@dataclass(frozen=True, slots=True)
class ExtractedCorporateActionRow:
    source_event_id: str
    source_security_id: str
    symbol: str
    market: Market
    exchange: str
    action_type: CandidateCorporateActionType
    lifecycle: CandidateCorporateActionLifecycle
    source_published_at: date | datetime | None
    source_published_granularity: SourcePublishedGranularity
    ex_date: date | None
    record_date: date | None
    payment_date: date | None
    share_listing_date: date | None
    effective_date: date | None
    automatic_share_ratio: Decimal | None
    cash_dividend_per_share: Decimal | None
    rights_entitlement_ratio: Decimal | None
    rights_subscription_price: Decimal | None
    currency: str | None
    reference_price: Decimal | None
    reference_price_snapshot_id: str | None
    revision_id: str
    supersedes_revision_id: str | None
    source_locator: str
    gaps: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        _require_text(self.source_event_id, "source_event_id")
        _require_text(self.source_security_id, "source_security_id")
        if not isinstance(self.market, Market):
            raise CorporateActionExtractionError("market must be Market")
        _require_text(self.exchange, "exchange")
        code, separator, suffix = self.symbol.rpartition(".")
        if (
            type(self.symbol) is not str
            or self.symbol != self.symbol.upper()
            or not separator
            or not code
            or suffix not in _SYMBOL_SUFFIXES[self.market]
        ):
            raise CorporateActionExtractionError("symbol is not canonical for market")
        if not isinstance(self.action_type, CandidateCorporateActionType):
            raise CorporateActionExtractionError(
                "action_type must be CandidateCorporateActionType"
            )
        if not isinstance(self.lifecycle, CandidateCorporateActionLifecycle):
            raise CorporateActionExtractionError(
                "lifecycle must be CandidateCorporateActionLifecycle"
            )
        if not isinstance(
            self.source_published_granularity,
            SourcePublishedGranularity,
        ):
            raise CorporateActionExtractionError(
                "publication granularity must be SourcePublishedGranularity"
            )
        if self.source_published_granularity is SourcePublishedGranularity.DATE:
            if type(self.source_published_at) is not date:
                raise CorporateActionExtractionError(
                    "DATE publication must remain a date without fabricated time"
                )
        elif self.source_published_granularity is SourcePublishedGranularity.SECOND:
            if not isinstance(self.source_published_at, datetime):
                raise CorporateActionExtractionError(
                    "SECOND publication requires datetime"
                )
            ensure_aware(self.source_published_at, "source_published_at")
        elif self.source_published_at is not None:
            raise CorporateActionExtractionError(
                "UNKNOWN publication granularity requires null value"
            )
        for name in (
            "automatic_share_ratio",
            "cash_dividend_per_share",
            "rights_entitlement_ratio",
            "rights_subscription_price",
            "reference_price",
        ):
            value = getattr(self, name)
            if value is not None and (type(value) is not Decimal or not value.is_finite()):
                raise CorporateActionExtractionError(
                    f"{name} must be finite Decimal or null"
                )
        if self.automatic_share_ratio is not None and self.automatic_share_ratio <= 0:
            raise CorporateActionExtractionError(
                "automatic_share_ratio must be positive"
            )
        for name in ("cash_dividend_per_share", "rights_entitlement_ratio"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise CorporateActionExtractionError(f"{name} cannot be negative")
        for name in ("rights_subscription_price", "reference_price"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise CorporateActionExtractionError(f"{name} must be positive")
        if self.currency is not None and (
            type(self.currency) is not str or _CURRENCY.fullmatch(self.currency) is None
        ):
            raise CorporateActionExtractionError(
                "currency must be uppercase three-letter text"
            )
        if self.reference_price_snapshot_id is not None:
            _require_sha256(
                self.reference_price_snapshot_id,
                "reference_price_snapshot_id",
            )
        _require_text(self.revision_id, "revision_id")
        if self.supersedes_revision_id is not None:
            _require_text(self.supersedes_revision_id, "supersedes_revision_id")
            if self.supersedes_revision_id == self.revision_id:
                raise CorporateActionExtractionError("revision cannot supersede itself")
        _require_text(self.source_locator, "source_locator")
        object.__setattr__(self, "gaps", self._derive_gaps())

    def _derive_gaps(self) -> tuple[str, ...]:
        gaps: set[str] = set()
        if self.source_published_granularity is SourcePublishedGranularity.DATE:
            gaps.add("DATE_ONLY_PUBLICATION_NO_INTRADAY_PRECISION")
        if self.lifecycle not in {
            CandidateCorporateActionLifecycle.EFFECTIVE,
            CandidateCorporateActionLifecycle.COMPLETED,
            CandidateCorporateActionLifecycle.CANCELLED,
        }:
            gaps.add("ACTION_NOT_IMPLEMENTED")
        if self.action_type in {
            CandidateCorporateActionType.PLACEMENT_OR_ISSUANCE,
            CandidateCorporateActionType.MERGER_OR_CONVERSION,
            CandidateCorporateActionType.OTHER,
            CandidateCorporateActionType.UNKNOWN,
        }:
            gaps.add(f"UNSUPPORTED_ACTION_TYPE_{self.action_type.value}")
        if self.ex_date is None:
            gaps.add("MISSING_EX_DATE")
        if self.record_date is None:
            gaps.add("MISSING_RECORD_DATE")
        if (
            self.lifecycle
            in {
                CandidateCorporateActionLifecycle.EFFECTIVE,
                CandidateCorporateActionLifecycle.COMPLETED,
            }
            and self.effective_date is None
        ):
            gaps.add("MISSING_EFFECTIVE_DATE")
        if self.lifecycle is CandidateCorporateActionLifecycle.CANCELLED:
            if any(
                value is not None
                for value in (
                    self.automatic_share_ratio,
                    self.cash_dividend_per_share,
                    self.rights_entitlement_ratio,
                    self.rights_subscription_price,
                    self.currency,
                    self.reference_price,
                    self.reference_price_snapshot_id,
                )
            ):
                gaps.add("CANCELLED_ACTION_CARRIES_TERMS")
            return tuple(sorted(gaps))
        if self.automatic_share_ratio is None:
            gaps.add("MISSING_AUTOMATIC_SHARE_RATIO")
        elif self.automatic_share_ratio != Decimal(1) and self.share_listing_date is None:
            gaps.add("MISSING_SHARE_LISTING_DATE")
        if self.cash_dividend_per_share is None:
            gaps.add("MISSING_CASH_DIVIDEND_PER_SHARE")
        if self.rights_entitlement_ratio is None:
            gaps.add("MISSING_RIGHTS_ENTITLEMENT_RATIO")
        elif self.rights_entitlement_ratio > 0 and self.rights_subscription_price is None:
            gaps.add("MISSING_RIGHTS_SUBSCRIPTION_PRICE")
        monetary = (
            (self.cash_dividend_per_share or Decimal(0)) > 0
            or (self.rights_entitlement_ratio or Decimal(0)) > 0
            or self.reference_price is not None
        )
        if monetary and self.currency is None:
            gaps.add("MISSING_CURRENCY")
        if (
            (self.cash_dividend_per_share or Decimal(0)) > 0
            or (self.rights_entitlement_ratio or Decimal(0)) > 0
        ) and self.reference_price is None:
            gaps.add("MISSING_REFERENCE_PRICE")
        if self.reference_price is not None and self.reference_price_snapshot_id is None:
            gaps.add("MISSING_REFERENCE_PRICE_SNAPSHOT_ID")
        if (
            self.automatic_share_ratio == Decimal(1)
            and self.cash_dividend_per_share == Decimal(0)
            and self.rights_entitlement_ratio == Decimal(0)
        ):
            gaps.add("NO_EFFECTIVE_ECONOMIC_TERMS")
        return tuple(sorted(gaps))

    @property
    def row_id(self) -> str:
        return fingerprint(self)

    @property
    def revision_stream(self) -> tuple[str, str]:
        return (self.source_security_id, self.source_event_id)

    def as_dict(self) -> dict[str, object]:
        return {
            "source_event_id": self.source_event_id,
            "source_security_id": self.source_security_id,
            "symbol": self.symbol,
            "market": self.market.value,
            "exchange": self.exchange,
            "action_type": self.action_type.value,
            "lifecycle": self.lifecycle.value,
            "source_published_at": _published_text(self.source_published_at),
            "source_published_granularity": self.source_published_granularity.value,
            "ex_date": None if self.ex_date is None else self.ex_date.isoformat(),
            "record_date": (
                None if self.record_date is None else self.record_date.isoformat()
            ),
            "payment_date": (
                None if self.payment_date is None else self.payment_date.isoformat()
            ),
            "share_listing_date": (
                None
                if self.share_listing_date is None
                else self.share_listing_date.isoformat()
            ),
            "effective_date": (
                None if self.effective_date is None else self.effective_date.isoformat()
            ),
            "automatic_share_ratio": _decimal_text(self.automatic_share_ratio),
            "cash_dividend_per_share": _decimal_text(
                self.cash_dividend_per_share
            ),
            "rights_entitlement_ratio": _decimal_text(
                self.rights_entitlement_ratio
            ),
            "rights_subscription_price": _decimal_text(
                self.rights_subscription_price
            ),
            "currency": self.currency,
            "reference_price": _decimal_text(self.reference_price),
            "reference_price_snapshot_id": self.reference_price_snapshot_id,
            "revision_id": self.revision_id,
            "supersedes_revision_id": self.supersedes_revision_id,
            "source_locator": self.source_locator,
        }


class _FrozenTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_target_table = False
        self.in_cell = False
        self.cell_kind: str | None = None
        self.cell_chunks: list[str] = []
        self.current_row: list[tuple[str, str]] = []
        self.rows: list[list[tuple[str, str]]] = []
        self.target_count = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        if tag == "table" and attributes.get("data-stage2d-schema") == HTML_EXTRACTION_SCHEMA:
            if self.in_target_table:
                raise CorporateActionExtractionError("nested target table is forbidden")
            self.target_count += 1
            self.in_target_table = True
            return
        if not self.in_target_table:
            return
        if tag == "tr":
            if self.current_row:
                raise CorporateActionExtractionError("nested table rows are forbidden")
            self.current_row = []
        elif tag in {"th", "td"}:
            if self.in_cell:
                raise CorporateActionExtractionError("nested cells are forbidden")
            self.in_cell = True
            self.cell_kind = tag
            self.cell_chunks = []
        elif tag not in {"thead", "tbody"}:
            raise CorporateActionExtractionError(
                f"unsupported tag inside frozen table: {tag}"
            )

    def handle_endtag(self, tag: str) -> None:
        if not self.in_target_table:
            return
        if tag in {"th", "td"}:
            if not self.in_cell or self.cell_kind != tag:
                raise CorporateActionExtractionError("malformed table cell")
            value = "".join(self.cell_chunks).strip()
            self.current_row.append((tag, value))
            self.in_cell = False
            self.cell_kind = None
            self.cell_chunks = []
        elif tag == "tr":
            if self.in_cell:
                raise CorporateActionExtractionError("row ended inside a cell")
            if self.current_row:
                self.rows.append(self.current_row)
            self.current_row = []
        elif tag == "table":
            if self.current_row or self.in_cell:
                raise CorporateActionExtractionError("table ended with open row/cell")
            self.in_target_table = False
        elif tag not in {"thead", "tbody"}:
            raise CorporateActionExtractionError(
                f"unsupported closing tag inside frozen table: {tag}"
            )

    def handle_data(self, data: str) -> None:
        if self.in_target_table and self.in_cell:
            self.cell_chunks.append(data)


@dataclass(frozen=True, slots=True)
class ExtractedSourceDocument:
    raw_artifact_id: str
    raw_descriptor_id: str
    extractor_name: str
    extractor_version: str
    extraction_method: ExtractionMethod
    reviewer_note: str
    extracted_at: datetime
    rows: tuple[ExtractedCorporateActionRow, ...]
    synthetic_fixture: bool
    document_id: str = field(init=False)
    gaps: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.raw_artifact_id, "raw_artifact_id")
        _require_sha256(self.raw_descriptor_id, "raw_descriptor_id")
        _require_text(self.extractor_name, "extractor_name")
        _require_text(self.extractor_version, "extractor_version")
        if not isinstance(self.extraction_method, ExtractionMethod):
            raise CorporateActionExtractionError(
                "extraction_method must be ExtractionMethod"
            )
        _require_text(self.reviewer_note, "reviewer_note")
        ensure_aware(self.extracted_at, "extracted_at")
        if self.extracted_at.utcoffset() != timedelta(0):
            raise CorporateActionExtractionError("extracted_at must be UTC")
        _require_bool(self.synthetic_fixture, "synthetic_fixture")
        if self.synthetic_fixture is not True:
            raise CorporateActionExtractionError(
                "Stage 2D frozen extraction documents are synthetic-only and cannot be relabelled"
            )
        order = tuple(
            (
                row.source_security_id,
                row.source_event_id,
                row.revision_id,
                row.row_id,
            )
            for row in self.rows
        )
        if not self.rows:
            raise CorporateActionExtractionError("extracted document must contain rows")
        if order != tuple(sorted(order)):
            raise CorporateActionExtractionError(
                "extracted rows must be deterministically sorted"
            )
        revision_keys = [
            (row.revision_stream, row.revision_id) for row in self.rows
        ]
        if len(set(revision_keys)) != len(revision_keys):
            raise CorporateActionExtractionError(
                "duplicate source event revision is forbidden"
            )
        object.__setattr__(
            self,
            "gaps",
            tuple(sorted({gap for row in self.rows for gap in row.gaps})),
        )
        object.__setattr__(
            self,
            "document_id",
            fingerprint(
                {
                    "schema": "stage2d-extracted-source-document-v1",
                    "raw_artifact_id": self.raw_artifact_id,
                    "raw_descriptor_id": self.raw_descriptor_id,
                    "extractor_name": self.extractor_name,
                    "extractor_version": self.extractor_version,
                    "extraction_method": self.extraction_method,
                    "reviewer_note": self.reviewer_note,
                    "extracted_at": to_utc(self.extracted_at),
                    "row_ids": [row.row_id for row in self.rows],
                    "synthetic_fixture": self.synthetic_fixture,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class CorporateActionExtractionDescriptor:
    raw_artifact_id: str
    raw_descriptor_id: str
    raw_descriptor_key: str
    extraction_payload_id: str
    extraction_payload_key: str
    extracted_document_id: str
    extractor_name: str
    extractor_version: str
    extraction_method: ExtractionMethod
    reviewer_note: str
    extracted_at: datetime
    synthetic_fixture: bool
    descriptor_id: str = field(init=False)
    descriptor_key: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "raw_artifact_id",
            "raw_descriptor_id",
            "extraction_payload_id",
            "extracted_document_id",
        ):
            _require_sha256(getattr(self, name), name)
        validate_storage_key(self.raw_descriptor_key)
        validate_storage_key(self.extraction_payload_key)
        _require_text(self.extractor_name, "extractor_name")
        _require_text(self.extractor_version, "extractor_version")
        if not isinstance(self.extraction_method, ExtractionMethod):
            raise CorporateActionExtractionError(
                "extraction_method must be ExtractionMethod"
            )
        _require_text(self.reviewer_note, "reviewer_note")
        ensure_aware(self.extracted_at, "extracted_at")
        if self.extracted_at.utcoffset() != timedelta(0):
            raise CorporateActionExtractionError("extracted_at must be UTC")
        _require_bool(self.synthetic_fixture, "synthetic_fixture")
        if self.synthetic_fixture is not True:
            raise CorporateActionExtractionError(
                "synthetic extraction descriptor cannot be relabelled"
            )
        identity = self._identity_payload()
        descriptor_id = fingerprint(identity)
        object.__setattr__(self, "descriptor_id", descriptor_id)
        object.__setattr__(
            self,
            "descriptor_key",
            validate_storage_key(
                f"extraction-descriptors/corporate-actions/{descriptor_id}.json"
            ),
        )

    def _identity_payload(self) -> dict[str, object]:
        return {
            "schema": EXTRACTION_DESCRIPTOR_SCHEMA,
            "raw_artifact_id": self.raw_artifact_id,
            "raw_descriptor_id": self.raw_descriptor_id,
            "raw_descriptor_key": self.raw_descriptor_key,
            "extraction_payload_id": self.extraction_payload_id,
            "extraction_payload_key": self.extraction_payload_key,
            "extracted_document_id": self.extracted_document_id,
            "extractor_name": self.extractor_name,
            "extractor_version": self.extractor_version,
            "extraction_method": self.extraction_method.value,
            "reviewer_note": self.reviewer_note,
            "extracted_at": to_utc(self.extracted_at).isoformat().replace(
                "+00:00", "Z"
            ),
            "synthetic_fixture": self.synthetic_fixture,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            **self._identity_payload(),
            "descriptor_id": self.descriptor_id,
            "descriptor_key": self.descriptor_key,
        }


def _row_from_mapping(value: object) -> ExtractedCorporateActionRow:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise CorporateActionExtractionError("extracted row must be a JSON object")
    unknown = sorted(set(value) - _ROW_FIELDS)
    missing = sorted(_ROW_FIELDS - set(value))
    if unknown:
        raise CorporateActionExtractionError(
            "extracted row contains unknown fields: " + ", ".join(unknown)
        )
    if missing:
        raise CorporateActionExtractionError(
            "extracted row is missing fields: " + ", ".join(missing)
        )
    if "instrument_id" in value or "identity_fact_id" in value:
        raise CorporateActionExtractionError(
            "source rows cannot self-assert internal identity"
        )
    try:
        granularity = SourcePublishedGranularity(
            _require_text(
                value["source_published_granularity"],
                "source_published_granularity",
            )
        )
        published = _parse_publication(value["source_published_at"], granularity)
        reference_snapshot_raw = value["reference_price_snapshot_id"]
        supersedes_raw = value["supersedes_revision_id"]
        currency_raw = value["currency"]
        return ExtractedCorporateActionRow(
            source_event_id=_require_text(value["source_event_id"], "source_event_id"),
            source_security_id=_require_text(
                value["source_security_id"], "source_security_id"
            ),
            symbol=_require_text(value["symbol"], "symbol"),
            market=Market(_require_text(value["market"], "market")),
            exchange=_require_text(value["exchange"], "exchange"),
            action_type=CandidateCorporateActionType(
                _require_text(value["action_type"], "action_type")
            ),
            lifecycle=CandidateCorporateActionLifecycle(
                _require_text(value["lifecycle"], "lifecycle")
            ),
            source_published_at=published,
            source_published_granularity=granularity,
            ex_date=_parse_optional_date(value["ex_date"], "ex_date"),
            record_date=_parse_optional_date(value["record_date"], "record_date"),
            payment_date=_parse_optional_date(
                value["payment_date"], "payment_date"
            ),
            share_listing_date=_parse_optional_date(
                value["share_listing_date"], "share_listing_date"
            ),
            effective_date=_parse_optional_date(
                value["effective_date"], "effective_date"
            ),
            automatic_share_ratio=_parse_optional_decimal(
                value["automatic_share_ratio"], "automatic_share_ratio"
            ),
            cash_dividend_per_share=_parse_optional_decimal(
                value["cash_dividend_per_share"], "cash_dividend_per_share"
            ),
            rights_entitlement_ratio=_parse_optional_decimal(
                value["rights_entitlement_ratio"], "rights_entitlement_ratio"
            ),
            rights_subscription_price=_parse_optional_decimal(
                value["rights_subscription_price"], "rights_subscription_price"
            ),
            currency=(
                None
                if currency_raw in (None, "")
                else _require_text(currency_raw, "currency")
            ),
            reference_price=_parse_optional_decimal(
                value["reference_price"], "reference_price"
            ),
            reference_price_snapshot_id=(
                None
                if reference_snapshot_raw in (None, "")
                else _require_sha256(
                    reference_snapshot_raw, "reference_price_snapshot_id"
                )
            ),
            revision_id=_require_text(value["revision_id"], "revision_id"),
            supersedes_revision_id=(
                None
                if supersedes_raw in (None, "")
                else _require_text(supersedes_raw, "supersedes_revision_id")
            ),
            source_locator=_require_text(value["source_locator"], "source_locator"),
        )
    except (KeyError, ValueError) as exc:
        if isinstance(exc, CorporateActionExtractionError):
            raise
        raise CorporateActionExtractionError(
            "extracted row contains invalid enum/date values"
        ) from exc


def parse_frozen_html_document(
    raw_bytes: bytes,
    *,
    capture: CorporateActionRawCapture,
    extractor_version: str,
    reviewer_note: str,
    extracted_at: datetime,
) -> ExtractedSourceDocument:
    """Parse the frozen synthetic HTML table contract from exact captured bytes."""

    if not isinstance(raw_bytes, bytes) or not raw_bytes:
        raise CorporateActionExtractionError("raw_bytes must be non-empty bytes")
    if hashlib.sha256(raw_bytes).hexdigest() != capture.raw_sha256:
        raise CorporateActionExtractionError(
            "HTML bytes do not match Stage 2C raw capture"
        )
    try:
        text = raw_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CorporateActionExtractionError("HTML must be strict UTF-8") from exc
    parser = _FrozenTableParser()
    try:
        parser.feed(text)
        parser.close()
    except CorporateActionExtractionError:
        raise
    except Exception as exc:
        raise CorporateActionExtractionError("HTML parser failed") from exc
    if parser.target_count != 1 or parser.in_target_table:
        raise CorporateActionExtractionError(
            "document must contain exactly one complete frozen table"
        )
    if len(parser.rows) < 2:
        raise CorporateActionExtractionError("frozen table requires header and rows")
    header = parser.rows[0]
    if tuple(kind for kind, _ in header) != ("th",) * len(header):
        raise CorporateActionExtractionError("first table row must contain headers")
    header_values = tuple(value for _, value in header)
    if header_values != _HTML_HEADERS:
        raise CorporateActionExtractionError("frozen table headers do not match schema")
    mappings: list[dict[str, object]] = []
    for index, raw_row in enumerate(parser.rows[1:], start=1):
        if tuple(kind for kind, _ in raw_row) != ("td",) * len(raw_row):
            raise CorporateActionExtractionError(
                f"data row {index} must contain td cells only"
            )
        values = tuple(value for _, value in raw_row)
        if len(values) != len(_HTML_HEADERS):
            raise CorporateActionExtractionError(
                f"data row {index} has wrong cell count"
            )
        mappings.append(dict(zip(_HTML_HEADERS, values, strict=True)))
    rows = tuple(_row_from_mapping(item) for item in mappings)
    return ExtractedSourceDocument(
        raw_artifact_id=capture.artifact_id,
        raw_descriptor_id=capture.descriptor_id,
        extractor_name="python-html.parser",
        extractor_version=_require_text(extractor_version, "extractor_version"),
        extraction_method=ExtractionMethod.FROZEN_HTML_TABLE,
        reviewer_note=_require_text(reviewer_note, "reviewer_note"),
        extracted_at=to_utc(extracted_at, "extracted_at"),
        rows=rows,
        synthetic_fixture=True,
    )


def parse_structured_extraction_document(
    payload: bytes,
    *,
    capture: CorporateActionRawCapture,
    extraction_method: ExtractionMethod,
    extracted_at: datetime,
) -> ExtractedSourceDocument:
    """Parse a strict extraction document bound to a PDF/XLS/XLSX/raw capture."""

    if extraction_method not in {
        ExtractionMethod.STRUCTURED_MANUAL,
        ExtractionMethod.STRUCTURED_EXTERNAL_TOOL,
    }:
        raise CorporateActionExtractionError(
            "structured document requires structured extraction method"
        )
    value = _strict_json_loads(payload, "structured extraction document")
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise CorporateActionExtractionError(
            "structured extraction document must be a JSON object"
        )
    unknown = sorted(set(value) - _TOP_FIELDS)
    missing = sorted(_TOP_FIELDS - set(value))
    if unknown:
        raise CorporateActionExtractionError(
            "structured extraction contains unknown fields: " + ", ".join(unknown)
        )
    if missing:
        raise CorporateActionExtractionError(
            "structured extraction is missing fields: " + ", ".join(missing)
        )
    if value.get("schema") != STRUCTURED_EXTRACTION_SCHEMA:
        raise CorporateActionExtractionError("unsupported extraction schema")
    if _require_bool(value["synthetic_fixture"], "synthetic_fixture") is not True:
        raise CorporateActionExtractionError(
            "structured extraction fixture cannot be relabelled as real"
        )
    rows_raw = value["rows"]
    if not isinstance(rows_raw, list) or not rows_raw:
        raise CorporateActionExtractionError("structured extraction rows must be array")
    rows = tuple(_row_from_mapping(item) for item in rows_raw)
    return ExtractedSourceDocument(
        raw_artifact_id=capture.artifact_id,
        raw_descriptor_id=capture.descriptor_id,
        extractor_name=_require_text(value["extractor_name"], "extractor_name"),
        extractor_version=_require_text(
            value["extractor_version"], "extractor_version"
        ),
        extraction_method=extraction_method,
        reviewer_note=_require_text(value["reviewer_note"], "reviewer_note"),
        extracted_at=to_utc(extracted_at, "extracted_at"),
        rows=rows,
        synthetic_fixture=True,
    )


def write_extraction_descriptor(
    root: str | Path,
    *,
    capture: CorporateActionRawCapture,
    extraction_payload: bytes,
    document: ExtractedSourceDocument,
) -> CorporateActionExtractionDescriptor:
    """Persist an immutable extraction payload and descriptor."""

    loaded_capture, raw_bytes = load_corporate_action_raw(
        root,
        descriptor_key=capture.descriptor_key,
    )
    if loaded_capture.descriptor_id != capture.descriptor_id:
        raise CorporateActionExtractionError("raw capture changed before extraction")
    if document.raw_artifact_id != capture.artifact_id:
        raise CorporateActionExtractionError("document raw artifact mismatch")
    if document.raw_descriptor_id != capture.descriptor_id:
        raise CorporateActionExtractionError("document raw descriptor mismatch")
    if document.extraction_method is ExtractionMethod.FROZEN_HTML_TABLE:
        if extraction_payload != raw_bytes:
            raise CorporateActionExtractionError(
                "frozen HTML extraction payload must equal exact raw bytes"
            )
        replay = parse_frozen_html_document(
            extraction_payload,
            capture=capture,
            extractor_version=document.extractor_version,
            reviewer_note=document.reviewer_note,
            extracted_at=document.extracted_at,
        )
    else:
        replay = parse_structured_extraction_document(
            extraction_payload,
            capture=capture,
            extraction_method=document.extraction_method,
            extracted_at=document.extracted_at,
        )
    if replay.document_id != document.document_id:
        raise CorporateActionExtractionError(
            "extracted document does not match deterministic extraction replay"
        )
    payload_id = hashlib.sha256(extraction_payload).hexdigest()
    suffix = "html" if document.extraction_method is ExtractionMethod.FROZEN_HTML_TABLE else "json"
    payload_key = validate_storage_key(
        f"extractions/corporate-actions/{payload_id}.{suffix}"
    )
    root_path = Path(root)
    _atomic_write(safe_artifact_path(root_path, payload_key), extraction_payload)
    descriptor = CorporateActionExtractionDescriptor(
        raw_artifact_id=capture.artifact_id,
        raw_descriptor_id=capture.descriptor_id,
        raw_descriptor_key=capture.descriptor_key,
        extraction_payload_id=payload_id,
        extraction_payload_key=payload_key,
        extracted_document_id=document.document_id,
        extractor_name=document.extractor_name,
        extractor_version=document.extractor_version,
        extraction_method=document.extraction_method,
        reviewer_note=document.reviewer_note,
        extracted_at=document.extracted_at,
        synthetic_fixture=document.synthetic_fixture,
    )
    descriptor_payload = (
        json.dumps(descriptor.as_dict(), ensure_ascii=False, sort_keys=True, indent=2)
        + "\n"
    ).encode("utf-8")
    _atomic_write(
        safe_artifact_path(root_path, descriptor.descriptor_key),
        descriptor_payload,
    )
    return descriptor


@dataclass(frozen=True, slots=True)
class SourceSecurityIdentityMapping:
    source_owner: str
    source_security_id: str
    identity_fact_id: str
    mapping_policy_version: str
    known_at: datetime
    usable_from: datetime
    status: SourceMappingStatus = SourceMappingStatus.ACTIVE
    mapping_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_text(self.source_owner, "source_owner")
        _require_text(self.source_security_id, "source_security_id")
        _require_sha256(self.identity_fact_id, "identity_fact_id")
        _require_text(self.mapping_policy_version, "mapping_policy_version")
        ensure_aware(self.known_at, "known_at")
        ensure_aware(self.usable_from, "usable_from")
        if to_utc(self.usable_from) < to_utc(self.known_at):
            raise CorporateActionExtractionError(
                "mapping usable_from cannot precede known_at"
            )
        if not isinstance(self.status, SourceMappingStatus):
            raise CorporateActionExtractionError("status must be SourceMappingStatus")
        object.__setattr__(
            self,
            "mapping_id",
            fingerprint(
                {
                    "schema": "stage2d-source-security-identity-mapping-v1",
                    "source_owner": self.source_owner,
                    "source_security_id": self.source_security_id,
                    "identity_fact_id": self.identity_fact_id,
                    "mapping_policy_version": self.mapping_policy_version,
                    "known_at": to_utc(self.known_at),
                    "usable_from": to_utc(self.usable_from),
                    "status": self.status,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class RowIdentityBinding:
    row_id: str
    status: IdentityBindingStatus
    mapping_id: str | None
    identity_fact_id: str | None
    instrument_id: str | None
    reason: str
    binding_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.row_id, "row_id")
        if not isinstance(self.status, IdentityBindingStatus):
            raise CorporateActionExtractionError(
                "status must be IdentityBindingStatus"
            )
        for name in ("mapping_id", "identity_fact_id"):
            value = getattr(self, name)
            if value is not None:
                _require_sha256(value, name)
        if self.instrument_id is not None:
            _require_text(self.instrument_id, "instrument_id")
        _require_text(self.reason, "reason")
        if self.status is IdentityBindingStatus.BOUND and None in (
            self.mapping_id,
            self.identity_fact_id,
            self.instrument_id,
        ):
            raise CorporateActionExtractionError(
                "BOUND result requires mapping and identity fields"
            )
        object.__setattr__(
            self,
            "binding_id",
            fingerprint(
                {
                    "schema": "stage2d-row-identity-binding-v1",
                    "row_id": self.row_id,
                    "status": self.status,
                    "mapping_id": self.mapping_id,
                    "identity_fact_id": self.identity_fact_id,
                    "instrument_id": self.instrument_id,
                    "reason": self.reason,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class BoundCorporateActionCandidateBundle:
    document_id: str
    extraction_descriptor_id: str
    raw_artifact_id: str
    raw_descriptor_id: str
    mapping_policy_version: str
    as_of: datetime
    bindings: tuple[RowIdentityBinding, ...]
    candidates: tuple[CandidateCorporateAction, ...]
    synthetic_fixture: bool = True
    gaps: tuple[str, ...] = field(init=False)
    bundle_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "document_id",
            "extraction_descriptor_id",
            "raw_artifact_id",
            "raw_descriptor_id",
        ):
            _require_sha256(getattr(self, name), name)
        _require_text(self.mapping_policy_version, "mapping_policy_version")
        ensure_aware(self.as_of, "as_of")
        _require_bool(self.synthetic_fixture, "synthetic_fixture")
        if self.synthetic_fixture is not True:
            raise CorporateActionExtractionError(
                "bound candidate bundle is synthetic-only and cannot be relabelled"
            )
        binding_order = tuple((item.row_id, item.binding_id) for item in self.bindings)
        if binding_order != tuple(sorted(binding_order)):
            raise CorporateActionExtractionError("bindings must be sorted")
        candidate_order = tuple(
            (
                item.instrument_id,
                item.action_id,
                item.revision_id,
                item.candidate_id,
            )
            for item in self.candidates
        )
        if candidate_order != tuple(sorted(candidate_order)):
            raise CorporateActionExtractionError("candidates must be sorted")
        if any(
            item.raw_artifact_id != self.raw_artifact_id
            or item.raw_descriptor_id != self.raw_descriptor_id
            for item in self.candidates
        ):
            raise CorporateActionExtractionError(
                "candidate raw evidence identity differs from bundle"
            )
        bound_identity_pairs = {
            (item.identity_fact_id, item.instrument_id)
            for item in self.bindings
            if item.status is IdentityBindingStatus.BOUND
        }
        if any(
            (item.identity_fact_id, item.instrument_id) not in bound_identity_pairs
            for item in self.candidates
        ):
            raise CorporateActionExtractionError(
                "candidate identity is not supported by a BOUND result"
            )
        gaps = {
            f"IDENTITY_BINDING_{item.status.value}:{item.row_id}"
            for item in self.bindings
            if item.status is not IdentityBindingStatus.BOUND
        }
        gaps.update(gap for item in self.candidates for gap in item.gaps)
        object.__setattr__(self, "gaps", tuple(sorted(gaps)))
        object.__setattr__(
            self,
            "bundle_id",
            fingerprint(
                {
                    "schema": "stage2d-bound-candidate-bundle-v1",
                    "document_id": self.document_id,
                    "extraction_descriptor_id": self.extraction_descriptor_id,
                    "raw_artifact_id": self.raw_artifact_id,
                    "raw_descriptor_id": self.raw_descriptor_id,
                    "mapping_policy_version": self.mapping_policy_version,
                    "as_of": to_utc(self.as_of),
                    "binding_ids": [item.binding_id for item in self.bindings],
                    "candidate_ids": [item.candidate_id for item in self.candidates],
                    "gaps": self.gaps,
                    "synthetic_fixture": self.synthetic_fixture,
                }
            ),
        )


def _row_revision_payload(row: ExtractedCorporateActionRow) -> dict[str, object]:
    payload = row.as_dict()
    payload.pop("revision_id")
    payload.pop("supersedes_revision_id")
    return payload


def resolve_extracted_rows_as_of(
    rows: Iterable[ExtractedCorporateActionRow],
) -> tuple[ExtractedCorporateActionRow, ...]:
    """Resolve unique terminal source revisions without lexical ordering."""

    grouped: dict[tuple[str, str], list[ExtractedCorporateActionRow]] = defaultdict(list)
    for row in rows:
        if not isinstance(row, ExtractedCorporateActionRow):
            raise CorporateActionExtractionError(
                "resolver accepts ExtractedCorporateActionRow only"
            )
        grouped[row.revision_stream].append(row)
    selected: list[ExtractedCorporateActionRow] = []
    for records in grouped.values():
        try:
            selected.append(
                select_superseding_revision(
                    records,
                    revision_of=lambda item: item.revision_id,
                    predecessor_of=lambda item: item.supersedes_revision_id,
                    payload_of=_row_revision_payload,
                    identity_of=lambda item: item.row_id,
                    known_at_of=lambda item: datetime(
                        1970,
                        1,
                        1,
                        tzinfo=timezone.utc,
                    ),
                )
            )
        except PITConflictError as exc:
            raise CorporateActionExtractionError(str(exc)) from exc
    return tuple(
        sorted(
            selected,
            key=lambda item: (
                item.source_security_id,
                item.source_event_id,
                item.ex_date or date.max,
                item.row_id,
            ),
        )
    )


def bind_extracted_document(
    document: ExtractedSourceDocument,
    *,
    extraction_descriptor: CorporateActionExtractionDescriptor,
    capture: CorporateActionRawCapture,
    identities: Sequence[InstrumentIdentityFact],
    mappings: Sequence[SourceSecurityIdentityMapping],
    as_of: datetime,
    mapping_policy_version: str = IDENTITY_BINDING_POLICY_VERSION,
) -> BoundCorporateActionCandidateBundle:
    """Bind source-native rows to Stage 2A identities and emit candidates."""

    if not isinstance(document, ExtractedSourceDocument):
        raise CorporateActionExtractionError(
            "document must be ExtractedSourceDocument"
        )
    if not isinstance(
        extraction_descriptor,
        CorporateActionExtractionDescriptor,
    ):
        raise CorporateActionExtractionError(
            "extraction_descriptor must be CorporateActionExtractionDescriptor"
        )
    if extraction_descriptor.extracted_document_id != document.document_id:
        raise CorporateActionExtractionError(
            "extraction descriptor does not bind extracted document"
        )
    if extraction_descriptor.raw_artifact_id != capture.artifact_id:
        raise CorporateActionExtractionError(
            "extraction descriptor raw artifact mismatch"
        )
    if extraction_descriptor.raw_descriptor_id != capture.descriptor_id:
        raise CorporateActionExtractionError(
            "extraction descriptor raw capture mismatch"
        )
    if (
        extraction_descriptor.extractor_name != document.extractor_name
        or extraction_descriptor.extractor_version != document.extractor_version
        or extraction_descriptor.extraction_method is not document.extraction_method
        or extraction_descriptor.reviewer_note != document.reviewer_note
        or to_utc(extraction_descriptor.extracted_at)
        != to_utc(document.extracted_at)
    ):
        raise CorporateActionExtractionError(
            "extraction descriptor metadata differs from extracted document"
        )
    cutoff = to_utc(as_of, "as_of")
    policy = _require_text(mapping_policy_version, "mapping_policy_version")
    if document.raw_artifact_id != capture.artifact_id:
        raise CorporateActionExtractionError("document/capture raw artifact mismatch")
    if document.raw_descriptor_id != capture.descriptor_id:
        raise CorporateActionExtractionError("document/capture descriptor mismatch")
    identity_by_fact = {item.fact_id: item for item in identities}
    mappings_by_source: dict[str, list[SourceSecurityIdentityMapping]] = defaultdict(list)
    for mapping in mappings:
        if mapping.mapping_policy_version != policy:
            continue
        if mapping.source_owner != capture.source_owner.value:
            continue
        if mapping.status is not SourceMappingStatus.ACTIVE:
            continue
        mappings_by_source[mapping.source_security_id].append(mapping)

    bindings: list[RowIdentityBinding] = []
    candidates: list[CandidateCorporateAction] = []
    for row in document.rows:
        all_source_mappings = list(
            mappings_by_source.get(row.source_security_id, ())
        )
        candidate_mappings = [
            item
            for item in all_source_mappings
            if to_utc(item.known_at) <= cutoff and to_utc(item.usable_from) <= cutoff
        ]
        if not candidate_mappings:
            if all_source_mappings:
                future_mapping = min(
                    all_source_mappings,
                    key=lambda item: (
                        to_utc(item.known_at),
                        to_utc(item.usable_from),
                        item.mapping_id,
                    ),
                )
                bindings.append(
                    RowIdentityBinding(
                        row_id=row.row_id,
                        status=IdentityBindingStatus.FUTURE,
                        mapping_id=future_mapping.mapping_id,
                        identity_fact_id=future_mapping.identity_fact_id,
                        instrument_id=None,
                        reason="source-security mapping is not visible at as_of",
                    )
                )
            else:
                bindings.append(
                    RowIdentityBinding(
                        row_id=row.row_id,
                        status=IdentityBindingStatus.UNBOUND,
                        mapping_id=None,
                        identity_fact_id=None,
                        instrument_id=None,
                        reason="no source-security mapping",
                    )
                )
            continue
        if len(candidate_mappings) != 1:
            bindings.append(
                RowIdentityBinding(
                    row_id=row.row_id,
                    status=IdentityBindingStatus.AMBIGUOUS,
                    mapping_id=None,
                    identity_fact_id=None,
                    instrument_id=None,
                    reason="multiple visible source-security mappings",
                )
            )
            continue
        mapping = candidate_mappings[0]
        identity = identity_by_fact.get(mapping.identity_fact_id)
        if identity is None:
            bindings.append(
                RowIdentityBinding(
                    row_id=row.row_id,
                    status=IdentityBindingStatus.UNBOUND,
                    mapping_id=mapping.mapping_id,
                    identity_fact_id=mapping.identity_fact_id,
                    instrument_id=None,
                    reason="mapping references missing identity fact",
                )
            )
            continue
        if to_utc(identity.known_at) > cutoff or to_utc(identity.usable_from) > cutoff:
            bindings.append(
                RowIdentityBinding(
                    row_id=row.row_id,
                    status=IdentityBindingStatus.FUTURE,
                    mapping_id=mapping.mapping_id,
                    identity_fact_id=identity.fact_id,
                    instrument_id=identity.instrument_id,
                    reason="identity fact is not visible at as_of",
                )
            )
            continue
        event_date = row.ex_date or row.effective_date
        if event_date is None or not identity.active_on(event_date):
            bindings.append(
                RowIdentityBinding(
                    row_id=row.row_id,
                    status=IdentityBindingStatus.INACTIVE,
                    mapping_id=mapping.mapping_id,
                    identity_fact_id=identity.fact_id,
                    instrument_id=identity.instrument_id,
                    reason="identity is not active on event date",
                )
            )
            continue
        if (
            identity.symbol != row.symbol
            or identity.market is not row.market
            or identity.exchange != row.exchange
        ):
            bindings.append(
                RowIdentityBinding(
                    row_id=row.row_id,
                    status=IdentityBindingStatus.IDENTITY_MISMATCH,
                    mapping_id=mapping.mapping_id,
                    identity_fact_id=identity.fact_id,
                    instrument_id=identity.instrument_id,
                    reason="symbol/market/exchange evidence disagrees with active identity",
                )
            )
            continue
        binding = RowIdentityBinding(
            row_id=row.row_id,
            status=IdentityBindingStatus.BOUND,
            mapping_id=mapping.mapping_id,
            identity_fact_id=identity.fact_id,
            instrument_id=identity.instrument_id,
            reason="explicit Stage 2A identity mapping matched active identity",
        )
        bindings.append(binding)
        try:
            candidates.append(
                CandidateCorporateAction(
                    action_id=row.source_event_id,
                    instrument_id=identity.instrument_id,
                    identity_fact_id=identity.fact_id,
                    symbol=row.symbol,
                    market=row.market,
                    exchange=row.exchange,
                    action_type=row.action_type,
                    lifecycle=row.lifecycle,
                    source_published_at=row.source_published_at,
                    source_published_granularity=row.source_published_granularity,
                    observed_at=capture.retrieved_at,
                    retrieved_at=capture.retrieved_at,
                    known_at=capture.retrieved_at,
                    usable_from=capture.retrieved_at,
                    ex_date=row.ex_date,
                    record_date=row.record_date,
                    payment_date=row.payment_date,
                    share_listing_date=row.share_listing_date,
                    effective_date=row.effective_date,
                    automatic_share_ratio=row.automatic_share_ratio,
                    cash_dividend_per_share=row.cash_dividend_per_share,
                    rights_entitlement_ratio=row.rights_entitlement_ratio,
                    rights_subscription_price=row.rights_subscription_price,
                    currency=row.currency,
                    reference_price=row.reference_price,
                    reference_price_snapshot_id=row.reference_price_snapshot_id,
                    revision_id=row.revision_id,
                    supersedes_revision_id=row.supersedes_revision_id,
                    source_uri=capture.request_url,
                    raw_artifact_id=capture.artifact_id,
                    raw_descriptor_id=capture.descriptor_id,
                    parser_version=(
                        f"stage2d:{document.extractor_name}:{document.extractor_version}"
                    ),
                    source_owner=capture.source_owner,
                    source_family=capture.source_family,
                    source_version=capture.source_version,
                    synthetic_fixture=True,
                )
            )
        except CorporateActionAdapterError as exc:
            raise CorporateActionExtractionError(str(exc)) from exc

    return BoundCorporateActionCandidateBundle(
        document_id=document.document_id,
        extraction_descriptor_id=extraction_descriptor.descriptor_id,
        raw_artifact_id=document.raw_artifact_id,
        raw_descriptor_id=document.raw_descriptor_id,
        mapping_policy_version=policy,
        as_of=cutoff,
        bindings=tuple(sorted(bindings, key=lambda item: (item.row_id, item.binding_id))),
        candidates=tuple(
            sorted(
                candidates,
                key=lambda item: (
                    item.instrument_id,
                    item.action_id,
                    item.revision_id,
                    item.candidate_id,
                ),
            )
        ),
        synthetic_fixture=True,
    )


__all__ = [
    "EXTRACTION_DESCRIPTOR_SCHEMA",
    "HTML_EXTRACTION_SCHEMA",
    "IDENTITY_BINDING_POLICY_VERSION",
    "STRUCTURED_EXTRACTION_SCHEMA",
    "BoundCorporateActionCandidateBundle",
    "CorporateActionExtractionDescriptor",
    "CorporateActionExtractionError",
    "ExtractedCorporateActionRow",
    "ExtractedSourceDocument",
    "ExtractionMethod",
    "IdentityBindingStatus",
    "RowIdentityBinding",
    "SourceMappingStatus",
    "SourceSecurityIdentityMapping",
    "bind_extracted_document",
    "parse_frozen_html_document",
    "parse_structured_extraction_document",
    "resolve_extracted_rows_as_of",
    "write_extraction_descriptor",
]

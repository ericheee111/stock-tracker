"""Exact-raw A-share corporate-action evidence capture and candidate parsing.

Stage 2C deliberately stops before trust promotion. It captures exact official
response/file bytes, persists immutable provenance descriptors, and parses only
a frozen synthetic JSON fixture schema used to validate the engineering
contract. PDF/XLS/XLSX/HTML artifacts may be captured, but this module never
pretends to understand them without a separately versioned extractor.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

from stock_tracker.core.types import Market

from ..core.calendar import select_superseding_revision
from ..core.corporate_actions import (
    CorporateActionContractError,
    CorporateActionFact,
    CorporateActionLifecycle,
)
from ..core.fingerprint import canonical_json, fingerprint
from ..core.point_in_time import PITConflictError
from ..core.time import ensure_aware, exchange_local_date, to_utc
from .manifest import safe_artifact_path, validate_storage_key

CORPORATE_ACTION_FIXTURE_PARSER_VERSION = "corporate-action-fixture-json-v1"
_CAPTURE_SCHEMA = "a-share-corporate-action-raw-capture-v1"
_PARSE_DESCRIPTOR_SCHEMA = "a-share-corporate-action-parse-descriptor-v1"
_FIXTURE_SCHEMA = "stage2c-corporate-action-fixture-v1"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_SELECTED_HEADERS = frozenset(
    {
        "cache-control",
        "content-disposition",
        "content-length",
        "content-type",
        "date",
        "etag",
        "last-modified",
        "location",
    }
)


class CorporateActionAdapterError(ValueError):
    """Raised when exact-raw provenance or candidate parsing is unsafe."""


class CorporateActionSourceOwner(StrEnum):
    SSE = "SSE"
    SZSE = "SZSE"
    CNINFO = "CNINFO"


class CorporateActionSourceFamily(StrEnum):
    SSE_LISTED_COMPANY_ANNOUNCEMENT = "SSE_LISTED_COMPANY_ANNOUNCEMENT"
    SSE_ANNOUNCEMENT_ATTACHMENT = "SSE_ANNOUNCEMENT_ATTACHMENT"
    SZSE_LISTED_COMPANY_ANNOUNCEMENT = "SZSE_LISTED_COMPANY_ANNOUNCEMENT"
    SZSE_DISCLOSURE_ATTACHMENT = "SZSE_DISCLOSURE_ATTACHMENT"
    CNINFO_DISCLOSURE_ATTACHMENT = "CNINFO_DISCLOSURE_ATTACHMENT"


class RawCorporateActionFormat(StrEnum):
    JSON = "JSON"
    HTML = "HTML"
    PDF = "PDF"
    XLS = "XLS"
    XLSX = "XLSX"


class SourcePublishedGranularity(StrEnum):
    DATE = "DATE"
    SECOND = "SECOND"
    UNKNOWN = "UNKNOWN"


class CandidateCorporateActionLifecycle(StrEnum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    IMPLEMENTATION_ANNOUNCED = "IMPLEMENTATION_ANNOUNCED"
    EFFECTIVE = "EFFECTIVE"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    CORRECTED = "CORRECTED"


class CandidateCorporateActionType(StrEnum):
    CASH_DIVIDEND = "CASH_DIVIDEND"
    STOCK_DIVIDEND = "STOCK_DIVIDEND"
    SPLIT = "SPLIT"
    REVERSE_SPLIT = "REVERSE_SPLIT"
    RIGHTS_ISSUE = "RIGHTS_ISSUE"
    PLACEMENT_OR_ISSUANCE = "PLACEMENT_OR_ISSUANCE"
    MERGER_OR_CONVERSION = "MERGER_OR_CONVERSION"
    COMBINED = "COMBINED"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class ExtractionStatus(StrEnum):
    PARSED = "PARSED"
    EXTRACTION_REQUIRED = "EXTRACTION_REQUIRED"


_FAMILY_OWNER = {
    CorporateActionSourceFamily.SSE_LISTED_COMPANY_ANNOUNCEMENT: (
        CorporateActionSourceOwner.SSE
    ),
    CorporateActionSourceFamily.SSE_ANNOUNCEMENT_ATTACHMENT: (
        CorporateActionSourceOwner.SSE
    ),
    CorporateActionSourceFamily.SZSE_LISTED_COMPANY_ANNOUNCEMENT: (
        CorporateActionSourceOwner.SZSE
    ),
    CorporateActionSourceFamily.SZSE_DISCLOSURE_ATTACHMENT: (
        CorporateActionSourceOwner.SZSE
    ),
    CorporateActionSourceFamily.CNINFO_DISCLOSURE_ATTACHMENT: (
        CorporateActionSourceOwner.CNINFO
    ),
}
_OWNER_DOMAIN = {
    CorporateActionSourceOwner.SSE: "sse.com.cn",
    CorporateActionSourceOwner.SZSE: "szse.cn",
    CorporateActionSourceOwner.CNINFO: "cninfo.com.cn",
}
_FORMAT_SUFFIX = {
    RawCorporateActionFormat.JSON: "json",
    RawCorporateActionFormat.HTML: "html",
    RawCorporateActionFormat.PDF: "pdf",
    RawCorporateActionFormat.XLS: "xls",
    RawCorporateActionFormat.XLSX: "xlsx",
}
_FORMAT_CONTENT_TYPES = {
    RawCorporateActionFormat.JSON: frozenset(
        {"application/json", "text/json", "application/octet-stream"}
    ),
    RawCorporateActionFormat.HTML: frozenset({"text/html", "application/xhtml+xml"}),
    RawCorporateActionFormat.PDF: frozenset({"application/pdf"}),
    RawCorporateActionFormat.XLS: frozenset(
        {"application/vnd.ms-excel", "application/octet-stream"}
    ),
    RawCorporateActionFormat.XLSX: frozenset(
        {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/octet-stream",
        }
    ),
}
_SUPPORTED_FACTOR_TYPES = frozenset(
    {
        CandidateCorporateActionType.CASH_DIVIDEND,
        CandidateCorporateActionType.STOCK_DIVIDEND,
        CandidateCorporateActionType.SPLIT,
        CandidateCorporateActionType.REVERSE_SPLIT,
        CandidateCorporateActionType.RIGHTS_ISSUE,
        CandidateCorporateActionType.COMBINED,
    }
)
_SYMBOL_SUFFIXES = {
    Market.A: frozenset({"SH", "SZ"}),
    Market.HK: frozenset({"HK"}),
    Market.US: frozenset({"US"}),
}
_ACTION_FIELDS = frozenset(
    {
        "action_id",
        "instrument_id",
        "identity_fact_id",
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
    }
)
_FIXTURE_FIELDS = frozenset({"schema", "synthetic_fixture", "actions"})


def _require_text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise CorporateActionAdapterError(f"{name} must be a non-empty trimmed string")
    return value


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise CorporateActionAdapterError(f"{name} must be a boolean")
    return value


def _require_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CorporateActionAdapterError(f"{name} must be an integer")
    return value


def _require_sha256(value: object, name: str) -> str:
    text = _require_text(value, name)
    if _SHA256.fullmatch(text) is None:
        raise CorporateActionAdapterError(f"{name} must be lowercase SHA-256")
    return text


def _reject_json_constant(value: str) -> object:
    raise CorporateActionAdapterError(
        f"non-finite JSON constant {value!r} is forbidden"
    )


def _strict_json_loads(value: str, name: str) -> object:
    try:
        return json.loads(value, parse_constant=_reject_json_constant)
    except json.JSONDecodeError as exc:
        raise CorporateActionAdapterError(f"{name} is not valid JSON") from exc


def _iso_utc(value: datetime) -> str:
    return to_utc(value).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: object, name: str) -> datetime:
    text = _require_text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CorporateActionAdapterError(f"{name} must be ISO-8601 datetime") from exc
    ensure_aware(parsed, name)
    return to_utc(parsed)


def _parse_optional_date(value: object, name: str) -> date | None:
    if value is None:
        return None
    text = _require_text(value, name)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise CorporateActionAdapterError(f"{name} must be YYYY-MM-DD or null") from exc


def _parse_optional_decimal(value: object, name: str) -> Decimal | None:
    if value is None:
        return None
    if type(value) is not str:
        raise CorporateActionAdapterError(f"{name} must be a decimal string or null")
    if value != value.strip() or not value:
        raise CorporateActionAdapterError(f"{name} must be a canonical decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise CorporateActionAdapterError(f"{name} is not a valid decimal") from exc
    if not parsed.is_finite():
        raise CorporateActionAdapterError(f"{name} must be finite")
    canonical = format(parsed, "f")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    if parsed == 0:
        canonical = "0"
    if value != canonical:
        raise CorporateActionAdapterError(
            f"{name} must use canonical decimal text {canonical!r}"
        )
    return parsed


def _parse_publication(
    value: object,
    granularity: SourcePublishedGranularity,
) -> date | datetime | None:
    if granularity is SourcePublishedGranularity.UNKNOWN:
        if value is not None:
            raise CorporateActionAdapterError(
                "UNKNOWN source publication granularity requires null value"
            )
        return None
    if granularity is SourcePublishedGranularity.DATE:
        text = _require_text(value, "source_published_at")
        try:
            return date.fromisoformat(text)
        except ValueError as exc:
            raise CorporateActionAdapterError(
                "DATE source publication must be YYYY-MM-DD"
            ) from exc
    return _parse_datetime(value, "source_published_at")


def digest_request_payload(value: bytes | Mapping[str, object] | None) -> str:
    if value is None:
        payload = b""
    elif isinstance(value, bytes):
        payload = value
    elif isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise CorporateActionAdapterError("request payload keys must be strings")
        try:
            payload = canonical_json(value).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise CorporateActionAdapterError("request payload is not canonical JSON") from exc
    else:
        raise CorporateActionAdapterError("request payload must be bytes, mapping or None")
    return hashlib.sha256(payload).hexdigest()


def _validate_owner_url(
    owner: CorporateActionSourceOwner,
    value: str,
    name: str,
) -> str:
    url = _require_text(value, name)
    parsed = urlsplit(url)
    expected = _OWNER_DOMAIN[owner]
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (
        hostname == expected or hostname.endswith("." + expected)
    ):
        raise CorporateActionAdapterError(
            f"{name} must remain on the official HTTPS {owner.value} domain"
        )
    return url


def _validate_source_url(
    owner: CorporateActionSourceOwner,
    family: CorporateActionSourceFamily,
    value: str,
) -> str:
    if _FAMILY_OWNER[family] is not owner:
        raise CorporateActionAdapterError("source owner and source family disagree")
    url = _validate_owner_url(owner, value, "request_url")
    parsed = urlsplit(url)
    path = parsed.path
    if family is CorporateActionSourceFamily.SSE_LISTED_COMPANY_ANNOUNCEMENT:
        if not path.startswith(
            (
                "/disclosure/listedinfo/announcement/",
                "/disclosure/announcement/",
            )
        ):
            raise CorporateActionAdapterError(
                "SSE announcement URL is outside the frozen source family"
            )
    elif family is CorporateActionSourceFamily.SSE_ANNOUNCEMENT_ATTACHMENT:
        if "/disclosure/listedinfo/announcement/" not in path:
            raise CorporateActionAdapterError(
                "SSE attachment URL is outside the frozen source family"
            )
    elif family is CorporateActionSourceFamily.SZSE_LISTED_COMPANY_ANNOUNCEMENT:
        if not path.startswith(
            ("/disclosure/notice/", "/disclosure/listedinfo/")
        ):
            raise CorporateActionAdapterError(
                "SZSE announcement URL is outside the frozen source family"
            )
    elif family is CorporateActionSourceFamily.SZSE_DISCLOSURE_ATTACHMENT:
        if not path.startswith("/download/disc/"):
            raise CorporateActionAdapterError(
                "SZSE attachment URL is outside the frozen source family"
            )
    elif (
        family is CorporateActionSourceFamily.CNINFO_DISCLOSURE_ATTACHMENT
        and not path.startswith(("/finalpage/", "/new/disclosure/", "/download/"))
    ):
        raise CorporateActionAdapterError(
            "CNINFO attachment URL is outside the frozen source family"
        )
    return url


def _normalize_headers(value: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise CorporateActionAdapterError("response_headers must be a mapping")
    selected: dict[str, str] = {}
    for raw_name, raw_value in value.items():
        if type(raw_name) is not str or type(raw_value) is not str:
            raise CorporateActionAdapterError(
                "response header names and values must be strings"
            )
        name = raw_name.strip().lower()
        if name in _SELECTED_HEADERS:
            normalized = raw_value.strip()
            if not normalized:
                raise CorporateActionAdapterError(
                    f"response header {name} cannot be empty"
                )
            selected[name] = normalized
    if "content-type" not in selected:
        raise CorporateActionAdapterError("response Content-Type is required")
    return dict(sorted(selected.items()))


def _content_type(headers: Mapping[str, str]) -> str:
    return headers["content-type"].split(";", 1)[0].strip().lower()


def _validate_content_length(headers: Mapping[str, str], actual_length: int) -> None:
    declared = headers.get("content-length")
    if declared is None:
        return
    try:
        parsed = int(declared)
    except ValueError as exc:
        raise CorporateActionAdapterError(
            "response Content-Length must be an integer"
        ) from exc
    if parsed != actual_length:
        raise CorporateActionAdapterError(
            "response Content-Length disagrees with exact raw bytes"
        )


def _validate_redirect_chain(
    owner: CorporateActionSourceOwner,
    request_url: str,
    redirects: tuple[RedirectHop, ...],
) -> None:
    previous = request_url
    seen = {request_url}
    for index, hop in enumerate(redirects):
        if not isinstance(hop, RedirectHop):
            raise CorporateActionAdapterError(
                "redirect_chain must contain RedirectHop values"
            )
        _validate_owner_url(
            owner,
            hop.from_url,
            f"redirect_chain[{index}].from_url",
        )
        _validate_owner_url(
            owner,
            hop.to_url,
            f"redirect_chain[{index}].to_url",
        )
        if hop.from_url != previous:
            raise CorporateActionAdapterError(
                "redirect_chain is discontinuous from the requested URL"
            )
        if hop.to_url in seen:
            raise CorporateActionAdapterError("redirect_chain contains a cycle")
        seen.add(hop.to_url)
        previous = hop.to_url


def _validate_content_type(
    raw_format: RawCorporateActionFormat,
    content_type: str,
) -> None:
    if content_type not in _FORMAT_CONTENT_TYPES[raw_format]:
        raise CorporateActionAdapterError(
            f"Content-Type {content_type!r} is incompatible with {raw_format.value}"
        )


def _reject_html_error_page(raw_bytes: bytes) -> None:
    try:
        text = raw_bytes[:65536].decode("utf-8", errors="strict").lower()
    except UnicodeDecodeError as exc:
        raise CorporateActionAdapterError("HTML response must be strict UTF-8") from exc
    markers = (
        "<title>error",
        "<title>错误",
        "系统维护",
        "访问异常",
        "request rejected",
        "service unavailable",
    )
    if any(marker in text for marker in markers):
        raise CorporateActionAdapterError("HTML response appears to be an error page")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise CorporateActionAdapterError(
                f"immutable artifact path contains different bytes: {path.name}"
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
class RedirectHop:
    status: int
    from_url: str
    to_url: str

    def __post_init__(self) -> None:
        if not 300 <= _require_int(self.status, "redirect status") <= 399:
            raise CorporateActionAdapterError("redirect status must be 3xx")
        _require_text(self.from_url, "redirect from_url")
        _require_text(self.to_url, "redirect to_url")

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "from_url": self.from_url,
            "to_url": self.to_url,
        }


@dataclass(frozen=True, slots=True)
class CorporateActionRawCapture:
    artifact_id: str
    descriptor_id: str
    storage_key: str
    descriptor_key: str
    request_url: str
    request_method: str
    request_payload_digest: str
    response_status: int
    response_headers: dict[str, str]
    content_type: str
    redirect_chain: tuple[RedirectHop, ...]
    retrieved_at: datetime
    raw_sha256: str
    byte_length: int
    source_owner: CorporateActionSourceOwner
    source_family: CorporateActionSourceFamily
    source_version: str
    raw_format: RawCorporateActionFormat

    def __post_init__(self) -> None:
        if not isinstance(self.source_owner, CorporateActionSourceOwner):
            raise CorporateActionAdapterError(
                "source_owner must be CorporateActionSourceOwner"
            )
        if not isinstance(self.source_family, CorporateActionSourceFamily):
            raise CorporateActionAdapterError(
                "source_family must be CorporateActionSourceFamily"
            )
        if not isinstance(self.raw_format, RawCorporateActionFormat):
            raise CorporateActionAdapterError(
                "raw_format must be RawCorporateActionFormat"
            )
        _require_sha256(self.artifact_id, "artifact_id")
        _require_sha256(self.descriptor_id, "descriptor_id")
        _require_sha256(self.raw_sha256, "raw_sha256")
        if self.artifact_id != self.raw_sha256:
            raise CorporateActionAdapterError(
                "artifact_id must equal the exact raw content SHA-256"
            )
        validate_storage_key(self.storage_key)
        validate_storage_key(self.descriptor_key)
        expected_storage_key = (
            "raw/corporate-actions/"
            f"{self.source_owner.value.lower()}/{self.artifact_id}."
            f"{_FORMAT_SUFFIX[self.raw_format]}"
        )
        if self.storage_key != expected_storage_key:
            raise CorporateActionAdapterError(
                "storage_key does not match raw artifact identity"
            )
        expected_descriptor_key = (
            f"descriptors/corporate-actions/{self.descriptor_id}.json"
        )
        if self.descriptor_key != expected_descriptor_key:
            raise CorporateActionAdapterError(
                "descriptor_key does not match descriptor identity"
            )
        _validate_source_url(self.source_owner, self.source_family, self.request_url)
        if self.request_method not in {"GET", "POST"}:
            raise CorporateActionAdapterError("request_method must be GET or POST")
        _require_sha256(self.request_payload_digest, "request_payload_digest")
        status = _require_int(self.response_status, "response_status")
        if not 200 <= status <= 299:
            raise CorporateActionAdapterError("only successful HTTP responses are capturable")
        normalized_headers = _normalize_headers(self.response_headers)
        if normalized_headers != self.response_headers:
            raise CorporateActionAdapterError(
                "response_headers must be selected and normalized"
            )
        if self.content_type != _content_type(normalized_headers):
            raise CorporateActionAdapterError(
                "content_type disagrees with response Content-Type"
            )
        _validate_content_type(self.raw_format, self.content_type)
        _validate_content_length(normalized_headers, self.byte_length)
        ensure_aware(self.retrieved_at, "retrieved_at")
        if self.retrieved_at.utcoffset() != timedelta(0):
            raise CorporateActionAdapterError("retrieved_at must be represented in UTC")
        if _require_int(self.byte_length, "byte_length") <= 0:
            raise CorporateActionAdapterError("byte_length must be positive")
        _require_text(self.source_version, "source_version")
        _validate_redirect_chain(
            self.source_owner,
            self.request_url,
            self.redirect_chain,
        )
        if self.descriptor_id != fingerprint(self._identity_payload()):
            raise CorporateActionAdapterError(
                "descriptor_id does not match raw capture content"
            )

    @property
    def observed_at(self) -> datetime:
        return self.retrieved_at

    @property
    def known_at(self) -> datetime:
        return self.retrieved_at

    def _identity_payload(self) -> dict[str, object]:
        return {
            "schema": _CAPTURE_SCHEMA,
            "artifact_id": self.artifact_id,
            "storage_key": self.storage_key,
            "request_url": self.request_url,
            "request_method": self.request_method,
            "request_payload_digest": self.request_payload_digest,
            "response_status": self.response_status,
            "response_headers": self.response_headers,
            "content_type": self.content_type,
            "redirect_chain": [hop.as_dict() for hop in self.redirect_chain],
            "retrieved_at": _iso_utc(self.retrieved_at),
            "raw_sha256": self.raw_sha256,
            "byte_length": self.byte_length,
            "source_owner": self.source_owner.value,
            "source_family": self.source_family.value,
            "source_version": self.source_version,
            "raw_format": self.raw_format.value,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            **self._identity_payload(),
            "descriptor_id": self.descriptor_id,
            "descriptor_key": self.descriptor_key,
        }


def capture_corporate_action_raw(
    root: str | Path,
    *,
    raw_bytes: bytes,
    request_url: str,
    request_method: str,
    request_payload_digest: str,
    response_status: int,
    response_headers: Mapping[str, str],
    redirect_chain: Iterable[RedirectHop],
    retrieved_at: datetime,
    source_owner: CorporateActionSourceOwner,
    source_family: CorporateActionSourceFamily,
    source_version: str,
    raw_format: RawCorporateActionFormat,
) -> CorporateActionRawCapture:
    """Persist exact response/file bytes before any extraction or decoding."""

    if not isinstance(raw_bytes, bytes) or not raw_bytes:
        raise CorporateActionAdapterError("raw_bytes must be non-empty bytes")
    if not isinstance(source_owner, CorporateActionSourceOwner):
        raise CorporateActionAdapterError(
            "source_owner must be CorporateActionSourceOwner"
        )
    if not isinstance(source_family, CorporateActionSourceFamily):
        raise CorporateActionAdapterError(
            "source_family must be CorporateActionSourceFamily"
        )
    if not isinstance(raw_format, RawCorporateActionFormat):
        raise CorporateActionAdapterError(
            "raw_format must be RawCorporateActionFormat"
        )
    normalized_url = _validate_source_url(source_owner, source_family, request_url)
    method = _require_text(request_method, "request_method").upper()
    if method not in {"GET", "POST"}:
        raise CorporateActionAdapterError("request_method must be GET or POST")
    _require_sha256(request_payload_digest, "request_payload_digest")
    status = _require_int(response_status, "response_status")
    if not 200 <= status <= 299:
        raise CorporateActionAdapterError("HTTP error response is not a valid artifact")
    headers = _normalize_headers(response_headers)
    content_type = _content_type(headers)
    _validate_content_type(raw_format, content_type)
    _validate_content_length(headers, len(raw_bytes))
    if raw_format is RawCorporateActionFormat.HTML:
        _reject_html_error_page(raw_bytes)
    retrieved_utc = to_utc(retrieved_at, "retrieved_at")
    redirects = tuple(redirect_chain)
    _validate_redirect_chain(source_owner, normalized_url, redirects)
    version = _require_text(source_version, "source_version")
    raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    suffix = _FORMAT_SUFFIX[raw_format]
    storage_key = validate_storage_key(
        "raw/corporate-actions/"
        f"{source_owner.value.lower()}/{raw_sha256}.{suffix}"
    )
    root_path = Path(root)
    root_path.mkdir(parents=True, exist_ok=True)
    _atomic_write(safe_artifact_path(root_path, storage_key), raw_bytes)
    identity = {
        "schema": _CAPTURE_SCHEMA,
        "artifact_id": raw_sha256,
        "storage_key": storage_key,
        "request_url": normalized_url,
        "request_method": method,
        "request_payload_digest": request_payload_digest,
        "response_status": status,
        "response_headers": headers,
        "content_type": content_type,
        "redirect_chain": [hop.as_dict() for hop in redirects],
        "retrieved_at": _iso_utc(retrieved_utc),
        "raw_sha256": raw_sha256,
        "byte_length": len(raw_bytes),
        "source_owner": source_owner.value,
        "source_family": source_family.value,
        "source_version": version,
        "raw_format": raw_format.value,
    }
    descriptor_id = fingerprint(identity)
    descriptor_key = validate_storage_key(
        f"descriptors/corporate-actions/{descriptor_id}.json"
    )
    payload = {
        **identity,
        "descriptor_id": descriptor_id,
        "descriptor_key": descriptor_key,
    }
    _atomic_write(
        safe_artifact_path(root_path, descriptor_key),
        (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
            "utf-8"
        ),
    )
    return CorporateActionRawCapture(
        artifact_id=raw_sha256,
        descriptor_id=descriptor_id,
        storage_key=storage_key,
        descriptor_key=descriptor_key,
        request_url=normalized_url,
        request_method=method,
        request_payload_digest=request_payload_digest,
        response_status=status,
        response_headers=headers,
        content_type=content_type,
        redirect_chain=redirects,
        retrieved_at=retrieved_utc,
        raw_sha256=raw_sha256,
        byte_length=len(raw_bytes),
        source_owner=source_owner,
        source_family=source_family,
        source_version=version,
        raw_format=raw_format,
    )


_CAPTURE_FIELDS = frozenset(
    {
        "schema",
        "artifact_id",
        "descriptor_id",
        "storage_key",
        "descriptor_key",
        "request_url",
        "request_method",
        "request_payload_digest",
        "response_status",
        "response_headers",
        "content_type",
        "redirect_chain",
        "retrieved_at",
        "raw_sha256",
        "byte_length",
        "source_owner",
        "source_family",
        "source_version",
        "raw_format",
    }
)


def load_corporate_action_raw(
    root: str | Path,
    *,
    descriptor_key: str,
) -> tuple[CorporateActionRawCapture, bytes]:
    root_path = Path(root)
    path = safe_artifact_path(root_path, descriptor_key)
    try:
        value = _strict_json_loads(
            path.read_text(encoding="utf-8"),
            "capture descriptor",
        )
    except (OSError, UnicodeError) as exc:
        raise CorporateActionAdapterError("capture descriptor is unreadable") from exc
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise CorporateActionAdapterError("capture descriptor must be a JSON object")
    unknown = sorted(set(value) - _CAPTURE_FIELDS)
    missing = sorted(_CAPTURE_FIELDS - set(value))
    if unknown:
        raise CorporateActionAdapterError(
            "capture descriptor contains unknown fields: " + ", ".join(unknown)
        )
    if missing:
        raise CorporateActionAdapterError(
            "capture descriptor is missing fields: " + ", ".join(missing)
        )
    if value.get("schema") != _CAPTURE_SCHEMA:
        raise CorporateActionAdapterError("unsupported capture descriptor schema")
    if value.get("descriptor_key") != descriptor_key:
        raise CorporateActionAdapterError(
            "descriptor_key does not match the requested path"
        )
    headers_value = value.get("response_headers")
    redirects_value = value.get("redirect_chain")
    if not isinstance(headers_value, dict):
        raise CorporateActionAdapterError("response_headers must be an object")
    if not isinstance(redirects_value, list):
        raise CorporateActionAdapterError("redirect_chain must be an array")
    redirects: list[RedirectHop] = []
    for item in redirects_value:
        if not isinstance(item, dict) or set(item) != {"status", "from_url", "to_url"}:
            raise CorporateActionAdapterError("redirect entries must be strict objects")
        redirects.append(
            RedirectHop(
                status=_require_int(item["status"], "redirect status"),
                from_url=_require_text(item["from_url"], "redirect from_url"),
                to_url=_require_text(item["to_url"], "redirect to_url"),
            )
        )
    try:
        capture = CorporateActionRawCapture(
            artifact_id=_require_sha256(value["artifact_id"], "artifact_id"),
            descriptor_id=_require_sha256(value["descriptor_id"], "descriptor_id"),
            storage_key=_require_text(value["storage_key"], "storage_key"),
            descriptor_key=descriptor_key,
            request_url=_require_text(value["request_url"], "request_url"),
            request_method=_require_text(value["request_method"], "request_method"),
            request_payload_digest=_require_sha256(
                value["request_payload_digest"],
                "request_payload_digest",
            ),
            response_status=_require_int(value["response_status"], "response_status"),
            response_headers={
                _require_text(key, "response header name"): _require_text(
                    item,
                    "response header value",
                )
                for key, item in headers_value.items()
            },
            content_type=_require_text(value["content_type"], "content_type"),
            redirect_chain=tuple(redirects),
            retrieved_at=_parse_datetime(value["retrieved_at"], "retrieved_at"),
            raw_sha256=_require_sha256(value["raw_sha256"], "raw_sha256"),
            byte_length=_require_int(value["byte_length"], "byte_length"),
            source_owner=CorporateActionSourceOwner(
                _require_text(value["source_owner"], "source_owner")
            ),
            source_family=CorporateActionSourceFamily(
                _require_text(value["source_family"], "source_family")
            ),
            source_version=_require_text(value["source_version"], "source_version"),
            raw_format=RawCorporateActionFormat(
                _require_text(value["raw_format"], "raw_format")
            ),
        )
    except (KeyError, ValueError) as exc:
        if isinstance(exc, CorporateActionAdapterError):
            raise
        raise CorporateActionAdapterError(
            "capture descriptor contains invalid values"
        ) from exc
    raw_path = safe_artifact_path(root_path, capture.storage_key)
    if not raw_path.is_file():
        raise CorporateActionAdapterError("raw artifact disappeared")
    try:
        raw_bytes = raw_path.read_bytes()
    except OSError as exc:
        raise CorporateActionAdapterError("raw artifact is unreadable") from exc
    if len(raw_bytes) != capture.byte_length:
        raise CorporateActionAdapterError("raw artifact byte length changed")
    if hashlib.sha256(raw_bytes).hexdigest() != capture.raw_sha256:
        raise CorporateActionAdapterError("raw artifact hash changed")
    return capture, raw_bytes


@dataclass(frozen=True, slots=True)
class CandidateCorporateAction:
    action_id: str
    instrument_id: str
    identity_fact_id: str
    symbol: str
    market: Market
    exchange: str
    action_type: CandidateCorporateActionType
    lifecycle: CandidateCorporateActionLifecycle
    source_published_at: date | datetime | None
    source_published_granularity: SourcePublishedGranularity
    observed_at: datetime
    retrieved_at: datetime
    known_at: datetime
    usable_from: datetime
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
    source_uri: str
    raw_artifact_id: str
    raw_descriptor_id: str
    parser_version: str
    source_owner: CorporateActionSourceOwner
    source_family: CorporateActionSourceFamily
    source_version: str
    synthetic_fixture: bool
    gaps: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        _require_text(self.action_id, "action_id")
        _require_text(self.instrument_id, "instrument_id")
        _require_sha256(self.identity_fact_id, "identity_fact_id")
        if not isinstance(self.market, Market):
            raise CorporateActionAdapterError("market must be Market")
        _require_text(self.exchange, "exchange")
        code, separator, suffix = self.symbol.rpartition(".")
        if (
            type(self.symbol) is not str
            or self.symbol != self.symbol.upper()
            or not separator
            or not code
            or suffix not in _SYMBOL_SUFFIXES[self.market]
        ):
            raise CorporateActionAdapterError("symbol must be canonical for market")
        if not isinstance(self.action_type, CandidateCorporateActionType):
            raise CorporateActionAdapterError(
                "action_type must be CandidateCorporateActionType"
            )
        if not isinstance(self.lifecycle, CandidateCorporateActionLifecycle):
            raise CorporateActionAdapterError(
                "lifecycle must be CandidateCorporateActionLifecycle"
            )
        if not isinstance(
            self.source_published_granularity,
            SourcePublishedGranularity,
        ):
            raise CorporateActionAdapterError(
                "source_published_granularity must be SourcePublishedGranularity"
            )
        for name in ("observed_at", "retrieved_at", "known_at", "usable_from"):
            ensure_aware(getattr(self, name), name)
        if to_utc(self.observed_at) != to_utc(self.retrieved_at):
            raise CorporateActionAdapterError(
                "candidate observed_at must equal exact raw retrieved_at"
            )
        if to_utc(self.known_at) != to_utc(self.observed_at):
            raise CorporateActionAdapterError(
                "candidate known_at cannot be backdated before first observation"
            )
        if to_utc(self.usable_from) < to_utc(self.known_at):
            raise CorporateActionAdapterError("usable_from cannot precede known_at")
        if self.source_published_granularity is SourcePublishedGranularity.DATE:
            if type(self.source_published_at) is not date:
                raise CorporateActionAdapterError(
                    "DATE publication requires date without fabricated time"
                )
            if self.source_published_at > exchange_local_date(
                self.known_at,
                self.market,
            ):
                raise CorporateActionAdapterError(
                    "source publication date cannot follow known_at"
                )
        elif self.source_published_granularity is SourcePublishedGranularity.SECOND:
            if not isinstance(self.source_published_at, datetime):
                raise CorporateActionAdapterError(
                    "SECOND publication requires timezone-aware datetime"
                )
            ensure_aware(self.source_published_at, "source_published_at")
            if to_utc(self.source_published_at) > to_utc(self.known_at):
                raise CorporateActionAdapterError(
                    "source publication timestamp cannot follow known_at"
                )
        elif self.source_published_at is not None:
            raise CorporateActionAdapterError(
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
                raise CorporateActionAdapterError(
                    f"{name} must be a finite Decimal or null"
                )
        if self.automatic_share_ratio is not None and self.automatic_share_ratio <= 0:
            raise CorporateActionAdapterError(
                "automatic_share_ratio must be positive when present"
            )
        for name in ("cash_dividend_per_share", "rights_entitlement_ratio"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise CorporateActionAdapterError(f"{name} cannot be negative")
        for name in ("rights_subscription_price", "reference_price"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise CorporateActionAdapterError(f"{name} must be positive")
        if (
            self.rights_entitlement_ratio is not None
            and self.rights_entitlement_ratio == 0
            and self.rights_subscription_price is not None
        ):
            raise CorporateActionAdapterError(
                "rights_subscription_price requires positive rights entitlement"
            )
        if self.reference_price is None and self.reference_price_snapshot_id is not None:
            raise CorporateActionAdapterError(
                "reference_price_snapshot_id requires reference_price"
            )
        if self.ex_date is not None:
            if self.payment_date is not None and self.payment_date < self.ex_date:
                raise CorporateActionAdapterError("payment_date cannot precede ex_date")
            if (
                self.share_listing_date is not None
                and self.share_listing_date < self.ex_date
            ):
                raise CorporateActionAdapterError(
                    "share_listing_date cannot precede ex_date"
                )
        if self.lifecycle is CandidateCorporateActionLifecycle.CANCELLED and any(
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
            raise CorporateActionAdapterError(
                "CANCELLED candidate cannot carry economic terms"
            )
        if self.currency is not None and (
            type(self.currency) is not str or _CURRENCY.fullmatch(self.currency) is None
        ):
            raise CorporateActionAdapterError("currency must be uppercase three-letter text")
        if self.reference_price_snapshot_id is not None:
            _require_sha256(
                self.reference_price_snapshot_id,
                "reference_price_snapshot_id",
            )
        _require_text(self.revision_id, "revision_id")
        if self.supersedes_revision_id is not None:
            _require_text(self.supersedes_revision_id, "supersedes_revision_id")
            if self.supersedes_revision_id == self.revision_id:
                raise CorporateActionAdapterError("revision cannot supersede itself")
        _validate_source_url(self.source_owner, self.source_family, self.source_uri)
        _require_sha256(self.raw_artifact_id, "raw_artifact_id")
        _require_sha256(self.raw_descriptor_id, "raw_descriptor_id")
        _require_text(self.parser_version, "parser_version")
        _require_text(self.source_version, "source_version")
        _require_bool(self.synthetic_fixture, "synthetic_fixture")
        if self.synthetic_fixture is not True:
            raise CorporateActionAdapterError(
                "fixture parser output is always synthetic and cannot be relabelled"
            )
        object.__setattr__(
            self,
            "gaps",
            _candidate_gaps(
                action_type=self.action_type,
                lifecycle=self.lifecycle,
                granularity=self.source_published_granularity,
                ex_date=self.ex_date,
                record_date=self.record_date,
                share_listing_date=self.share_listing_date,
                effective_date=self.effective_date,
                automatic_share_ratio=self.automatic_share_ratio,
                cash_dividend_per_share=self.cash_dividend_per_share,
                rights_entitlement_ratio=self.rights_entitlement_ratio,
                rights_subscription_price=self.rights_subscription_price,
                currency=self.currency,
                reference_price=self.reference_price,
                reference_price_snapshot_id=self.reference_price_snapshot_id,
            ),
        )

    @property
    def candidate_id(self) -> str:
        return fingerprint(self)

    @property
    def stream_identity(self) -> tuple[str, str, str, str, str]:
        return (
            self.instrument_id,
            self.source_owner.value,
            self.source_family.value,
            self.source_version,
            self.action_id,
        )

    def _revision_payload(self) -> dict[str, object]:
        return {
            "instrument_id": self.instrument_id,
            "identity_fact_id": self.identity_fact_id,
            "symbol": self.symbol,
            "market": self.market,
            "exchange": self.exchange,
            "action_type": self.action_type,
            "lifecycle": self.lifecycle,
            "source_published_at": self.source_published_at,
            "source_published_granularity": self.source_published_granularity,
            "usable_from": self.usable_from,
            "ex_date": self.ex_date,
            "record_date": self.record_date,
            "payment_date": self.payment_date,
            "share_listing_date": self.share_listing_date,
            "effective_date": self.effective_date,
            "automatic_share_ratio": self.automatic_share_ratio,
            "cash_dividend_per_share": self.cash_dividend_per_share,
            "rights_entitlement_ratio": self.rights_entitlement_ratio,
            "rights_subscription_price": self.rights_subscription_price,
            "currency": self.currency,
            "reference_price": self.reference_price,
            "reference_price_snapshot_id": self.reference_price_snapshot_id,
            "raw_artifact_id": self.raw_artifact_id,
            "raw_descriptor_id": self.raw_descriptor_id,
            "parser_version": self.parser_version,
            "gaps": self.gaps,
        }

    def to_core_fact(self) -> CorporateActionFact:
        """Convert one candidate without promoting verification or completeness."""

        if self.ex_date is None:
            raise CorporateActionAdapterError("core fact requires ex_date")
        if self.action_type not in _SUPPORTED_FACTOR_TYPES:
            raise CorporateActionAdapterError(
                f"{self.action_type.value} is explicit but not factor-normalized"
            )
        if self.lifecycle in {
            CandidateCorporateActionLifecycle.EFFECTIVE,
            CandidateCorporateActionLifecycle.COMPLETED,
        }:
            lifecycle = CorporateActionLifecycle.EFFECTIVE
        elif self.lifecycle is CandidateCorporateActionLifecycle.CANCELLED:
            lifecycle = CorporateActionLifecycle.CANCELLED
        else:
            lifecycle = CorporateActionLifecycle.ANNOUNCED
        try:
            return CorporateActionFact(
                action_id=self.action_id,
                instrument_id=self.instrument_id,
                identity_fact_id=self.identity_fact_id,
                symbol=self.symbol,
                market=self.market,
                ex_date=self.ex_date,
                record_date=self.record_date,
                payment_date=self.payment_date,
                share_listing_date=self.share_listing_date,
                lifecycle=lifecycle,
                automatic_share_ratio=self.automatic_share_ratio,
                cash_dividend_per_share=self.cash_dividend_per_share,
                rights_entitlement_ratio=self.rights_entitlement_ratio,
                rights_subscription_price=self.rights_subscription_price,
                currency=self.currency,
                reference_price=self.reference_price,
                reference_price_snapshot_id=self.reference_price_snapshot_id,
                known_at=self.known_at,
                usable_from=self.usable_from,
                source=f"{self.source_owner.value}/{self.source_family.value}",
                action_version=self.source_version,
                revision=self.revision_id,
                supersedes_revision=self.supersedes_revision_id,
                verified=False,
                source_note=(
                    "Stage 2C unverified candidate; "
                    f"raw={self.raw_artifact_id}; descriptor={self.raw_descriptor_id}"
                ),
            )
        except CorporateActionContractError as exc:
            raise CorporateActionAdapterError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class CorporateActionCandidateDocument:
    raw_descriptor_id: str
    raw_artifact_id: str
    parser_version: str
    candidates: tuple[CandidateCorporateAction, ...]
    synthetic_fixture: bool = True
    gaps: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.raw_descriptor_id, "raw_descriptor_id")
        _require_sha256(self.raw_artifact_id, "raw_artifact_id")
        _require_text(self.parser_version, "parser_version")
        _require_bool(self.synthetic_fixture, "synthetic_fixture")
        if self.synthetic_fixture is not True:
            raise CorporateActionAdapterError(
                "fixture document is always synthetic and cannot be relabelled"
            )
        order = tuple(
            (
                item.stream_identity,
                item.revision_id,
                item.candidate_id,
            )
            for item in self.candidates
        )
        if order != tuple(sorted(order)):
            raise CorporateActionAdapterError(
                "candidate actions must be deterministically sorted"
            )
        revision_keys = [
            (item.stream_identity, item.revision_id) for item in self.candidates
        ]
        if len(set(revision_keys)) != len(revision_keys):
            raise CorporateActionAdapterError(
                "duplicate action/revision identity is forbidden"
            )
        if any(item.raw_descriptor_id != self.raw_descriptor_id for item in self.candidates):
            raise CorporateActionAdapterError(
                "candidate raw descriptor identity differs from document"
            )
        if any(item.raw_artifact_id != self.raw_artifact_id for item in self.candidates):
            raise CorporateActionAdapterError(
                "candidate raw artifact identity differs from document"
            )
        if any(item.parser_version != self.parser_version for item in self.candidates):
            raise CorporateActionAdapterError(
                "candidate parser version differs from document"
            )
        object.__setattr__(
            self,
            "gaps",
            tuple(sorted({gap for item in self.candidates for gap in item.gaps})),
        )

    @property
    def document_id(self) -> str:
        return fingerprint(
            {
                "schema": "stage2c-corporate-action-candidate-document-v1",
                "raw_descriptor_id": self.raw_descriptor_id,
                "raw_artifact_id": self.raw_artifact_id,
                "parser_version": self.parser_version,
                "candidate_ids": [item.candidate_id for item in self.candidates],
                "gaps": self.gaps,
                "synthetic_fixture": self.synthetic_fixture,
            }
        )


def _candidate_gaps(
    *,
    action_type: CandidateCorporateActionType,
    lifecycle: CandidateCorporateActionLifecycle,
    granularity: SourcePublishedGranularity,
    ex_date: date | None,
    record_date: date | None,
    share_listing_date: date | None,
    effective_date: date | None,
    automatic_share_ratio: Decimal | None,
    cash_dividend_per_share: Decimal | None,
    rights_entitlement_ratio: Decimal | None,
    rights_subscription_price: Decimal | None,
    currency: str | None,
    reference_price: Decimal | None,
    reference_price_snapshot_id: str | None,
) -> tuple[str, ...]:
    gaps: set[str] = set()
    if granularity is SourcePublishedGranularity.DATE:
        gaps.add("DATE_ONLY_PUBLICATION_NO_INTRADAY_PRECISION")
    if lifecycle not in {
        CandidateCorporateActionLifecycle.EFFECTIVE,
        CandidateCorporateActionLifecycle.COMPLETED,
        CandidateCorporateActionLifecycle.CANCELLED,
    }:
        gaps.add("ACTION_NOT_IMPLEMENTED")
    if action_type not in _SUPPORTED_FACTOR_TYPES:
        gaps.add(f"UNSUPPORTED_ACTION_TYPE_{action_type.value}")
    if ex_date is None:
        gaps.add("MISSING_EX_DATE")
    if record_date is None:
        gaps.add("MISSING_RECORD_DATE")
    if (
        lifecycle
        in {
            CandidateCorporateActionLifecycle.EFFECTIVE,
            CandidateCorporateActionLifecycle.COMPLETED,
        }
        and effective_date is None
    ):
        gaps.add("MISSING_EFFECTIVE_DATE")
    if lifecycle is CandidateCorporateActionLifecycle.CANCELLED:
        return tuple(sorted(gaps))
    if automatic_share_ratio is None:
        gaps.add("MISSING_AUTOMATIC_SHARE_RATIO")
    elif automatic_share_ratio != Decimal(1) and share_listing_date is None:
        gaps.add("MISSING_SHARE_LISTING_DATE")
    if cash_dividend_per_share is None:
        gaps.add("MISSING_CASH_DIVIDEND_PER_SHARE")
    if rights_entitlement_ratio is None:
        gaps.add("MISSING_RIGHTS_ENTITLEMENT_RATIO")
    elif rights_entitlement_ratio > 0 and rights_subscription_price is None:
        gaps.add("MISSING_RIGHTS_SUBSCRIPTION_PRICE")
    if (
        automatic_share_ratio == Decimal(1)
        and cash_dividend_per_share == Decimal(0)
        and rights_entitlement_ratio == Decimal(0)
    ):
        gaps.add("NO_EFFECTIVE_ECONOMIC_TERMS")
    monetary_terms_present = (
        (cash_dividend_per_share is not None and cash_dividend_per_share > 0)
        or (rights_entitlement_ratio is not None and rights_entitlement_ratio > 0)
        or reference_price is not None
    )
    if monetary_terms_present and currency is None:
        gaps.add("MISSING_CURRENCY")
    if (
        (cash_dividend_per_share is not None and cash_dividend_per_share > 0)
        or (rights_entitlement_ratio is not None and rights_entitlement_ratio > 0)
    ) and reference_price is None:
        gaps.add("MISSING_REFERENCE_PRICE")
    if reference_price is not None and reference_price_snapshot_id is None:
        gaps.add("MISSING_REFERENCE_PRICE_SNAPSHOT_ID")
    return tuple(sorted(gaps))


def _parse_candidate(
    value: object,
    *,
    capture: CorporateActionRawCapture,
    parser_version: str,
) -> CandidateCorporateAction:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise CorporateActionAdapterError("action row must be a JSON object")
    unknown = sorted(set(value) - _ACTION_FIELDS)
    missing = sorted(_ACTION_FIELDS - set(value))
    if unknown:
        raise CorporateActionAdapterError(
            "action row contains unknown fields: " + ", ".join(unknown)
        )
    if missing:
        raise CorporateActionAdapterError(
            "action row is missing fields: " + ", ".join(missing)
        )
    try:
        market = Market(_require_text(value["market"], "market"))
        action_type = CandidateCorporateActionType(
            _require_text(value["action_type"], "action_type")
        )
        lifecycle = CandidateCorporateActionLifecycle(
            _require_text(value["lifecycle"], "lifecycle")
        )
        granularity = SourcePublishedGranularity(
            _require_text(
                value["source_published_granularity"],
                "source_published_granularity",
            )
        )
        published = _parse_publication(value["source_published_at"], granularity)
        automatic = _parse_optional_decimal(
            value["automatic_share_ratio"],
            "automatic_share_ratio",
        )
        cash = _parse_optional_decimal(
            value["cash_dividend_per_share"],
            "cash_dividend_per_share",
        )
        rights = _parse_optional_decimal(
            value["rights_entitlement_ratio"],
            "rights_entitlement_ratio",
        )
        rights_price = _parse_optional_decimal(
            value["rights_subscription_price"],
            "rights_subscription_price",
        )
        reference_price = _parse_optional_decimal(
            value["reference_price"],
            "reference_price",
        )
        reference_snapshot_raw = value["reference_price_snapshot_id"]
        reference_snapshot = (
            None
            if reference_snapshot_raw is None
            else _require_sha256(
                reference_snapshot_raw,
                "reference_price_snapshot_id",
            )
        )
        supersedes_raw = value["supersedes_revision_id"]
        supersedes = (
            None
            if supersedes_raw is None
            else _require_text(supersedes_raw, "supersedes_revision_id")
        )
        ex_date = _parse_optional_date(value["ex_date"], "ex_date")
        record_date = _parse_optional_date(value["record_date"], "record_date")
        payment_date = _parse_optional_date(value["payment_date"], "payment_date")
        share_listing_date = _parse_optional_date(
            value["share_listing_date"],
            "share_listing_date",
        )
        effective_date = _parse_optional_date(
            value["effective_date"],
            "effective_date",
        )
        currency_raw = value["currency"]
        currency = (
            None if currency_raw is None else _require_text(currency_raw, "currency")
        )
        return CandidateCorporateAction(
            action_id=_require_text(value["action_id"], "action_id"),
            instrument_id=_require_text(value["instrument_id"], "instrument_id"),
            identity_fact_id=_require_sha256(
                value["identity_fact_id"],
                "identity_fact_id",
            ),
            symbol=_require_text(value["symbol"], "symbol"),
            market=market,
            exchange=_require_text(value["exchange"], "exchange"),
            action_type=action_type,
            lifecycle=lifecycle,
            source_published_at=published,
            source_published_granularity=granularity,
            observed_at=capture.retrieved_at,
            retrieved_at=capture.retrieved_at,
            known_at=capture.retrieved_at,
            usable_from=capture.retrieved_at,
            ex_date=ex_date,
            record_date=record_date,
            payment_date=payment_date,
            share_listing_date=share_listing_date,
            effective_date=effective_date,
            automatic_share_ratio=automatic,
            cash_dividend_per_share=cash,
            rights_entitlement_ratio=rights,
            rights_subscription_price=rights_price,
            currency=currency,
            reference_price=reference_price,
            reference_price_snapshot_id=reference_snapshot,
            revision_id=_require_text(value["revision_id"], "revision_id"),
            supersedes_revision_id=supersedes,
            source_uri=capture.request_url,
            raw_artifact_id=capture.artifact_id,
            raw_descriptor_id=capture.descriptor_id,
            parser_version=parser_version,
            source_owner=capture.source_owner,
            source_family=capture.source_family,
            source_version=capture.source_version,
            synthetic_fixture=True,
        )
    except (KeyError, ValueError) as exc:
        if isinstance(exc, CorporateActionAdapterError):
            raise
        raise CorporateActionAdapterError("action row contains invalid values") from exc


def parse_corporate_action_document(
    raw_bytes: bytes,
    *,
    capture: CorporateActionRawCapture,
    parser_version: str = CORPORATE_ACTION_FIXTURE_PARSER_VERSION,
) -> CorporateActionCandidateDocument:
    if not isinstance(capture, CorporateActionRawCapture):
        raise CorporateActionAdapterError(
            "capture must be CorporateActionRawCapture"
        )
    if not isinstance(raw_bytes, bytes) or not raw_bytes:
        raise CorporateActionAdapterError("raw_bytes must be non-empty bytes")
    if len(raw_bytes) != capture.byte_length:
        raise CorporateActionAdapterError(
            "raw bytes do not match capture byte length"
        )
    if hashlib.sha256(raw_bytes).hexdigest() != capture.raw_sha256:
        raise CorporateActionAdapterError(
            "raw bytes do not match capture SHA-256"
        )
    if capture.raw_format is not RawCorporateActionFormat.JSON:
        raise CorporateActionAdapterError(
            f"extraction required for {capture.raw_format.value}"
        )
    version = _require_text(parser_version, "parser_version")
    if version != CORPORATE_ACTION_FIXTURE_PARSER_VERSION:
        raise CorporateActionAdapterError("unsupported corporate-action parser version")
    try:
        value = _strict_json_loads(
            raw_bytes.decode("utf-8", errors="strict"),
            "corporate-action fixture",
        )
    except UnicodeError as exc:
        raise CorporateActionAdapterError(
            "corporate-action fixture must be strict UTF-8 JSON"
        ) from exc
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise CorporateActionAdapterError("fixture must be a JSON object")
    unknown = sorted(set(value) - _FIXTURE_FIELDS)
    missing = sorted(_FIXTURE_FIELDS - set(value))
    if unknown:
        raise CorporateActionAdapterError(
            "fixture contains unknown fields: " + ", ".join(unknown)
        )
    if missing:
        raise CorporateActionAdapterError(
            "fixture is missing fields: " + ", ".join(missing)
        )
    if value.get("schema") != _FIXTURE_SCHEMA:
        raise CorporateActionAdapterError("unsupported fixture schema")
    if _require_bool(value["synthetic_fixture"], "synthetic_fixture") is not True:
        raise CorporateActionAdapterError(
            "fixture parser accepts synthetic_fixture=true only"
        )
    actions = value["actions"]
    if not isinstance(actions, list) or not actions:
        raise CorporateActionAdapterError("fixture actions must be a non-empty array")
    candidates = tuple(
        sorted(
            (
                _parse_candidate(
                    item,
                    capture=capture,
                    parser_version=version,
                )
                for item in actions
            ),
            key=lambda item: (
                item.stream_identity,
                item.revision_id,
                item.candidate_id,
            ),
        )
    )
    return CorporateActionCandidateDocument(
        raw_descriptor_id=capture.descriptor_id,
        raw_artifact_id=capture.artifact_id,
        parser_version=version,
        candidates=candidates,
        synthetic_fixture=True,
    )


def resolve_corporate_action_candidates(
    candidates: Iterable[CandidateCorporateAction],
    *,
    as_of: datetime | None,
) -> tuple[CandidateCorporateAction, ...]:
    cutoff = None if as_of is None else to_utc(as_of, "as_of")
    unique: dict[str, CandidateCorporateAction] = {}
    for candidate in candidates:
        if not isinstance(candidate, CandidateCorporateAction):
            raise CorporateActionAdapterError(
                "resolver accepts CandidateCorporateAction values only"
            )
        if cutoff is not None and (
            to_utc(candidate.known_at) > cutoff
            or to_utc(candidate.usable_from) > cutoff
        ):
            continue
        unique[candidate.candidate_id] = candidate
    grouped: dict[
        tuple[str, str, str, str, str],
        list[CandidateCorporateAction],
    ] = defaultdict(list)
    for candidate in unique.values():
        grouped[candidate.stream_identity].append(candidate)
    selected: list[CandidateCorporateAction] = []
    for records in grouped.values():
        try:
            selected.append(
                select_superseding_revision(
                    records,
                    revision_of=lambda item: item.revision_id,
                    predecessor_of=lambda item: item.supersedes_revision_id,
                    payload_of=lambda item: item._revision_payload(),
                    identity_of=lambda item: item.candidate_id,
                    known_at_of=lambda item: item.known_at,
                )
            )
        except PITConflictError as exc:
            raise CorporateActionAdapterError(str(exc)) from exc
    return tuple(
        sorted(
            selected,
            key=lambda item: (
                item.instrument_id,
                item.ex_date or date.max,
                item.action_id,
                item.candidate_id,
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class CorporateActionParseDescriptor:
    parse_descriptor_id: str
    parse_descriptor_key: str
    raw_descriptor_id: str
    raw_descriptor_key: str
    raw_artifact_id: str
    parser_version: str
    extraction_status: ExtractionStatus
    document_id: str | None
    candidate_ids: tuple[str, ...]
    gaps: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_sha256(self.parse_descriptor_id, "parse_descriptor_id")
        _require_sha256(self.raw_descriptor_id, "raw_descriptor_id")
        _require_sha256(self.raw_artifact_id, "raw_artifact_id")
        validate_storage_key(self.parse_descriptor_key)
        validate_storage_key(self.raw_descriptor_key)
        if self.parse_descriptor_key != (
            f"parse-descriptors/corporate-actions/{self.parse_descriptor_id}.json"
        ):
            raise CorporateActionAdapterError(
                "parse_descriptor_key does not match parse descriptor identity"
            )
        if self.raw_descriptor_key != (
            f"descriptors/corporate-actions/{self.raw_descriptor_id}.json"
        ):
            raise CorporateActionAdapterError(
                "raw_descriptor_key does not match raw descriptor identity"
            )
        _require_text(self.parser_version, "parser_version")
        if not isinstance(self.extraction_status, ExtractionStatus):
            raise CorporateActionAdapterError(
                "extraction_status must be ExtractionStatus"
            )
        if self.extraction_status is ExtractionStatus.PARSED:
            _require_sha256(self.document_id, "document_id")
            if not self.candidate_ids:
                raise CorporateActionAdapterError(
                    "PARSED descriptor requires candidate_ids"
                )
        else:
            if self.document_id is not None or self.candidate_ids:
                raise CorporateActionAdapterError(
                    "EXTRACTION_REQUIRED descriptor cannot claim parsed candidates"
                )
        for item in self.candidate_ids:
            _require_sha256(item, "candidate_id")
        if self.candidate_ids != tuple(sorted(set(self.candidate_ids))):
            raise CorporateActionAdapterError(
                "candidate_ids must be sorted and unique"
            )
        if self.gaps != tuple(sorted(set(self.gaps))):
            raise CorporateActionAdapterError("gaps must be sorted and unique")
        if self.parse_descriptor_id != fingerprint(self._identity_payload()):
            raise CorporateActionAdapterError(
                "parse_descriptor_id does not match descriptor content"
            )

    def _identity_payload(self) -> dict[str, object]:
        return {
            "schema": _PARSE_DESCRIPTOR_SCHEMA,
            "raw_descriptor_id": self.raw_descriptor_id,
            "raw_descriptor_key": self.raw_descriptor_key,
            "raw_artifact_id": self.raw_artifact_id,
            "parser_version": self.parser_version,
            "extraction_status": self.extraction_status.value,
            "document_id": self.document_id,
            "candidate_ids": list(self.candidate_ids),
            "gaps": list(self.gaps),
        }

    def as_dict(self) -> dict[str, object]:
        return {
            **self._identity_payload(),
            "parse_descriptor_id": self.parse_descriptor_id,
            "parse_descriptor_key": self.parse_descriptor_key,
        }


def write_corporate_action_parse_descriptor(
    root: str | Path,
    *,
    capture: CorporateActionRawCapture,
    parser_version: str = CORPORATE_ACTION_FIXTURE_PARSER_VERSION,
) -> CorporateActionParseDescriptor:
    version = _require_text(parser_version, "parser_version")
    loaded_capture, raw_bytes = load_corporate_action_raw(
        root,
        descriptor_key=capture.descriptor_key,
    )
    if loaded_capture.descriptor_id != capture.descriptor_id:
        raise CorporateActionAdapterError("raw capture identity changed before parsing")
    if capture.raw_format is RawCorporateActionFormat.JSON:
        document = parse_corporate_action_document(
            raw_bytes,
            capture=capture,
            parser_version=version,
        )
        status = ExtractionStatus.PARSED
        document_id: str | None = document.document_id
        candidate_ids = tuple(sorted(item.candidate_id for item in document.candidates))
        gaps = document.gaps
    else:
        status = ExtractionStatus.EXTRACTION_REQUIRED
        document_id = None
        candidate_ids = ()
        gaps = (f"EXTRACTION_REQUIRED_{capture.raw_format.value}",)
    identity = {
        "schema": _PARSE_DESCRIPTOR_SCHEMA,
        "raw_descriptor_id": capture.descriptor_id,
        "raw_descriptor_key": capture.descriptor_key,
        "raw_artifact_id": capture.artifact_id,
        "parser_version": version,
        "extraction_status": status.value,
        "document_id": document_id,
        "candidate_ids": list(candidate_ids),
        "gaps": list(gaps),
    }
    descriptor_id = fingerprint(identity)
    descriptor_key = validate_storage_key(
        f"parse-descriptors/corporate-actions/{descriptor_id}.json"
    )
    descriptor = CorporateActionParseDescriptor(
        parse_descriptor_id=descriptor_id,
        parse_descriptor_key=descriptor_key,
        raw_descriptor_id=capture.descriptor_id,
        raw_descriptor_key=capture.descriptor_key,
        raw_artifact_id=capture.artifact_id,
        parser_version=version,
        extraction_status=status,
        document_id=document_id,
        candidate_ids=candidate_ids,
        gaps=gaps,
    )
    _atomic_write(
        safe_artifact_path(Path(root), descriptor_key),
        (
            json.dumps(
                descriptor.as_dict(),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode("utf-8"),
    )
    return descriptor


_PARSE_DESCRIPTOR_FIELDS = frozenset(
    {
        "schema",
        "parse_descriptor_id",
        "parse_descriptor_key",
        "raw_descriptor_id",
        "raw_descriptor_key",
        "raw_artifact_id",
        "parser_version",
        "extraction_status",
        "document_id",
        "candidate_ids",
        "gaps",
    }
)


def load_corporate_action_parse_descriptor(
    root: str | Path,
    *,
    parse_descriptor_key: str,
) -> tuple[CorporateActionParseDescriptor, CorporateActionRawCapture, bytes]:
    root_path = Path(root)
    path = safe_artifact_path(root_path, parse_descriptor_key)
    try:
        value = _strict_json_loads(
            path.read_text(encoding="utf-8"),
            "parse descriptor",
        )
    except (OSError, UnicodeError) as exc:
        raise CorporateActionAdapterError("parse descriptor is unreadable") from exc
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise CorporateActionAdapterError("parse descriptor must be a JSON object")
    unknown = sorted(set(value) - _PARSE_DESCRIPTOR_FIELDS)
    missing = sorted(_PARSE_DESCRIPTOR_FIELDS - set(value))
    if unknown:
        raise CorporateActionAdapterError(
            "parse descriptor contains unknown fields: " + ", ".join(unknown)
        )
    if missing:
        raise CorporateActionAdapterError(
            "parse descriptor is missing fields: " + ", ".join(missing)
        )
    if value.get("schema") != _PARSE_DESCRIPTOR_SCHEMA:
        raise CorporateActionAdapterError("unsupported parse descriptor schema")
    if value.get("parse_descriptor_key") != parse_descriptor_key:
        raise CorporateActionAdapterError(
            "parse_descriptor_key does not match requested path"
        )
    candidate_ids_raw = value.get("candidate_ids")
    gaps_raw = value.get("gaps")
    if not isinstance(candidate_ids_raw, list) or not isinstance(gaps_raw, list):
        raise CorporateActionAdapterError("candidate_ids and gaps must be arrays")
    document_raw = value.get("document_id")
    try:
        descriptor = CorporateActionParseDescriptor(
            parse_descriptor_id=_require_sha256(
                value["parse_descriptor_id"],
                "parse_descriptor_id",
            ),
            parse_descriptor_key=parse_descriptor_key,
            raw_descriptor_id=_require_sha256(
                value["raw_descriptor_id"],
                "raw_descriptor_id",
            ),
            raw_descriptor_key=_require_text(
                value["raw_descriptor_key"],
                "raw_descriptor_key",
            ),
            raw_artifact_id=_require_sha256(
                value["raw_artifact_id"],
                "raw_artifact_id",
            ),
            parser_version=_require_text(value["parser_version"], "parser_version"),
            extraction_status=ExtractionStatus(
                _require_text(value["extraction_status"], "extraction_status")
            ),
            document_id=(
                None
                if document_raw is None
                else _require_sha256(document_raw, "document_id")
            ),
            candidate_ids=tuple(
                _require_sha256(item, "candidate_id") for item in candidate_ids_raw
            ),
            gaps=tuple(_require_text(item, "gap") for item in gaps_raw),
        )
    except (KeyError, ValueError) as exc:
        if isinstance(exc, CorporateActionAdapterError):
            raise
        raise CorporateActionAdapterError(
            "parse descriptor contains invalid values"
        ) from exc
    capture, raw_bytes = load_corporate_action_raw(
        root_path,
        descriptor_key=descriptor.raw_descriptor_key,
    )
    if capture.descriptor_id != descriptor.raw_descriptor_id:
        raise CorporateActionAdapterError(
            "parse descriptor raw descriptor identity mismatch"
        )
    if capture.artifact_id != descriptor.raw_artifact_id:
        raise CorporateActionAdapterError(
            "parse descriptor raw artifact identity mismatch"
        )
    return descriptor, capture, raw_bytes


def parse_corporate_action_from_descriptor(
    root: str | Path,
    *,
    parse_descriptor_key: str,
) -> CorporateActionCandidateDocument:
    descriptor, capture, raw_bytes = load_corporate_action_parse_descriptor(
        root,
        parse_descriptor_key=parse_descriptor_key,
    )
    if descriptor.extraction_status is ExtractionStatus.EXTRACTION_REQUIRED:
        raise CorporateActionAdapterError(
            "extraction required: " + ", ".join(descriptor.gaps)
        )
    document = parse_corporate_action_document(
        raw_bytes,
        capture=capture,
        parser_version=descriptor.parser_version,
    )
    if document.document_id != descriptor.document_id:
        raise CorporateActionAdapterError("document_id changed during deterministic replay")
    candidate_ids = tuple(sorted(item.candidate_id for item in document.candidates))
    if candidate_ids != descriptor.candidate_ids:
        raise CorporateActionAdapterError(
            "candidate identities changed during deterministic replay"
        )
    if document.gaps != descriptor.gaps:
        raise CorporateActionAdapterError("candidate gaps changed during replay")
    return document


__all__ = [
    "CORPORATE_ACTION_FIXTURE_PARSER_VERSION",
    "CandidateCorporateAction",
    "CandidateCorporateActionLifecycle",
    "CandidateCorporateActionType",
    "CorporateActionAdapterError",
    "CorporateActionCandidateDocument",
    "CorporateActionParseDescriptor",
    "CorporateActionRawCapture",
    "CorporateActionSourceFamily",
    "CorporateActionSourceOwner",
    "ExtractionStatus",
    "RawCorporateActionFormat",
    "RedirectHop",
    "SourcePublishedGranularity",
    "capture_corporate_action_raw",
    "digest_request_payload",
    "load_corporate_action_parse_descriptor",
    "load_corporate_action_raw",
    "parse_corporate_action_document",
    "parse_corporate_action_from_descriptor",
    "resolve_corporate_action_candidates",
    "write_corporate_action_parse_descriptor",
]

"""Exact-raw A-share calendar capture and deterministic candidate parsing.

The adapter deliberately stops before trust reconciliation.  It stores the exact
HTTP response bytes, validates a frozen source-family identity, and emits only
unverified candidate calendar facts plus explicit gaps.  It never creates a
verified/complete snapshot or assigns a Trust Tier.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from stock_tracker.core.types import Market

from ..core.calendar import (
    CalendarCoverage,
    CalendarDay,
    CalendarStatus,
    SessionKind,
)
from ..core.fingerprint import canonical_json, fingerprint, hash_file
from ..core.time import ensure_aware, to_utc
from .manifest import safe_artifact_path, validate_storage_key


CALENDAR_HTML_PARSER_VERSION = "calendar-html-table-v1"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
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


class CalendarAdapterError(ValueError):
    pass


class Exchange(StrEnum):
    SSE = "SSE"
    SZSE = "SZSE"


class CalendarSourceFamily(StrEnum):
    SSE_CLOSED_LIST_ARCHIVE = "SSE_CLOSED_LIST_ARCHIVE"
    SSE_OFFICIAL_NOTICE_DETAIL = "SSE_OFFICIAL_NOTICE_DETAIL"
    SSE_OFFICIAL_NOTICE_ATTACHMENT = "SSE_OFFICIAL_NOTICE_ATTACHMENT"
    SZSE_OFFICIAL_NOTICE_GENERAL = "SZSE_OFFICIAL_NOTICE_GENERAL"
    SZSE_OFFICIAL_NOTICE_DETAIL = "SZSE_OFFICIAL_NOTICE_DETAIL"
    SZSE_OFFICIAL_NOTICE_ATTACHMENT = "SZSE_OFFICIAL_NOTICE_ATTACHMENT"


class RawCalendarFormat(StrEnum):
    HTML = "HTML"
    PDF = "PDF"
    DOCX = "DOCX"
    XLS = "XLS"
    XLSX = "XLSX"


class NoticeType(StrEnum):
    ANNUAL = "ANNUAL"
    HOLIDAY = "HOLIDAY"
    TEMPORARY = "TEMPORARY"
    TECHNICAL = "TECHNICAL"
    REVISION = "REVISION"


class PublishedGranularity(StrEnum):
    DATE = "DATE"
    SECOND = "SECOND"
    UNKNOWN = "UNKNOWN"


class CalendarCoverageMode(StrEnum):
    EXPLICIT_DAILY = "EXPLICIT_DAILY"
    ANNUAL_EXCEPTIONS = "ANNUAL_EXCEPTIONS"


_FAMILY_OWNER = {
    CalendarSourceFamily.SSE_CLOSED_LIST_ARCHIVE: Exchange.SSE,
    CalendarSourceFamily.SSE_OFFICIAL_NOTICE_DETAIL: Exchange.SSE,
    CalendarSourceFamily.SSE_OFFICIAL_NOTICE_ATTACHMENT: Exchange.SSE,
    CalendarSourceFamily.SZSE_OFFICIAL_NOTICE_GENERAL: Exchange.SZSE,
    CalendarSourceFamily.SZSE_OFFICIAL_NOTICE_DETAIL: Exchange.SZSE,
    CalendarSourceFamily.SZSE_OFFICIAL_NOTICE_ATTACHMENT: Exchange.SZSE,
}
_FORMAT_SUFFIX = {
    RawCalendarFormat.HTML: "html",
    RawCalendarFormat.PDF: "pdf",
    RawCalendarFormat.DOCX: "docx",
    RawCalendarFormat.XLS: "xls",
    RawCalendarFormat.XLSX: "xlsx",
}
_NOTICE_PRIORITY = {
    NoticeType.ANNUAL: 10,
    NoticeType.HOLIDAY: 20,
    NoticeType.TECHNICAL: 30,
    NoticeType.TEMPORARY: 40,
    NoticeType.REVISION: 50,
}


def _require_text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise CalendarAdapterError(f"{name} must be a non-empty trimmed string")
    return value


def _require_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CalendarAdapterError(f"{name} must be an integer")
    return value


def _require_sha256(value: object, name: str) -> str:
    text = _require_text(value, name)
    if not _SHA256.fullmatch(text):
        raise CalendarAdapterError(f"{name} must be SHA-256")
    return text


def _iso_utc(value: datetime) -> str:
    return to_utc(value).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: object, name: str) -> datetime:
    text = _require_text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CalendarAdapterError(f"{name} must be ISO-8601 datetime") from exc
    ensure_aware(parsed, name)
    return parsed


def _validate_source_url(
    owner: Exchange,
    family: CalendarSourceFamily,
    value: str,
) -> str:
    url = _require_text(value, "request_url")
    if _FAMILY_OWNER[family] is not owner:
        raise CalendarAdapterError("source owner and source family disagree")
    parsed = urlsplit(url)
    expected_domain = "sse.com.cn" if owner is Exchange.SSE else "szse.cn"
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (
        hostname == expected_domain or hostname.endswith("." + expected_domain)
    ):
        raise CalendarAdapterError("source URL must be official HTTPS SSE/SZSE")
    path = parsed.path
    if family is CalendarSourceFamily.SSE_CLOSED_LIST_ARCHIVE and not path.startswith(
        "/disclosure/dealinstruc/closed/list/"
    ):
        raise CalendarAdapterError("SSE archive URL is outside frozen source family")
    if family is CalendarSourceFamily.SSE_OFFICIAL_NOTICE_DETAIL and not path.startswith(
        "/disclosure/announcement/general/"
    ):
        raise CalendarAdapterError("SSE detail URL is outside frozen source family")
    if family is CalendarSourceFamily.SZSE_OFFICIAL_NOTICE_GENERAL and not path.startswith(
        "/disclosure/notice/general/"
    ):
        raise CalendarAdapterError("SZSE general URL is outside frozen source family")
    if family is CalendarSourceFamily.SZSE_OFFICIAL_NOTICE_DETAIL and not path.startswith(
        "/disclosure/notice/"
    ):
        raise CalendarAdapterError("SZSE detail URL is outside frozen source family")
    return url


def _validate_official_redirect_url(owner: Exchange, value: str, name: str) -> str:
    url = _require_text(value, name)
    parsed = urlsplit(url)
    expected_domain = "sse.com.cn" if owner is Exchange.SSE else "szse.cn"
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (
        hostname == expected_domain or hostname.endswith("." + expected_domain)
    ):
        raise CalendarAdapterError(
            f"{name} must remain on the official HTTPS {owner.value} domain"
        )
    return url


def digest_request_payload(value: bytes | Mapping[str, object] | None) -> str:
    """Hash request parameters or body without storing their raw values."""

    if value is None:
        payload = b""
    elif isinstance(value, bytes):
        payload = value
    elif isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise CalendarAdapterError("request parameter keys must be strings")
        try:
            payload = canonical_json(value).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise CalendarAdapterError("request parameters are not canonical") from exc
    else:
        raise CalendarAdapterError("request payload must be bytes, mapping or None")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class RedirectHop:
    status: int
    from_url: str
    to_url: str

    def __post_init__(self) -> None:
        status = _require_int(self.status, "redirect status")
        if not 300 <= status <= 399:
            raise CalendarAdapterError("redirect status must be 3xx")
        _require_text(self.from_url, "redirect from_url")
        _require_text(self.to_url, "redirect to_url")

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "from_url": self.from_url,
            "to_url": self.to_url,
        }


def _normalize_headers(value: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise CalendarAdapterError("response_headers must be a mapping")
    selected: dict[str, str] = {}
    for raw_name, raw_value in value.items():
        if type(raw_name) is not str or type(raw_value) is not str:
            raise CalendarAdapterError("response header names and values must be strings")
        name = raw_name.strip().lower()
        if name in _SELECTED_HEADERS:
            if not raw_value.strip():
                raise CalendarAdapterError(f"response header {name} cannot be empty")
            selected[name] = raw_value.strip()
    if "content-type" not in selected:
        raise CalendarAdapterError("response Content-Type is required")
    return dict(sorted(selected.items()))


@dataclass(frozen=True, slots=True)
class CalendarRawCapture:
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
    source_owner: Exchange
    source_family: CalendarSourceFamily
    source_version: str
    parser_version: str
    raw_format: RawCalendarFormat

    def __post_init__(self) -> None:
        _require_sha256(self.artifact_id, "artifact_id")
        _require_sha256(self.descriptor_id, "descriptor_id")
        _require_sha256(self.raw_sha256, "raw_sha256")
        if self.artifact_id != self.raw_sha256:
            raise CalendarAdapterError("artifact_id must equal raw content SHA-256")
        validate_storage_key(self.storage_key)
        validate_storage_key(self.descriptor_key)
        _validate_source_url(self.source_owner, self.source_family, self.request_url)
        if self.request_method not in {"GET", "POST"}:
            raise CalendarAdapterError("request_method must be GET or POST")
        _require_sha256(self.request_payload_digest, "request_payload_digest")
        status = _require_int(self.response_status, "response_status")
        if not 100 <= status <= 599:
            raise CalendarAdapterError("response_status must be a valid HTTP status")
        normalized_headers = _normalize_headers(self.response_headers)
        if normalized_headers != self.response_headers:
            raise CalendarAdapterError("response_headers must be selected and normalized")
        expected_content_type = normalized_headers["content-type"].split(";", 1)[0].lower()
        if self.content_type != expected_content_type:
            raise CalendarAdapterError("content_type disagrees with response Content-Type")
        ensure_aware(self.retrieved_at, "retrieved_at")
        if self.retrieved_at.utcoffset() != timedelta(0):
            raise CalendarAdapterError("retrieved_at must be represented in UTC")
        length = _require_int(self.byte_length, "byte_length")
        if length <= 0:
            raise CalendarAdapterError("byte_length must be positive")
        _require_text(self.source_version, "source_version")
        _require_text(self.parser_version, "parser_version")
        for index, hop in enumerate(self.redirect_chain):
            if not isinstance(hop, RedirectHop):
                raise CalendarAdapterError("redirect_chain must contain RedirectHop values")
            _validate_official_redirect_url(
                self.source_owner,
                hop.from_url,
                f"redirect_chain[{index}].from_url",
            )
            _validate_official_redirect_url(
                self.source_owner,
                hop.to_url,
                f"redirect_chain[{index}].to_url",
            )

    def _identity_payload(self) -> dict[str, object]:
        return {
            "schema": "a-share-calendar-raw-capture-v1",
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
            "parser_version": self.parser_version,
            "raw_format": self.raw_format.value,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            **self._identity_payload(),
            "descriptor_id": self.descriptor_id,
            "descriptor_key": self.descriptor_key,
        }


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise CalendarAdapterError(
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


def capture_calendar_raw(
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
    source_owner: Exchange,
    source_family: CalendarSourceFamily,
    source_version: str,
    parser_version: str,
    raw_format: RawCalendarFormat,
) -> CalendarRawCapture:
    """Persist exact response bytes before any parser is called."""

    if not isinstance(raw_bytes, bytes) or not raw_bytes:
        raise CalendarAdapterError("raw_bytes must be non-empty bytes")
    if not isinstance(source_owner, Exchange):
        raise CalendarAdapterError("source_owner must be Exchange")
    if not isinstance(source_family, CalendarSourceFamily):
        raise CalendarAdapterError("source_family must be CalendarSourceFamily")
    if not isinstance(raw_format, RawCalendarFormat):
        raise CalendarAdapterError("raw_format must be RawCalendarFormat")
    normalized_url = _validate_source_url(source_owner, source_family, request_url)
    normalized_method = _require_text(request_method, "request_method").upper()
    if normalized_method not in {"GET", "POST"}:
        raise CalendarAdapterError("request_method must be GET or POST")
    _require_sha256(request_payload_digest, "request_payload_digest")
    status = _require_int(response_status, "response_status")
    if not 100 <= status <= 599:
        raise CalendarAdapterError("response_status must be a valid HTTP status")
    normalized_headers = _normalize_headers(response_headers)
    retrieved_utc = to_utc(retrieved_at, "retrieved_at")
    redirects = tuple(redirect_chain)
    if any(not isinstance(hop, RedirectHop) for hop in redirects):
        raise CalendarAdapterError("redirect_chain must contain RedirectHop values")
    for index, hop in enumerate(redirects):
        _validate_official_redirect_url(
            source_owner,
            hop.from_url,
            f"redirect_chain[{index}].from_url",
        )
        _validate_official_redirect_url(
            source_owner,
            hop.to_url,
            f"redirect_chain[{index}].to_url",
        )
    normalized_source_version = _require_text(source_version, "source_version")
    normalized_parser_version = _require_text(parser_version, "parser_version")
    raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    suffix = _FORMAT_SUFFIX[raw_format]
    storage_key = validate_storage_key(
        f"raw/exchange-calendar/{source_owner.value.lower()}/{raw_sha256}.{suffix}"
    )
    root_path = Path(root)
    root_path.mkdir(parents=True, exist_ok=True)
    raw_path = safe_artifact_path(root_path, storage_key)
    _atomic_write(raw_path, raw_bytes)

    identity = {
        "schema": "a-share-calendar-raw-capture-v1",
        "artifact_id": raw_sha256,
        "storage_key": storage_key,
        "request_url": normalized_url,
        "request_method": normalized_method,
        "request_payload_digest": request_payload_digest,
        "response_status": status,
        "response_headers": normalized_headers,
        "content_type": normalized_headers["content-type"].split(";", 1)[0].lower(),
        "redirect_chain": [hop.as_dict() for hop in redirects],
        "retrieved_at": _iso_utc(retrieved_utc),
        "raw_sha256": raw_sha256,
        "byte_length": len(raw_bytes),
        "source_owner": source_owner.value,
        "source_family": source_family.value,
        "source_version": normalized_source_version,
        "parser_version": normalized_parser_version,
        "raw_format": raw_format.value,
    }
    descriptor_id = fingerprint(identity)
    descriptor_key = validate_storage_key(
        f"descriptors/exchange-calendar/{descriptor_id}.json"
    )
    payload = {
        **identity,
        "descriptor_id": descriptor_id,
        "descriptor_key": descriptor_key,
    }
    descriptor_bytes = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    _atomic_write(safe_artifact_path(root_path, descriptor_key), descriptor_bytes)
    return CalendarRawCapture(
        artifact_id=raw_sha256,
        descriptor_id=descriptor_id,
        storage_key=storage_key,
        descriptor_key=descriptor_key,
        request_url=normalized_url,
        request_method=normalized_method,
        request_payload_digest=request_payload_digest,
        response_status=status,
        response_headers=normalized_headers,
        content_type=identity["content_type"],
        redirect_chain=redirects,
        retrieved_at=retrieved_utc,
        raw_sha256=raw_sha256,
        byte_length=len(raw_bytes),
        source_owner=source_owner,
        source_family=source_family,
        source_version=normalized_source_version,
        parser_version=normalized_parser_version,
        raw_format=raw_format,
    )


_CAPTURE_FIELDS = {
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
    "parser_version",
    "raw_format",
}


def load_calendar_raw(
    root: str | Path,
    *,
    descriptor_key: str,
) -> tuple[CalendarRawCapture, bytes]:
    """Load one descriptor and fail on descriptor or raw-byte tampering."""

    root_path = Path(root)
    path = safe_artifact_path(root_path, descriptor_key)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CalendarAdapterError("capture descriptor is unreadable") from exc
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise CalendarAdapterError("capture descriptor must be a JSON object")
    unknown = sorted(set(value) - _CAPTURE_FIELDS)
    missing = sorted(_CAPTURE_FIELDS - set(value))
    if unknown:
        raise CalendarAdapterError(
            "capture descriptor contains unknown fields: " + ", ".join(unknown)
        )
    if missing:
        raise CalendarAdapterError(
            "capture descriptor is missing fields: " + ", ".join(missing)
        )
    if value.get("schema") != "a-share-calendar-raw-capture-v1":
        raise CalendarAdapterError("unsupported capture descriptor schema")
    if value.get("descriptor_key") != descriptor_key:
        raise CalendarAdapterError("descriptor_key does not match requested path")
    identity = dict(value)
    expected_descriptor_id = identity.pop("descriptor_id")
    identity.pop("descriptor_key")
    if fingerprint(identity) != expected_descriptor_id:
        raise CalendarAdapterError("descriptor_id does not match descriptor content")
    try:
        headers_value = value["response_headers"]
        if not isinstance(headers_value, dict):
            raise CalendarAdapterError("response_headers must be an object")
        redirects_value = value["redirect_chain"]
        if not isinstance(redirects_value, list):
            raise CalendarAdapterError("redirect_chain must be an array")
        redirects = tuple(
            RedirectHop(
                status=_require_int(item.get("status"), "redirect status"),
                from_url=_require_text(item.get("from_url"), "redirect from_url"),
                to_url=_require_text(item.get("to_url"), "redirect to_url"),
            )
            for item in redirects_value
            if isinstance(item, dict)
        )
        if len(redirects) != len(redirects_value):
            raise CalendarAdapterError("redirect entries must be objects")
        capture = CalendarRawCapture(
            artifact_id=_require_sha256(value["artifact_id"], "artifact_id"),
            descriptor_id=_require_sha256(
                expected_descriptor_id,
                "descriptor_id",
            ),
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
            redirect_chain=redirects,
            retrieved_at=_parse_datetime(value["retrieved_at"], "retrieved_at"),
            raw_sha256=_require_sha256(value["raw_sha256"], "raw_sha256"),
            byte_length=_require_int(value["byte_length"], "byte_length"),
            source_owner=Exchange(_require_text(value["source_owner"], "source_owner")),
            source_family=CalendarSourceFamily(
                _require_text(value["source_family"], "source_family")
            ),
            source_version=_require_text(value["source_version"], "source_version"),
            parser_version=_require_text(value["parser_version"], "parser_version"),
            raw_format=RawCalendarFormat(
                _require_text(value["raw_format"], "raw_format")
            ),
        )
    except (KeyError, ValueError) as exc:
        if isinstance(exc, CalendarAdapterError):
            raise
        raise CalendarAdapterError("capture descriptor contains invalid values") from exc
    raw_path = safe_artifact_path(root_path, capture.storage_key)
    if not raw_path.is_file():
        raise CalendarAdapterError("raw artifact disappeared")
    if raw_path.stat().st_size != capture.byte_length:
        raise CalendarAdapterError("raw artifact byte length changed")
    if hash_file(raw_path) != capture.raw_sha256:
        raise CalendarAdapterError("raw artifact hash changed")
    raw_bytes = raw_path.read_bytes()
    return capture, raw_bytes


@dataclass(frozen=True, slots=True)
class CalendarProvenance:
    exchange: Exchange
    source_owner: Exchange
    source_family: CalendarSourceFamily
    source_version: str
    notice_id: str
    notice_type: NoticeType
    source_uri: str
    source_published_at: date | datetime | None
    source_published_granularity: PublishedGranularity
    observed_at: datetime
    retrieved_at: datetime
    known_at: datetime
    usable_from: datetime
    effective_from: date
    effective_to: date
    revision_id: str
    supersedes_revision_id: str | None
    raw_artifact_id: str
    response_status: int
    content_type: str
    coverage_mode: CalendarCoverageMode

    def __post_init__(self) -> None:
        if self.exchange is not self.source_owner:
            raise CalendarAdapterError("exchange and source_owner must match")
        _validate_source_url(self.source_owner, self.source_family, self.source_uri)
        _require_text(self.source_version, "source_version")
        _require_text(self.notice_id, "notice_id")
        _require_text(self.revision_id, "revision_id")
        if self.supersedes_revision_id is not None:
            _require_text(self.supersedes_revision_id, "supersedes_revision_id")
            if self.supersedes_revision_id == self.revision_id:
                raise CalendarAdapterError("revision cannot supersede itself")
        _require_sha256(self.raw_artifact_id, "raw_artifact_id")
        if self.effective_to < self.effective_from:
            raise CalendarAdapterError("effective_to cannot precede effective_from")
        for name in ("observed_at", "retrieved_at", "known_at", "usable_from"):
            ensure_aware(getattr(self, name), name)
        if self.retrieved_at.utcoffset() != timedelta(0):
            raise CalendarAdapterError("retrieved_at must be represented in UTC")
        if to_utc(self.observed_at) != to_utc(self.retrieved_at):
            raise CalendarAdapterError(
                "Stage 2A Calendar provenance requires observed_at == retrieved_at; "
                "no independently bound earlier observation authority exists"
            )
        if to_utc(self.known_at) != to_utc(self.observed_at):
            raise CalendarAdapterError(
                "Stage 2A Calendar provenance requires known_at == observed_at; "
                "source publication metadata cannot backdate first knowledge"
            )
        if to_utc(self.usable_from) < to_utc(self.known_at):
            raise CalendarAdapterError("usable_from cannot precede known_at")
        if (
            self.coverage_mode is CalendarCoverageMode.ANNUAL_EXCEPTIONS
            and self.notice_type is not NoticeType.ANNUAL
        ):
            raise CalendarAdapterError(
                "ANNUAL_EXCEPTIONS mode is limited to ANNUAL notices"
            )
        if self.source_published_granularity is PublishedGranularity.DATE:
            if type(self.source_published_at) is not date:
                raise CalendarAdapterError(
                    "DATE publication requires a date without fabricated time"
                )
            if self.source_published_at > self.known_at.astimezone(_SHANGHAI).date():
                raise CalendarAdapterError("source publication cannot follow known_at")
        elif self.source_published_granularity is PublishedGranularity.SECOND:
            if not isinstance(self.source_published_at, datetime):
                raise CalendarAdapterError("SECOND publication requires datetime")
            ensure_aware(self.source_published_at, "source_published_at")
            if to_utc(self.source_published_at) > to_utc(self.known_at):
                raise CalendarAdapterError("source publication cannot follow known_at")
        elif self.source_published_at is not None:
            raise CalendarAdapterError("UNKNOWN publication granularity requires null value")
        status = _require_int(self.response_status, "response_status")
        if not 100 <= status <= 599:
            raise CalendarAdapterError("response_status must be valid HTTP status")
        _require_text(self.content_type, "content_type")


def _provenance_as_dict(value: CalendarProvenance) -> dict[str, object]:
    published: str | None
    if value.source_published_at is None:
        published = None
    elif isinstance(value.source_published_at, datetime):
        published = _iso_utc(value.source_published_at)
    else:
        published = value.source_published_at.isoformat()
    return {
        "exchange": value.exchange.value,
        "source_owner": value.source_owner.value,
        "source_family": value.source_family.value,
        "source_version": value.source_version,
        "notice_id": value.notice_id,
        "notice_type": value.notice_type.value,
        "source_uri": value.source_uri,
        "source_published_at": published,
        "source_published_granularity": value.source_published_granularity.value,
        "observed_at": _iso_utc(value.observed_at),
        "retrieved_at": _iso_utc(value.retrieved_at),
        "known_at": _iso_utc(value.known_at),
        "usable_from": _iso_utc(value.usable_from),
        "effective_from": value.effective_from.isoformat(),
        "effective_to": value.effective_to.isoformat(),
        "revision_id": value.revision_id,
        "supersedes_revision_id": value.supersedes_revision_id,
        "raw_artifact_id": value.raw_artifact_id,
        "response_status": value.response_status,
        "content_type": value.content_type,
        "coverage_mode": value.coverage_mode.value,
    }


_PROVENANCE_FIELDS = frozenset(
    {
        "exchange",
        "source_owner",
        "source_family",
        "source_version",
        "notice_id",
        "notice_type",
        "source_uri",
        "source_published_at",
        "source_published_granularity",
        "observed_at",
        "retrieved_at",
        "known_at",
        "usable_from",
        "effective_from",
        "effective_to",
        "revision_id",
        "supersedes_revision_id",
        "raw_artifact_id",
        "response_status",
        "content_type",
        "coverage_mode",
    }
)


def _provenance_from_dict(value: object) -> CalendarProvenance:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise CalendarAdapterError("calendar provenance must be a JSON object")
    unknown = sorted(set(value) - _PROVENANCE_FIELDS)
    missing = sorted(_PROVENANCE_FIELDS - set(value))
    if unknown:
        raise CalendarAdapterError(
            "calendar provenance contains unknown fields: " + ", ".join(unknown)
        )
    if missing:
        raise CalendarAdapterError(
            "calendar provenance is missing fields: " + ", ".join(missing)
        )
    try:
        granularity = PublishedGranularity(
            _require_text(value["source_published_granularity"], "source_published_granularity")
        )
        published_raw = value["source_published_at"]
        published: date | datetime | None
        if granularity is PublishedGranularity.UNKNOWN:
            if published_raw is not None:
                raise CalendarAdapterError(
                    "UNKNOWN publication granularity requires null value"
                )
            published = None
        elif granularity is PublishedGranularity.DATE:
            published = date.fromisoformat(
                _require_text(published_raw, "source_published_at")
            )
        else:
            published = _parse_datetime(published_raw, "source_published_at")
        supersedes_raw = value["supersedes_revision_id"]
        supersedes = (
            None
            if supersedes_raw is None
            else _require_text(supersedes_raw, "supersedes_revision_id")
        )
        return CalendarProvenance(
            exchange=Exchange(_require_text(value["exchange"], "exchange")),
            source_owner=Exchange(
                _require_text(value["source_owner"], "source_owner")
            ),
            source_family=CalendarSourceFamily(
                _require_text(value["source_family"], "source_family")
            ),
            source_version=_require_text(value["source_version"], "source_version"),
            notice_id=_require_text(value["notice_id"], "notice_id"),
            notice_type=NoticeType(_require_text(value["notice_type"], "notice_type")),
            source_uri=_require_text(value["source_uri"], "source_uri"),
            source_published_at=published,
            source_published_granularity=granularity,
            observed_at=_parse_datetime(value["observed_at"], "observed_at"),
            retrieved_at=_parse_datetime(value["retrieved_at"], "retrieved_at"),
            known_at=_parse_datetime(value["known_at"], "known_at"),
            usable_from=_parse_datetime(value["usable_from"], "usable_from"),
            effective_from=date.fromisoformat(
                _require_text(value["effective_from"], "effective_from")
            ),
            effective_to=date.fromisoformat(
                _require_text(value["effective_to"], "effective_to")
            ),
            revision_id=_require_text(value["revision_id"], "revision_id"),
            supersedes_revision_id=supersedes,
            raw_artifact_id=_require_sha256(
                value["raw_artifact_id"], "raw_artifact_id"
            ),
            response_status=_require_int(value["response_status"], "response_status"),
            content_type=_require_text(value["content_type"], "content_type"),
            coverage_mode=CalendarCoverageMode(
                _require_text(value["coverage_mode"], "coverage_mode")
            ),
        )
    except (KeyError, ValueError) as exc:
        if isinstance(exc, CalendarAdapterError):
            raise
        raise CalendarAdapterError("calendar provenance contains invalid values") from exc


@dataclass(frozen=True, slots=True)
class CalendarParseDescriptor:
    parse_descriptor_id: str
    parse_descriptor_key: str
    raw_descriptor_id: str
    raw_descriptor_key: str
    raw_artifact_id: str
    parser_version: str
    provenance: CalendarProvenance

    def __post_init__(self) -> None:
        _require_sha256(self.parse_descriptor_id, "parse_descriptor_id")
        _require_sha256(self.raw_descriptor_id, "raw_descriptor_id")
        _require_sha256(self.raw_artifact_id, "raw_artifact_id")
        validate_storage_key(self.parse_descriptor_key)
        validate_storage_key(self.raw_descriptor_key)
        _require_text(self.parser_version, "parser_version")
        if self.provenance.raw_artifact_id != self.raw_artifact_id:
            raise CalendarAdapterError(
                "parse provenance raw_artifact_id disagrees with descriptor"
            )
        if self.parse_descriptor_id != fingerprint(self._identity_payload()):
            raise CalendarAdapterError(
                "parse_descriptor_id does not match parse descriptor content"
            )

    def _identity_payload(self) -> dict[str, object]:
        return {
            "schema": "a-share-calendar-parse-descriptor-v1",
            "raw_descriptor_id": self.raw_descriptor_id,
            "raw_descriptor_key": self.raw_descriptor_key,
            "raw_artifact_id": self.raw_artifact_id,
            "parser_version": self.parser_version,
            "provenance": _provenance_as_dict(self.provenance),
        }

    def as_dict(self) -> dict[str, object]:
        return {
            **self._identity_payload(),
            "parse_descriptor_id": self.parse_descriptor_id,
            "parse_descriptor_key": self.parse_descriptor_key,
        }


def _validate_parse_binding(
    capture: CalendarRawCapture,
    provenance: CalendarProvenance,
    parser_version: str,
) -> str:
    normalized_parser_version = _require_text(parser_version, "parser_version")
    if capture.artifact_id != provenance.raw_artifact_id:
        raise CalendarAdapterError("parse provenance does not bind captured raw artifact")
    if capture.parser_version != normalized_parser_version:
        raise CalendarAdapterError("parser_version disagrees with raw descriptor")
    if capture.source_owner is not provenance.source_owner:
        raise CalendarAdapterError("parse provenance source owner mismatch")
    if capture.source_family is not provenance.source_family:
        raise CalendarAdapterError("parse provenance source family mismatch")
    if capture.source_version != provenance.source_version:
        raise CalendarAdapterError("parse provenance source version mismatch")
    if capture.request_url != provenance.source_uri:
        raise CalendarAdapterError("parse provenance source_uri mismatch")
    if capture.response_status != provenance.response_status:
        raise CalendarAdapterError("parse provenance response status mismatch")
    if capture.content_type != provenance.content_type.split(";", 1)[0].lower():
        raise CalendarAdapterError("parse provenance content type mismatch")
    if to_utc(capture.retrieved_at) != to_utc(provenance.retrieved_at):
        raise CalendarAdapterError("parse provenance retrieved_at mismatch")
    return normalized_parser_version


def write_calendar_parse_descriptor(
    root: str | Path,
    *,
    capture: CalendarRawCapture,
    provenance: CalendarProvenance,
    parser_version: str,
) -> CalendarParseDescriptor:
    normalized_parser_version = _validate_parse_binding(
        capture,
        provenance,
        parser_version,
    )
    identity = {
        "schema": "a-share-calendar-parse-descriptor-v1",
        "raw_descriptor_id": capture.descriptor_id,
        "raw_descriptor_key": capture.descriptor_key,
        "raw_artifact_id": capture.artifact_id,
        "parser_version": normalized_parser_version,
        "provenance": _provenance_as_dict(provenance),
    }
    parse_descriptor_id = fingerprint(identity)
    parse_descriptor_key = validate_storage_key(
        f"parse-descriptors/exchange-calendar/{parse_descriptor_id}.json"
    )
    descriptor = CalendarParseDescriptor(
        parse_descriptor_id=parse_descriptor_id,
        parse_descriptor_key=parse_descriptor_key,
        raw_descriptor_id=capture.descriptor_id,
        raw_descriptor_key=capture.descriptor_key,
        raw_artifact_id=capture.artifact_id,
        parser_version=normalized_parser_version,
        provenance=provenance,
    )
    payload = (
        json.dumps(descriptor.as_dict(), ensure_ascii=False, sort_keys=True, indent=2)
        + "\n"
    ).encode("utf-8")
    _atomic_write(safe_artifact_path(Path(root), parse_descriptor_key), payload)
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
        "provenance",
    }
)


def load_calendar_parse_descriptor(
    root: str | Path,
    *,
    parse_descriptor_key: str,
) -> tuple[CalendarParseDescriptor, CalendarRawCapture, bytes]:
    root_path = Path(root)
    path = safe_artifact_path(root_path, parse_descriptor_key)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CalendarAdapterError("parse descriptor is unreadable") from exc
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise CalendarAdapterError("parse descriptor must be a JSON object")
    unknown = sorted(set(value) - _PARSE_DESCRIPTOR_FIELDS)
    missing = sorted(_PARSE_DESCRIPTOR_FIELDS - set(value))
    if unknown:
        raise CalendarAdapterError(
            "parse descriptor contains unknown fields: " + ", ".join(unknown)
        )
    if missing:
        raise CalendarAdapterError(
            "parse descriptor is missing fields: " + ", ".join(missing)
        )
    if value.get("schema") != "a-share-calendar-parse-descriptor-v1":
        raise CalendarAdapterError("unsupported parse descriptor schema")
    if value.get("parse_descriptor_key") != parse_descriptor_key:
        raise CalendarAdapterError("parse_descriptor_key does not match requested path")
    try:
        descriptor = CalendarParseDescriptor(
            parse_descriptor_id=_require_sha256(
                value["parse_descriptor_id"], "parse_descriptor_id"
            ),
            parse_descriptor_key=parse_descriptor_key,
            raw_descriptor_id=_require_sha256(
                value["raw_descriptor_id"], "raw_descriptor_id"
            ),
            raw_descriptor_key=_require_text(
                value["raw_descriptor_key"], "raw_descriptor_key"
            ),
            raw_artifact_id=_require_sha256(
                value["raw_artifact_id"], "raw_artifact_id"
            ),
            parser_version=_require_text(value["parser_version"], "parser_version"),
            provenance=_provenance_from_dict(value["provenance"]),
        )
    except (KeyError, ValueError) as exc:
        if isinstance(exc, CalendarAdapterError):
            raise
        raise CalendarAdapterError("parse descriptor contains invalid values") from exc
    capture, raw_bytes = load_calendar_raw(
        root_path,
        descriptor_key=descriptor.raw_descriptor_key,
    )
    if capture.descriptor_id != descriptor.raw_descriptor_id:
        raise CalendarAdapterError("parse descriptor raw descriptor identity mismatch")
    if capture.artifact_id != descriptor.raw_artifact_id:
        raise CalendarAdapterError("parse descriptor raw artifact identity mismatch")
    if capture.parser_version != descriptor.parser_version:
        raise CalendarAdapterError("parse descriptor parser version mismatch")
    _validate_parse_binding(
        capture,
        descriptor.provenance,
        descriptor.parser_version,
    )
    return descriptor, capture, raw_bytes


def parse_calendar_from_descriptor(
    root: str | Path,
    *,
    parse_descriptor_key: str,
) -> CalendarCandidateDocument:
    descriptor, capture, raw_bytes = load_calendar_parse_descriptor(
        root,
        parse_descriptor_key=parse_descriptor_key,
    )
    if capture.raw_format is not RawCalendarFormat.HTML:
        raise CalendarAdapterError(
            f"parser is not implemented for {capture.raw_format.value}"
        )
    return parse_calendar_document(
        raw_bytes,
        provenance=descriptor.provenance,
        parser_version=descriptor.parser_version,
    )


@dataclass(frozen=True, slots=True)
class CandidateCalendarFact:
    exchange: Exchange
    civil_date: date
    status: CalendarStatus
    session_kind: SessionKind
    open_time: datetime | None
    close_time: datetime | None
    notice_id: str
    notice_type: NoticeType
    source_published_at: date | datetime | None
    source_published_granularity: PublishedGranularity
    observed_at: datetime
    retrieved_at: datetime
    known_at: datetime
    usable_from: datetime
    effective_from: date
    effective_to: date
    revision_id: str
    supersedes_revision_id: str | None
    source_uri: str
    raw_artifact_id: str
    parser_version: str
    source_owner: Exchange
    source_family: CalendarSourceFamily
    source_version: str

    @property
    def candidate_id(self) -> str:
        return fingerprint(self)

    def to_calendar_day(self) -> CalendarDay:
        return CalendarDay(
            market=Market.A,
            session_date=self.civil_date,
            status=self.status,
            open_time=self.open_time,
            close_time=self.close_time,
            session_kind=self.session_kind,
            known_at=self.known_at,
            usable_from=self.usable_from,
            source=f"{self.source_owner.value}/{self.source_family.value}",
            revision=self.revision_id,
            supersedes_revision=self.supersedes_revision_id,
            calendar_version=self.source_version,
            verified=False,
            source_note=(
                f"candidate only; {self.source_family.value}; raw={self.raw_artifact_id}"
            ),
        )


@dataclass(frozen=True, slots=True)
class CalendarCandidateDocument:
    provenance: CalendarProvenance
    parser_version: str
    facts: tuple[CandidateCalendarFact, ...]
    gaps: tuple[str, ...]

    @property
    def document_id(self) -> str:
        return fingerprint(
            {
                "schema": "calendar-candidate-document-v1",
                "provenance": self.provenance,
                "parser_version": self.parser_version,
                "fact_ids": [fact.candidate_id for fact in self.facts],
                "gaps": self.gaps,
            }
        )


@dataclass(frozen=True, slots=True)
class CalendarAdapterResult:
    base_document_id: str
    coverage: CalendarCoverage
    days: tuple[CalendarDay, ...]
    candidate_facts: tuple[CandidateCalendarFact, ...]
    gaps: tuple[str, ...]

    @property
    def result_id(self) -> str:
        return fingerprint(
            {
                "schema": "calendar-adapter-result-v1",
                "base_document_id": self.base_document_id,
                "coverage_fact_id": self.coverage.fact_id,
                "day_fact_ids": [day.fact_id for day in self.days],
                "candidate_fact_ids": [fact.candidate_id for fact in self.candidate_facts],
                "gaps": self.gaps,
            }
        )


class _CalendarTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.table_count = 0
        self.in_table = False
        self.in_row = False
        self.cell_tag: str | None = None
        self.cell_text: list[str] = []
        self.row: list[str] = []
        self.rows: list[list[str]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        if tag == "table" and attributes.get("data-calendar-facts") == "v1":
            self.table_count += 1
            self.in_table = True
        elif self.in_table and tag == "tr":
            self.in_row = True
            self.row = []
        elif self.in_table and self.in_row and tag in {"th", "td"}:
            self.cell_tag = tag
            self.cell_text = []

    def handle_data(self, data: str) -> None:
        if self.cell_tag is not None:
            self.cell_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.cell_tag == tag:
            self.row.append("".join(self.cell_text).strip())
            self.cell_tag = None
            self.cell_text = []
        elif self.in_table and tag == "tr":
            if self.row:
                self.rows.append(self.row)
            self.in_row = False
            self.row = []
        elif self.in_table and tag == "table":
            self.in_table = False


def _decode_html(raw_bytes: bytes, content_type: str) -> str:
    match = re.search(r"charset\s*=\s*['\"]?([A-Za-z0-9._-]+)", content_type, re.I)
    charset = (match.group(1) if match else "utf-8").lower()
    aliases = {"gb2312": "gb18030", "gbk": "gb18030", "utf8": "utf-8"}
    charset = aliases.get(charset, charset)
    if charset not in {"utf-8", "gb18030"}:
        raise CalendarAdapterError(f"unsupported HTML charset: {charset}")
    try:
        return raw_bytes.decode(charset, errors="strict")
    except UnicodeDecodeError as exc:
        raise CalendarAdapterError("calendar HTML cannot be decoded exactly") from exc


def _parse_clock(value: str, name: str) -> time:
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise CalendarAdapterError(f"{name} must be HH:MM[:SS]") from exc
    if parsed.tzinfo is not None:
        raise CalendarAdapterError(f"{name} must not embed a different timezone")
    return parsed


def _date_sequence(start: date, end: date) -> tuple[date, ...]:
    return tuple(start + timedelta(days=index) for index in range((end - start).days + 1))


def _fact_from_row(
    civil_date: date,
    status: CalendarStatus,
    session_kind: SessionKind,
    open_clock: time | None,
    close_clock: time | None,
    provenance: CalendarProvenance,
    parser_version: str,
) -> CandidateCalendarFact:
    if status is CalendarStatus.OPEN:
        if open_clock is None:
            open_clock = time(9, 30)
        if close_clock is None:
            close_clock = time(11, 30) if session_kind is SessionKind.HALF_DAY else time(15)
        open_at = datetime.combine(civil_date, open_clock, tzinfo=_SHANGHAI)
        close_at = datetime.combine(civil_date, close_clock, tzinfo=_SHANGHAI)
        if close_at <= open_at:
            raise CalendarAdapterError("session close must follow open")
    else:
        if open_clock is not None or close_clock is not None:
            raise CalendarAdapterError("CLOSED date cannot carry session times")
        open_at = None
        close_at = None
    return CandidateCalendarFact(
        exchange=provenance.exchange,
        civil_date=civil_date,
        status=status,
        session_kind=session_kind,
        open_time=open_at,
        close_time=close_at,
        notice_id=provenance.notice_id,
        notice_type=provenance.notice_type,
        source_published_at=provenance.source_published_at,
        source_published_granularity=provenance.source_published_granularity,
        observed_at=provenance.observed_at,
        retrieved_at=provenance.retrieved_at,
        known_at=provenance.known_at,
        usable_from=provenance.usable_from,
        effective_from=provenance.effective_from,
        effective_to=provenance.effective_to,
        revision_id=provenance.revision_id,
        supersedes_revision_id=provenance.supersedes_revision_id,
        source_uri=provenance.source_uri,
        raw_artifact_id=provenance.raw_artifact_id,
        parser_version=parser_version,
        source_owner=provenance.source_owner,
        source_family=provenance.source_family,
        source_version=provenance.source_version,
    )


def parse_calendar_document(
    raw_bytes: bytes,
    *,
    provenance: CalendarProvenance,
    parser_version: str,
) -> CalendarCandidateDocument:
    """Purely parse exact raw HTML plus explicit provenance into candidates."""

    if not isinstance(raw_bytes, bytes) or not raw_bytes:
        raise CalendarAdapterError("raw_bytes must be non-empty bytes")
    if parser_version != CALENDAR_HTML_PARSER_VERSION:
        raise CalendarAdapterError("unsupported calendar parser version")
    if hashlib.sha256(raw_bytes).hexdigest() != provenance.raw_artifact_id:
        raise CalendarAdapterError("raw bytes do not match provenance artifact ID")
    if not 200 <= provenance.response_status <= 299:
        raise CalendarAdapterError("HTTP error response cannot produce calendar facts")
    if not provenance.content_type.lower().startswith("text/html"):
        raise CalendarAdapterError("calendar-html-table-v1 requires exact HTML raw bytes")
    text = _decode_html(raw_bytes, provenance.content_type)
    parser = _CalendarTableParser()
    parser.feed(text)
    parser.close()
    if parser.table_count != 1 or not parser.rows:
        if re.search(r"(?:error|异常|错误|维护中|not found)", text, re.I):
            raise CalendarAdapterError("HTML error page cannot produce calendar facts")
        raise CalendarAdapterError("calendar facts table is missing")

    header = tuple(item.strip().lower() for item in parser.rows[0])
    allowed = {"date", "status", "session_kind", "open_time", "close_time"}
    unknown = sorted(set(header) - allowed)
    if unknown:
        raise CalendarAdapterError("calendar table has unknown fields: " + ", ".join(unknown))
    if len(set(header)) != len(header):
        raise CalendarAdapterError("calendar table header fields must be unique")
    missing = sorted({"date", "status"} - set(header))
    if missing:
        raise CalendarAdapterError("calendar table is missing fields: " + ", ".join(missing))

    entries: list[tuple[date, CalendarStatus, SessionKind, time | None, time | None]] = []
    previous: date | None = None
    seen: set[date] = set()
    for row_number, values in enumerate(parser.rows[1:], start=2):
        if len(values) != len(header):
            raise CalendarAdapterError(f"calendar table row {row_number} has wrong field count")
        row = dict(zip(header, values))
        try:
            civil_date = date.fromisoformat(row["date"])
            status = CalendarStatus(row["status"])
            session_kind = SessionKind(row.get("session_kind") or SessionKind.REGULAR.value)
        except ValueError as exc:
            raise CalendarAdapterError(f"calendar table row {row_number} is invalid") from exc
        if civil_date in seen:
            raise CalendarAdapterError(f"duplicate calendar date: {civil_date.isoformat()}")
        if previous is not None and civil_date < previous:
            raise CalendarAdapterError("calendar dates must be strictly chronological")
        if not provenance.effective_from <= civil_date <= provenance.effective_to:
            raise CalendarAdapterError("calendar date is outside effective range")
        seen.add(civil_date)
        previous = civil_date
        open_value = row.get("open_time", "")
        close_value = row.get("close_time", "")
        open_clock = _parse_clock(open_value, "open_time") if open_value else None
        close_clock = _parse_clock(close_value, "close_time") if close_value else None
        if session_kind is SessionKind.SPECIAL and (
            status is CalendarStatus.OPEN and (open_clock is None or close_clock is None)
        ):
            raise CalendarAdapterError("SPECIAL OPEN session requires explicit times")
        entries.append((civil_date, status, session_kind, open_clock, close_clock))

    expected_dates = _date_sequence(provenance.effective_from, provenance.effective_to)
    if provenance.coverage_mode is CalendarCoverageMode.EXPLICIT_DAILY:
        actual_dates = tuple(item[0] for item in entries)
        if actual_dates != expected_dates:
            missing_dates = sorted(set(expected_dates) - set(actual_dates))
            detail = ", ".join(item.isoformat() for item in missing_dates[:5])
            raise CalendarAdapterError("explicit calendar is missing civil dates: " + detail)
        expanded = entries
        gaps = (
            "LICENSE_PENDING",
            "SINGLE_SOURCE_NOT_RECONCILED",
        )
    else:
        overrides = {item[0]: item[1:] for item in entries}
        expanded = []
        for civil_date in expected_dates:
            default_status = (
                CalendarStatus.CLOSED
                if civil_date.weekday() >= 5
                else CalendarStatus.OPEN
            )
            status, kind, open_clock, close_clock = overrides.get(
                civil_date,
                (default_status, SessionKind.REGULAR, None, None),
            )
            expanded.append((civil_date, status, kind, open_clock, close_clock))
        gaps = (
            "LICENSE_PENDING",
            "SINGLE_SOURCE_NOT_RECONCILED",
            "TEMPORARY_AND_TECHNICAL_NOTICE_COVERAGE_UNPROVEN",
            "WEEKDAY_OPEN_BASELINE_INFERRED",
        )
    facts = tuple(
        _fact_from_row(
            civil_date,
            status,
            session_kind,
            open_clock,
            close_clock,
            provenance,
            parser_version,
        )
        for civil_date, status, session_kind, open_clock, close_clock in expanded
    )
    return CalendarCandidateDocument(
        provenance=provenance,
        parser_version=parser_version,
        facts=facts,
        gaps=gaps,
    )


def _fact_payload(fact: CandidateCalendarFact) -> tuple[object, ...]:
    return (
        fact.status,
        fact.session_kind,
        fact.open_time,
        fact.close_time,
    )


def assemble_calendar_candidates(
    documents: Iterable[CalendarCandidateDocument],
) -> CalendarAdapterResult:
    """Validate append-only notice precedence and expose unverified core facts."""

    items = tuple(documents)
    if not items:
        raise CalendarAdapterError("at least one calendar document is required")
    identities = {
        (
            item.provenance.exchange,
            item.provenance.source_owner,
            item.provenance.source_family,
            item.provenance.source_version,
        )
        for item in items
    }
    if len(identities) != 1:
        raise CalendarAdapterError(
            "cannot mix calendar source/version identities or source families"
        )
    bases = [item for item in items if item.provenance.notice_type is NoticeType.ANNUAL]
    if len(bases) != 1:
        raise CalendarAdapterError("exactly one ANNUAL base document is required")
    base = bases[0]
    start = base.provenance.effective_from
    end = base.provenance.effective_to
    expected_dates = set(_date_sequence(start, end))
    if {fact.civil_date for fact in base.facts} != expected_dates:
        raise CalendarAdapterError("ANNUAL base does not cover every civil date")

    by_date_revision: dict[tuple[date, str], CandidateCalendarFact] = {}
    priority_by_revision: dict[str, int] = {}
    for document in sorted(
        items,
        key=lambda item: (
            to_utc(item.provenance.known_at),
            _NOTICE_PRIORITY[item.provenance.notice_type],
            item.provenance.revision_id,
        ),
    ):
        provenance = document.provenance
        if not start <= provenance.effective_from <= provenance.effective_to <= end:
            raise CalendarAdapterError("notice effective range exceeds ANNUAL coverage")
        priority = _NOTICE_PRIORITY[provenance.notice_type]
        prior_priority = (
            priority_by_revision.get(provenance.supersedes_revision_id)
            if provenance.supersedes_revision_id is not None
            else None
        )
        if prior_priority is not None and priority < prior_priority:
            raise CalendarAdapterError("lower-priority notice cannot supersede later notice")
        for fact in document.facts:
            key = (fact.civil_date, fact.revision_id)
            prior_same_revision = by_date_revision.get(key)
            if prior_same_revision is not None:
                if prior_same_revision.candidate_id != fact.candidate_id:
                    raise CalendarAdapterError(
                        "same date/revision maps to conflicting calendar facts"
                    )
                continue
            existing_for_date = [
                value for (civil_date, _), value in by_date_revision.items()
                if civil_date == fact.civil_date
            ]
            if existing_for_date and any(
                _fact_payload(value) != _fact_payload(fact) for value in existing_for_date
            ):
                if provenance.supersedes_revision_id is None:
                    raise CalendarAdapterError(
                        "calendar override requires supersedes_revision_id"
                    )
                if not any(
                    value.revision_id == provenance.supersedes_revision_id
                    for value in existing_for_date
                ):
                    raise CalendarAdapterError(
                        "supersedes_revision_id does not identify prior date fact"
                    )
            by_date_revision[key] = fact
        priority_by_revision[provenance.revision_id] = priority

    candidate_facts = tuple(
        sorted(
            by_date_revision.values(),
            key=lambda fact: (
                fact.civil_date,
                to_utc(fact.known_at),
                fact.revision_id,
                fact.candidate_id,
            ),
        )
    )
    days = tuple(fact.to_calendar_day() for fact in candidate_facts)
    coverage = CalendarCoverage(
        market=Market.A,
        start_date=start,
        end_date=end,
        source=(
            f"{base.provenance.source_owner.value}/"
            f"{base.provenance.source_family.value}"
        ),
        calendar_version=base.provenance.source_version,
        known_at=base.provenance.known_at,
        usable_from=base.provenance.usable_from,
        revision=base.provenance.revision_id,
        verified=False,
        source_note=(
            "candidate coverage only; exact raw captured; trust requires reconciliation"
        ),
    )
    gaps = tuple(sorted({gap for item in items for gap in item.gaps}))
    return CalendarAdapterResult(
        base_document_id=base.document_id,
        coverage=coverage,
        days=days,
        candidate_facts=candidate_facts,
        gaps=gaps,
    )

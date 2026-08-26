"""Isolated localhost-only free-stockdb market-data sidecar.

This adapter deliberately exposes only bounded, read-only A-share raw bar
queries.  It is a runtime WARM/COLD accelerator candidate, not an authoritative
PIT source and not a replacement for the quant Calendar/Universe/Corporate
Action contracts.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

from ..core import types as T
from ..core.config import ProviderConfig
from .provider import MarketDataProvider

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_SHA256_CHARS = frozenset("0123456789abcdef")
_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
_MAX_DAILY_SPAN_DAYS = 3660
_MAX_MINUTE_SPAN_DAYS = 31
_REQUIRED_DAILY_FIELDS = frozenset(
    {"code", "date", "open", "high", "low", "close", "volume", "amount", "turnover"}
)
_REQUIRED_MINUTE_FIELDS = frozenset(
    {"code", "date", "open", "high", "low", "close", "volume", "amount"}
)


class FreeStockDbContractError(ValueError):
    """Raised when sidecar configuration or response data is unsafe."""


class _NoRedirectHandler(urllib_request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _require_text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise FreeStockDbContractError(f"{name} must be a non-empty trimmed string")
    return value


def _require_sha256(value: object, name: str) -> str:
    text = _require_text(value, name)
    if len(text) != 64 or any(character not in _SHA256_CHARS for character in text):
        raise FreeStockDbContractError(f"{name} must be lowercase SHA-256")
    return text


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalize_base_url(host: str) -> str:
    raw = _require_text(host, "free-stockdb host")
    candidate = raw if "://" in raw else f"http://{raw}"
    parsed = urlparse(candidate)
    if parsed.scheme != "http":
        raise FreeStockDbContractError("free-stockdb sidecar must use local HTTP")
    if parsed.username is not None or parsed.password is not None:
        raise FreeStockDbContractError("free-stockdb URL must not contain credentials")
    if parsed.path not in ("", "/") or parsed.params or parsed.query or parsed.fragment:
        raise FreeStockDbContractError("free-stockdb host must not contain path/query/fragment")
    if parsed.hostname is None:
        raise FreeStockDbContractError("free-stockdb host is missing")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError as exc:
        raise FreeStockDbContractError(
            "free-stockdb host must be a literal loopback IP, not DNS"
        ) from exc
    if not address.is_loopback:
        raise FreeStockDbContractError("free-stockdb host must be loopback-only")
    port = parsed.port or 7899
    if not 1 <= port <= 65535:
        raise FreeStockDbContractError("free-stockdb port is invalid")
    rendered_host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    return f"http://{rendered_host}:{port}"


def _strict_json_loads(raw: bytes) -> object:
    if not isinstance(raw, bytes) or not raw:
        raise FreeStockDbContractError("free-stockdb response must be non-empty bytes")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise FreeStockDbContractError("free-stockdb response must be strict UTF-8") from exc

    def reject_constant(value: str) -> object:
        raise FreeStockDbContractError(f"non-finite JSON constant is forbidden: {value}")

    def reject_duplicates(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise FreeStockDbContractError(f"duplicate JSON key is forbidden: {key}")
            result[key] = value
        return result

    try:
        return json.loads(
            text,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except json.JSONDecodeError as exc:
        raise FreeStockDbContractError("free-stockdb response is not valid JSON") from exc


def _number(value: object, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FreeStockDbContractError(f"{name} must be a JSON number")
    result = float(value)
    if not math.isfinite(result):
        raise FreeStockDbContractError(f"{name} must be finite")
    if positive and result <= 0:
        raise FreeStockDbContractError(f"{name} must be positive")
    return result


def _volume(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FreeStockDbContractError("volume must be a JSON integer")
    if value < 0:
        raise FreeStockDbContractError("volume cannot be negative")
    return value


def _date_digits(value: object, interval: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise FreeStockDbContractError("date must be integer or digit string")
    text = str(value)
    expected = 8 if interval == "1d" else 14
    if len(text) != expected or not text.isdigit():
        raise FreeStockDbContractError(
            f"date must contain exactly {expected} digits for {interval}"
        )
    return text


def _timestamp_from_digits(value: object, interval: str) -> datetime:
    digits = _date_digits(value, interval)
    try:
        year = int(digits[0:4])
        month = int(digits[4:6])
        day = int(digits[6:8])
        if interval == "1d":
            session = datetime(year, month, day, tzinfo=_SHANGHAI).date()
            return datetime.combine(session, time(15, 0), tzinfo=_SHANGHAI)
        hour = int(digits[8:10])
        minute = int(digits[10:12])
        second = int(digits[12:14])
        return datetime(year, month, day, hour, minute, second, tzinfo=_SHANGHAI)
    except ValueError as exc:
        raise FreeStockDbContractError("date is not a valid calendar timestamp") from exc


def _extract_rows(payload: object, interval: str) -> tuple[dict[str, object], ...]:
    required = _REQUIRED_DAILY_FIELDS if interval == "1d" else _REQUIRED_MINUTE_FIELDS
    if payload in (None, {}, []):
        return ()
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict) and required.issubset(payload):
        rows = [payload]
    elif isinstance(payload, dict) and payload and all(
        isinstance(value, dict) for value in payload.values()
    ):
        rows = list(payload.values())
    else:
        raise FreeStockDbContractError("unsupported free-stockdb JSON response shape")
    if any(not isinstance(row, dict) for row in rows):
        raise FreeStockDbContractError("free-stockdb bar collection must contain objects")
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class FreeStockDbReadEvidence:
    provider: str
    release_version: str
    binary_inventory_sha256: str
    data_snapshot_manifest_sha256: str
    sync_manifest_sha256: str
    response_sha256: str
    queried_at: datetime
    symbol: str
    interval: str
    adjustment_mode: str
    request_url: str
    evidence_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_text(self.provider, "provider")
        _require_text(self.release_version, "release_version")
        for name in (
            "binary_inventory_sha256",
            "data_snapshot_manifest_sha256",
            "sync_manifest_sha256",
            "response_sha256",
        ):
            _require_sha256(getattr(self, name), name)
        if not isinstance(self.queried_at, datetime):
            raise FreeStockDbContractError("queried_at must be datetime")
        if self.queried_at.tzinfo is None or self.queried_at.utcoffset() is None:
            raise FreeStockDbContractError("queried_at must be timezone-aware")
        _require_text(self.symbol, "symbol")
        if self.interval not in {"1d", "1m"}:
            raise FreeStockDbContractError("interval must be 1d or 1m")
        if self.adjustment_mode != "raw":
            raise FreeStockDbContractError("free-stockdb evidence must identify raw bars")
        parsed = urlparse(_require_text(self.request_url, "request_url"))
        if parsed.hostname is None or not ipaddress.ip_address(parsed.hostname).is_loopback:
            raise FreeStockDbContractError("request_url must remain loopback-only")
        payload = {
            "schema": "free-stockdb-read-evidence-v1",
            "provider": self.provider,
            "release_version": self.release_version,
            "binary_inventory_sha256": self.binary_inventory_sha256,
            "data_snapshot_manifest_sha256": self.data_snapshot_manifest_sha256,
            "sync_manifest_sha256": self.sync_manifest_sha256,
            "response_sha256": self.response_sha256,
            "queried_at": self.queried_at.astimezone(timezone.utc).isoformat(),
            "symbol": self.symbol,
            "interval": self.interval,
            "adjustment_mode": self.adjustment_mode,
            "request_url": self.request_url,
        }
        object.__setattr__(
            self,
            "evidence_id",
            hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest(),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "provider": self.provider,
            "release_version": self.release_version,
            "binary_inventory_sha256": self.binary_inventory_sha256,
            "data_snapshot_manifest_sha256": self.data_snapshot_manifest_sha256,
            "sync_manifest_sha256": self.sync_manifest_sha256,
            "response_sha256": self.response_sha256,
            "queried_at": self.queried_at.astimezone(timezone.utc).isoformat(),
            "symbol": self.symbol,
            "interval": self.interval,
            "adjustment_mode": self.adjustment_mode,
            "request_url": self.request_url,
            "trust_tier": "T1_BEST_EFFORT",
            "allow_live_decision": False,
            "allow_model_training": False,
            "allow_public_redistribution": False,
        }


class FreeStockDbProvider(MarketDataProvider):
    """Read-only A-share raw-bar adapter for a pinned local free-stockdb release."""

    def __init__(self, cfg: ProviderConfig) -> None:
        super().__init__(cfg)
        self._base_url = _normalize_base_url(cfg.host or "127.0.0.1:7899")
        self._last_read_evidence: FreeStockDbReadEvidence | None = None
        self._validate_policy()

    def _validate_policy(self) -> None:
        if self.cfg.enabled is not True:
            raise FreeStockDbContractError(
                "free-stockdb provider cannot be constructed while enabled=false"
            )
        if self.cfg.primary:
            raise FreeStockDbContractError("free-stockdb cannot be a HOT primary provider")
        if self.cfg.supports_snapshot:
            raise FreeStockDbContractError("free-stockdb snapshot routing is not enabled")
        if self.cfg.read_only is not True:
            raise FreeStockDbContractError("free-stockdb integration must be read_only=true")
        if self.cfg.trust_tier != "T1_BEST_EFFORT":
            raise FreeStockDbContractError("free-stockdb trust_tier must stay T1_BEST_EFFORT")
        for name in (
            "allow_live_decision",
            "allow_model_training",
            "allow_public_redistribution",
        ):
            if getattr(self.cfg, name) is not False:
                raise FreeStockDbContractError(f"free-stockdb {name} must be false")
        if type(self.cfg.bars_priority) is not int or not -1000 <= self.cfg.bars_priority <= 1000:
            raise FreeStockDbContractError(
                "free-stockdb bars_priority must be an integer within [-1000, 1000]"
            )
        if set(self.cfg.markets) != {"a"}:
            raise FreeStockDbContractError("free-stockdb PoC supports A-share market only")
        # The application never instantiates disabled providers. Requiring identities
        # here as well prevents direct construction from becoming an unpinned bypass.
        _require_text(self.cfg.release_version, "release_version")
        _require_sha256(self.cfg.binary_inventory_sha256, "binary_inventory_sha256")
        _require_sha256(self.cfg.data_snapshot_manifest_sha256, "data_snapshot_manifest_sha256")
        _require_sha256(self.cfg.sync_manifest_sha256, "sync_manifest_sha256")

    @property
    def last_read_evidence(self) -> FreeStockDbReadEvidence | None:
        return self._last_read_evidence

    def supports_bars(self) -> bool:
        return True

    def supports_quotes(self) -> bool:
        return False

    def supports_raw_bars(self) -> bool:
        return True

    def supports_adjustment(self, adjust: str) -> bool:
        return adjust.lower() in {"raw", "none"}

    @staticmethod
    def _symbol_code(symbol: str, market: T.Market) -> str:
        if market is not T.Market.A:
            raise FreeStockDbContractError("free-stockdb PoC supports A-share bars only")
        code, separator, suffix = symbol.partition(".")
        if separator != "." or suffix not in {"SH", "SZ"}:
            raise FreeStockDbContractError("symbol must use CODE.SH or CODE.SZ")
        if len(code) != 6 or not code.isdigit():
            raise FreeStockDbContractError("A-share code must contain six digits")
        return code

    @staticmethod
    def _aware(value: datetime | None, name: str) -> datetime:
        if not isinstance(value, datetime):
            raise FreeStockDbContractError(f"{name} is required and must be datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise FreeStockDbContractError(f"{name} must be timezone-aware")
        return value.astimezone(_SHANGHAI)

    def _query_url(
        self,
        symbol: str,
        market: T.Market,
        interval: str,
        start: datetime | None,
        end: datetime | None,
        adjust: str,
    ) -> tuple[str, datetime, datetime]:
        if interval not in {"1d", "1m"}:
            raise FreeStockDbContractError("free-stockdb PoC supports 1d and 1m only")
        if not self.supports_adjustment(adjust):
            raise FreeStockDbContractError(
                "free-stockdb PoC returns raw bars only; qfq/hfq are forbidden"
            )
        code = self._symbol_code(symbol, market)
        start_local = self._aware(start, "start")
        end_local = self._aware(end, "end")
        if end_local < start_local:
            raise FreeStockDbContractError("end cannot precede start")
        span_days = (end_local.date() - start_local.date()).days
        limit = _MAX_DAILY_SPAN_DAYS if interval == "1d" else _MAX_MINUTE_SPAN_DAYS
        if span_days > limit:
            raise FreeStockDbContractError(
                f"free-stockdb {interval} query exceeds bounded span of {limit} days"
            )
        if interval == "1d":
            table = "日k"
            first = start_local.strftime("%Y%m%d")
            last = end_local.strftime("%Y%m%d")
        else:
            table = "分钟k"
            first = start_local.strftime("%Y%m%d%H%M%S")
            last = end_local.strftime("%Y%m%d%H%M%S")
        date_expression = first if first == last else f"{first}<{last}"
        query = urlencode({"cmd": "get", "t": f"{table}:{code}:{date_expression}"})
        return f"{self._base_url}/?{query}", start_local, end_local

    def _request_local(self, url: str) -> bytes:
        self._rl.acquire()
        parsed = urlparse(url)
        if parsed.hostname is None or not ipaddress.ip_address(parsed.hostname).is_loopback:
            raise FreeStockDbContractError("free-stockdb request escaped loopback")
        # Never inherit HTTP(S)_PROXY for a contractually local sidecar.
        opener = urllib_request.build_opener(
            urllib_request.ProxyHandler({}),
            _NoRedirectHandler(),
        )
        req = urllib_request.Request(
            url,
            headers={"User-Agent": "stock-tracker/free-stockdb-sidecar-v1"},
            method="GET",
        )
        try:
            with opener.open(req, timeout=self.timeout) as response:
                if response.status != 200:
                    raise FreeStockDbContractError(
                        f"free-stockdb returned HTTP {response.status}"
                    )
                final_url = urlparse(response.geturl())
                if (
                    final_url.hostname is None
                    or not ipaddress.ip_address(final_url.hostname).is_loopback
                ):
                    raise FreeStockDbContractError("free-stockdb response redirected off loopback")
                content_type = response.headers.get("Content-Type", "").lower()
                if "html" in content_type:
                    raise FreeStockDbContractError("free-stockdb returned HTML, not JSON")
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib_error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise FreeStockDbContractError("free-stockdb redirects are forbidden") from exc
            raise
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise FreeStockDbContractError("free-stockdb response exceeds size limit")
        return raw

    def fetch_bars_raw(
        self,
        symbol: str,
        market: T.Market,
        interval: str = "1d",
        start: datetime | None = None,
        end: datetime | None = None,
        adjust: str = "raw",
    ) -> bytes:
        url, _, _ = self._query_url(symbol, market, interval, start, end, adjust)
        return self._request_local(url)

    def _parse_bars(
        self,
        raw: bytes,
        symbol: str,
        market: T.Market,
        interval: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[T.Bar]:
        code = self._symbol_code(symbol, market)
        rows = _extract_rows(_strict_json_loads(raw), interval)
        start_local = None if start is None else self._aware(start, "start")
        end_local = None if end is None else self._aware(end, "end")
        bars: list[T.Bar] = []
        for index, row in enumerate(rows):
            required = _REQUIRED_DAILY_FIELDS if interval == "1d" else _REQUIRED_MINUTE_FIELDS
            missing = sorted(required - set(row))
            if missing:
                raise FreeStockDbContractError(
                    f"bar row {index} is missing required fields: {', '.join(missing)}"
                )
            row_code = str(row["code"])
            if row_code != code:
                raise FreeStockDbContractError("bar code does not match requested symbol")
            timestamp = _timestamp_from_digits(row["date"], interval)
            if start_local is not None:
                if interval == "1d" and timestamp.date() < start_local.date():
                    raise FreeStockDbContractError("bar precedes requested start date")
                if interval == "1m" and timestamp < start_local:
                    raise FreeStockDbContractError("bar precedes requested start time")
            if end_local is not None:
                if interval == "1d" and timestamp.date() > end_local.date():
                    raise FreeStockDbContractError("bar follows requested end date")
                if interval == "1m" and timestamp > end_local:
                    raise FreeStockDbContractError("bar follows requested end time")
            open_price = _number(row["open"], "open", positive=True)
            high = _number(row["high"], "high", positive=True)
            low = _number(row["low"], "low", positive=True)
            close = _number(row["close"], "close", positive=True)
            if low > min(open_price, close, high) or high < max(open_price, close, low):
                raise FreeStockDbContractError("bar OHLC values are inconsistent")
            amount = _number(row["amount"], "amount")
            if amount < 0:
                raise FreeStockDbContractError("amount cannot be negative")
            turnover = 0.0
            if "turnover" in row:
                turnover = _number(row["turnover"], "turnover")
                if turnover < 0:
                    raise FreeStockDbContractError("turnover cannot be negative")
            bars.append(
                T.Bar(
                    symbol=symbol,
                    market=market,
                    timestamp=timestamp,
                    interval=interval,
                    open=open_price,
                    high=high,
                    low=low,
                    close=close,
                    volume=_volume(row["volume"]),
                    amount=amount,
                    turnover=turnover,
                    source=self.name,
                    adjustment_factor=1.0,
                    quality_status=T.DataStatus.UNKNOWN,
                )
            )
        order = tuple(bar.timestamp for bar in bars)
        if len(set(order)) != len(order):
            raise FreeStockDbContractError("duplicate free-stockdb bar timestamp")
        bars.sort(key=lambda bar: bar.timestamp)
        return bars

    def parse_bars(
        self,
        raw: bytes,
        symbol: str,
        market: T.Market,
        interval: str = "1d",
    ) -> list[T.Bar]:
        return self._parse_bars(raw, symbol, market, interval)

    def fetch_bars_with_evidence(
        self,
        symbol: str,
        market: T.Market,
        interval: str = "1d",
        start: datetime | None = None,
        end: datetime | None = None,
        adjust: str = "raw",
    ) -> tuple[list[T.Bar], FreeStockDbReadEvidence]:
        url, start_local, end_local = self._query_url(
            symbol,
            market,
            interval,
            start,
            end,
            adjust,
        )
        raw = self._request_local(url)
        bars = self._parse_bars(
            raw,
            symbol,
            market,
            interval,
            start=start_local,
            end=end_local,
        )
        evidence = FreeStockDbReadEvidence(
            provider=self.name,
            release_version=self.cfg.release_version,
            binary_inventory_sha256=self.cfg.binary_inventory_sha256,
            data_snapshot_manifest_sha256=self.cfg.data_snapshot_manifest_sha256,
            sync_manifest_sha256=self.cfg.sync_manifest_sha256,
            response_sha256=hashlib.sha256(raw).hexdigest(),
            queried_at=datetime.now(timezone.utc),
            symbol=symbol,
            interval=interval,
            adjustment_mode="raw",
            request_url=url,
        )
        self._last_read_evidence = evidence
        return bars, evidence

    def fetch_bars(
        self,
        symbol: str,
        market: T.Market,
        interval: str = "1d",
        start: datetime | None = None,
        end: datetime | None = None,
        adjust: str = "raw",
    ) -> list[T.Bar]:
        bars, _ = self.fetch_bars_with_evidence(
            symbol,
            market,
            interval,
            start,
            end,
            adjust,
        )
        return bars

    def _raw_quotes(self, symbols: list[str]) -> list[tuple[T.Market, Any]]:
        raise NotImplementedError("free-stockdb is not enabled for HOT quote routing")

    def normalize(self, payload: Any, market: T.Market) -> T.Quote:
        raise NotImplementedError("free-stockdb is not enabled for HOT quote routing")


__all__ = [
    "FreeStockDbContractError",
    "FreeStockDbProvider",
    "FreeStockDbReadEvidence",
]

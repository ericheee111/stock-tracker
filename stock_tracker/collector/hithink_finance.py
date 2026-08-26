"""Optional HiThink Finance A-share historical-bar adapter.

The upstream service is accessed through its official REST contract. The
credential is read only from the documented process environment variable. This
adapter deliberately ships as a BEST_EFFORT research-capture source: it is not
eligible for runtime quotes, live decisions, model training, or public
redistribution.
"""

from __future__ import annotations

import json
import math
import os
import ssl
from collections.abc import Callable, Mapping
from datetime import datetime, time, timezone
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

from ..core import types as T
from ..core.config import ProviderConfig
from .provider import MarketDataProvider

HITHINK_FINANCE_API_KEY_ENV = "_".join(  # noqa: FLY002
    ("HITHINK", "FINANCE", "API", "KEY")
)
_AUTH_HEADER = "-".join(("X", "api", "key"))  # noqa: FLY002
_BASE_URL = "https://fuyao.aicubes.cn"
_HISTORICAL_PATH = "/api/a-share/prices/historical"
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_ADJUSTMENT_TO_API = {
    "raw": "none",
    "none": "none",
    "qfq": "forward",
    "hfq": "backward",
}


class HithinkFinanceContractError(RuntimeError):
    """Raised when the provider or upstream response violates the frozen contract."""


class _NoRedirectHandler(urllib_request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None  # noqa: PLR1711, RET501


def _environment_credential() -> str | None:
    return os.environ.get(HITHINK_FINANCE_API_KEY_ENV)


def _strict_json_loads(raw: bytes) -> object:
    if not isinstance(raw, bytes) or not raw:
        raise HithinkFinanceContractError("HiThink Finance returned an empty response")

    def pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise HithinkFinanceContractError(
                    "HiThink Finance JSON contains duplicate object keys"
                )
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise HithinkFinanceContractError(
            f"HiThink Finance JSON contains non-finite number token: {value}"
        )

    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except UnicodeDecodeError as exc:
        raise HithinkFinanceContractError(
            "HiThink Finance response is not UTF-8"
        ) from exc
    except json.JSONDecodeError as exc:
        raise HithinkFinanceContractError(
            "HiThink Finance response is not valid JSON"
        ) from exc


def _number(value: object, field: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HithinkFinanceContractError(f"{field} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise HithinkFinanceContractError(f"{field} must be finite")
    if positive and number <= 0:
        raise HithinkFinanceContractError(f"{field} must be positive")
    return number


def _volume(value: object) -> int:
    number = _number(value, "volume")
    if number < 0:
        raise HithinkFinanceContractError("volume cannot be negative")
    return round(number)


def _request_id(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        return "UNAVAILABLE"
    if len(value) > 128 or any(ord(char) < 33 or ord(char) == 127 for char in value):
        return "UNAVAILABLE"
    return value


def _normalize_base_url(host: str) -> str:
    if type(host) is not str:
        raise HithinkFinanceContractError(
            "HiThink Finance host must be a string"
        )
    value = host.strip()
    if not value:
        return _BASE_URL
    if value == "fuyao.aicubes.cn":
        value = f"https://{value}"
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "fuyao.aicubes.cn"
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise HithinkFinanceContractError(
            "HiThink Finance host must remain the exact official HTTPS origin"
        )
    return _BASE_URL


def _market_datetime(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise HithinkFinanceContractError(f"{name} is required and must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise HithinkFinanceContractError(f"{name} must be timezone-aware")
    return value.astimezone(_SHANGHAI)


def _ten_year_limit(value: datetime) -> datetime:
    try:
        return value.replace(year=value.year + 10)
    except ValueError:
        # February 29 has no direct representation in most target years.
        return value.replace(year=value.year + 10, day=28)


def _milliseconds(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _bar_datetime(value: object) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise HithinkFinanceContractError("date_ms must be a positive integer")
    current = datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).astimezone(
        _SHANGHAI
    )
    return datetime.combine(current.date(), time.min)


class HithinkFinanceProvider(MarketDataProvider):
    """Read-only A-share daily-bar adapter for the official HiThink REST API."""

    HISTORICAL_ENDPOINT = _HISTORICAL_PATH
    PROVIDER_VERSION = "financial-api-rest-contract-observed-2026-08-26"
    HISTORICAL_SCHEMA_VERSION = "hithink-a-share-prices-historical-v1"
    HISTORICAL_ADAPTER_VERSION = "hithink-bars-v1"
    SOURCE_DATASET = "a-share-prices-historical"

    def __init__(
        self,
        cfg: ProviderConfig,
        *,
        credential_provider: Callable[[], str | None] = _environment_credential,
        opener: Any | None = None,
    ) -> None:
        super().__init__(cfg)
        self._base_url = _normalize_base_url(cfg.host)
        self._credential_provider = credential_provider
        self._opener = opener or urllib_request.build_opener(
            urllib_request.ProxyHandler({}),
            urllib_request.HTTPSHandler(context=ssl.create_default_context()),
            _NoRedirectHandler(),
        )
        self._validate_policy()
        self._credential()

    def _validate_policy(self) -> None:
        if self.cfg.enabled is not True:
            raise HithinkFinanceContractError(
                "HiThink Finance provider cannot be constructed while enabled=false"
            )
        if self.cfg.primary:
            raise HithinkFinanceContractError(
                "HiThink Finance cannot be a runtime primary provider"
            )
        if self.cfg.supports_snapshot:
            raise HithinkFinanceContractError(
                "HiThink Finance snapshot routing is not enabled in this integration"
            )
        if self.cfg.read_only is not True:
            raise HithinkFinanceContractError(
                "HiThink Finance integration must be read_only=true"
            )
        if self.cfg.trust_tier != "T1_BEST_EFFORT":
            raise HithinkFinanceContractError(
                "HiThink Finance trust_tier must stay T1_BEST_EFFORT"
            )
        for name in (
            "allow_live_decision",
            "allow_model_training",
            "allow_public_redistribution",
        ):
            if getattr(self.cfg, name) is not False:
                raise HithinkFinanceContractError(
                    f"HiThink Finance {name} must be false"
                )
        if set(self.cfg.markets) != {"a"}:
            raise HithinkFinanceContractError(
                "HiThink Finance integration currently supports A shares only"
            )

    def _credential(self) -> str:
        value = self._credential_provider()
        if type(value) is not str or not value or value != value.strip():
            raise HithinkFinanceContractError(
                f"set {HITHINK_FINANCE_API_KEY_ENV} before enabling HiThink Finance"
            )
        if len(value) > 512 or any(ord(char) < 33 or ord(char) == 127 for char in value):
            raise HithinkFinanceContractError(
                f"{HITHINK_FINANCE_API_KEY_ENV} contains invalid characters"
            )
        return value

    def supports_quotes(self) -> bool:
        return False

    def supports_bars(self) -> bool:
        """Stay outside runtime BAR routing; exact raw capture is explicit only."""

        return False

    def supports_raw_bars(self) -> bool:
        return True

    def supports_adjustment(self, adjust: str) -> bool:
        return isinstance(adjust, str) and adjust.lower() in _ADJUSTMENT_TO_API

    @staticmethod
    def _symbol(symbol: str, market: T.Market) -> str:
        if market is not T.Market.A:
            raise HithinkFinanceContractError(
                "HiThink Finance integration supports A-share bars only"
            )
        code, separator, suffix = symbol.partition(".")
        if separator != "." or suffix not in {"SH", "SZ"}:
            raise HithinkFinanceContractError(
                "symbol must use the canonical CODE.SH or CODE.SZ form"
            )
        if len(code) != 6 or not code.isdigit():
            raise HithinkFinanceContractError(
                "A-share symbol code must contain six digits"
            )
        return symbol

    def historical_request_parameters(
        self,
        symbol: str,
        market: T.Market,
        interval: str,
        start: datetime | None,
        end: datetime | None,
        adjust: str,
    ) -> dict[str, object]:
        thscode = self._symbol(symbol, market)
        if interval != "1d":
            raise HithinkFinanceContractError(
                "HiThink Finance currently supports interval='1d' only"
            )
        normalized_adjust = adjust.lower() if isinstance(adjust, str) else ""
        if normalized_adjust not in _ADJUSTMENT_TO_API:
            raise HithinkFinanceContractError(
                "adjust must be one of: raw, none, qfq, hfq"
            )
        if start is None or end is None:
            raise HithinkFinanceContractError(
                "start and end are required for HiThink Finance historical bars"
            )
        start_local = _market_datetime(start, "start")
        end_local = _market_datetime(end, "end")
        if end_local < start_local:
            raise HithinkFinanceContractError("end cannot precede start")
        if end_local > _ten_year_limit(start_local):
            raise HithinkFinanceContractError(
                "HiThink Finance historical range cannot exceed 10 years"
            )
        start_ms = _milliseconds(start_local)
        end_ms = _milliseconds(end_local)
        return {
            "thscode": thscode,
            "interval": interval,
            "start": start_ms,
            "end": end_ms,
            "adjust": _ADJUSTMENT_TO_API[normalized_adjust],
            "offset": 0,
        }

    def _historical_url(self, parameters: Mapping[str, object]) -> str:
        return f"{self._base_url}{_HISTORICAL_PATH}?{urlencode(parameters)}"

    def _request_official(self, url: str) -> bytes:
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "fuyao.aicubes.cn"
            or parsed.port not in (None, 443)
            or parsed.path != _HISTORICAL_PATH
        ):
            raise HithinkFinanceContractError(
                "HiThink Finance request escaped the frozen official endpoint"
            )
        self._rl.acquire()
        request = urllib_request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "stock-tracker/hithink-finance-v1",
                _AUTH_HEADER: self._credential(),
            },
            method="GET",
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise HithinkFinanceContractError(
                        f"HiThink Finance returned HTTP {response.status}"
                    )
                final = urlparse(response.geturl())
                if (
                    final.scheme != "https"
                    or final.hostname != "fuyao.aicubes.cn"
                    or final.port not in (None, 443)
                    or final.path != _HISTORICAL_PATH
                ):
                    raise HithinkFinanceContractError(
                        "HiThink Finance response redirected away from the official endpoint"
                    )
                content_type = response.headers.get("Content-Type", "").lower()
                if "html" in content_type:
                    raise HithinkFinanceContractError(
                        "HiThink Finance returned HTML instead of JSON"
                    )
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib_error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise HithinkFinanceContractError(
                    "HiThink Finance redirects are forbidden"
                ) from exc
            raise HithinkFinanceContractError(
                f"HiThink Finance transport returned HTTP {exc.code}"
            ) from exc
        except (urllib_error.URLError, TimeoutError, OSError) as exc:
            raise HithinkFinanceContractError(
                "HiThink Finance transport failed"
            ) from exc
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise HithinkFinanceContractError(
                "HiThink Finance response exceeds the size limit"
            )
        return raw

    def fetch_bars_raw(
        self,
        symbol: str,
        market: T.Market,
        interval: str = "1d",
        start: datetime | None = None,
        end: datetime | None = None,
        adjust: str = "qfq",
    ) -> bytes:
        parameters = self.historical_request_parameters(
            symbol,
            market,
            interval,
            start,
            end,
            adjust,
        )
        return self._request_official(self._historical_url(parameters))

    def parse_bars(
        self,
        raw: bytes,
        symbol: str,
        market: T.Market,
        interval: str = "1d",
    ) -> list[T.Bar]:
        self._symbol(symbol, market)
        if interval != "1d":
            raise HithinkFinanceContractError(
                "HiThink Finance parser supports interval='1d' only"
            )
        envelope = _strict_json_loads(raw)
        if not isinstance(envelope, dict):
            raise HithinkFinanceContractError(
                "HiThink Finance response envelope must be an object"
            )
        code = envelope.get("code")
        if isinstance(code, bool) or not isinstance(code, int):
            raise HithinkFinanceContractError(
                "HiThink Finance response code must be an integer"
            )
        request_id = _request_id(envelope.get("request_id"))
        if code != 0:
            raise HithinkFinanceContractError(
                f"HiThink Finance business error code={code}; request_id={request_id}"
            )
        data = envelope.get("data")
        if not isinstance(data, dict):
            raise HithinkFinanceContractError(
                "HiThink Finance success response data must be an object"
            )
        data_timestamp = data.get("timestamp")
        if (
            isinstance(data_timestamp, bool)
            or not isinstance(data_timestamp, int)
            or data_timestamp <= 0
        ):
            raise HithinkFinanceContractError(
                "HiThink Finance historical data.timestamp must be a positive integer"
            )
        rows = data.get("item")
        if not isinstance(rows, list) or not rows:
            raise HithinkFinanceContractError(
                "HiThink Finance historical data.item must be a non-empty array"
            )
        bars: list[T.Bar] = []
        seen: set[datetime] = set()
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise HithinkFinanceContractError(
                    f"HiThink Finance bar row {index} must be an object"
                )
            required = {
                "date_ms",
                "open_price",
                "high_price",
                "low_price",
                "close_price",
                "volume",
                "turnover",
            }
            missing = sorted(required - set(row))
            if missing:
                raise HithinkFinanceContractError(
                    f"HiThink Finance bar row {index} is missing: {', '.join(missing)}"
                )
            if (
                isinstance(row["date_ms"], bool)
                or not isinstance(row["date_ms"], int)
                or row["date_ms"] > data_timestamp
            ):
                raise HithinkFinanceContractError(
                    "HiThink Finance bar date exceeds or violates the response timestamp"
                )
            timestamp = _bar_datetime(row["date_ms"])
            if timestamp in seen:
                raise HithinkFinanceContractError(
                    "HiThink Finance response contains duplicate bar dates"
                )
            seen.add(timestamp)
            open_price = _number(row["open_price"], "open_price", positive=True)
            high = _number(row["high_price"], "high_price", positive=True)
            low = _number(row["low_price"], "low_price", positive=True)
            close = _number(row["close_price"], "close_price", positive=True)
            if low > min(open_price, close, high) or high < max(
                open_price, close, low
            ):
                raise HithinkFinanceContractError(
                    "HiThink Finance bar OHLC values are inconsistent"
                )
            amount = _number(row["turnover"], "turnover")
            if amount < 0:
                raise HithinkFinanceContractError("turnover cannot be negative")
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
                    turnover=0.0,
                    source=self.name,
                    adjustment_factor=1.0,
                    quality_status=T.DataStatus.UNKNOWN,
                )
            )
        bars.sort(key=lambda bar: bar.timestamp)
        return bars

    def parse_bars_strict(
        self,
        raw: bytes,
        symbol: str,
        market: T.Market,
        interval: str = "1d",
    ) -> list[T.Bar]:
        return self.parse_bars(raw, symbol, market, interval)

    def fetch_bars(
        self,
        symbol: str,
        market: T.Market,
        interval: str = "1d",
        start: datetime | None = None,
        end: datetime | None = None,
        adjust: str = "qfq",
    ) -> list[T.Bar]:
        raw = self.fetch_bars_raw(symbol, market, interval, start, end, adjust)
        return self.parse_bars(raw, symbol, market, interval)

    def _raw_quotes(self, symbols: list[str]) -> list[tuple[T.Market, Any]]:
        del symbols
        raise NotImplementedError(
            "HiThink Finance is not enabled for runtime quote routing"
        )

    def normalize(self, payload: Any, market: T.Market) -> T.Quote:
        del payload, market
        raise NotImplementedError(
            "HiThink Finance is not enabled for runtime quote routing"
        )


__all__ = [
    "HITHINK_FINANCE_API_KEY_ENV",
    "HithinkFinanceContractError",
    "HithinkFinanceProvider",
]

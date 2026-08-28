"""MarketDataProvider 抽象基类（§4.1）。

职责：
- 限频（token bucket，max_rps）；超时；请求计时（latency）。
- 提供 HTTP 拉取骨架；具体协议与 ``normalize`` 由子类实现。
- 失败直接抛异常，交给 ProviderRouter 做健康统计 / 熔断 / 退避（不在 provider 内重试风暴）。
"""

from __future__ import annotations

import ssl
import time
from abc import ABC, abstractmethod
from datetime import datetime
from threading import Lock
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlparse, urlunparse

from ..core import types as T
from ..core.config import ProviderConfig


class RateLimiter:
    """线程安全的 token bucket 限频器。"""

    def __init__(self, max_rps: float) -> None:
        self.rate = max(0.1, float(max_rps))
        self.capacity = max(1.0, self.rate)
        self.tokens = self.capacity
        self.last = time.time()
        self.lock = Lock()
        self.hits = 0

    def acquire(self) -> float:
        """获取一个令牌；若需等待则返回等待秒数（并计数 rate_limit_hits）。"""
        with self.lock:
            now = time.time()
            self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
            self.last = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return 0.0
            wait = (1.0 - self.tokens) / self.rate
            self.tokens = 0.0
            self.hits += 1
        time.sleep(wait)
        return wait


def _ssl_ctx() -> ssl.SSLContext:
    """禁用证书校验的旧 Runtime 上下文；研究 exact-raw 通道禁止使用。"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class _NoRedirectHandler(urllib_request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class MarketDataProvider(ABC):
    """行情源抽象基类。"""

    def __init__(self, cfg: ProviderConfig) -> None:
        self.cfg = cfg
        self.name = cfg.name
        self.markets: list[T.Market] = [T.Market(m.upper()) for m in cfg.markets]
        self.timeout = max(1.0, cfg.timeout_ms / 1000.0)
        self.host_override: str = (cfg.host or "").strip()
        self._rl = RateLimiter(cfg.max_rps)
        self._lock = Lock()

    def _with_host(self, url: str) -> str:
        """可选 host 覆盖（故障注入/自托管）。空则原样返回。"""
        if not self.host_override:
            return url
        p = urlparse(url)
        return urlunparse(p._replace(netloc=self.host_override))

    # ---- 公共能力 ----
    def applies_to(self, market: T.Market) -> bool:
        return market in self.markets

    @staticmethod
    def _validate_bar_identity(symbol: str, market: T.Market) -> None:
        """Require a canonical symbol suffix that agrees with the requested market."""

        if type(symbol) is not str or not symbol or symbol != symbol.strip():
            raise ValueError("bar symbol must be a non-empty canonical string")
        if not isinstance(market, T.Market):
            raise TypeError("bar market must be Market")
        code, separator, suffix = symbol.rpartition(".")
        if not separator or not code or any(ord(char) < 33 or ord(char) == 127 for char in code):
            raise ValueError("bar symbol must use canonical CODE.MARKET form")
        allowed_suffixes = {
            T.Market.A: {"SH", "SZ"},
            T.Market.HK: {"HK"},
            T.Market.US: {"US"},
        }
        if suffix.upper() not in allowed_suffixes[market]:
            raise ValueError("bar symbol suffix does not match requested market")

    def supports_snapshot(self) -> bool:
        return False

    def supports_quotes(self) -> bool:
        """Whether this provider may participate in runtime quote routing."""

        return True

    def supports_bars(self) -> bool:
        """是否支持历史 K 线采集（默认 False，子类覆盖）。"""
        return False

    def supports_raw_bars(self) -> bool:
        """Whether exact provider bytes can be captured before normalization."""
        return False

    def supports_adjustment(self, adjust: str) -> bool:
        """Whether this provider can honestly satisfy the requested bar adjustment."""
        return adjust in {"raw", "none", "qfq", "hfq"}

    def fetch_bars_raw(
        self,
        symbol: str,
        market: T.Market,
        interval: str = "1d",
        start: datetime | None = None,
        end: datetime | None = None,
        adjust: str = "qfq",
    ) -> bytes:
        """Fetch exact bar-response bytes for immutable research capture."""
        raise NotImplementedError(f"{self.name} 不支持原始 K 线响应留存")

    def parse_bars(
        self,
        raw: bytes,
        symbol: str,
        market: T.Market,
        interval: str = "1d",
    ) -> list[T.Bar]:
        """Deterministically parse exact provider bytes into normalized bars."""
        raise NotImplementedError(f"{self.name} 不支持确定性 K 线解析")

    def fetch_bars(
        self,
        symbol: str,
        market: T.Market,
        interval: str = "1d",
        start: datetime | None = None,
        end: datetime | None = None,
        adjust: str = "qfq",
    ) -> list[T.Bar]:
        """拉取历史 K 线并归一化为 ``list[Bar]``。

        与 ``fetch_quotes`` 同风格：**失败直接上抛**，交由 ProviderRouter 做
        健康统计 / 熔断 / 退避（不在 provider 内重试风暴）。默认实现不支持，
        子类（eastmoney）覆盖为真实 HTTP 实现。
        """
        raise NotImplementedError(f"{self.name} 不支持历史 K 线采集")

    def _request_research(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        max_response_bytes: int = 16 * 1024 * 1024,
    ) -> bytes:
        """Fetch exact research bytes with system CA, no proxy and no redirects."""

        if self.host_override:
            raise ValueError("research exact-raw requests forbid host override")
        if type(max_response_bytes) is not int or max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be a positive integer")
        if (
            type(url) is not str
            or not url
            or url != url.strip()
            or "\\" in url
            or any(ord(character) < 32 or ord(character) == 127 for character in url)
        ):
            raise ValueError("research exact-raw URL is not canonical")
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError("research exact-raw URL must be credential-free HTTPS")
        if parsed.port not in (None, 443):
            raise ValueError("research exact-raw URL must use the default HTTPS port")
        hdrs = {
            "Accept": "application/json,text/plain;q=0.9",
            "User-Agent": "stock-tracker/exact-raw-research-v1",
        }
        if headers is not None and not isinstance(headers, dict):
            raise ValueError("research exact-raw headers must be a dictionary")
        if headers:
            forbidden_headers = {
                "authorization",
                "cookie",
                "host",
                "proxy-authorization",
                "x-api-key",
                "api-key",
            }
            normalized_names: set[str] = set()
            for name, value in headers.items():
                if (
                    type(name) is not str
                    or not name
                    or name != name.strip()
                    or any(ord(character) <= 32 or ord(character) == 127 for character in name)
                ):
                    raise ValueError("research exact-raw header name is invalid")
                normalized_name = name.lower()
                if normalized_name in normalized_names:
                    raise ValueError("research exact-raw headers contain duplicate names")
                if normalized_name in forbidden_headers:
                    raise ValueError(
                        "research exact-raw public channel forbids authority or credential headers"
                    )
                if type(value) is not str or any(
                    ord(character) < 32 or ord(character) == 127 for character in value
                ):
                    raise ValueError("research exact-raw header value is invalid")
                normalized_names.add(normalized_name)
                hdrs[name] = value
        opener = urllib_request.build_opener(
            urllib_request.ProxyHandler({}),
            urllib_request.HTTPSHandler(context=ssl.create_default_context()),
            _NoRedirectHandler(),
        )
        request = urllib_request.Request(url, headers=hdrs, method="GET")
        try:
            response = opener.open(request, timeout=self.timeout)
        except urllib_error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise ValueError("research exact-raw redirects are forbidden") from exc
            raise
        with response:
            if response.status != 200:
                raise ValueError(
                    f"research exact-raw request returned HTTP {response.status}"
                )
            if response.geturl() != url:
                raise ValueError("research exact-raw response URL changed")
            content_type = response.headers.get("Content-Type", "").lower()
            if "html" in content_type:
                raise ValueError("research exact-raw endpoint returned HTML")
            if not content_type or not any(
                token in content_type for token in ("json", "text/plain")
            ):
                raise ValueError("research exact-raw content type is unsupported")
            content_length = response.headers.get("Content-Length")
            declared_length: int | None = None
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except ValueError as exc:
                    raise ValueError(
                        "research exact-raw Content-Length is invalid"
                    ) from exc
                if declared_length < 0 or declared_length > max_response_bytes:
                    raise ValueError(
                        "research exact-raw response exceeds the size limit"
                    )
            raw = response.read(max_response_bytes + 1)
        if len(raw) > max_response_bytes:
            raise ValueError("research exact-raw response exceeds the size limit")
        if declared_length is not None and len(raw) != declared_length:
            raise ValueError(
                "research exact-raw response length differs from Content-Length"
            )
        if not raw:
            raise ValueError("research exact-raw response is empty")
        prefix = raw[:512]
        if prefix.startswith(b"\xef\xbb\xbf"):
            prefix = prefix[3:]
        prefix = prefix.lstrip().lower()
        if prefix.startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
            raise ValueError("research exact-raw endpoint returned an HTML error page")
        return raw

    def _request(self, url: str, headers: dict | None = None) -> bytes:
        """发起 HTTP GET，返回原始字节；失败/超时直接抛异常。"""
        hdrs = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        if headers:
            hdrs.update(headers)
        req = urllib_request.Request(self._with_host(url), headers=hdrs)
        with urllib_request.urlopen(req, timeout=self.timeout, context=_ssl_ctx()) as resp:
            return resp.read()

    # ---- 批量拉取（HOT/WARM） ----
    def fetch_quotes(self, symbols: list[str]) -> list[T.Quote]:
        """拉取一批标的行情并归一化。异常上抛由 router 处理。"""
        wait = self._rl.acquire()
        if wait > 0:
            pass  # rate_limit_hits 已在限频器内计数
        t0 = time.perf_counter()
        raws = self._raw_quotes(symbols)  # list[(Market, payload)]
        latency_ms = (time.perf_counter() - t0) * 1000.0
        # Runtime Quote timestamps are still local-naive across existing providers;
        # keep this boundary consistent until the product contract is migrated as a unit.
        received_at = datetime.now()  # noqa: DTZ005
        out: list[T.Quote] = []
        for market, payload in raws:
            try:
                q = self.normalize(payload, market)
                q.source = self.name
                q.received_at = received_at
                q.computed_at = datetime.now()  # noqa: DTZ005
                q.latency = latency_ms
                q.observed_age_ms = max(0, int((q.received_at - q.timestamp).total_seconds() * 1000))
                out.append(q)
            except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                # 单条解析失败不影响其他标的
                continue
        return out

    def fetch_snapshot(self) -> list[T.Quote]:
        """全市场批量快照（COLD）。默认不支持，子类（eastmoney）覆盖。"""
        if not self.supports_snapshot():
            raise NotImplementedError(f"{self.name} 不支持快照")
        t0 = time.perf_counter()
        raws = self._raw_snapshot()
        latency_ms = (time.perf_counter() - t0) * 1000.0
        # See ``fetch_quotes``: preserve the existing local-naive runtime contract.
        received_at = datetime.now()  # noqa: DTZ005
        out: list[T.Quote] = []
        for market, payload in raws:
            try:
                q = self.normalize(payload, market)
                q.source = self.name
                q.received_at = received_at
                q.computed_at = datetime.now()  # noqa: DTZ005
                q.latency = latency_ms
                q.observed_age_ms = max(0, int((q.received_at - q.timestamp).total_seconds() * 1000))
                out.append(q)
            except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                continue
        return out

    # ---- 子类需实现 ----
    @abstractmethod
    def _raw_quotes(self, symbols: list[str]) -> list[tuple[T.Market, Any]]:
        """返回 [(market, provider原始响应), ...]。"""
        raise NotImplementedError

    @abstractmethod
    def normalize(self, payload: Any, market: T.Market) -> T.Quote:
        """将原始响应映射为统一 Quote。"""
        raise NotImplementedError

    # 快照子类可覆盖
    def _raw_snapshot(self) -> list[tuple[T.Market, Any]]:
        raise NotImplementedError

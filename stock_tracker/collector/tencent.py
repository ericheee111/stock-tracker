"""TencentProvider（qt.gtimg.cn，GBK 编码，§4.1）。

已实测字段布局（A/HK/US 三种）：
- A股：f[3]=最新 f[4]=昨收 f[5]=今开 f[6]=成交量(手) f[30]=日期时间(14位紧凑)
        f[33]=最高 f[34]=最低 f[35]="最新/量/额"(额单位元) f[37]=额(万元) f[38]=换手%
- 港股/美股：f[35] 非 "a/b/c" 形式；额取 f[37]（完整本币）；时间 f[30] 为 "YYYY/MM/DD HH:MM:SS"
  或 "YYYY-MM-DD HH:MM:SS"；成交量为股（无需 ×100）。
"""

# Runtime provider timestamps intentionally remain market-local naive datetimes
# until the existing Quote/Bar contract is migrated as one unit.
# ruff: noqa: DTZ005, DTZ007

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from urllib.parse import quote

from ..core import types as T
from .provider import MarketDataProvider


def _strict_json_loads(raw: bytes) -> object:
    """Decode Tencent research payloads without duplicate/non-finite JSON values."""

    if not isinstance(raw, bytes) or not raw:
        raise ValueError("Tencent K-line response must be non-empty bytes")

    def pairs_hook(pairs):
        output = {}
        for key, value in pairs:
            if key in output:
                raise ValueError("Tencent K-line response contains duplicate JSON keys")
            output[key] = value
        return output

    def reject_constant(value: str):
        raise ValueError(f"Tencent K-line response contains non-finite token: {value}")

    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except UnicodeDecodeError as exc:
        raise ValueError("Tencent K-line response must use UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("Tencent K-line response is invalid JSON") from exc


def _market_of_provider_symbol(ps: str) -> T.Market:
    if ps.startswith("hk"):
        return T.Market.HK
    if ps.startswith("us"):
        return T.Market.US
    if ps.startswith(("sh", "sz")):
        return T.Market.A
    return T.Market.A


def _parse_dt(s: str) -> datetime:
    s = (s or "").strip()
    if not s:
        return datetime.now()
    if re.fullmatch(r"\d{14}", s):
        try:
            return datetime.strptime(s, "%Y%m%d%H%M%S")
        except ValueError:
            return datetime.now()
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return datetime.now()


def _f(parts: list[str], idx: int, default: float = 0.0) -> float | None:
    """取第 idx 段并转 float。

    - 索引越界 → 返回 default（保持旧行为：缺字段回落 0.0，测试 test_malformed_body 依赖）。
    - 字段存在但不可解析（如源返回 ``--`` / 空字符串）→ 返回 ``None``（价格缺失），
      而非 ``0.0``。``0.0`` 会被数据质量闸门误判为「非法价格」，且前端会把缺失渲染成
      ``0.00``；``None`` 才是正确的「无数据」语义。
    """
    if idx < 0 or idx >= len(parts):
        return default
    raw = (parts[idx] or "").strip()
    if raw == "":
        return default
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None


def _parse_body(body: str, market: T.Market) -> T.Quote:
    parts = body.split("~")
    last = _f(parts, 3)
    prev = _f(parts, 4)
    open_ = _f(parts, 5)
    high = _f(parts, 33)
    low = _f(parts, 34)

    # 成交量（股）；量/额/换手为非价格字段，缺失时回落 0.0，避免下游算术崩溃
    vol = _f(parts, 6)
    vol = vol if vol is not None else 0.0
    if market == T.Market.A:
        vol *= 100.0  # 手 → 股

    # 成交额（本币）
    amt_raw = parts[35] if len(parts) > 35 else ""
    if "/" in amt_raw:
        segs = amt_raw.split("/")
        amount = _f(segs, 2) if len(segs) >= 3 else 0.0
        amount = amount if amount is not None else 0.0
        if amount == 0.0 and len(parts) > 37:
            a37 = _f(parts, 37)
            amount = (a37 * (10000.0 if market == T.Market.A else 1.0)) if a37 is not None else 0.0
    else:
        a37 = _f(parts, 37)
        amount = a37 if a37 is not None else 0.0
        if market == T.Market.A:
            amount *= 10000.0  # 万元 → 元

    # 换手率（%）
    turnover = _f(parts, 38)
    turnover = turnover if turnover is not None else 0.0

    ts = _parse_dt(parts[30] if len(parts) > 30 else "")

    return T.Quote(
        symbol="",  # 由调用方填充
        market=market,
        name=parts[1] if len(parts) > 1 else "",
        timestamp=ts,
        # 价格字段允许为 None（源返回 "--"/空 → 缺失），非价格字段已回落数值
        open=open_, high=high, low=low, close=last, last=last, prev_close=prev,
        volume=int(vol), amount=amount, turnover=turnover,
    )


class TencentProvider(MarketDataProvider):
    """腾讯行情源（A/HK/US 通用，GBK）。"""

    BASE = "https://qt.gtimg.cn/q="

    def _raw_quotes(self, symbols: list[str]) -> list[tuple[T.Market, tuple[str, str]]]:
        prov_syms = [T.to_provider_symbol(s, "tencent") for s in symbols]
        mapping = {ps: (s, T.market_from_symbol(s)) for s, ps in zip(symbols, prov_syms)}
        url = self.BASE + ",".join(prov_syms)
        raw = self._request(url).decode("gbk", "ignore")
        out: list[tuple[T.Market, tuple[str, str]]] = []
        for line in raw.split(";"):
            line = line.strip()
            if not line or "=" not in line:
                continue
            key = line.split("=")[0].strip()
            ps = key.removeprefix("v_")
            if ps not in mapping:
                continue
            start = line.find('"')
            end = line.rfind('"')
            if start == -1 or end <= start:
                continue
            body = line[start + 1:end]
            if not body:
                continue
            out.append((mapping[ps][1], (ps, body)))
        return out

    def normalize(self, payload: tuple[str, str], market: T.Market) -> T.Quote:
        ps, body = payload
        q = _parse_body(body, market)
        symbol = self._resolve_symbol(ps)
        q.symbol = symbol
        return q

    @staticmethod
    def _resolve_symbol(ps: str) -> str:
        # 港股/美股指数腾讯使用 r_ 前缀（如 r_hkHSI / r_usIXIC），先剥离
        ps = ps.removeprefix("r_")
        code = ps[2:]
        if ps.startswith("hk"):
            return f"{code}.HK"
        if ps.startswith("us"):
            return f"{code.upper()}.US"
        # A 股：前缀 sh/sz 推导
        if ps.startswith("sh"):
            return f"{code}.SH"
        if ps.startswith("sz"):
            return f"{code}.SZ"
        return code

    # ---- 历史 K 线（腾讯 web.ifzq.gtimg.cn 兜底源，本环境实测可达） ----
    KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    KLINE_SCHEMA_VERSION = "tencent-fqkline-qfqday-v1"
    KLINE_ADAPTER_VERSION = "tencent-bars-v2-raw-split"

    def supports_bars(self) -> bool:
        """保持 Runtime 兜底源默认 OFF；Router 仅按 ``bars_fallback`` 显式使用。"""

        return False

    def supports_raw_bars(self) -> bool:
        """Expose exact response bytes before normalization for research capture."""

        return True

    def supports_adjustment(self, adjust: str) -> bool:
        """Tencent's strict parser supports the qfqday response contract."""

        return adjust == "qfq"

    def supports_market_adjustment(self, market: T.Market, adjust: str) -> bool:
        """The live endpoint has only demonstrated qfqday for A shares."""

        return self.applies_to(market) and market is T.Market.A and adjust == "qfq"

    def _bars_url(
        self,
        symbol: str,
        market: T.Market,
        interval: str = "1d",
        start: datetime | None = None,
        end: datetime | None = None,
        adjust: str = "qfq",
    ) -> str:
        if interval != "1d":
            raise ValueError("Tencent fallback currently supports interval='1d' only")
        if adjust != "qfq":
            raise ValueError("Tencent fallback currently supports adjust='qfq' only")
        prov_sym = self._kline_symbol(symbol, market)
        start_s = start.strftime("%Y-%m-%d") if start else ""
        end_s = end.strftime("%Y-%m-%d") if end else ""
        count = 320
        param = f"{prov_sym},day,{start_s},{end_s},{count},qfq"
        return f"{self.KLINE}?param={quote(param)}"

    def fetch_bars_raw(
        self,
        symbol: str,
        market: T.Market,
        interval: str = "1d",
        start: datetime | None = None,
        end: datetime | None = None,
        adjust: str = "qfq",
    ) -> bytes:
        """Fetch exact Tencent K-line bytes; parsing is deliberately separate."""

        if not self.applies_to(market):
            raise ValueError(f"{self.name} is not configured for market {market.value}")
        self._validate_bar_identity(symbol, market)
        self._rl.acquire()
        return self._request_research(
            self._bars_url(symbol, market, interval, start, end, adjust),
            allow_mislabeled_json=True,
        )

    def _parse_bars(
        self,
        raw: bytes,
        symbol: str,
        market: T.Market,
        interval: str,
        *,
        strict: bool,
    ) -> list[T.Bar]:
        if type(strict) is not bool:
            raise TypeError("strict must be a boolean")
        self._validate_bar_identity(symbol, market)
        if interval != "1d":
            raise ValueError("Tencent fallback currently supports interval='1d' only")
        if not isinstance(raw, bytes) or not raw:
            raise ValueError("Tencent K-line response must be non-empty bytes")
        if strict:
            payload = _strict_json_loads(raw)
        else:
            payload = json.loads(raw.decode("utf-8", "ignore"))
        if not isinstance(payload, dict):
            raise TypeError("Tencent K-line response must be a JSON object")
        if payload.get("code", 0) != 0:
            if strict:
                raise ValueError("Tencent K-line response returned a non-zero code")
            return []
        data = payload.get("data")
        if data in (None, {}):
            return []
        if not isinstance(data, dict):
            if strict:
                raise ValueError("Tencent K-line data must be a JSON object")
            return []
        prov_sym = self._kline_symbol(symbol, market)
        node = data.get(prov_sym)
        if node in (None, {}):
            return []
        if not isinstance(node, dict):
            if strict:
                raise ValueError("Tencent K-line symbol node must be a JSON object")
            return []
        rows = node.get("qfqday")
        if rows is None:
            if strict:
                raise ValueError("Tencent qfqday node is missing")
            return []
        if not isinstance(rows, list):
            if strict:
                raise ValueError("Tencent K-line rows must be a JSON array")
            return []

        def number(value: object, name: str, *, positive: bool = False) -> float:
            if isinstance(value, bool):
                raise TypeError(f"Tencent K-line {name} must be numeric")
            try:
                result = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Tencent K-line {name} must be numeric") from exc
            if not math.isfinite(result):
                raise ValueError(f"Tencent K-line {name} must be finite")
            if positive and result <= 0:
                raise ValueError(f"Tencent K-line {name} must be positive")
            return result

        scale = 100 if market == T.Market.A else 1
        bars: list[T.Bar] = []
        previous: datetime | None = None
        seen: set[datetime] = set()
        for index, row in enumerate(rows):
            if not isinstance(row, list) or len(row) < 6:
                if strict:
                    raise ValueError(f"Tencent K-line row {index} is malformed")
                continue
            try:
                timestamp = datetime.strptime(str(row[0]), "%Y-%m-%d")
                open_price = number(row[1], "open", positive=True)
                close = number(row[2], "close", positive=True)
                high = number(row[3], "high", positive=True)
                low = number(row[4], "low", positive=True)
                raw_volume = number(row[5], "volume")
                if raw_volume < 0:
                    raise ValueError("Tencent K-line volume cannot be negative")
                amount = 0.0
                if len(row) > 6 and row[6] not in ("", None):
                    amount = number(row[6], "amount")
                    if amount < 0:
                        raise ValueError("Tencent K-line amount cannot be negative")
                if low > min(open_price, close, high) or high < max(
                    open_price,
                    close,
                    low,
                ):
                    raise ValueError("Tencent K-line OHLC values are inconsistent")
                if timestamp in seen or (previous is not None and timestamp <= previous):
                    raise ValueError(
                        "Tencent K-line rows must be strictly chronological and unique"
                    )
                bar = T.Bar(
                    symbol=symbol,
                    market=market,
                    timestamp=timestamp,
                    interval=interval,
                    open=open_price,
                    high=high,
                    low=low,
                    close=close,
                    volume=round(raw_volume * scale),
                    amount=amount,
                    turnover=0.0,
                    source=self.name,
                    adjustment_factor=1.0,
                )
            except (TypeError, ValueError):
                if strict:
                    raise ValueError(f"Tencent K-line row {index} is invalid") from None
                continue
            bars.append(bar)
            seen.add(timestamp)
            previous = timestamp
        return bars

    def parse_bars(
        self,
        raw: bytes,
        symbol: str,
        market: T.Market,
        interval: str = "1d",
    ) -> list[T.Bar]:
        """Operational parser: deterministic but tolerant of isolated bad rows."""

        return self._parse_bars(raw, symbol, market, interval, strict=False)

    def parse_bars_strict(
        self,
        raw: bytes,
        symbol: str,
        market: T.Market,
        interval: str = "1d",
    ) -> list[T.Bar]:
        """Research parser: one malformed row rejects the complete raw capture."""

        return self._parse_bars(raw, symbol, market, interval, strict=True)

    def fetch_bars(
        self,
        symbol: str,
        market: T.Market,
        interval: str = "1d",
        start: datetime | None = None,
        end: datetime | None = None,
        adjust: str = "qfq",
    ) -> list[T.Bar]:
        """Fetch and tolerantly parse Tencent fallback K-line data for Runtime use."""

        raw = self.fetch_bars_raw(symbol, market, interval, start, end, adjust)
        return self.parse_bars(raw, symbol, market, interval)

    @staticmethod
    def _kline_symbol(symbol: str, market: T.Market) -> str:
        """腾讯 K 线查询码：A 用 sh/sz；港股 hk；美股 us+CODE.OQ。"""

        code = symbol.split(".", 1)[0]
        if market == T.Market.HK:
            return ("r_hk" if T.is_index_symbol(symbol) else "hk") + code
        if market == T.Market.US:
            return ("r_us" if T.is_index_symbol(symbol) else "us") + code.upper() + ".OQ"
        mk = symbol.rsplit(".", 1)[-1].upper()
        return ("sh" if mk == "SH" else "sz") + code

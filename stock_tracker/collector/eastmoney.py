"""EastmoneyProvider（push2.eastmoney.com，JSON，§4.1）。

- 单票 ``stock/get``：价格字段 ×100 整数（f43 现/f44 高/f45 低/f46 开/f60 昨收）。
- 批量快照 ``clist/get``：``fs`` 覆盖沪深主板/创业板/科创板/北交所；``fltt=2`` 使 f2 已是浮点现价。
- secid：SH=``1.`` / SZ=``0.``。

注：本环境实测 eastmoney 直连被远端断开（RemoteDisconnected），Router 会自动熔断并回退
腾讯/新浪；此实现按文档契约保留，确保未来源可用时即插即用。``normalize`` 同时兼容
单票与快照两种字段布局。
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from urllib.parse import quote_plus

from ..core import types as T
from .provider import MarketDataProvider


class EastmoneyProvider(MarketDataProvider):
    """东财行情源（A 股，批量快照主源）。"""

    SINGLE = "https://push2.eastmoney.com/api/qt/stock/get"
    CLIST = "https://push2.eastmoney.com/api/qt/clist/get"
    # 历史 K 线（独立域名 push2his，本环境实测正常，独立于被远端断开的 push2 实时）
    KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    # 沪深主板/创业板/科创板/北交所
    FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
    SNAP_FIELDS = "f12,f13,f14,f2,f3,f15,f16,f17,f18,f5,f6,f8"

    def supports_snapshot(self) -> bool:
        return True

    KLINE_SCHEMA_VERSION = "eastmoney-kline-f51-f58-v1"
    KLINE_ADAPTER_VERSION = "eastmoney-bars-v2-raw-split"

    def supports_bars(self) -> bool:
        """历史 K 线主源（东财 push2his 三市场 + 港股指数）。"""
        return True

    def supports_raw_bars(self) -> bool:
        """Expose exact response bytes before normalization for research capture."""
        return True

    def _bars_url(
        self,
        symbol: str,
        interval: str = "1d",
        start: "datetime | None" = None,
        end: "datetime | None" = None,
        adjust: str = "qfq",
    ) -> str:
        if interval != "1d":
            raise ValueError("Eastmoney raw-bar adapter currently supports interval='1d' only")
        if adjust not in {"qfq", "hfq", "raw"}:
            raise ValueError("adjust must be one of: qfq, hfq, raw")
        secid = T.to_kline_secid(symbol)
        fqt = {"qfq": 1, "hfq": 2, "raw": 0}[adjust]
        beg = start.strftime("%Y%m%d") if start else "0"
        end_s = end.strftime("%Y%m%d") if end else "20500101"
        return (
            f"{self.KLINE}?secid={secid}"
            f"&fields1=f1,f2,f3,f4,f5,f6"
            f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
            f"&klt=101&fqt={fqt}&beg={beg}&end={end_s}"
        )

    def fetch_bars_raw(
        self,
        symbol: str,
        market: T.Market,
        interval: str = "1d",
        start: "datetime | None" = None,
        end: "datetime | None" = None,
        adjust: str = "qfq",
    ) -> bytes:
        """Fetch exact provider bytes; parsing is deliberately separate."""
        if not self.applies_to(market):
            raise ValueError(f"{self.name} is not configured for market {market.value}")
        self._rl.acquire()
        return self._request(self._bars_url(symbol, interval, start, end, adjust))

    def _parse_bars(
        self,
        raw: bytes,
        symbol: str,
        market: T.Market,
        interval: str,
        *,
        strict: bool,
    ) -> "list[T.Bar]":
        if type(strict) is not bool:
            raise TypeError("strict must be a boolean")
        if not isinstance(raw, bytes) or not raw:
            raise ValueError("raw K-line response must be non-empty bytes")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Eastmoney K-line response must be a JSON object")
        rc = payload.get("rc", 0)
        data = payload.get("data")
        if rc != 0 or not isinstance(data, dict) or not data.get("klines"):
            return []
        klines = data["klines"]
        if not isinstance(klines, list):
            raise ValueError("Eastmoney data.klines must be a JSON array")

        scale = 100 if market == T.Market.A else 1
        bars: list[T.Bar] = []
        for index, line in enumerate(klines):
            if not isinstance(line, str):
                if strict:
                    raise ValueError(f"Eastmoney K-line row {index} must be a string")
                continue
            parts = line.split(",")
            if len(parts) < 8:
                if strict:
                    raise ValueError(f"Eastmoney K-line row {index} has fewer than 8 fields")
                continue
            date_s, o, c, h, l, vol, amt, turnover = parts[:8]
            try:
                bars.append(
                    T.Bar(
                        symbol=symbol,
                        market=market,
                        timestamp=datetime.strptime(date_s, "%Y-%m-%d"),
                        interval=interval,
                        open=float(o),
                        high=float(h),
                        low=float(l),
                        close=float(c),
                        volume=int(round(float(vol) * scale)),
                        amount=float(amt) if amt else 0.0,
                        turnover=float(turnover) if turnover else 0.0,
                        source=self.name,
                        adjustment_factor=1.0,
                    )
                )
            except (ValueError, TypeError) as exc:
                if strict:
                    raise ValueError(f"Eastmoney K-line row {index} is invalid") from exc
        return bars

    def parse_bars(
        self,
        raw: bytes,
        symbol: str,
        market: T.Market,
        interval: str = "1d",
    ) -> "list[T.Bar]":
        """Operational parser: deterministic, but tolerant of isolated bad rows."""
        return self._parse_bars(raw, symbol, market, interval, strict=False)

    def parse_bars_strict(
        self,
        raw: bytes,
        symbol: str,
        market: T.Market,
        interval: str = "1d",
    ) -> "list[T.Bar]":
        """Research parser: reject the complete capture if any row is malformed."""
        return self._parse_bars(raw, symbol, market, interval, strict=True)

    # ---- 历史 K 线 ----
    def fetch_bars(
        self,
        symbol: str,
        market: T.Market,
        interval: str = "1d",
        start: "datetime | None" = None,
        end: "datetime | None" = None,
        adjust: str = "qfq",
    ) -> "list[T.Bar]":
        raw = self.fetch_bars_raw(symbol, market, interval, start, end, adjust)
        return self.parse_bars(raw, symbol, market, interval)

    # ---- 单票 ----
    def _raw_quotes(self, symbols: list[str]) -> list[tuple[T.Market, dict]]:
        out: list[tuple[T.Market, dict]] = []
        for sym in symbols:
            secid = T.to_provider_symbol(sym, "eastmoney")
            url = f"{self.SINGLE}?secid={quote_plus(secid)}&fields=f43,f44,f45,f46,f57,f58,f60,f169,f170"
            raw = self._request(url).decode("utf-8")
            data = json.loads(raw).get("data")
            if not data:
                continue
            out.append((T.market_from_symbol(sym), data))
        return out

    # ---- 批量快照 ----
    def _raw_snapshot(self) -> list[tuple[T.Market, dict]]:
        url = (
            f"{self.CLIST}?pn=1&pz=2000&po=1&np=1&fltt=2&invt=2&fid=f3"
            f"&fs={quote_plus(self.FS)}&fields={self.SNAP_FIELDS}"
        )
        raw = self._request(url).decode("utf-8")
        data = json.loads(raw).get("data") or {}
        diff = data.get("diff") or []
        out: list[tuple[T.Market, dict]] = []
        for item in diff:
            f13 = item.get("f13")
            # f13: 1=上交所, 0=深交所/北交所 —— 均归为 A 股（Market 枚举仅 A/HK/US）
            market = T.Market.A if f13 in (0, 1) else None
            if market is None:
                continue
            out.append((market, item))
        return out

    # ---- 归一化（兼容两种布局） ----
    def normalize(self, payload: dict, market: T.Market) -> T.Quote:
        if "f43" in payload:  # 单票布局
            return self._normalize_single(payload, market)
        return self._normalize_snapshot(payload, market)

    def _normalize_single(self, d: dict, market: T.Market) -> T.Quote:
        # 价格字段缺失（源返回 null/缺字段）→ None，不要回落 0.0（0.0 会被 DQ 误判非法）
        f43 = d.get("f43")
        f44 = d.get("f44")
        f45 = d.get("f45")
        f46 = d.get("f46")
        f60 = d.get("f60")
        last = None if f43 is None else f43 / 100.0
        high = None if f44 is None else f44 / 100.0
        low = None if f45 is None else f45 / 100.0
        open_ = None if f46 is None else f46 / 100.0
        prev = None if f60 is None else f60 / 100.0
        code = str(d.get("f57") or "")
        name = (d.get("f58") or "") or ""  # 单票布局 f58=名称
        symbol = f"{code}.SH" if market == T.Market.A and code else ""
        return T.Quote(
            symbol=symbol, market=market, timestamp=datetime.now(),
            name=name,
            open=open_, high=high, low=low, close=last, last=last, prev_close=prev,
            volume=0, amount=0.0, turnover=0.0,
        )

    def _normalize_snapshot(self, item: dict, market: T.Market) -> T.Quote:
        f13 = item.get("f13")
        code = str(item.get("f12") or "")
        # 价格字段缺失 → None（不要 0.0）
        f2 = item.get("f2")
        last = None if f2 is None else float(f2)
        high = None if item.get("f15") is None else float(item.get("f15"))
        low = None if item.get("f16") is None else float(item.get("f16"))
        open_ = None if item.get("f17") is None else float(item.get("f17"))
        prev = None if item.get("f18") is None else float(item.get("f18"))
        volume = int(float(item.get("f5") or 0.0)) * 100  # 手 → 股（估算）
        amount = float(item.get("f6") or 0.0)
        turnover = float(item.get("f8") or 0.0)
        name = (item.get("f14") or "") or ""  # 快照布局 f14=名称
        # 后缀基于 f13（1=SH，0/其他=SZ/北交所），与 _raw_snapshot 一致
        symbol = f"{code}.SH" if f13 == 1 else f"{code}.SZ"
        return T.Quote(
            symbol=symbol, market=market, timestamp=datetime.now(),
            name=name,
            open=open_, high=high, low=low, close=last, last=last, prev_close=prev,
            volume=volume, amount=amount, turnover=turnover,
        )

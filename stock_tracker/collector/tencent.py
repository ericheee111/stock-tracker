"""TencentProvider（qt.gtimg.cn，GBK 编码，§4.1）。

已实测字段布局（A/HK/US 三种）：
- A股：f[3]=最新 f[4]=昨收 f[5]=今开 f[6]=成交量(手) f[30]=日期时间(14位紧凑)
        f[33]=最高 f[34]=最低 f[35]="最新/量/额"(额单位元) f[37]=额(万元) f[38]=换手%
- 港股/美股：f[35] 非 "a/b/c" 形式；额取 f[37]（完整本币）；时间 f[30] 为 "YYYY/MM/DD HH:MM:SS"
  或 "YYYY-MM-DD HH:MM:SS"；成交量为股（无需 ×100）。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Optional
from urllib.parse import quote

from ..core import types as T
from .provider import MarketDataProvider


def _market_of_provider_symbol(ps: str) -> T.Market:
    if ps.startswith("hk"):
        return T.Market.HK
    if ps.startswith("us"):
        return T.Market.US
    if ps.startswith("sh") or ps.startswith("sz"):
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


def _f(parts: list[str], idx: int, default: float = 0.0) -> Optional[float]:
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
            ps = key[2:] if key.startswith("v_") else key
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
        if ps.startswith("r_"):
            ps = ps[2:]
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
    def supports_bars(self) -> bool:
        """腾讯 K 线作为「兜底源」，默认不声明为主 K 线源（``supports_bars=False``）。

        Router 通过独立的 ``bars_fallback`` 配置标记将其纳入 K 线候选（仅在 eastmoney
        主源不可用/熔断时兜底）。这样既保留「腾讯默认 OFF」的契约（``supports_bars=False``），
        又提供 K 线韧性，不改动既有单测断言。
        """
        return False

    def fetch_bars(self, symbol: str, market: T.Market, interval: str = "1d",
                   start: "datetime | None" = None, end: "datetime | None" = None,
                   adjust: str = "qfq") -> "list[T.Bar]":
        """腾讯历史 K 线（兜底源）：``web.ifzq.gtimg.cn/appstock/app/fqkline/get``。

        响应 ``data[prov_sym][qfqday]`` 为**列表**数组，每行：
        ``[日期, 开, 收, 高, 低, 成交量(, 成交额)]``（注意：收在开后、高/低之前）。
        - A 股 prov_sym=``sh/sz``+code；港股=``hk``+code；美股=``us``+CODE``.OQ``
          （腾讯美股 K 线需交易所后缀，NASDAQ 为 ``.OQ``）。
        - A 股成交量单位为「手」需 ×100 → 股；HK/US 已是股。
        - 空 / 无数据 → 返回 ``[]``（不抛、不阻塞，交由 Router 兜底或调度跳过）。
        """
        if interval != "1d":
            raise ValueError("Tencent fallback currently supports interval='1d' only")
        if adjust != "qfq":
            raise ValueError("Tencent fallback currently supports adjust='qfq' only")
        self._rl.acquire()
        prov_sym = self._kline_symbol(symbol, market)
        start_s = start.strftime("%Y-%m-%d") if start else ""
        end_s = end.strftime("%Y-%m-%d") if end else ""
        count = 320  # 覆盖约 1.3 年日 K（满足 MA60/ROC60/52 周所需窗口）
        param = f"{prov_sym},day,{start_s},{end_s},{count},qfq"
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={quote(param)}"
        raw = self._request(url).decode("utf-8", "ignore")
        payload = json.loads(raw)
        node = (payload.get("data") or {}).get(prov_sym, {})
        key = "qfqday" if adjust in ("qfq", "") else "day"
        rows = node.get(key) or node.get("day") or []
        scale = 100 if market == T.Market.A else 1  # A 股手→股
        bars: list[T.Bar] = []
        for r in rows:
            # 腾讯返回为列表：[日期, 开, 收, 高, 低, 成交量(, 成交额?)]
            if not isinstance(r, (list, tuple)) or len(r) < 6:
                continue
            try:
                ts = datetime.strptime(str(r[0]), "%Y-%m-%d")
                bar = T.Bar(
                    symbol=symbol, market=market, timestamp=ts, interval=interval,
                    open=float(r[1]), high=float(r[3]), low=float(r[4]), close=float(r[2]),
                    volume=int(round(float(r[5]) * scale)),
                    amount=(float(r[6]) if len(r) > 6 and r[6] not in ("", None) else 0.0),
                    turnover=0.0,
                    source="tencent", adjustment_factor=1.0,
                )
                bars.append(bar)
            except (ValueError, TypeError):
                continue
        return bars

    @staticmethod
    def _kline_symbol(symbol: str, market: T.Market) -> str:
        """腾讯 K 线查询码：A 用 sh/sz 前缀；港股 hk；美股 us+CODE.OQ（交易所后缀）。"""
        code = symbol.split(".", 1)[0]
        if market == T.Market.HK:
            return ("r_hk" if T.is_index_symbol(symbol) else "hk") + code
        if market == T.Market.US:
            return ("r_us" if T.is_index_symbol(symbol) else "us") + code.upper() + ".OQ"
        mk = symbol.rsplit(".", 1)[-1].upper()
        return ("sh" if mk == "SH" else "sz") + code

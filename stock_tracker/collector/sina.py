"""SinaProvider（hq.sinajs.cn，CSV，需 Referer 头，§4.1）。

仅作 A 股备份源。响应形如：
  var hq_str_sh600519="贵州茅台,1346.5,1346.5,1343.0,1356.88,1332.51,1343,1343.03,3505960,4717613108,2026-08-12,16:14:32,..."
字段（逗号分隔）：0名称 1今开 2昨收 3当前 4最高 5最低 6竞买 7竞卖 8成交量(股) 9成交额(元) 10日期 11时间
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from ..core import types as T
from .provider import MarketDataProvider


def _resolve_symbol(ps: str) -> str:
    code = ps[2:]
    if ps.startswith("sh"):
        return f"{code}.SH"
    if ps.startswith("sz"):
        return f"{code}.SZ"
    return code


def _safe_float(v: str, default: float = 0.0) -> Optional[float]:
    """转 float；值缺失/不可解析（如源返回 "--"）返回 None（价格缺失），而非 0.0。"""
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


class SinaProvider(MarketDataProvider):
    """新浪行情源（A 股备份）。"""

    BASE = "https://hq.sinajs.cn/list="
    HEADERS = {"Referer": "https://finance.sina.com.cn"}

    def _raw_quotes(self, symbols: list[str]) -> list[tuple[T.Market, tuple[str, str]]]:
        prov_syms = [T.to_provider_symbol(s, "sina") for s in symbols]
        mapping = {ps: s for s, ps in zip(symbols, prov_syms)}
        url = self.BASE + ",".join(prov_syms)
        raw = self._request(url, headers=self.HEADERS).decode("gbk", "ignore")
        out: list[tuple[T.Market, tuple[str, str]]] = []
        for line in raw.split(";"):
            line = line.strip()
            if not line or "hq_str_" not in line or "=" not in line:
                continue
            key = line.split("=")[0].strip()
            ps = key[len("hq_str_"):]
            if ps not in mapping:
                continue
            start = line.find('"')
            end = line.rfind('"')
            if start == -1 or end <= start:
                continue
            body = line[start + 1:end]
            if not body or body == "":   # 空行情（如退市）
                continue
            out.append((T.Market.A, (ps, body)))
        return out

    def normalize(self, payload: tuple[str, str], market: T.Market) -> T.Quote:
        ps, body = payload
        parts = body.split(",")
        if len(parts) < 12:
            raise ValueError("新浪响应字段不足")
        name = parts[0]
        open_ = _safe_float(parts[1])
        prev = _safe_float(parts[2])
        last = _safe_float(parts[3])
        high = _safe_float(parts[4])
        low = _safe_float(parts[5])
        volume = int(_safe_float(parts[8]) or 0)  # 量缺失回落 0，避免 int(None)
        amount = _safe_float(parts[9])
        date_s = parts[10].strip()
        time_s = parts[11].strip()
        try:
            ts = datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M:%S")
        except ValueError:
            ts = datetime.now()
        return T.Quote(
            symbol=_resolve_symbol(ps),
            market=T.Market.A,
            timestamp=ts,
            name=name,
            open=open_, high=high, low=low, close=last, last=last, prev_close=prev,
            volume=volume, amount=amount, turnover=0.0,
        )

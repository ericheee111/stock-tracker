"""板块评分 + 生命周期状态机（§8 / PRD #7）。

SectorScore = 0.25*RS + 0.20*Breadth + 0.20*VolLiquidity + 0.15*LeaderQuality
             + 0.15*Catalyst + 0.05*Persistence - CrowdingPenalty
生命周期：EARLY→ACCUMULATION→LEADING→PEAK→DIVERGENCE→DECLINE（二启回 ACCUMULATION）。
Phase1：无事件引擎 → Catalyst 留空；crowding 由板块内极端涨幅近似。
若标的不含板块标签，则归入单一 ``BROAD`` 板块（保证降级态也有板块快照）。
"""

from __future__ import annotations

from ..core import types as T


# 简易板块映射（已知宇宙内代表性标的 → 板块），其余归入 BROAD
_SECTOR_MAP = {
    "600519.SH": "白酒", "000858.SZ": "白酒",
    "601318.SH": "金融", "600036.SH": "金融", "601166.SH": "金融",
    "300750.SZ": "新能源", "002594.SZ": "新能源",
    "600276.SH": "医药", "603259.SH": "医药",
    "601012.SH": "新能源", "600900.SH": "公用事业", "600887.SH": "食品",
    "000333.SZ": "家电", "000651.SZ": "家电",
    "600030.SH": "金融", "601899.SH": "有色", "600309.SH": "化工",
    "300059.SZ": "金融科技", "002415.SZ": "科技", "688981.SH": "半导体",
    "00700.HK": "港股科技", "03690.HK": "港股科技", "09988.HK": "港股科技",
    "01810.HK": "港股科技", "00939.HK": "港股金融",
    "AAPL.US": "美股科技", "MSFT.US": "美股科技", "NVDA.US": "美股科技",
    "GOOGL.US": "美股科技", "AMZN.US": "美股科技", "TSLA.US": "美股新能源",
}


def _sector_of(symbol: str, instruments: dict) -> str:
    meta = instruments.get(symbol)
    if meta and meta.get("sector"):
        return meta["sector"]
    return _SECTOR_MAP.get(symbol, "BROAD")


def _day_change(q: T.Quote) -> float:
    return (q.last / q.prev_close - 1.0) * 100.0 if q.prev_close > 0 else 0.0


class SectorEngine:
    """板块引擎（含生命周期状态机）。"""

    def __init__(self) -> None:
        self._stages: dict[str, T.SectorStage] = {}

    def update(self, quotes: list[T.Quote], instruments: dict) -> dict[str, T.SectorSnapshot]:
        groups: dict[str, list[T.Quote]] = {}
        for q in quotes:
            sec = _sector_of(q.symbol, instruments)
            groups.setdefault(sec, []).append(q)

        # 市场平均涨跌（RS 基准）
        market_avg = (sum(_day_change(q) for q in quotes) / len(quotes)) if quotes else 0.0

        out: dict[str, T.SectorSnapshot] = {}
        for sec, qs in groups.items():
            snap = self._score_sector(sec, qs, market_avg)
            out[sec] = snap
        return out

    def _score_sector(self, sec: str, qs: list[T.Quote], market_avg: float) -> T.SectorSnapshot:
        n = len(qs)
        chgs = [_day_change(q) for q in qs]
        rs = (sum(chgs) / n - market_avg) if n else 0.0
        breadth = sum(1 for c in chgs if c > 0) / n if n else 0.5
        ups = [c for c in chgs if c > 0]
        leader = max(chgs) if chgs else 0.0
        turns = [q.turnover for q in qs if q.turnover > 0]
        vol = (sum(turns) / len(turns)) if turns else 1.0
        extreme = sum(1 for c in chgs if c > 5.0) / n if n else 0.0

        rs_score = max(0.0, min(100.0, 50.0 + rs * 12.0))
        breadth_score = breadth * 100.0
        vol_score = max(0.0, min(100.0, 40.0 + vol * 20.0))
        leader_score = max(0.0, min(100.0, 50.0 + leader * 8.0))
        crowding_penalty = max(0.0, extreme - 0.15) * 100.0
        catalyst = ""  # Phase1 无事件引擎

        score = (
            0.25 * rs_score + 0.20 * breadth_score + 0.20 * vol_score
            + 0.15 * leader_score + 0.15 * (50.0 if not catalyst else 70.0)
            + 0.05 * 50.0 - crowding_penalty
        )
        score = max(0.0, min(100.0, score))

        stage = self._transition(sec, score, rs_score, extreme)
        return T.SectorSnapshot(
            sector=sec, score=round(score, 1), stage=stage,
            relative_strength=round(rs_score, 1), breadth=round(breadth_score, 1),
            volume=round(vol_score, 1), leader_quality=round(leader_score, 1),
            catalyst=catalyst, persistence=50.0, crowding=round(extreme * 100.0, 1),
        )

    def _transition(self, sec: str, score: float, rs: float, extreme: float) -> T.SectorStage:
        cur = self._stages.get(sec, T.SectorStage.EARLY)
        if cur == T.SectorStage.EARLY:
            nxt = T.SectorStage.ACCUMULATION if (rs > 52 or score > 52) else cur
        elif cur == T.SectorStage.ACCUMULATION:
            nxt = T.SectorStage.LEADING if (score >= 60 and rs >= 55) else cur
        elif cur == T.SectorStage.LEADING:
            nxt = T.SectorStage.PEAK if (score >= 72 or extreme > 0.15) else cur
        elif cur == T.SectorStage.PEAK:
            nxt = T.SectorStage.DIVERGENCE if (score < 66 or rs < 50) else cur
        elif cur == T.SectorStage.DIVERGENCE:
            nxt = T.SectorStage.DECLINE if (score < 48 or rs < 45) else cur
        else:  # DECLINE
            nxt = T.SectorStage.ACCUMULATION if (score > 52 and rs > 50) else cur
        self._stages[sec] = nxt
        return nxt

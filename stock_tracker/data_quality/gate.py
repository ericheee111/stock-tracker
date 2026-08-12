"""Data Quality Gate（§6 / PRD #5.2 / #5.4）。

七类规则按严重度评估，输出 ``DataQuality`` + ``DataStatus``：
- 时间戳异常（未来 / 远早于上一笔）
- 重复（连续 tick 时间戳与最新价均不变）
- 新鲜度（observed_age_ms 对照 delayed/stale 阈值）
- 完整性（必填缺失 / last<=0）
- 停牌（volume==0 且 无波动）
- 跨源偏差（deviation > 容忍度）
- future-leak 硬阻断（computed_at < timestamp）

下游阻断：``quality.status ∈ {INVALID, STALE, DEGRADED}`` 时禁止产生/升级强信号
（§6 / PRD #5.2：「DEGRADED 不允许强执行级别」）。``DELAYED`` 仅降权，不阻断。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..core import types as T
from ..core.config import ConfigBundle

# 阻断强信号的质量状态（DEGRADED 亦按 PRD #5.2 不允许强执行）
BLOCKING_STATUSES = {T.QualityStatus.INVALID, T.QualityStatus.STALE, T.QualityStatus.DEGRADED}

_FUTURE_DRIFT = timedelta(seconds=120)
_CROSS_DEV_TOL = 0.01  # 跨源价格偏差容忍度 1%


def blocks_strong_signal(dq: T.DataQuality) -> bool:
    """质量是否阻断强信号（TRIGGERED/ACTIVE）。"""
    return dq.status in BLOCKING_STATUSES


class DataQualityGate:
    """数据质量闸门。"""

    def __init__(self, bundle: ConfigBundle) -> None:
        self.bundle = bundle

    def _market_cfg(self, market: T.Market):
        return {"A": self.bundle.markets.a, "HK": self.bundle.markets.hk,
                "US": self.bundle.markets.us}[market.value]

    def evaluate(self, quote: T.Quote, prev: T.Quote | None = None,
                 deviation: float = 0.0) -> tuple[T.DataQuality, T.DataStatus]:
        reasons: list[str] = []
        score = 100
        status = T.QualityStatus.VALID
        now = datetime.now()

        # ---- 1. future-leak 硬阻断（PRD #5.4） ----
        if quote.computed_at < quote.timestamp:
            return T.DataQuality(T.QualityStatus.INVALID, 0,
                                 ["future-leak: computed_at < timestamp"]), T.DataStatus.UNKNOWN
        if quote.timestamp > now + _FUTURE_DRIFT:
            return T.DataQuality(T.QualityStatus.INVALID, 0,
                                 ["时间戳来自未来"]), T.DataStatus.UNKNOWN

        # ---- 2. 完整性 ----
        # 价格字段为 None（源缺失）或 <=0（非法）均判 INVALID；None 不能参与比较，先单独判。
        if (quote.last is None or quote.high is None or quote.low is None
                or quote.open is None or quote.last <= 0 or quote.high <= 0
                or quote.low <= 0 or quote.open <= 0):
            return T.DataQuality(T.QualityStatus.INVALID, 0,
                                 ["必填价格字段缺失/非法"]), T.DataStatus.UNKNOWN
        if quote.timestamp.year < 2000:  # 默认时间，视为缺失
            return T.DataQuality(T.QualityStatus.INVALID, 0,
                                 ["行情时间戳缺失"]), T.DataStatus.UNKNOWN

        # ---- 3. 新鲜度（对照市场阈值，§26.10） ----
        mc = self._market_cfg(quote.market)
        age_ms = quote.observed_age_ms
        data_status = T.DataStatus.LIVE
        if mc.stale_ms and age_ms > mc.stale_ms:
            status = T.QualityStatus.STALE
            data_status = T.DataStatus.STALE
            reasons.append(f"数据过期：age={age_ms}ms > stale={mc.stale_ms}ms")
            score -= 55
        elif mc.delayed_ms and age_ms > mc.delayed_ms:
            data_status = T.DataStatus.DELAYED
            reasons.append(f"数据延迟：age={age_ms}ms > delayed={mc.delayed_ms}ms")
            score -= 15
        if age_ms <= 0 and quote.timestamp < now - _FUTURE_DRIFT:
            # 无法确定可靠源时间
            data_status = T.DataStatus.UNKNOWN
            reasons.append("无法确定可靠源时间戳")
            score -= 10

        # ---- 4. 重复（疑似停更） ----
        if prev is not None and prev.timestamp == quote.timestamp and prev.last == quote.last:
            status = self._worse(status, T.QualityStatus.DEGRADED)
            reasons.append("连续 tick 时间与价格均未变（疑似停更）")
            score -= 20

        # ---- 5. 停牌 / 特殊 ----
        if quote.volume == 0 and quote.high == quote.low and quote.last == quote.close:
            status = self._worse(status, T.QualityStatus.DEGRADED)
            reasons.append("疑似停牌：成交量为 0 且无波动")
            score -= 25

        # ---- 6. 跨源偏差 ----
        if deviation > _CROSS_DEV_TOL:
            status = self._worse(status, T.QualityStatus.DEGRADED)
            reasons.append(f"跨源偏差过大：{deviation:.2%}")
            score -= 25

        score = max(0, min(100, score))
        # INVALID 强制低分
        if status == T.QualityStatus.INVALID:
            score = min(score, 40)
        return T.DataQuality(status, score, reasons), data_status

    @staticmethod
    def _worse(a: T.QualityStatus, b: T.QualityStatus) -> T.QualityStatus:
        """取更严重状态：INVALID > STALE > DEGRADED > VALID。"""
        order = {T.QualityStatus.VALID: 0, T.QualityStatus.DEGRADED: 1,
                 T.QualityStatus.STALE: 2, T.QualityStatus.INVALID: 3}
        return a if order[a] >= order[b] else b

"""持仓拥挤度 / 追高风险仪表（§24.6，普通层，不引用 quant）。

纯展示启发式：把「近期涨幅分位 / 距 MA20 的 ATR 距离 / 量比极值 / 动量加速」
合成 0—100 的拥挤度分与档位（安全 / 关注 / 拥挤 / 追高）。

设计红线：
- 这是**展示辅助**，绝不是评分模型，绝不进入信号打分、风险闸门或任何决策路径；
- 仅消费已存在的展示指标（``build_indicators`` 的输出），不引入新算法、不引用
  ``stock_tracker.quant``；
- 任何指标缺失都安全降级为「安全 / 暂无指标」，不抛异常、不编造数字。
"""
from __future__ import annotations

from typing import Any, Optional


# 档位阈值（降序）：分数 ≥ 阈值即归入该档。
_LEVELS = [
    (75, "OVEREXT", "追高", "#ff453a"),
    (50, "CROWDED", "拥挤", "#ff9f0a"),
    (25, "WATCH", "关注", "#64d2ff"),
    (0, "SAFE", "安全", "#30d158"),
]

# 各因子权重上限（合计 100）。
_W_POS52W = 40.0    # 52 周位置（高位 = 拥挤）
_W_DIST = 30.0      # 距 MA20 的 ATR 距离（正偏离 = 偏高）
_W_VOL = 20.0       # 量比极值（放量 = 拥挤）
_W_MOM = 10.0       # 20 日动量加速


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _num(v: Any) -> Optional[float]:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    return None


def crowding_for(indicators: Optional[dict], sig: Any = None) -> dict:
    """由展示指标合成拥挤度分与档位。

    ``indicators`` 为 ``build_indicators`` / ``serialize_indicators`` 的输出（可能
    为 ``None`` 或部分字段缺失）。``sig`` 为可选 Signal，预留市场/状态上下文，
    当前仅用于防御性跳过——不读取任何评分字段。
    返回：``{"score", "level", "level_key", "color", "factors"}``。
    """
    ind = indicators if isinstance(indicators, dict) else {}
    factors: list[str] = []
    score = 0.0

    # 1) 52 周位置：越靠高位越拥挤
    pos = _num(ind.get("pos52w"))
    if pos is not None:
        p = _clamp(pos, 0.0, 1.0)
        score += p * _W_POS52W
        pct = int(round(p * 100))
        tier = "高位" if pct > 80 else ("中位" if pct > 40 else "低位")
        factors.append(f"52周位置 {pct}%（{tier}）")

    # 2) 距 MA20 的 ATR 距离：正偏离 = 涨太多、偏高
    ma20 = _num(ind.get("ma20"))
    last = _num(ind.get("last_close"))
    atr = _num(ind.get("atr14"))
    if ma20 is not None and last is not None and atr is not None and ma20 > 0 and atr > 0:
        dist_atr = (last - ma20) / atr
        if dist_atr > 0:
            score += _clamp(dist_atr, 0.0, 3.0) / 3.0 * _W_DIST
        factors.append(
            f"距MA20 {dist_atr:+.1f}ATR（{'偏高' if dist_atr > 1.5 else '适中'}）"
        )

    # 3) 量比极值：放量 = 拥挤
    vr = _num(ind.get("vol_ratio"))
    if vr is not None:
        v = max(vr - 1.0, 0.0)
        score += _clamp(v, 0.0, 3.0) / 3.0 * _W_VOL
        factors.append(f"量比 {vr:.2f}（{'放量' if vr > 2 else '正常'}）")

    # 4) 20 日动量加速：roc20 为正且强于 roc60（加速中）= 拥挤
    roc20 = _num(ind.get("roc20"))
    roc60 = _num(ind.get("roc60"))
    if roc20 is not None and roc20 > 0:
        accel = 1.0
        if roc60 is not None and roc60 > 0 and roc20 <= roc60:
            accel = 0.4
        score += _W_MOM * (_clamp(roc20, 0.0, 30.0) / 30.0) * accel
        factors.append(f"20日涨幅 {roc20:.1f}%")

    score = int(round(_clamp(score, 0.0, 100.0)))

    key, label, color = "SAFE", "安全", "#30d158"
    for thr, k, lbl, col in _LEVELS:
        if score >= thr:
            key, label, color = k, lbl, col
            break

    if not factors:
        factors.append("暂无指标，无法评估拥挤度")
        key, label, color = "SAFE", "安全", "#30d158"

    return {
        "score": score,
        "level": label,
        "level_key": key,
        "color": color,
        "factors": factors,
    }

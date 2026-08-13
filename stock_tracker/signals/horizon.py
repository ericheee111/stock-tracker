"""持仓周期维度（普通层，§38 边界之外）。

本模块**不引用、不修改** ``stock_tracker.quant/``（该目录为 PRD §38 划定的
独立车道，由 ChatGPT/Codex 负责）。时间维度本身在 quant 已有定义，但本车道
仅做**展示用派生**：从已有信号的 ``strategy_id`` 推断一个粗略的「持仓周期」
分桶，供收市态面板按「几天 / 几周 / 几个月~几年」分组展示。

设计纪律：
- 不往 Signal/Position schema 新增字段（不改数据结构）。
- 不引用 quant 的任何 symbol / 模块。
- 纯派生、纯展示：返回的 dict 直接塞进 serializer 输出，前端按 order 分组。
- 三桶为展示约定；LONG 桶当前为空属正常（扩展点：后续若 quant 产出长线策略
  信号，仅需在此增补 ``STRATEGY_HORIZON`` 映射即可，无需改 schema）。
"""

from __future__ import annotations

from typing import Optional

from ..core import types as T

__all__ = ["HORIZONS", "STRATEGY_HORIZON", "horizon_for_signal", "horizon_for_key"]


# 三桶展示维度：key / 中文标签 / 持仓周期描述 / 排序（升序 = 短→长）。
HORIZONS = {
    "SHORT": {"key": "SHORT", "label": "短线", "span": "几天", "order": 1},
    "MEDIUM": {"key": "MEDIUM", "label": "中线", "span": "几周", "order": 2},
    "LONG": {"key": "LONG", "label": "长线", "span": "几个月~几年", "order": 3},
}
DEFAULT_HORIZON = "MEDIUM"

# 已确认的策略 id 取值（见 strategies/s1_breakout.py "S1"、s2_pullback.py "S2"、
# s3_event.py "S3"、signals/manager.py _watch_candidate "BASE"）。
# 映射逻辑：突破/事件驱动属短线（几天）；回踩/基础观察属中线（几周）。
STRATEGY_HORIZON = {
    "S1": "SHORT",      # 突破策略：短炒
    "S3": "SHORT",      # 事件驱动：短线
    "S2": "MEDIUM",     # 回踩策略：中线波段
    "BASE": "MEDIUM",   # 基础观察候选：中线
}


def horizon_for_key(key: str) -> dict:
    """按桶 key 取展示 dict；未知 key 回退默认 MEDIUM。"""
    return dict(HORIZONS.get(key, HORIZONS[DEFAULT_HORIZON]))


def horizon_for_signal(sig: Optional[T.Signal]) -> dict:
    """从信号派生持仓周期维度。

    入参可为 Signal 对象或任意带 ``strategy_id`` 属性的对象；缺失/未知一律回退
    MEDIUM（中线）。返回纯 dict（key/label/span/order），便于 JSON 序列化。
    """
    if sig is None:
        return horizon_for_key(DEFAULT_HORIZON)
    sid = getattr(sig, "strategy_id", None) or ""
    bucket = STRATEGY_HORIZON.get(sid, DEFAULT_HORIZON)
    return horizon_for_key(bucket)

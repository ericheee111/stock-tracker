"""ProviderHealth 滚动统计 + 熔断状态机（§4.2 / PRD #26.7）。

- 滚动窗口统计延迟分位、错误率、超时率、陈旧率、限频命中。
- 熔断：连续失败超阈值 → OPEN（暂停 N 秒）→ HALF_OPEN（试探）→ 成功回 CLOSED。
- 退避：指数退避 ``backoff_base * 2**n``，封顶 ``backoff_max``。
"""

from __future__ import annotations

import time
from collections import deque
from datetime import datetime

from ..core import types as T
from ..core.config import ProviderConfig


class HealthTracker:
    """单 Provider 健康追踪器。"""

    def __init__(self, cfg: ProviderConfig, window: int = 200) -> None:
        self.cfg = cfg
        self.name = cfg.name
        self._latency: deque[float] = deque(maxlen=window)
        self._success = 0
        self._error = 0
        self._timeout = 0
        self._stale = 0
        self.rate_limit_hits = 0
        self.last_success_at: datetime | None = None
        self.cross_source_deviation = 0.0
        # 熔断
        self.circuit = T.CircuitState.CLOSED
        self._consecutive_fail = 0
        self._backoff_level = 0
        self._open_until = 0.0  # epoch 秒

    # ---- 记录 ----
    def record_success(self, latency_ms: float, stale: bool = False) -> None:
        self._latency.append(latency_ms)
        self._success += 1
        if stale:
            self._stale += 1
        self.last_success_at = datetime.now()
        self._consecutive_fail = 0
        self._backoff_level = 0
        if self.circuit != T.CircuitState.CLOSED:
            self.circuit = T.CircuitState.CLOSED

    def record_failure(self, is_timeout: bool = False) -> None:
        self._error += 1
        if is_timeout:
            self._timeout += 1
        self._consecutive_fail += 1
        if self._consecutive_fail >= self.cfg.circuit_fail_threshold:
            self._open_up()

    def _open_up(self) -> None:
        self.circuit = T.CircuitState.OPEN
        backoff = min(self.cfg.backoff_max_sec,
                      self.cfg.backoff_base_sec * (2 ** self._backoff_level))
        self._backoff_level += 1
        self._open_until = time.time() + backoff

    def can_try(self) -> bool:
        """当前是否允许请求（熔断状态下按时间试探）。"""
        if self.circuit == T.CircuitState.CLOSED:
            return True
        if time.time() >= self._open_until:
            self.circuit = T.CircuitState.HALF_OPEN
            return True
        return False

    # ---- 统计 ----
    def _pct(self, p: float) -> float:
        if not self._latency:
            return 0.0
        s = sorted(self._latency)
        k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
        return s[k]

    def _rate(self, num: int) -> float:
        total = self._success + self._error
        return (num / total) if total > 0 else 0.0

    def to_provider_health(self) -> T.ProviderHealth:
        return T.ProviderHealth(
            provider=self.name,
            latency_p50=self._pct(50),
            latency_p95=self._pct(95),
            error_rate=self._rate(self._error),
            timeout_rate=self._rate(self._timeout),
            stale_ratio=(self._stale / self._success) if self._success > 0 else 0.0,
            rate_limit_hits=self.rate_limit_hits,
            last_success_at=self.last_success_at,
            cross_source_deviation=self.cross_source_deviation,
            circuit_state=self.circuit,
        )

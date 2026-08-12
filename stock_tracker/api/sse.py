"""SSE 推送（§9.2 / PRD #26）。

``SSEHub`` 订阅 ``core.eventbus`` 的 ``quote`` / ``signal`` / ``regime`` / ``sector`` /
``provider_health`` 事件，向所有已连接客户端广播。每个客户端持有一个 ``queue.Queue``，
由 HTTP handler 长连读取并逐条写出 ``event:`` / ``data:`` 帧。

注意：本模块只做转发，不触上游；所有事件由 Collector / SignalManager 发布。
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Callable


class SSEHub:
    """进程内 SSE 客户端管理器。"""

    def __init__(self, bus) -> None:
        self._clients: set[queue.Queue] = set()
        self._lock = threading.Lock()
        self._seq = 0
        bus.subscribe(self._on_event)

    def _on_event(self, topic: str, payload: Any) -> None:
        # 只转发关注的事件类型，避免噪音
        if topic not in ("quote", "signal", "regime", "sector", "provider_health"):
            return
        with self._lock:
            dead = []
            for q in list(self._clients):
                try:
                    q.put_nowait((topic, payload))
                except Exception:
                    dead.append(q)
            for q in dead:
                self._clients.discard(q)

    def add_client(self, q: "queue.Queue") -> None:
        with self._lock:
            self._clients.add(q)

    def remove_client(self, q: "queue.Queue") -> None:
        with self._lock:
            self._clients.discard(q)

    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)


# 便捷类型别名
SSEHandler = Callable[[str, dict], None]

"""进程内发布/订阅（供 SSE 推送与模块解耦）。

事件主题（§9.2）：``quote`` / ``signal`` / ``regime`` / ``sector`` / ``provider_health``。
订阅者收到 ``(topic, payload)``；payload 为已序列化的 dict（由发布方决定）。
"""

from __future__ import annotations

import threading
from typing import Callable

Topic = str
Handler = Callable[[str, dict], None]


class EventBus:
    """线程安全的简单事件总线。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._handlers: list[Handler] = []

    def subscribe(self, handler: Handler) -> Handler:
        """注册订阅者，返回原 handler 便于注销。"""
        with self._lock:
            if handler not in self._handlers:
                self._handlers.append(handler)
        return handler

    def unsubscribe(self, handler: Handler) -> None:
        with self._lock:
            if handler in self._handlers:
                self._handlers.remove(handler)

    def publish(self, topic: str, payload: dict) -> None:
        """发布事件；异常被隔离，不影响发布者。"""
        with self._lock:
            handlers = list(self._handlers)
        for h in handlers:
            try:
                h(topic, payload)
            except Exception:
                # 单个订阅者出错不影响其他订阅者（SSE 断开等）
                continue


# 全局单例（进程内共享）
_bus = EventBus()


def get_bus() -> EventBus:
    return _bus

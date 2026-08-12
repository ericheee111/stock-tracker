"""统一日志（标准库 logging + RotatingFileHandler）。

按 app.toml 配置级别与轮转；同时输出到控制台便于 ``python -m stock_tracker`` 观察。
"""

from __future__ import annotations

import logging
import os

from .config import LoggingConfig

_LOGGER_NAME = "stock_tracker"
_configured = False


def setup_logging(cfg: LoggingConfig, level: str | None = None) -> logging.Logger:
    """配置并返回一个命名 logger。幂等：仅首次生效。"""
    global _configured
    logger = logging.getLogger(_LOGGER_NAME)
    if _configured:
        return logger

    lvl = getattr(logging, (level or cfg.level or "INFO").upper(), logging.INFO)
    logger.setLevel(lvl)
    logger.propagate = False

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s:%(funcName)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 控制台
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    # 文件轮转
    try:
        path = cfg.file
        os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
        from logging.handlers import RotatingFileHandler

        fh = RotatingFileHandler(path, maxBytes=cfg.max_bytes, backupCount=cfg.backup, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        logger.warning("无法初始化日志文件，仅使用控制台输出", exc_info=False)

    _configured = True
    return logger


def get_logger() -> logging.Logger:
    """获取已配置的 logger（未配置则用默认 INFO）。"""
    logger = logging.getLogger(_LOGGER_NAME)
    if not logger.handlers:
        setup_logging(LoggingConfig())
    return logger

"""命令行参数解析（轻量）。

``python -m stock_tracker [--host HOST] [--port N] [--config-dir DIR] [--once]``

- ``--port``    ：覆盖 app.toml 的服务端口。
- ``--config-dir``：配置文件目录（默认 ``<项目根>/config``）。
- ``--once``    ：自检模式，只拉一轮数据并打印摘要后退出（用于验证 normalize/连通性）。
- ``--allow-non-loopback``：仅供经过审查的纯云实验显式确认 LAN/公网绑定风险。
"""

from __future__ import annotations

import argparse
import os


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    default_config = os.path.join(here, "config")
    parser = argparse.ArgumentParser(
        prog="stock_tracker",
        description="Stock Tracker · 系统化股票决策系统（Phase 1 后端）",
    )
    parser.add_argument("--port", type=int, default=None, help="服务端口（覆盖 app.toml）")
    parser.add_argument("--config-dir", type=str, default=default_config, help="配置目录")
    parser.add_argument("--once", action="store_true", help="自检模式：拉一轮数据后退出")
    parser.add_argument("--host", type=str, default=None, help="绑定地址（覆盖 app.toml）")
    parser.add_argument(
        "--allow-non-loopback",
        action="store_true",
        help="显式允许非 loopback 绑定；仅用于经过审查的 PURE_CLOUD_EXPERIMENTAL",
    )
    return parser.parse_args(argv)

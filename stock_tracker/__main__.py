"""程序入口（``python -m stock_tracker``，§14 T1）。

装配各模块并启动 API 服务 + HOT/WARM/COLD 采集调度：
- 零第三方依赖：纯标准库。无网络安装即可运行。
- 真实数据优先：默认直连腾讯/东财/新浪；任一源故障由 Router 熔断/退避/备用接管。
- 配置：``config/*.toml``（tomllib 只读，缺失字段用缺省兜底）。

参数（见 ``cli.parse_args``）：
- ``--port``   覆盖 app.toml 端口
- ``--host``   覆盖 app.toml 绑定地址
- ``--config-dir`` 配置目录（默认 ``<项目根>/config``）
- ``--once``   自检模式：拉一轮数据 + 跑一次信号扫描，打印摘要后退出
"""

from __future__ import annotations

import os
import sys

from .cli import parse_args
from .core.config import load_configs
from .core.logging import setup_logging, get_logger
from .core.store import MarketStore
from .collector import router as R
from .collector.scheduler import Scheduler
from .collector.tencent import TencentProvider
from .collector.sina import SinaProvider
from .collector.eastmoney import EastmoneyProvider
from .data_quality.gate import DataQualityGate
from .features.engine import FeatureEngine
from .signals.manager import SignalManager
from .storage.repository import Repository
from .api.handlers import AppContext
from .api.sse import SSEHub
from .api.server import APIServer
from .core.eventbus import get_bus


# Provider 类注册表（由 providers.toml 的 cls 字段实例化）
_PROVIDER_REGISTRY = {
    "TencentProvider": TencentProvider,
    "SinaProvider": SinaProvider,
    "EastmoneyProvider": EastmoneyProvider,
}


def _build_providers(bundle, logger):
    """按 providers.toml 实例化 Provider 列表。"""
    providers = []
    for cfg in bundle.providers:
        cls = _PROVIDER_REGISTRY.get(cfg.cls)
        if cls is None:
            logger.warning("未知 provider 类：%s（已跳过）", cfg.cls)
            continue
        providers.append(cls(cfg))
    if not providers:
        raise RuntimeError("未配置任何可用 Provider，无法启动")
    return providers


def build_context(args) -> tuple:
    """装配全部组件，返回 (ctx, scheduler, api_server, logger)。"""
    bundle = load_configs(args.config_dir)
    logger = setup_logging(bundle.app.logging)

    # 命令行覆盖
    if args.port is not None:
        bundle.app.server.port = args.port
    if args.host is not None:
        bundle.app.server.host = args.host

    # 云部署注入：Render 等平台经 $PORT 注入监听端口；--port 参数优先于环境变量。
    if args.port is None:
        env_port = os.environ.get("PORT")
        if env_port:
            try:
                bundle.app.server.port = int(env_port)
            except ValueError:
                logger.warning("$PORT 非整数，忽略：%s", env_port)

    root_dir = bundle.app.root_dir or os.path.dirname(os.path.abspath(args.config_dir))
    web_root = os.path.join(root_dir, "web")

    # SQLite 路径：相对路径基于项目根解析
    db_path = bundle.app.store.sqlite_path
    if not os.path.isabs(db_path):
        db_path = os.path.join(root_dir, db_path)

    # 云部署保障：Render 的 Procfile/Dockerfile 直接 `python -m stock_tracker`，
    # 不经 scripts/start.py 预建 data/ 目录；此处确保父目录存在，避免 SQLite 打开即崩溃。
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    logger.info("Stock Tracker 启动 | root=%s | db=%s | port=%s",
                root_dir, db_path, bundle.app.server.port)

    store = MarketStore()
    repo = Repository(db_path)

    # 重启恢复：熔断态重置为 HALF_OPEN 试探（§12）
    repo.recover_provider_states()

    providers = _build_providers(bundle, logger)
    router = R.ProviderRouter(bundle, providers)
    gate = DataQualityGate(bundle)
    feature_engine = FeatureEngine(bundle)
    signal_manager = SignalManager(bundle, store, repo, router, feature_engine, gate)

    # 恢复信号/自选/持仓到进程内
    signal_manager.recover()

    sse_hub = SSEHub(get_bus())
    ctx = AppContext(
        bundle=bundle, store=store, repo=repo, router=router,
        signal_manager=signal_manager, sse_hub=sse_hub, web_root=web_root,
    )

    scheduler = Scheduler(bundle, store, repo, router, feature_engine,
                          signal_manager, gate, logger)
    api_server = APIServer(bundle.app.server.host, bundle.app.server.port, ctx, logger)
    return ctx, scheduler, api_server, logger


def _self_check(ctx, scheduler, logger) -> int:
    """--once 自检：拉一轮 COLD + HOT/WARM，打印摘要。"""
    logger.info("自检模式（--once）：执行一轮采集与信号扫描")
    # 预热 COLD
    scheduler._run_cold()  # noqa: SLF001 自检专用
    # WARM 扫描（含信号管线）
    scheduler._run_pool("WARM", scheduler._warm_pool,  # noqa: SLF001
                        ctx.bundle.app.collector.warm_pool_size)
    scheduler._publish_health()  # noqa: SLF001

    quotes = ctx.store.get_quotes()
    signals = ctx.store.get_signals()
    regime = ctx.store.get_regime()
    healths = ctx.router.health_list()

    logger.info("=" * 60)
    logger.info("自检摘要：")
    logger.info("  行情标的数：%d", len(quotes))
    for q in list(quotes.values())[:10]:
        logger.info("    %s %s last=%.2f ds=%s age=%dms src=%s",
                    q.symbol, q.name, q.last, q.data_status.value,
                    q.observed_age_ms, q.source)
    logger.info("  信号数：%d", len(signals))
    logger.info("  Regime：%s", regime.regime.value if regime else "N/A")
    logger.info("  Provider 健康：")
    for h in healths:
        logger.info("    %s circuit=%s p50=%.1fms err=%.2f last=%s",
                    h.provider, h.circuit_state.value, h.latency_p50,
                    h.error_rate, h.last_success_at.isoformat() if h.last_success_at else "无")
    logger.info("=" * 60)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    ctx, scheduler, api_server, logger = build_context(args)

    if args.once:
        return _self_check(ctx, scheduler, logger)

    try:
        scheduler.start()
        logger.info("API 服务启动：http://%s:%s",
                    ctx.bundle.app.server.host,
                    ctx.bundle.app.server.port)
        api_server.serve_forever()
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在关闭…")
    finally:
        try:
            scheduler.stop()
        except Exception:
            pass
        try:
            api_server.shutdown_wait()
        except Exception:
            pass
    logger.info("已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())

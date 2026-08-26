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
import threading
from pathlib import Path

from .api.audit import AuditWriteError, RemoteAuditLogger
from .api.handlers import AppContext
from .api.server import APIServer
from .api.sse import SSEHub
from .cli import parse_args
from .collector import router as R
from .collector.eastmoney import EastmoneyProvider
from .collector.free_stockdb import FreeStockDbProvider
from .collector.hithink_finance import HithinkFinanceProvider
from .collector.scheduler import Scheduler
from .collector.sina import SinaProvider
from .collector.tencent import TencentProvider
from .core.config import load_configs
from .core.eventbus import get_bus
from .core.logging import setup_logging
from .core.network import UnsafeBindError, require_safe_bind
from .core.store import MarketStore
from .data_quality.gate import DataQualityGate
from .deployment.power_guard import TradingPowerGuard
from .features.engine import FeatureEngine
from .signals.manager import SignalManager
from .storage.repository import Repository

_PROVIDER_REGISTRY = {
    "EastmoneyProvider": EastmoneyProvider,
    "FreeStockDbProvider": FreeStockDbProvider,
    "HithinkFinanceProvider": HithinkFinanceProvider,
    "SinaProvider": SinaProvider,
    "TencentProvider": TencentProvider,
}


def _build_audit_logger(bundle, root_dir: str) -> RemoteAuditLogger:
    runtime = bundle.app.runtime
    root = Path(root_dir).resolve()
    audit_path = (root / runtime.audit_log_path).resolve(strict=False)
    if os.path.commonpath((str(root), str(audit_path))) != str(root):
        raise RuntimeError("remote audit log escaped the project root")
    audit_logger = RemoteAuditLogger(
        audit_path,
        enabled=runtime.audit_enabled,
        max_bytes=runtime.audit_max_bytes,
        backup_count=runtime.audit_backup_count,
    )
    if audit_logger.enabled:
        audit_logger.ensure_ready()
    return audit_logger


def _build_api_target_server(ctx: AppContext, logger) -> APIServer | None:
    runtime = ctx.bundle.app.runtime
    if not runtime.api_target_enabled:
        return None
    return APIServer(
        "127.0.0.1",
        runtime.api_target_port,
        ctx,
        logger,
        api_only=True,
        audit_logger=ctx.audit_logger,
    )


def _build_providers(bundle, logger):
    """按 providers.toml 实例化 Provider 列表。"""

    providers = []
    for config in bundle.providers:
        if not config.enabled:
            logger.info("provider 已禁用：%s", config.name)
            continue
        provider_class = _PROVIDER_REGISTRY.get(config.cls)
        if provider_class is None:
            logger.warning("未知 provider 类：%s（已跳过）", config.cls)
            continue
        providers.append(provider_class(config))
    if not providers:
        raise RuntimeError("未配置任何可用 Provider，无法启动")
    return providers


def build_context(args) -> tuple:
    """装配全部组件，返回 ``(ctx, scheduler, api_server, logger)``。"""

    bundle = load_configs(args.config_dir)
    logger = setup_logging(bundle.app.logging)

    if args.port is not None:
        bundle.app.server.port = args.port
    if args.host is not None:
        bundle.app.server.host = args.host

    # Render 等实验平台可通过 $PORT 注入端口；命令行优先。
    if args.port is None:
        env_port = os.environ.get("PORT")
        if env_port:
            try:
                bundle.app.server.port = int(env_port)
            except ValueError:
                logger.warning("$PORT 非整数，忽略：%s", env_port)

    require_safe_bind(
        bundle.app.server.host,
        allow_non_loopback=getattr(args, "allow_non_loopback", False),
    )
    if (
        bundle.app.runtime.api_target_enabled
        and bundle.app.runtime.api_target_port == bundle.app.server.port
    ):
        raise RuntimeError("H3 API target port conflicts with the effective main server port")

    root_dir = bundle.app.root_dir or os.path.dirname(os.path.abspath(args.config_dir))
    web_root = os.path.join(root_dir, "web")

    db_path = bundle.app.store.sqlite_path
    if db_path != ":memory:" and not os.path.isabs(db_path):
        db_path = os.path.join(root_dir, db_path)
    if db_path != ":memory:":
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    logger.info(
        "Stock Tracker 启动 | root=%s | db=%s | bind=%s:%s",
        root_dir,
        db_path,
        bundle.app.server.host,
        bundle.app.server.port,
    )

    store = MarketStore()
    repo = Repository(db_path)
    audit_logger = _build_audit_logger(bundle, root_dir)

    repo.recover_provider_states()

    providers = _build_providers(bundle, logger)
    router = R.ProviderRouter(bundle, providers)
    gate = DataQualityGate(bundle)
    feature_engine = FeatureEngine(bundle)
    signal_manager = SignalManager(bundle, store, repo, router, feature_engine, gate)
    signal_manager.recover()

    sse_hub = SSEHub(get_bus())
    ctx = AppContext(
        bundle=bundle,
        store=store,
        repo=repo,
        router=router,
        signal_manager=signal_manager,
        sse_hub=sse_hub,
        web_root=web_root,
    )
    ctx.audit_logger = audit_logger

    scheduler = Scheduler(
        bundle,
        store,
        repo,
        router,
        feature_engine,
        signal_manager,
        gate,
        logger,
    )
    ctx.scheduler = scheduler
    ctx.power_guard = TradingPowerGuard(
        bundle,
        logger,
        enabled=bundle.app.runtime.prevent_sleep_during_trading,
        interval_sec=bundle.app.runtime.power_guard_interval_sec,
    )

    api_server = APIServer(
        bundle.app.server.host,
        bundle.app.server.port,
        ctx,
        logger,
        allow_non_loopback=getattr(args, "allow_non_loopback", False),
        audit_logger=audit_logger,
    )
    try:
        ctx.api_target_server = _build_api_target_server(ctx, logger)
    except (OSError, RuntimeError, TypeError, ValueError):
        api_server.server_close()
        raise
    return ctx, scheduler, api_server, logger


def _self_check(ctx, scheduler, logger) -> int:
    """``--once`` 自检：拉一轮 COLD + HOT/WARM，打印摘要。"""

    logger.info("自检模式（--once）：执行一轮采集与信号扫描")
    scheduler._run_cold()
    scheduler._run_pool(
        "WARM",
        scheduler._warm_pool,
        ctx.bundle.app.collector.warm_pool_size,
    )
    scheduler._publish_health()

    quotes = ctx.store.get_quotes()
    signals = ctx.store.get_signals()
    regime = ctx.store.get_regime()
    healths = ctx.router.health_list()

    logger.info("=" * 60)
    logger.info("自检摘要：")
    logger.info("  行情标的数：%d", len(quotes))
    for quote in list(quotes.values())[:10]:
        logger.info(
            "    %s %s last=%.2f ds=%s age=%dms src=%s",
            quote.symbol,
            quote.name,
            quote.last,
            quote.data_status.value,
            quote.observed_age_ms,
            quote.source,
        )
    logger.info("  信号数：%d", len(signals))
    logger.info("  Regime：%s", regime.regime.value if regime else "N/A")
    logger.info("  Provider 健康：")
    for health in healths:
        logger.info(
            "    %s circuit=%s p50=%.1fms err=%.2f last=%s",
            health.provider,
            health.circuit_state.value,
            health.latency_p50,
            health.error_rate,
            health.last_success_at.isoformat() if health.last_success_at else "无",
        )
    logger.info("=" * 60)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        ctx, scheduler, api_server, logger = build_context(args)
    except (AuditWriteError, OSError, RuntimeError, TypeError, UnsafeBindError, ValueError) as exc:
        print(f"Stock Tracker 启动被拒绝：{exc}", file=sys.stderr)
        return 2

    target_server = ctx.api_target_server
    power_guard = ctx.power_guard
    target_thread: threading.Thread | None = None
    target_started = False
    api_serve_entered = False

    if args.once:
        try:
            return _self_check(ctx, scheduler, logger)
        finally:
            if target_server is not None:
                target_server.server_close()
            api_server.server_close()

    try:
        if target_server is not None:
            target_thread = threading.Thread(
                target=target_server.serve_forever,
                name="hybrid-h3-api-target",
                daemon=True,
            )
            target_thread.start()
            target_started = True
            logger.info(
                "H3 API-only Target 启动：http://127.0.0.1:%s",
                ctx.bundle.app.runtime.api_target_port,
            )
        if power_guard is not None and power_guard.start():
            logger.info("H3 交易时段防休眠已启用")
        scheduler.start()
        logger.info(
            "API 服务启动：http://%s:%s",
            ctx.bundle.app.server.host,
            ctx.bundle.app.server.port,
        )
        api_serve_entered = True
        api_server.serve_forever()
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在关闭…")
    finally:
        if power_guard is not None:
            power_guard.stop()
        try:
            scheduler.stop()
        except Exception:
            logger.exception("调度器关闭失败")
        if target_server is not None:
            try:
                if target_started:
                    target_server.shutdown_wait()
                else:
                    target_server.server_close()
            except Exception:
                logger.exception("H3 API-only Target 关闭失败")
            if target_thread is not None:
                target_thread.join(timeout=5)
        try:
            if api_serve_entered:
                api_server.shutdown_wait()
            else:
                api_server.server_close()
        except Exception:
            logger.exception("API 服务关闭失败")
    logger.info("已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())

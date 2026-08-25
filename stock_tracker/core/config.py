"""配置加载（tomllib 只读 + 缺省兜底）。

只读加载 ``config/*.toml`` 并映射为 dataclass。任一文件缺失或字段缺失时
使用下方 ``DEFAULTS`` 兜底，保证零配置亦可启动（PRD #11 真实可运行优先）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import tomllib

from . import types as T


@dataclass(slots=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8080


@dataclass(slots=True)
class LoggingConfig:
    level: str = "INFO"
    file: str = "data/stock_tracker.log"
    max_bytes: int = 5 * 1024 * 1024
    backup: int = 3


@dataclass(slots=True)
class CollectorConfig:
    hot_interval_sec: float = 3.0
    warm_interval_sec: float = 10.0
    cold_interval_sec: float = 45.0
    hot_pool_size: int = 200
    warm_pool_size: int = 300
    max_workers: int = 3
    cold_universe: list[str] = field(default_factory=list)
    # K 线低频采集链路（BAR 守护线程）配置
    bars_enabled: bool = True           # 总开关：false 则跳过 BAR 线程
    bars_interval_sec: float = 21600.0  # BAR 线程循环间隔（6h，收盘后为主）
    bar_batch_size: int = 3             # 每批处理的标的数（分批节流）
    bar_batch_pause_sec: float = 1.5    # 批间暂停（秒）
    bar_backfill_days: int = 400        # 首跑全量回填请求窗口（today - N 自然日）
    bar_keep_days: int = 260            # 每标的保留最近交易日数（覆盖 MA60/ROC60/52周）


@dataclass(slots=True)
class StoreConfig:
    sqlite_path: str = "data/stock_tracker.db"


@dataclass(slots=True)
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    collector: CollectorConfig = field(default_factory=CollectorConfig)
    store: StoreConfig = field(default_factory=StoreConfig)
    markets_enabled: dict = field(default_factory=dict)  # {"a": bool, "hk": bool, "us": bool}
    root_dir: str = "."


@dataclass(slots=True)
class MarketConfig:
    prefixes: list[str] = field(default_factory=list)
    limit_up_pct: float = 0.10
    limit_down_pct: float = 0.10
    trading_hours: list = field(default_factory=list)  # [[h,m,h,m], ...]
    delayed_ms: int = 15000
    stale_ms: int = 60000
    timezone: str = "UTC"
    utc_offset_hours: int = 0


@dataclass(slots=True)
class MarketsConfig:
    a: MarketConfig = field(default_factory=MarketConfig)
    hk: MarketConfig = field(default_factory=MarketConfig)
    us: MarketConfig = field(default_factory=MarketConfig)
    price_limit_version: str = "unknown"
    # 各市场代表性指数 symbol（如 {"a":"000001.SH","hk":"HSI.HK","us":"IXIC.US"}）。
    # 供 handlers 构造指数卡，并经由 T.register_index_symbols 让 provider 符号映射区分指数。
    index: dict = field(default_factory=dict)


@dataclass(slots=True)
class StrategyConfig:
    enabled: bool = True
    min_opportunity: int = 55
    min_confidence: int = 50
    params: dict = field(default_factory=dict)


@dataclass(slots=True)
class StrategiesConfig:
    s1: StrategyConfig = field(default_factory=StrategyConfig)
    s2: StrategyConfig = field(default_factory=StrategyConfig)
    s3: StrategyConfig = field(default_factory=StrategyConfig)


@dataclass(slots=True)
class ProviderConfig:
    name: str = ""
    cls: str = ""
    markets: list[str] = field(default_factory=list)
    enabled: bool = True
    primary: bool = False
    supports_snapshot: bool = False
    timeout_ms: int = 3000
    max_rps: float = 5.0
    backoff_base_sec: float = 1.0
    backoff_max_sec: float = 60.0
    circuit_fail_threshold: int = 5
    host: str = ""               # 可选 host 覆盖（用于故障注入/自托管，默认用源码内置 BASE）
    bars_fallback: bool = False  # 是否作为 K 线兜底源（supports_bars=False 时仍可被 Router 选用）
    bars_priority: int = 0       # 同类 K 线源的显式排序权重；越高越优先
    read_only: bool = False
    trust_tier: str = "T0_UNKNOWN"
    allow_live_decision: bool = True
    allow_model_training: bool = True
    allow_public_redistribution: bool = True
    release_version: str = ""
    binary_inventory_sha256: str = ""
    data_snapshot_manifest_sha256: str = ""
    sync_manifest_sha256: str = ""


@dataclass(slots=True)
class RiskConfig:
    overextension_max_gain_from_low_pct: float = 0.30
    overextension_max_above_entry_pct: float = 0.05
    min_r_multiple: float = 2.0
    max_heat_pct: float = 0.30
    max_single_pct: float = 0.10
    max_theme_pct: float = 0.20
    regime_blocked_states: list[str] = field(default_factory=list)
    dq_min_score_to_strong: int = 60
    dq_block_if_stale: bool = True


@dataclass(slots=True)
class ConfigBundle:
    app: AppConfig
    markets: MarketsConfig
    strategies: StrategiesConfig
    providers: list[ProviderConfig]
    risk: RiskConfig


class ConfigError(Exception):
    """配置解析/加载失败。"""


def _read_toml(path: str) -> dict:
    """读取 TOML；文件缺失返回空 dict，解析失败抛 ConfigError。"""
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"TOML 解析失败: {path} -> {exc}") from exc


def _opt(d: dict, key: str, default: Any) -> Any:
    return d.get(key, default)


def _expect_bool(value: object, name: str) -> bool:
    """Require an actual TOML boolean at safety-sensitive boundaries."""

    if type(value) is not bool:
        raise ConfigError(f"{name} 必须是 TOML boolean，不能使用字符串或 0/1")
    return value


def _expect_string(value: object, name: str, *, nonempty: bool = False) -> str:
    """Require an actual TOML string and optionally reject blank values."""

    if type(value) is not str:
        raise ConfigError(f"{name} 必须是 TOML string")
    if value != value.strip():
        raise ConfigError(f"{name} 不能包含首尾空白")
    if nonempty and not value:
        raise ConfigError(f"{name} 不能为空")
    return value


def _expect_int(
    value: object,
    name: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Require a bounded TOML integer and reject bool-as-int coercion."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} 必须是 TOML integer，不能使用 boolean 或字符串")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} 必须大于等于 {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} 必须小于等于 {maximum}")
    return value


def load_app(path: str, root_dir: str) -> AppConfig:
    d = _read_toml(path)
    server_d = d.get("server", {})
    if not isinstance(server_d, dict):
        raise ConfigError("server 必须是 TOML table")
    srv = ServerConfig(
        host=_expect_string(
            _opt(server_d, "host", "127.0.0.1"),
            "server.host",
            nonempty=True,
        ),
        port=_expect_int(
            _opt(server_d, "port", 8080),
            "server.port",
            minimum=1,
            maximum=65535,
        ),
    )
    log = LoggingConfig(
        level=_opt(d.get("logging", {}), "level", "INFO"),
        file=_opt(d.get("logging", {}), "file", "data/stock_tracker.log"),
        max_bytes=_opt(d.get("logging", {}), "max_bytes", 5 * 1024 * 1024),
        backup=_opt(d.get("logging", {}), "backup", 3),
    )
    col_d = d.get("collector", {})
    col = CollectorConfig(
        hot_interval_sec=_opt(col_d, "hot_interval_sec", 3.0),
        warm_interval_sec=_opt(col_d, "warm_interval_sec", 10.0),
        cold_interval_sec=_opt(col_d, "cold_interval_sec", 45.0),
        hot_pool_size=_opt(col_d, "hot_pool_size", 200),
        warm_pool_size=_opt(col_d, "warm_pool_size", 300),
        max_workers=_opt(col_d, "max_workers", 3),
        cold_universe=_opt(col_d, "cold_universe", []),
        bars_enabled=_expect_bool(
            _opt(col_d, "bars_enabled", True),
            "collector.bars_enabled",
        ),
        bars_interval_sec=_opt(col_d, "bars_interval_sec", 21600.0),
        bar_batch_size=_opt(col_d, "bar_batch_size", 3),
        bar_batch_pause_sec=_opt(col_d, "bar_batch_pause_sec", 1.5),
        bar_backfill_days=_opt(col_d, "bar_backfill_days", 400),
        bar_keep_days=_opt(col_d, "bar_keep_days", 260),
    )
    sto = StoreConfig(sqlite_path=_opt(d.get("store", {}), "sqlite_path", "data/stock_tracker.db"))
    markets_d = d.get("markets", {})
    markets_enabled = {
        "a": _expect_bool(_opt(markets_d, "a", True), "markets.a"),
        "hk": _expect_bool(_opt(markets_d, "hk", True), "markets.hk"),
        "us": _expect_bool(_opt(markets_d, "us", True), "markets.us"),
    }
    return AppConfig(server=srv, logging=log, collector=col, store=sto,
                     markets_enabled=markets_enabled, root_dir=root_dir)


def load_markets(path: str) -> MarketsConfig:
    d = _read_toml(path)

    def mk(key: str, **over) -> MarketConfig:
        md = d.get(key, {})
        return MarketConfig(
            prefixes=_opt(md, "prefixes", []),
            limit_up_pct=_opt(md, "limit_up_pct", 0.10),
            limit_down_pct=_opt(md, "limit_down_pct", 0.10),
            trading_hours=_opt(md, "trading_hours", []),
            delayed_ms=_opt(md, "delayed_ms", 15000),
            stale_ms=_opt(md, "stale_ms", 60000),
            timezone=_opt(md, "timezone", "UTC"),
            utc_offset_hours=_opt(md, "utc_offset_hours", 0),
            **over,
        )

    raw_index = d.get("index", {}) or {}
    index_map = {k: v for k, v in raw_index.items() if v}
    # 注册指数标的符号，供 to_provider_symbol 对港/美指数使用 r_ 前缀（r_hkHSI / r_usIXIC）
    T.register_index_symbols(list(index_map.values()))
    return MarketsConfig(
        a=mk("a"),
        hk=mk("hk"),
        us=mk("us"),
        price_limit_version=_opt(d.get("price_limit_rules", {}), "version", "unknown"),
        index=index_map,
    )


def load_strategies(path: str) -> StrategiesConfig:
    d = _read_toml(path)

    def mk(key: str) -> StrategyConfig:
        sd = d.get(key, {})
        return StrategyConfig(
            enabled=_expect_bool(_opt(sd, "enabled", True), f"{key}.enabled"),
            min_opportunity=_opt(sd, "min_opportunity", 55),
            min_confidence=_opt(sd, "min_confidence", 50),
            params=_opt(sd, "params", {}),
        )

    return StrategiesConfig(s1=mk("s1"), s2=mk("s2"), s3=mk("s3"))


def load_providers(path: str) -> list[ProviderConfig]:
    d = _read_toml(path)
    out: list[ProviderConfig] = []
    for item in _opt(d, "providers", []):
        name = _opt(item, "name", "")
        out.append(ProviderConfig(
            name=name,
            cls=_opt(item, "cls", ""),
            markets=_opt(item, "markets", []),
            enabled=_expect_bool(
                _opt(item, "enabled", True),
                f"providers[{name}].enabled",
            ),
            primary=_expect_bool(
                _opt(item, "primary", False),
                f"providers[{name}].primary",
            ),
            supports_snapshot=_expect_bool(
                _opt(item, "supports_snapshot", False),
                f"providers[{name}].supports_snapshot",
            ),
            timeout_ms=_opt(item, "timeout_ms", 3000),
            host=_opt(item, "host", ""),
            max_rps=_opt(item, "max_rps", 5.0),
            backoff_base_sec=_opt(item, "backoff_base_sec", 1.0),
            backoff_max_sec=_opt(item, "backoff_max_sec", 60.0),
            circuit_fail_threshold=_opt(item, "circuit_fail_threshold", 5),
            bars_fallback=_expect_bool(
                _opt(item, "bars_fallback", False),
                f"providers[{name}].bars_fallback",
            ),
            bars_priority=_expect_int(
                _opt(item, "bars_priority", 0),
                f"providers[{name}].bars_priority",
                minimum=-1000,
                maximum=1000,
            ),
            read_only=_expect_bool(
                _opt(item, "read_only", False),
                f"providers[{name}].read_only",
            ),
            trust_tier=_opt(item, "trust_tier", "T0_UNKNOWN"),
            allow_live_decision=_expect_bool(
                _opt(item, "allow_live_decision", True),
                f"providers[{name}].allow_live_decision",
            ),
            allow_model_training=_expect_bool(
                _opt(item, "allow_model_training", True),
                f"providers[{name}].allow_model_training",
            ),
            allow_public_redistribution=_expect_bool(
                _opt(item, "allow_public_redistribution", True),
                f"providers[{name}].allow_public_redistribution",
            ),
            release_version=_opt(item, "release_version", ""),
            binary_inventory_sha256=_opt(item, "binary_inventory_sha256", ""),
            data_snapshot_manifest_sha256=_opt(item, "data_snapshot_manifest_sha256", ""),
            sync_manifest_sha256=_opt(item, "sync_manifest_sha256", ""),
        ))
    return out


def load_risk(path: str) -> RiskConfig:
    d = _read_toml(path)
    oe = d.get("overextension", {})
    rr = d.get("reward_risk", {})
    ph = d.get("portfolio_heat", {})
    rb = d.get("regime_block", {})
    dq = d.get("data_quality", {})
    return RiskConfig(
        overextension_max_gain_from_low_pct=_opt(oe, "max_gain_from_low_pct", 0.30),
        overextension_max_above_entry_pct=_opt(oe, "max_above_entry_pct", 0.05),
        min_r_multiple=_opt(rr, "min_r_multiple", 2.0),
        max_heat_pct=_opt(ph, "max_heat_pct", 0.30),
        max_single_pct=_opt(ph, "max_single_pct", 0.10),
        max_theme_pct=_opt(ph, "max_theme_pct", 0.20),
        regime_blocked_states=_opt(rb, "blocked_states", []),
        dq_min_score_to_strong=_opt(dq, "min_score_to_strong", 60),
        dq_block_if_stale=_expect_bool(
            _opt(dq, "block_if_stale", True),
            "data_quality.block_if_stale",
        ),
    )


def load_configs(config_dir: str) -> ConfigBundle:
    """加载全部 5 个 TOML → ConfigBundle。"""
    return ConfigBundle(
        app=load_app(os.path.join(config_dir, "app.toml"), root_dir=os.path.dirname(config_dir)),
        markets=load_markets(os.path.join(config_dir, "markets.toml")),
        strategies=load_strategies(os.path.join(config_dir, "strategies.toml")),
        providers=load_providers(os.path.join(config_dir, "providers.toml")),
        risk=load_risk(os.path.join(config_dir, "risk.toml")),
    )

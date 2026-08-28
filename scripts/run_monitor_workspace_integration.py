#!/usr/bin/env python3
"""Run Stage 4E Monitor Workspace against real Python API and temp databases."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sidecars.xtp.runtime import SidecarRuntime, SimulatorBackend
from sidecars.xtp.server import XtpSidecarHTTPServer
from stock_tracker.api import handlers as api_handlers
from stock_tracker.api.handlers import AppContext
from stock_tracker.api.server import APIServer
from stock_tracker.api.sse import SSEHub
from stock_tracker.collector.xtp_sidecar import load_xtp_sidecar_config
from stock_tracker.core import types as T
from stock_tracker.core.config import load_configs
from stock_tracker.core.eventbus import EventBus
from stock_tracker.core.store import MarketStore
from stock_tracker.monitor import (
    MonitorCondition,
    MonitorExpression,
    MonitorRule,
    MonitorScope,
    MonitorService,
    MonitorSeverity,
    RuleLogic,
    RuleOperator,
    ScopeKind,
)
from stock_tracker.storage.db import close_all
from stock_tracker.storage.repository import Repository

_SIDECAR_ACCESS = "monitor-workspace-sidecar-access-" + ("x" * 40)
_SYMBOLS = ("600519.SH", "000001.SZ", "300750.SZ", "688981.SH")


def _sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


class _Router:
    def health_list(self) -> list[T.ProviderHealth]:
        return [
            T.ProviderHealth(
                provider="monitor-fixture",
                latency_p50=5.0,
                latency_p95=12.0,
                error_rate=0.0,
                timeout_rate=0.0,
                stale_ratio=0.0,
                last_success_at=datetime.now(timezone.utc),
                circuit_state=T.CircuitState.CLOSED,
            )
        ]


class _AliveThread:
    @staticmethod
    def is_alive() -> bool:
        return True


class _Scheduler:
    def __init__(self) -> None:
        self._thread = _AliveThread()
        self._last_successful_scan_at = datetime.now(timezone.utc)


def _fixture_rule() -> MonitorRule:
    now = datetime.now(timezone.utc)
    return MonitorRule(
        rule_id="qa-markup-rule",
        name="<b>链路延迟</b>",
        expression=MonitorExpression(
            RuleLogic.AND,
            (
                MonitorCondition(
                    "market_event.connection_state",
                    RuleOperator.EQ,
                    "CONNECTED",
                ),
                MonitorCondition(
                    "market_event.latency_p95_ms",
                    RuleOperator.GE,
                    0.0,
                ),
            ),
        ),
        scope=MonitorScope(
            ScopeKind.SYMBOLS,
            symbols=("600519.SH",),
            max_symbols=1,
        ),
        severity=MonitorSeverity.WARNING,
        enabled=True,
        cooldown_sec=0,
        duplicate_window_sec=0,
        notification_channels=("BROWSER",),
        created_at=now,
        updated_at=now,
    )


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    production = ROOT / "data" / "stock_tracker.db"
    before = _sha(production)
    with tempfile.TemporaryDirectory(prefix="stock-tracker-monitor-qa-") as directory:
        temp_root = Path(directory)
        bundle = load_configs(str(ROOT / "config"))
        event_bus = EventBus()
        market_store = MarketStore()
        quote_now = datetime.now(timezone.utc).astimezone().replace(tzinfo=None)
        market_store.update_quote(
            T.Quote(
                symbol="600519.SH",
                market=T.Market.A,
                name="贵州茅台",
                last=10.2,
                prev_close=10.0,
                open=10.0,
                high=10.3,
                low=9.9,
                volume=1300,
                amount=13040.0,
                timestamp=quote_now,
                received_at=quote_now,
                computed_at=quote_now,
                displayed_at=quote_now,
                source="monitor-fixture",
                data_status=T.DataStatus.LIVE,
            )
        )
        repository = Repository(str(temp_root / "runtime.sqlite3"))

        sidecar_runtime = SidecarRuntime(_SYMBOLS, backend="simulator")
        simulator = SimulatorBackend(sidecar_runtime, interval_sec=0.025)
        sidecar_server = XtpSidecarHTTPServer(
            "127.0.0.1",
            0,
            sidecar_runtime,
            access_value=_SIDECAR_ACCESS,
            health_public=True,
        )
        sidecar_port = int(sidecar_server.server_address[1])
        sidecar_thread = threading.Thread(
            target=sidecar_server.serve_forever,
            daemon=True,
            name="monitor-qa-sidecar",
        )
        base_sidecar_config = load_xtp_sidecar_config(
            ROOT / "config" / "xtp_sidecar.toml"
        )
        sidecar_config = replace(
            base_sidecar_config,
            enabled=True,
            backend="simulator",
            bind_port=sidecar_port,
            event_root="market-events",
            metadata_db="market-events/catalog.sqlite3",
            quarantine_root="market-events-quarantine",
            monitor_db="monitor.sqlite3",
        )
        monitor = MonitorService(
            sidecar_config,
            project_root=temp_root,
            publisher=event_bus.publish,
            access_provider=lambda: _SIDECAR_ACCESS,
        )
        monitor.repository.upsert_rule(_fixture_rule())

        context = AppContext(
            bundle=bundle,
            store=market_store,
            repo=repository,
            router=_Router(),
            signal_manager=None,
            sse_hub=SSEHub(event_bus),
            web_root=str(ROOT / "web"),
            scheduler=_Scheduler(),
            monitor_service=monitor,
        )
        # The monitor page shares the existing application shell. Keep its QA
        # fixture honest by proving the shell's initial data endpoints are also
        # constructible before launching the browser.
        api_handlers.get_markets(context)
        api_handlers.get_overview(context)
        api_handlers.get_today_brief(context)
        server = APIServer("127.0.0.1", 0, context, None)
        api_port = int(server.server_address[1])
        api_thread = threading.Thread(
            target=server.serve_forever,
            daemon=True,
            name="monitor-qa-api",
        )

        simulator.start()
        sidecar_thread.start()
        api_thread.start()
        try:
            time.sleep(0.22)
            first_poll = monitor.poll_once(limit=200)
            if first_poll["accepted"] <= 0:
                raise RuntimeError("monitor fixture ingestion did not accept events")
            command = [
                "node",
                str(ROOT / "qa" / "ui" / "monitor_workspace_qa.cjs"),
                f"http://127.0.0.1:{api_port}",
            ]
            environment = os.environ.copy()
            environment.setdefault("PYTHONIOENCODING", "utf-8")
            completed = subprocess.run(
                command,
                cwd=ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            print(completed.stdout, end="")
            if completed.stderr:
                print(completed.stderr, file=sys.stderr, end="")
            after = _sha(production)
            database_equal = before == after
            print(
                "MONITOR_WORKSPACE_PRODUCTION_DB_UNCHANGED="
                + ("true" if database_equal else "false")
            )
            print("MONITOR_WORKSPACE_SYNTHETIC_FIXTURE_ONLY=true")
            print("MONITOR_WORKSPACE_REAL_XTP_ACCOUNT_ACCEPTANCE=PENDING")
            print("MONITOR_WORKSPACE_AUTO_TRADE=false")
            passed = completed.returncode == 0 and database_equal
            print(
                "MONITOR_WORKSPACE_ACCEPTANCE="
                + ("PASSED" if passed else "FAILED")
            )
            return 0 if passed else 1
        finally:
            server.shutdown_wait()
            sidecar_server.shutdown()
            simulator.stop()
            sidecar_server.server_close()
            api_thread.join(timeout=5.0)
            sidecar_thread.join(timeout=5.0)
            close_all()


if __name__ == "__main__":
    raise SystemExit(main())

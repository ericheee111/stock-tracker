#!/usr/bin/env python3
"""Verify the simulator and loopback IPC without XTP credentials or production writes."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sidecars.xtp.runtime import SidecarRuntime, SimulatorBackend
from sidecars.xtp.server import XtpSidecarHTTPServer
from stock_tracker.collector.xtp_sidecar import (
    XtpSidecarClient,
    load_xtp_sidecar_config,
)

_ACCESS = "xtp-verification-access-" + ("x" * 40)


def _sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _symbols(value: str) -> list[str]:
    output = [item.strip().upper() for item in value.split(",") if item.strip()]
    if not output:
        raise argparse.ArgumentTypeError("at least one symbol is required")
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify the read-only XTP sidecar simulator")
    parser.add_argument(
        "--symbols",
        type=_symbols,
        default=_symbols("600519.SH,000001.SZ,300750.SZ,688981.SH"),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config" / "xtp_sidecar.toml",
    )
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    production_db = ROOT / "data" / "stock_tracker.db"
    before = _sha(production_db)
    base_config = load_xtp_sidecar_config(args.config)
    runtime = SidecarRuntime(args.symbols, backend="simulator")
    simulator = SimulatorBackend(runtime, interval_sec=0.03)
    server = XtpSidecarHTTPServer(
        "127.0.0.1",
        0,
        runtime,
        access_value=_ACCESS,
        health_public=True,
    )
    port = int(server.server_address[1])
    config = replace(base_config, enabled=True, bind_port=port, backend="simulator")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    simulator.start()
    thread.start()
    try:
        time.sleep(0.12)
        client = XtpSidecarClient(config, access_provider=lambda: _ACCESS)
        health = client.health()
        session = client.session()
        events, cursor, has_more = client.events(
            after=0,
            limit=100,
            expected_session_id=session["session_id"],
            expected_feed_mode=session["feed_mode"],
            expected_symbols=tuple(session["symbols"]),
        )
        # Read metrics after the event page. The simulator continues producing
        # callbacks concurrently, so an earlier metrics snapshot can honestly
        # be smaller than a later event snapshot even when both are valid.
        metrics = client.metrics()
        checks = {
            "health_schema": health.get("schema") == "stock-tracker-xtp-health-v1",
            "loopback_origin": config.origin.startswith("http://127.0.0.1:"),
            "read_only": health.get("read_only") is True,
            "auto_trade_false": health.get("auto_trade") is False,
            "algorithm_account_unused": session.get("algorithm_account_used") is False,
            "events_received": bool(events),
            "symbols_bounded": 0 < len(session.get("symbols", [])) <= 20,
            "cursor_advanced": cursor > 0,
            "metrics_present": metrics.get("callback_count", 0) >= cursor >= len(events),
            "access_not_serialized": _ACCESS not in json.dumps(
                {"health": health, "session": session, "metrics": metrics},
                ensure_ascii=False,
            ),
        }
        after = _sha(production_db)
        report = {
            "schema": "stock-tracker-xtp-sidecar-verification-v1",
            "passed": all(checks.values()) and before == after,
            "checks": checks,
            "event_count": len(events),
            "next_cursor": cursor,
            "has_more": has_more,
            "health": health,
            "session": session,
            "metrics": metrics,
            "synthetic_fixture_only": True,
            "real_xtp_account_acceptance": "PENDING",
            "stock_test_account_registration": "USER_REPORTED_NOT_MACHINE_VERIFIED",
            "algorithm_test_account_registration": "USER_REPORTED_NOT_MACHINE_VERIFIED",
            "algorithm_account_used": False,
            "allow_live_decision": False,
            "allow_model_training": False,
            "auto_trade": False,
            "contains_account_value": False,
            "contains_sidecar_access": False,
            "production_database_sha256_before": before,
            "production_database_sha256_after": after,
            "production_database_modified": before != after,
        }
        rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
        return 0 if report["passed"] else 1
    finally:
        server.shutdown()
        simulator.stop()
        server.server_close()
        thread.join(timeout=2.0)


if __name__ == "__main__":
    raise SystemExit(main())

# ruff: noqa: UP006, UP031, UP035, UP045
"""Command-line entry point for the read-only XTP quote sidecar."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from typing import List, Optional

from .contracts import ENV_SIDECAR_ACCESS, XtpSidecarContractError, validate_access
from .official import (
    OfficialQuoteEnvironment,
    load_quote_module,
    quote_module_capabilities,
)
from .runtime import SidecarRuntime, SimulatorBackend
from .server import XtpSidecarHTTPServer

_DEFAULT_SYMBOLS = "600519.SH,000001.SZ,300750.SZ,688981.SH"


def _symbols(value: str) -> List[str]:
    symbols = [item.strip().upper() for item in value.split(",") if item.strip()]
    if not symbols:
        raise argparse.ArgumentTypeError("at least one symbol is required")
    return symbols


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the loopback-only read-only XTP quote sidecar")
    parser.add_argument("--backend", choices=("simulator", "xtp"), default="simulator")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=17991)
    parser.add_argument("--symbols", type=_symbols, default=_symbols(_DEFAULT_SYMBOLS))
    parser.add_argument("--interval-sec", type=float, default=0.25)
    parser.add_argument("--api-version", default="2.2.50.8")
    parser.add_argument("--private-health", action="store_true")
    parser.add_argument("--acknowledge-read-only-live-data", action="store_true")
    parser.add_argument("--probe-official", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        access = validate_access(os.environ.get(ENV_SIDECAR_ACCESS))
        if args.backend == "xtp":
            if not args.acknowledge_read_only_live_data:
                raise XtpSidecarContractError(
                    "real XTP backend requires --acknowledge-read-only-live-data"
                )
            environment = OfficialQuoteEnvironment.from_environ()
            module = load_quote_module()
            capabilities = quote_module_capabilities(module)
            if args.probe_official:
                import json

                print(json.dumps(
                    {
                        "schema": "stock-tracker-xtp-official-probe-v1",
                        "environment": environment.as_safe_dict(),
                        "module": capabilities,
                        "read_only": True,
                        "auto_trade": False,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ))
                return 0
            raise XtpSidecarContractError(
                "official callback binding remains an operational acceptance step; "
                "run --probe-official first and keep the simulator for engineering acceptance"
            )

        runtime = SidecarRuntime(args.symbols, backend="simulator", api_version=args.api_version)
        backend = SimulatorBackend(runtime, interval_sec=args.interval_sec)
        server = XtpSidecarHTTPServer(
            args.host,
            args.port,
            runtime,
            access_value=access,
            health_public=not args.private_health,
        )
        stopping = threading.Event()

        def stop_handler(signum, frame) -> None:
            del signum, frame
            if stopping.is_set():
                return
            stopping.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

        for name in ("SIGINT", "SIGTERM"):
            current = getattr(signal, name, None)
            if current is not None:
                signal.signal(current, stop_handler)

        backend.start()
        try:
            server.serve_forever(poll_interval=0.25)
        finally:
            backend.stop()
            server.server_close()
        return 0
    except XtpSidecarContractError as exc:
        print("XTP_SIDECAR_ERROR: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

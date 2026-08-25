"""Operate the tailnet-only Hybrid H0 Tailscale Serve bootstrap.

The bearer value is accepted only through STOCK_TRACKER_PRIVATE_ACCESS in the
process environment. It is never a command-line option or JSON output field.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tracker.deployment.hybrid_h0 import (
    DEFAULT_ENGINE_PORT,
    HybridH0Error,
    disable_serve,
    enable_serve,
    run_preflight,
    serve_status,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Hybrid H0 Tailscale Serve bootstrap. Set STOCK_TRACKER_PRIVATE_ACCESS "
            "in the process environment before preflight/enable."
        )
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    def add_common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--tailscale-binary", help="Optional explicit tailscale executable path")
        subparser.add_argument("--timeout-sec", type=float, default=15.0)
        subparser.add_argument("--json-output", help="Optional non-secret JSON result path")

    preflight = subparsers.add_parser("preflight", help="Read-only H0 prerequisite checks")
    add_common(preflight)
    preflight.add_argument("--port", type=int, default=DEFAULT_ENGINE_PORT)

    enable = subparsers.add_parser("enable", help="Enable tailnet-only Serve after preflight")
    add_common(enable)
    enable.add_argument("--port", type=int, default=DEFAULT_ENGINE_PORT)

    status = subparsers.add_parser("status", help="Read current Serve status")
    add_common(status)

    disable = subparsers.add_parser("disable", help="Disable the root Serve owned by H0")
    add_common(disable)
    disable.add_argument("--port", type=int, default=DEFAULT_ENGINE_PORT)

    return parser


def _write_result(result: dict[str, object], output_path: str | None) -> None:
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if output_path:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.action == "preflight":
            result = run_preflight(
                port=args.port,
                tailscale_binary=args.tailscale_binary,
                timeout_sec=args.timeout_sec,
            ).as_dict()
        elif args.action == "enable":
            result = enable_serve(
                port=args.port,
                tailscale_binary=args.tailscale_binary,
                timeout_sec=args.timeout_sec,
            )
        elif args.action == "status":
            result = serve_status(
                tailscale_binary=args.tailscale_binary,
                timeout_sec=args.timeout_sec,
            )
        else:
            result = disable_serve(
                port=args.port,
                tailscale_binary=args.tailscale_binary,
                timeout_sec=args.timeout_sec,
            )
    except HybridH0Error as exc:
        error = {
            "schema": "stock-tracker-hybrid-h0-cli-error-v1",
            "passed": False,
            "error": {"code": "HYBRID_H0_FAILED", "message": str(exc)},
            "contains_private_access": False,
        }
        _write_result(error, args.json_output)
        return 2

    _write_result(result, args.json_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

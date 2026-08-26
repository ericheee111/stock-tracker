#!/usr/bin/env python3
"""Build or verify the Hybrid H4 no-secret static site."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tracker.deployment.hybrid_h4 import (
    HybridH4Error,
    StaticBuildConfig,
    build_static_site,
    verify_static_build,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hybrid H4 deterministic static build")
    sub = parser.add_subparsers(dest="action", required=True)

    build = sub.add_parser("build")
    build.add_argument("--web-origin", required=True)
    build.add_argument("--api-origin", required=True)
    build.add_argument("--engine-id", default="stock-tracker-local")
    build.add_argument("--build-id", required=True)
    build.add_argument("--expected-api-major", type=int, default=1)
    build.add_argument("--host", choices=("cloudflare", "github"), default="cloudflare")
    build.add_argument("--health-poll-ms", type=int, default=15000)
    build.add_argument("--source", default=str(ROOT / "web"))
    build.add_argument("--output", default=str(ROOT / "build" / "hybrid-h4-static"))
    build.add_argument("--allow-loopback-http", action="store_true", help=argparse.SUPPRESS)
    build.add_argument("--json-output")

    verify = sub.add_parser("verify")
    verify.add_argument("--output", default=str(ROOT / "build" / "hybrid-h4-static"))
    verify.add_argument("--json-output")
    return parser


def _forbidden_values() -> tuple[str, ...]:
    value = os.environ.get("STOCK_TRACKER_PRIVATE_ACCESS", "")
    replacement = os.environ.get("STOCK_TRACKER_NEW_PRIVATE_ACCESS", "")
    return tuple(item for item in (value, replacement) if item)


def _emit(result: dict[str, object], output_path: str | None) -> None:
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if output_path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.action == "build":
            result = build_static_site(
                args.source,
                args.output,
                StaticBuildConfig(
                    web_origin=args.web_origin,
                    api_origin=args.api_origin,
                    engine_id=args.engine_id,
                    build_id=args.build_id,
                    expected_api_major=args.expected_api_major,
                    host=args.host,
                    health_poll_ms=args.health_poll_ms,
                    allow_loopback_http=args.allow_loopback_http,
                ),
                forbidden_values=_forbidden_values(),
            )
        else:
            result = verify_static_build(
                args.output,
                forbidden_values=_forbidden_values(),
            )
    except (HybridH4Error, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        result = {
            "schema": "stock-tracker-hybrid-h4-cli-error-v1",
            "passed": False,
            "contains_private_access": False,
            "error": {"code": "HYBRID_H4_FAILED", "message": str(exc)},
        }
        _emit(result, args.json_output)
        return 2
    _emit(result, args.json_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

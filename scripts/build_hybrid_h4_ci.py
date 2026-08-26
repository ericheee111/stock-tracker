#!/usr/bin/env python3
"""Environment-driven Hybrid H4 build for Cloudflare/GitHub CI.

All accepted variables are public deployment metadata. This script rejects the
private access environment so a CI job cannot accidentally bake a Bearer value
into the static artifact.
"""

from __future__ import annotations

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
)

_REQUIRED = (
    "STOCK_TRACKER_WEB_ORIGIN",
    "STOCK_TRACKER_API_ORIGIN",
    "STOCK_TRACKER_ENGINE_ID",
)


def _value(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise HybridH4Error(f"required public build variable is missing: {name}")
    return value


def main() -> int:
    if os.environ.get("STOCK_TRACKER_PRIVATE_ACCESS") or os.environ.get(
        "STOCK_TRACKER_NEW_PRIVATE_ACCESS"
    ):
        print(
            json.dumps(
                {
                    "passed": False,
                    "contains_private_access": False,
                    "error": {
                        "code": "PRIVATE_ACCESS_PRESENT_IN_STATIC_CI",
                        "message": "remove private access variables from the static build job",
                    },
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    try:
        for name in _REQUIRED:
            _value(name)
        build_id = (
            os.environ.get("STOCK_TRACKER_BUILD_ID")
            or os.environ.get("CF_PAGES_COMMIT_SHA")
            or os.environ.get("GITHUB_SHA")
            or "development"
        )
        host = os.environ.get("STOCK_TRACKER_STATIC_HOST", "cloudflare")
        output = os.environ.get(
            "STOCK_TRACKER_STATIC_OUTPUT",
            str(ROOT / "build" / "hybrid-h4-static"),
        )
        result = build_static_site(
            ROOT / "web",
            output,
            StaticBuildConfig(
                web_origin=_value("STOCK_TRACKER_WEB_ORIGIN"),
                api_origin=_value("STOCK_TRACKER_API_ORIGIN"),
                engine_id=_value("STOCK_TRACKER_ENGINE_ID"),
                build_id=build_id,
                expected_api_major=int(
                    os.environ.get("STOCK_TRACKER_EXPECTED_API_MAJOR", "1")
                ),
                host=host,
                health_poll_ms=int(os.environ.get("STOCK_TRACKER_HEALTH_POLL_MS", "15000")),
            ),
        )
    except (HybridH4Error, OSError, ValueError, TypeError) as exc:
        print(
            json.dumps(
                {
                    "passed": False,
                    "contains_private_access": False,
                    "error": {"code": "HYBRID_H4_CI_BUILD_FAILED", "message": str(exc)},
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

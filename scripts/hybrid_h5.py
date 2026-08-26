#!/usr/bin/env python3
"""Read-only Hybrid H5 sharing/public-access preflight."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tracker.deployment.hybrid_h5 import (
    HybridH5Error,
    PublicAccessMode,
    public_access_preflight,
)

from stock_tracker.core.config import load_configs


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fail-closed H5 preflight. This tool has no public enable action."
    )
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in PublicAccessMode],
        default=PublicAccessMode.TRUSTED_TAILNET.value,
    )
    parser.add_argument("--config-dir", default=str(ROOT / "config"))
    parser.add_argument("--acknowledge-public-exposure", action="store_true")
    parser.add_argument("--independent-review-id")
    parser.add_argument("--json-output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        bundle = load_configs(args.config_dir)
        result = public_access_preflight(
            bundle,
            mode=args.mode,
            acknowledge_public_exposure=args.acknowledge_public_exposure,
            independent_review_id=args.independent_review_id,
        )
    except (HybridH5Error, OSError, ValueError, TypeError) as exc:
        result = {
            "schema": "stock-tracker-hybrid-h5-cli-error-v1",
            "passed": False,
            "contains_private_access": False,
            "mutates_host_or_network": False,
            "error": {"code": "HYBRID_H5_PREFLIGHT_FAILED", "message": str(exc)},
        }
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.json_output:
        path = Path(args.json_output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")
    return 0 if result.get("passed") else 2


if __name__ == "__main__":
    raise SystemExit(main())

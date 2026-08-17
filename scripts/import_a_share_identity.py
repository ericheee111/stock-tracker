from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from stock_tracker.quant.data.security_universe_adapter import (
    SecurityUniverseAdapterError,
    parse_security_universe_artifact,
    read_security_universe_descriptor,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Offline A-share identity/status/universe candidate importer. "
            "It verifies captured artifact bytes and never writes SQLite."
        )
    )
    parser.add_argument(
        "--artifact",
        required=True,
        help="Path to an immutable captured JSON artifact.",
    )
    parser.add_argument(
        "--descriptor",
        required=True,
        help="Path to the checksum-bound artifact descriptor.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory for candidate JSON/JSONL and coverage report files.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        descriptor = read_security_universe_descriptor(args.descriptor)
        artifact = Path(args.artifact).read_bytes()
        bundle = parse_security_universe_artifact(artifact, descriptor)
        written = bundle.write_outputs(args.output_dir)
    except (OSError, SecurityUniverseAdapterError, ValueError) as exc:
        print(f"A-share identity import failed: {exc}", file=sys.stderr)
        return 2
    result = {
        "schema": "a-share-security-universe-import-result-v1",
        "artifact_id": descriptor.artifact_id,
        "bundle_id": bundle.bundle_id,
        "candidate_state": (
            "SYNTHETIC_FIXTURE" if descriptor.synthetic else "REAL_SOURCE_CANDIDATE"
        ),
        "complete": False,
        "verified": False,
        "trust_ceiling": "T2_CANDIDATE_EVIDENCE",
        "trust_state": "T3_NOT_REACHED",
        "database_modified": False,
        "output_files": [str(path.resolve()) for path in written],
        "coverage_report_id": bundle.coverage_report.report_id,
        "snapshot_blocked": bundle.coverage_report.has_snapshot_blockers,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

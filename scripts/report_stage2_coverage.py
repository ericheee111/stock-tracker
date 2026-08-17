from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from stock_tracker.quant.data.calendar_adapter import load_calendar_parse_descriptor
from stock_tracker.quant.data.reconciliation import (
    DEFAULT_RECONCILIATION_POLICY_VERSION,
    CalendarReconciliationInput,
    FindingSeverity,
    ReconciliationContractError,
    ReconciliationInputError,
    SecurityUniverseReconciliationInput,
    reconcile_stage2,
    write_reconciliation_json,
    write_reconciliation_markdown,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate an offline Stage 2A reconciliation and coverage-gap report."
    )
    parser.add_argument("--calendar-root", required=True)
    parser.add_argument(
        "--calendar-parse-descriptor",
        action="append",
        required=True,
        dest="calendar_parse_descriptors",
    )
    parser.add_argument(
        "--security-artifact",
        action="append",
        required=True,
        dest="security_artifacts",
    )
    parser.add_argument(
        "--security-descriptor",
        action="append",
        required=True,
        dest="security_descriptors",
    )
    parser.add_argument("--as-of", required=True)
    parser.add_argument(
        "--policy-version",
        default=DEFAULT_RECONCILIATION_POLICY_VERSION,
    )
    parser.add_argument("--json-output", required=True)
    parser.add_argument("--markdown-output", required=True)
    return parser


def _parse_as_of(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReconciliationContractError("--as-of must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReconciliationContractError("--as-of must include a timezone")
    return parsed


def _path_identity(value: str | Path) -> str:
    return os.path.normcase(str(Path(value).resolve(strict=False)))


def _validate_output_paths(
    *,
    calendar_root: str | Path,
    calendar_parse_descriptors: list[str],
    security_artifacts: list[str],
    security_descriptors: list[str],
    json_output: str | Path,
    markdown_output: str | Path,
) -> None:
    outputs = {
        _path_identity(json_output): "--json-output",
        _path_identity(markdown_output): "--markdown-output",
    }
    if len(outputs) != 2:
        raise ReconciliationContractError(
            "--json-output and --markdown-output must be different files"
        )

    inputs: dict[str, str] = {}
    for artifact in security_artifacts:
        inputs[_path_identity(artifact)] = "--security-artifact"
    for descriptor in security_descriptors:
        inputs[_path_identity(descriptor)] = "--security-descriptor"

    root = Path(calendar_root)
    for descriptor_key in calendar_parse_descriptors:
        descriptor, capture, _ = load_calendar_parse_descriptor(
            root,
            parse_descriptor_key=descriptor_key,
        )
        calendar_paths = {
            root / descriptor.parse_descriptor_key: "calendar parse descriptor",
            root / descriptor.raw_descriptor_key: "calendar raw descriptor",
            root / capture.storage_key: "calendar raw artifact",
        }
        for path, label in calendar_paths.items():
            inputs[_path_identity(path)] = label

    collisions = sorted(set(outputs).intersection(inputs))
    if collisions:
        path = collisions[0]
        raise ReconciliationContractError(
            f"report output would overwrite input ({outputs[path]} vs {inputs[path]}): {path}"
        )


def _error_payload(
    code: str,
    severity: FindingSeverity,
    message: str,
) -> str:
    return json.dumps(
        {"error_code": code, "severity": severity.value, "message": message},
        ensure_ascii=False,
        sort_keys=True,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if len(args.security_artifacts) != len(args.security_descriptors):
        print(
            _error_payload(
                "SECURITY_INPUT_PAIRING_INVALID",
                FindingSeverity.HARD_BLOCK,
                "--security-artifact and --security-descriptor counts must match",
            ),
            file=sys.stderr,
        )
        return 2
    try:
        _validate_output_paths(
            calendar_root=args.calendar_root,
            calendar_parse_descriptors=args.calendar_parse_descriptors,
            security_artifacts=args.security_artifacts,
            security_descriptors=args.security_descriptors,
            json_output=args.json_output,
            markdown_output=args.markdown_output,
        )
        calendars = tuple(
            CalendarReconciliationInput.from_parse_descriptor(
                args.calendar_root,
                descriptor_key,
            )
            for descriptor_key in args.calendar_parse_descriptors
        )
        securities = tuple(
            SecurityUniverseReconciliationInput.from_artifact_files(
                artifact,
                descriptor,
            )
            for artifact, descriptor in zip(
                args.security_artifacts,
                args.security_descriptors,
            )
        )
        report = reconcile_stage2(
            calendar_inputs=calendars,
            security_universe_inputs=securities,
            as_of=_parse_as_of(args.as_of),
            reconciliation_policy_version=args.policy_version,
        )
        write_reconciliation_json(report, Path(args.json_output))
        write_reconciliation_markdown(report, Path(args.markdown_output))
    except ReconciliationInputError as exc:
        print(_error_payload(exc.code, exc.severity, str(exc)), file=sys.stderr)
        return 2
    except (ReconciliationContractError, OSError, ValueError) as exc:
        print(
            _error_payload(
                "RECONCILIATION_GENERATION_FAILED",
                FindingSeverity.HARD_BLOCK,
                str(exc),
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "report_id": report.report_id,
                "policy_version": report.reconciliation_policy_version,
                "as_of": report.as_of.isoformat().replace("+00:00", "Z"),
                "finding_counts": report.finding_counts,
                "hard_block_count": report.finding_counts["HARD_BLOCK"],
                "trust_block_count": report.finding_counts["TRUST_BLOCK"],
                "open_inherited_blockers": list(report.open_inherited_blockers),
                "closed_with_evidence_blockers": [
                    item.code for item in report.blocker_closures
                ],
                "license_status": report.license_status,
                "evidence_tier_status": report.evidence_tier_status,
                "report_generated": True,
                "trust_passed": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

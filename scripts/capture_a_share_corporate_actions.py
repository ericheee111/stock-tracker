from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from stock_tracker.quant.data.corporate_action_adapter import (
    CORPORATE_ACTION_FIXTURE_PARSER_VERSION,
    CorporateActionAdapterError,
    CorporateActionSourceFamily,
    CorporateActionSourceOwner,
    ExtractionStatus,
    RawCorporateActionFormat,
    capture_corporate_action_raw,
    digest_request_payload,
    parse_corporate_action_from_descriptor,
    write_corporate_action_parse_descriptor,
)

_DEFAULT_CONTENT_TYPE = {
    RawCorporateActionFormat.JSON: "application/json",
    RawCorporateActionFormat.HTML: "text/html; charset=utf-8",
    RawCorporateActionFormat.PDF: "application/pdf",
    RawCorporateActionFormat.XLS: "application/vnd.ms-excel",
    RawCorporateActionFormat.XLSX: (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Offline exact-raw capture of one explicit A-share corporate-action "
            "artifact. This command does not crawl, access SQLite, verify coverage, "
            "or promote a trust tier."
        )
    )
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--input-file", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--method", choices=("GET", "POST"), default="GET")
    parser.add_argument("--request-payload-json", default="{}")
    parser.add_argument(
        "--source-owner",
        choices=tuple(CorporateActionSourceOwner),
        required=True,
    )
    parser.add_argument(
        "--source-family",
        choices=tuple(CorporateActionSourceFamily),
        required=True,
    )
    parser.add_argument("--source-version", required=True)
    parser.add_argument(
        "--raw-format",
        choices=tuple(RawCorporateActionFormat),
        required=True,
    )
    parser.add_argument("--content-type")
    parser.add_argument(
        "--parser-version",
        default=CORPORATE_ACTION_FIXTURE_PARSER_VERSION,
    )
    return parser


def _request_digest(value: str) -> str:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise CorporateActionAdapterError(
            "--request-payload-json must be valid JSON"
        ) from exc
    if not isinstance(payload, dict) or any(type(key) is not str for key in payload):
        raise CorporateActionAdapterError(
            "--request-payload-json must be a JSON object with string keys"
        )
    return digest_request_payload(payload)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        input_path = Path(args.input_file).expanduser().resolve(strict=True)
        if not input_path.is_file():
            raise CorporateActionAdapterError("--input-file must be a regular file")
        output_root = Path(args.output_root).expanduser()
        if output_root.exists() and not output_root.is_dir():
            raise CorporateActionAdapterError("--output-root must be a directory")
        if input_path == output_root.resolve(strict=False):
            raise CorporateActionAdapterError(
                "--input-file and --output-root cannot be the same path"
            )

        raw_format = RawCorporateActionFormat(args.raw_format)
        content_type = args.content_type or _DEFAULT_CONTENT_TYPE[raw_format]
        retrieved_at = datetime.now(timezone.utc)
        capture = capture_corporate_action_raw(
            output_root,
            raw_bytes=input_path.read_bytes(),
            request_url=args.url,
            request_method=args.method,
            request_payload_digest=_request_digest(args.request_payload_json),
            response_status=200,
            response_headers={"Content-Type": content_type},
            redirect_chain=(),
            retrieved_at=retrieved_at,
            source_owner=CorporateActionSourceOwner(args.source_owner),
            source_family=CorporateActionSourceFamily(args.source_family),
            source_version=args.source_version,
            raw_format=raw_format,
        )
        parse_descriptor = write_corporate_action_parse_descriptor(
            output_root,
            capture=capture,
            parser_version=args.parser_version,
        )
        candidate_count = 0
        document_id: str | None = None
        if parse_descriptor.extraction_status is ExtractionStatus.PARSED:
            document = parse_corporate_action_from_descriptor(
                output_root,
                parse_descriptor_key=parse_descriptor.parse_descriptor_key,
            )
            candidate_count = len(document.candidates)
            document_id = document.document_id
        print(
            json.dumps(
                {
                    "schema": "stage2c-corporate-action-offline-capture-v1",
                    "artifact_id": capture.artifact_id,
                    "raw_descriptor_id": capture.descriptor_id,
                    "raw_descriptor_key": capture.descriptor_key,
                    "parse_descriptor_id": parse_descriptor.parse_descriptor_id,
                    "parse_descriptor_key": parse_descriptor.parse_descriptor_key,
                    "document_id": document_id,
                    "candidate_count": candidate_count,
                    "extraction_status": parse_descriptor.extraction_status.value,
                    "gaps": list(parse_descriptor.gaps),
                    "source_owner": capture.source_owner.value,
                    "source_family": capture.source_family.value,
                    "source_version": capture.source_version,
                    "raw_format": capture.raw_format.value,
                    "retrieved_at": capture.retrieved_at.isoformat().replace(
                        "+00:00",
                        "Z",
                    ),
                    "evidence_boundary": (
                        "CONTRACT_ONLY / SYNTHETIC_VALIDATED / "
                        "LICENSE_PENDING / T3_NOT_REACHED"
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        )
        return 0
    except (
        CorporateActionAdapterError,
        OSError,
        ValueError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from stock_tracker.quant.data.corporate_action_adapter import (  # noqa: E402
    load_corporate_action_raw,
)
from stock_tracker.quant.data.corporate_action_extraction import (  # noqa: E402
    CorporateActionExtractionError,
    ExtractionMethod,
    parse_frozen_html_document,
    parse_structured_extraction_document,
    write_extraction_descriptor,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Offline Stage 2D extraction from an existing immutable Stage 2C "
            "corporate-action raw descriptor. No network, SQLite, trust, or "
            "promotion operation is available."
        )
    )
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--raw-descriptor-key", required=True)
    parser.add_argument("--extraction-input", required=True)
    parser.add_argument(
        "--method",
        choices=tuple(ExtractionMethod),
        required=True,
    )
    parser.add_argument("--extractor-version", required=True)
    parser.add_argument("--reviewer-note", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = Path(args.artifact_root).expanduser()
        if not root.is_dir():
            raise CorporateActionExtractionError(
                "--artifact-root must be an existing directory"
            )
        input_path = Path(args.extraction_input).expanduser().resolve(strict=True)
        if not input_path.is_file():
            raise CorporateActionExtractionError(
                "--extraction-input must be a regular file"
            )
        try:
            input_path.relative_to(root.resolve())
        except ValueError:
            pass
        else:
            raise CorporateActionExtractionError(
                "extraction input cannot be inside the artifact output root"
            )
        capture, raw_bytes = load_corporate_action_raw(
            root,
            descriptor_key=args.raw_descriptor_key,
        )
        method = ExtractionMethod(args.method)
        extracted_at = datetime.now(timezone.utc)
        payload = input_path.read_bytes()
        if method is ExtractionMethod.FROZEN_HTML_TABLE:
            if payload != raw_bytes:
                raise CorporateActionExtractionError(
                    "frozen HTML extraction input must equal exact captured bytes"
                )
            document = parse_frozen_html_document(
                payload,
                capture=capture,
                extractor_version=args.extractor_version,
                reviewer_note=args.reviewer_note,
                extracted_at=extracted_at,
            )
        else:
            document = parse_structured_extraction_document(
                payload,
                capture=capture,
                extraction_method=method,
                extracted_at=extracted_at,
            )
        descriptor = write_extraction_descriptor(
            root,
            capture=capture,
            extraction_payload=payload,
            document=document,
        )
        print(
            json.dumps(
                {
                    "schema": "stage2d-offline-extraction-result-v1",
                    "raw_artifact_id": capture.artifact_id,
                    "raw_descriptor_id": capture.descriptor_id,
                    "extraction_descriptor_id": descriptor.descriptor_id,
                    "extraction_descriptor_key": descriptor.descriptor_key,
                    "extracted_document_id": document.document_id,
                    "row_count": len(document.rows),
                    "gaps": list(document.gaps),
                    "evidence_boundary": (
                        "CANDIDATE_ONLY / LICENSE_PENDING / T3_NOT_REACHED"
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        )
        return 0
    except (CorporateActionExtractionError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

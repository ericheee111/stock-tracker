from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from stock_tracker.quant.data.calendar_adapter import (
    CALENDAR_HTML_PARSER_VERSION,
    CalendarAdapterError,
    CalendarCoverageMode,
    CalendarProvenance,
    CalendarSourceFamily,
    Exchange,
    NoticeType,
    PublishedGranularity,
    RawCalendarFormat,
    RedirectHop,
    capture_calendar_raw,
    digest_request_payload,
    parse_calendar_from_descriptor,
    write_calendar_parse_descriptor,
)


class _RedirectRecorder(HTTPRedirectHandler):
    def __init__(self) -> None:
        super().__init__()
        self.hops: list[RedirectHop] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.hops.append(
            RedirectHop(status=code, from_url=req.full_url, to_url=newurl)
        )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _parse_iso_datetime(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CalendarAdapterError(f"{name} must be ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CalendarAdapterError(f"{name} must include timezone")
    return parsed


def _request_parts(args: argparse.Namespace) -> tuple[str, bytes | None, str]:
    try:
        parameters = json.loads(args.params_json)
    except json.JSONDecodeError as exc:
        raise CalendarAdapterError("--params-json must be valid JSON") from exc
    if not isinstance(parameters, dict) or any(
        type(key) is not str for key in parameters
    ):
        raise CalendarAdapterError("--params-json must be a JSON object")
    query = urlencode(parameters, doseq=True)
    url = args.url + (("&" if "?" in args.url else "?") + query if query else "")
    body = Path(args.body_file).read_bytes() if args.body_file else None
    if args.method == "GET" and body is not None:
        raise CalendarAdapterError("GET cannot use --body-file")
    request_identity: dict[str, object] = {"parameters": parameters}
    if body is not None:
        request_identity["body_sha256"] = digest_request_payload(body)
    return url, body, digest_request_payload(request_identity)


def _fetch(
    url: str,
    method: str,
    body: bytes | None,
    timeout: float,
) -> tuple[bytes, int, dict[str, str], tuple[RedirectHop, ...]]:
    recorder = _RedirectRecorder()
    opener = build_opener(recorder)
    request = Request(
        url,
        data=body,
        method=method,
        headers={"User-Agent": "stock-tracker-calendar-audit/1"},
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            return (
                response.read(),
                int(response.status),
                dict(response.headers.items()),
                tuple(recorder.hops),
            )
    except HTTPError as exc:
        return exc.read(), int(exc.code), dict(exc.headers.items()), tuple(recorder.hops)


def _source_published(
    value: str | None,
    granularity: PublishedGranularity,
) -> date | datetime | None:
    if granularity is PublishedGranularity.UNKNOWN:
        if value is not None:
            raise CalendarAdapterError(
                "--source-published-at must be omitted for UNKNOWN granularity"
            )
        return None
    if value is None:
        raise CalendarAdapterError(
            "--source-published-at is required for DATE or SECOND granularity"
        )
    if granularity is PublishedGranularity.DATE:
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise CalendarAdapterError(
                "DATE source publication must be YYYY-MM-DD"
            ) from exc
    return _parse_iso_datetime(value, "source_published_at")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture exact SSE/SZSE calendar raw bytes into an explicit root."
    )
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--method", choices=("GET", "POST"), default="GET")
    parser.add_argument("--params-json", default="{}")
    parser.add_argument("--body-file")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--source-owner", choices=tuple(Exchange), required=True)
    parser.add_argument(
        "--source-family",
        choices=tuple(CalendarSourceFamily),
        required=True,
    )
    parser.add_argument("--source-version", required=True)
    parser.add_argument(
        "--parser-version",
        default=CALENDAR_HTML_PARSER_VERSION,
    )
    parser.add_argument("--raw-format", choices=tuple(RawCalendarFormat), required=True)
    parser.add_argument("--notice-id", required=True)
    parser.add_argument("--notice-type", choices=tuple(NoticeType), required=True)
    parser.add_argument(
        "--source-published-granularity",
        choices=tuple(PublishedGranularity),
        required=True,
    )
    parser.add_argument("--source-published-at")
    parser.add_argument("--usable-from", required=True)
    parser.add_argument("--effective-from", required=True)
    parser.add_argument("--effective-to", required=True)
    parser.add_argument("--revision-id", required=True)
    parser.add_argument("--supersedes-revision-id")
    parser.add_argument(
        "--coverage-mode",
        choices=tuple(CalendarCoverageMode),
        required=True,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.timeout <= 0:
            raise CalendarAdapterError("--timeout must be positive")
        request_url, body, request_digest = _request_parts(args)
        raw_bytes, status, headers, redirects = _fetch(
            request_url,
            args.method,
            body,
            args.timeout,
        )
        retrieved_at = datetime.now(timezone.utc)
        capture = capture_calendar_raw(
            args.output_root,
            raw_bytes=raw_bytes,
            request_url=args.url,
            request_method=args.method,
            request_payload_digest=request_digest,
            response_status=status,
            response_headers=headers,
            redirect_chain=redirects,
            retrieved_at=retrieved_at,
            source_owner=Exchange(args.source_owner),
            source_family=CalendarSourceFamily(args.source_family),
            source_version=args.source_version,
            parser_version=args.parser_version,
            raw_format=RawCalendarFormat(args.raw_format),
        )
        gaps: tuple[str, ...]
        document_id: str | None = None
        parse_descriptor_id: str | None = None
        parse_descriptor_key: str | None = None
        if capture.raw_format is RawCalendarFormat.HTML:
            # Stage 2A has no trusted external timestamp authority capable of
            # proving an earlier first-observed/known time. Exact raw capture
            # time is therefore the conservative PIT knowledge boundary.
            observed_at = retrieved_at
            known_at = retrieved_at
            granularity = PublishedGranularity(
                args.source_published_granularity
            )
            provenance = CalendarProvenance(
                exchange=Exchange(args.source_owner),
                source_owner=Exchange(args.source_owner),
                source_family=CalendarSourceFamily(args.source_family),
                source_version=args.source_version,
                notice_id=args.notice_id,
                notice_type=NoticeType(args.notice_type),
                source_uri=args.url,
                source_published_at=_source_published(
                    args.source_published_at,
                    granularity,
                ),
                source_published_granularity=granularity,
                observed_at=observed_at,
                retrieved_at=retrieved_at,
                known_at=known_at,
                usable_from=_parse_iso_datetime(args.usable_from, "usable_from"),
                effective_from=date.fromisoformat(args.effective_from),
                effective_to=date.fromisoformat(args.effective_to),
                revision_id=args.revision_id,
                supersedes_revision_id=args.supersedes_revision_id,
                raw_artifact_id=capture.artifact_id,
                response_status=capture.response_status,
                content_type=capture.response_headers["content-type"],
                coverage_mode=CalendarCoverageMode(args.coverage_mode),
            )
            parse_descriptor = write_calendar_parse_descriptor(
                args.output_root,
                capture=capture,
                provenance=provenance,
                parser_version=args.parser_version,
            )
            document = parse_calendar_from_descriptor(
                args.output_root,
                parse_descriptor_key=parse_descriptor.parse_descriptor_key,
            )
            gaps = document.gaps
            document_id = document.document_id
            parse_descriptor_id = parse_descriptor.parse_descriptor_id
            parse_descriptor_key = parse_descriptor.parse_descriptor_key
        else:
            gaps = (f"PARSER_NOT_IMPLEMENTED_FOR_{capture.raw_format.value}",)
        print(
            json.dumps(
                {
                    "artifact_id": capture.artifact_id,
                    "descriptor_id": capture.descriptor_id,
                    "descriptor_key": capture.descriptor_key,
                    "parse_descriptor_id": parse_descriptor_id,
                    "parse_descriptor_key": parse_descriptor_key,
                    "document_id": document_id,
                    "source": (
                        f"{capture.source_owner.value}/{capture.source_family.value}"
                    ),
                    "retrieved_at": capture.retrieved_at.isoformat(),
                    "gap_status": "HAS_GAPS" if gaps else "NO_REPORTED_GAPS",
                    "gaps": list(gaps),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except (CalendarAdapterError, OSError, URLError, ValueError) as exc:
        print(f"calendar capture failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

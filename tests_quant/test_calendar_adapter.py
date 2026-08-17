from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError
from zoneinfo import ZoneInfo

from scripts import capture_a_share_calendar as capture_cli
from stock_tracker.core.types import Market
from stock_tracker.quant.core.calendar import (
    CalendarStatus,
    SessionKind,
    TradingCalendar,
)
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
    assemble_calendar_candidates,
    capture_calendar_raw,
    digest_request_payload,
    load_calendar_parse_descriptor,
    load_calendar_raw,
    parse_calendar_document,
    parse_calendar_from_descriptor,
    write_calendar_parse_descriptor,
)


FIXTURES = Path(__file__).parent / "fixtures" / "calendar"
SSE_URL = (
    "https://www.sse.com.cn/disclosure/announcement/general/"
    "c/c_20231201_00000001.shtml"
)


class TestCalendarAdapter(unittest.TestCase):
    def raw(self, name: str) -> bytes:
        return (FIXTURES / name).read_bytes()

    def capture(
        self,
        root: str | Path,
        raw: bytes,
        *,
        url: str = SSE_URL,
        parser_version: str = CALENDAR_HTML_PARSER_VERSION,
        raw_format: RawCalendarFormat = RawCalendarFormat.HTML,
        content_type: str = "text/html; charset=utf-8",
        source_family: CalendarSourceFamily = (
            CalendarSourceFamily.SSE_OFFICIAL_NOTICE_DETAIL
        ),
    ):
        return capture_calendar_raw(
            root,
            raw_bytes=raw,
            request_url=url,
            request_method="GET",
            request_payload_digest=digest_request_payload({"year": 2024}),
            response_status=200,
            response_headers={
                "Content-Type": content_type,
                "Date": "Fri, 01 Dec 2023 08:00:00 GMT",
                "ETag": '"fixture"',
                "X-Ignored": "not persisted",
            },
            redirect_chain=(
                RedirectHop(
                    302,
                    "https://www.sse.com.cn/disclosure/announcement/general/old",
                    url,
                ),
            ),
            retrieved_at=datetime(2023, 12, 2, tzinfo=timezone.utc),
            source_owner=Exchange.SSE,
            source_family=source_family,
            source_version="sse-calendar-2024-v1",
            parser_version=parser_version,
            raw_format=raw_format,
        )

    def provenance(
        self,
        raw: bytes,
        *,
        notice_id: str = "annual-2024",
        notice_type: NoticeType = NoticeType.ANNUAL,
        effective_from: date = date(2024, 1, 1),
        effective_to: date = date(2024, 1, 7),
        known_at: datetime = datetime(2023, 12, 2, tzinfo=timezone.utc),
        usable_from: datetime = datetime(2023, 12, 4, tzinfo=timezone.utc),
        revision_id: str = "annual-r1",
        supersedes_revision_id: str | None = None,
        source_version: str = "sse-calendar-2024-v1",
        source_owner: Exchange = Exchange.SSE,
        source_family: CalendarSourceFamily = (
            CalendarSourceFamily.SSE_OFFICIAL_NOTICE_DETAIL
        ),
        source_uri: str = SSE_URL,
        response_status: int = 200,
        content_type: str = "text/html; charset=utf-8",
        coverage_mode: CalendarCoverageMode = CalendarCoverageMode.EXPLICIT_DAILY,
        source_published_at: date | datetime | None = date(2023, 12, 1),
        source_published_granularity: PublishedGranularity = (
            PublishedGranularity.DATE
        ),
    ) -> CalendarProvenance:
        return CalendarProvenance(
            exchange=source_owner,
            source_owner=source_owner,
            source_family=source_family,
            source_version=source_version,
            notice_id=notice_id,
            notice_type=notice_type,
            source_uri=source_uri,
            source_published_at=source_published_at,
            source_published_granularity=source_published_granularity,
            observed_at=known_at,
            retrieved_at=known_at,
            known_at=known_at,
            usable_from=usable_from,
            effective_from=effective_from,
            effective_to=effective_to,
            revision_id=revision_id,
            supersedes_revision_id=supersedes_revision_id,
            raw_artifact_id=digest_request_payload(raw),
            response_status=response_status,
            content_type=content_type,
            coverage_mode=coverage_mode,
        )

    def parse(
        self,
        fixture: str,
        **provenance_overrides,
    ):
        raw = self.raw(fixture)
        return parse_calendar_document(
            raw,
            provenance=self.provenance(raw, **provenance_overrides),
            parser_version=CALENDAR_HTML_PARSER_VERSION,
        )

    def test_complete_calendar_covers_every_civil_date(self) -> None:
        document = self.parse("full_calendar.html")
        result = assemble_calendar_candidates((document,))
        latest = {fact.civil_date: fact for fact in result.candidate_facts}
        self.assertEqual(len(latest), 7)
        self.assertEqual(latest[date(2024, 1, 1)].status, CalendarStatus.CLOSED)
        self.assertEqual(latest[date(2024, 1, 6)].status, CalendarStatus.CLOSED)
        self.assertEqual(latest[date(2024, 1, 4)].session_kind, SessionKind.HALF_DAY)
        self.assertEqual(latest[date(2024, 1, 4)].close_time.hour, 11)
        self.assertEqual(
            getattr(latest[date(2024, 1, 2)].open_time.tzinfo, "key", None),
            "Asia/Shanghai",
        )
        self.assertFalse(result.coverage.verified)
        self.assertTrue(all(not day.verified for day in result.days))

    def test_annual_and_later_holiday_notice_preserve_pit_visibility(self) -> None:
        annual = self.parse(
            "annual_calendar.html",
            coverage_mode=CalendarCoverageMode.ANNUAL_EXCEPTIONS,
        )
        holiday = self.parse(
            "holiday_notice.html",
            notice_id="holiday-jan-3",
            notice_type=NoticeType.HOLIDAY,
            effective_from=date(2024, 1, 3),
            effective_to=date(2024, 1, 3),
            known_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
            usable_from=datetime(2024, 1, 3, tzinfo=timezone.utc),
            revision_id="holiday-r1",
            supersedes_revision_id="annual-r1",
        )
        result = assemble_calendar_candidates((holiday, annual))
        calendar = TradingCalendar((result.coverage,), result.days)
        before = calendar.snapshot(
            Market.A,
            date(2024, 1, 3),
            date(2024, 1, 3),
            datetime(2024, 1, 2, 12, tzinfo=timezone.utc),
            require_verified=False,
        )
        after = calendar.snapshot(
            Market.A,
            date(2024, 1, 3),
            date(2024, 1, 3),
            datetime(2024, 1, 3, 1, tzinfo=timezone.utc),
            require_verified=False,
        )
        self.assertEqual(before.days[0].status, CalendarStatus.OPEN)
        self.assertEqual(after.days[0].status, CalendarStatus.CLOSED)

    def test_temporary_revision_is_append_only_and_lower_priority_cannot_replace_it(self) -> None:
        annual = self.parse(
            "annual_calendar.html",
            coverage_mode=CalendarCoverageMode.ANNUAL_EXCEPTIONS,
        )
        temporary = self.parse(
            "temporary_revision.html",
            notice_id="temporary-jan-4",
            notice_type=NoticeType.TEMPORARY,
            effective_from=date(2024, 1, 4),
            effective_to=date(2024, 1, 4),
            known_at=datetime(2024, 1, 3, tzinfo=timezone.utc),
            usable_from=datetime(2024, 1, 4, tzinfo=timezone.utc),
            revision_id="temporary-r1",
            supersedes_revision_id="annual-r1",
        )
        result = assemble_calendar_candidates((annual, temporary))
        revisions = [
            fact
            for fact in result.candidate_facts
            if fact.civil_date == date(2024, 1, 4)
        ]
        self.assertEqual([item.revision_id for item in revisions], ["annual-r1", "temporary-r1"])

        lower_priority = replace(
            temporary,
            provenance=replace(
                temporary.provenance,
                notice_type=NoticeType.HOLIDAY,
                revision_id="holiday-late",
                supersedes_revision_id="temporary-r1",
                known_at=datetime(2024, 1, 5, tzinfo=timezone.utc),
                observed_at=datetime(2024, 1, 5, tzinfo=timezone.utc),
                retrieved_at=datetime(2024, 1, 5, tzinfo=timezone.utc),
                usable_from=datetime(2024, 1, 6, tzinfo=timezone.utc),
            ),
            facts=tuple(
                replace(
                    fact,
                    notice_type=NoticeType.HOLIDAY,
                    revision_id="holiday-late",
                    supersedes_revision_id="temporary-r1",
                    known_at=datetime(2024, 1, 5, tzinfo=timezone.utc),
                    observed_at=datetime(2024, 1, 5, tzinfo=timezone.utc),
                    usable_from=datetime(2024, 1, 6, tzinfo=timezone.utc),
                )
                for fact in temporary.facts
            ),
        )
        with self.assertRaisesRegex(CalendarAdapterError, "lower-priority"):
            assemble_calendar_candidates((annual, temporary, lower_priority))

    def test_same_url_changed_bytes_create_new_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = self.capture(directory, self.raw("annual_calendar.html"))
            second = self.capture(directory, self.raw("holiday_notice.html"))
            self.assertNotEqual(first.artifact_id, second.artifact_id)
            self.assertNotEqual(first.storage_key, second.storage_key)
            self.assertNotEqual(first.descriptor_id, second.descriptor_id)
            self.assertTrue((Path(directory) / first.storage_key).is_file())
            self.assertTrue((Path(directory) / second.storage_key).is_file())

    def test_capture_binds_request_response_and_redirect_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            captured = self.capture(directory, self.raw("annual_calendar.html"))
            loaded, raw = load_calendar_raw(
                directory,
                descriptor_key=captured.descriptor_key,
            )
            self.assertEqual(raw, self.raw("annual_calendar.html"))
            self.assertEqual(loaded.request_method, "GET")
            self.assertEqual(loaded.response_status, 200)
            self.assertEqual(loaded.content_type, "text/html")
            self.assertEqual(loaded.response_headers["etag"], '"fixture"')
            self.assertNotIn("x-ignored", loaded.response_headers)
            self.assertEqual(len(loaded.redirect_chain), 1)
            self.assertEqual(loaded.byte_length, len(raw))

    def test_redirect_chain_cannot_leave_official_owner_domain(self) -> None:
        raw = self.raw("annual_calendar.html")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(CalendarAdapterError, "official HTTPS SSE domain"):
                capture_calendar_raw(
                    directory,
                    raw_bytes=raw,
                    request_url=SSE_URL,
                    request_method="GET",
                    request_payload_digest=digest_request_payload({"year": 2024}),
                    response_status=200,
                    response_headers={"Content-Type": "text/html; charset=utf-8"},
                    redirect_chain=(
                        RedirectHop(302, SSE_URL, "https://example.com/calendar.html"),
                    ),
                    retrieved_at=datetime(2023, 12, 2, tzinfo=timezone.utc),
                    source_owner=Exchange.SSE,
                    source_family=CalendarSourceFamily.SSE_OFFICIAL_NOTICE_DETAIL,
                    source_version="sse-calendar-2024-v1",
                    parser_version=CALENDAR_HTML_PARSER_VERSION,
                    raw_format=RawCalendarFormat.HTML,
                )

    def test_parse_descriptor_round_trip_replays_identical_document(self) -> None:
        raw = self.raw("full_calendar.html")
        with tempfile.TemporaryDirectory() as directory:
            capture = self.capture(directory, raw)
            provenance = replace(
                self.provenance(raw),
                observed_at=capture.retrieved_at,
                retrieved_at=capture.retrieved_at,
                known_at=capture.retrieved_at,
                usable_from=datetime(2023, 12, 4, tzinfo=timezone.utc),
            )
            parse_descriptor = write_calendar_parse_descriptor(
                directory,
                capture=capture,
                provenance=provenance,
                parser_version=CALENDAR_HTML_PARSER_VERSION,
            )
            loaded, loaded_capture, loaded_raw = load_calendar_parse_descriptor(
                directory,
                parse_descriptor_key=parse_descriptor.parse_descriptor_key,
            )
            replay = parse_calendar_from_descriptor(
                directory,
                parse_descriptor_key=parse_descriptor.parse_descriptor_key,
            )
            direct = parse_calendar_document(
                raw,
                provenance=provenance,
                parser_version=CALENDAR_HTML_PARSER_VERSION,
            )
            self.assertEqual(loaded.parse_descriptor_id, parse_descriptor.parse_descriptor_id)
            self.assertEqual(loaded_capture.descriptor_id, capture.descriptor_id)
            self.assertEqual(loaded_raw, raw)
            self.assertEqual(replay.document_id, direct.document_id)
            self.assertEqual(replay.facts, direct.facts)

    def test_same_raw_with_different_parse_provenance_reuses_raw_only(self) -> None:
        raw = self.raw("full_calendar.html")
        with tempfile.TemporaryDirectory() as directory:
            capture = self.capture(directory, raw)
            base = replace(
                self.provenance(raw),
                observed_at=capture.retrieved_at,
                retrieved_at=capture.retrieved_at,
                known_at=capture.retrieved_at,
                usable_from=datetime(2023, 12, 4, tzinfo=timezone.utc),
            )
            first = write_calendar_parse_descriptor(
                directory,
                capture=capture,
                provenance=base,
                parser_version=CALENDAR_HTML_PARSER_VERSION,
            )
            second = write_calendar_parse_descriptor(
                directory,
                capture=capture,
                provenance=replace(base, notice_id="annual-2024-reindexed"),
                parser_version=CALENDAR_HTML_PARSER_VERSION,
            )
            self.assertEqual(first.raw_artifact_id, second.raw_artifact_id)
            self.assertEqual(first.raw_descriptor_id, second.raw_descriptor_id)
            self.assertNotEqual(first.parse_descriptor_id, second.parse_descriptor_id)

    def test_parse_descriptor_tamper_is_rejected(self) -> None:
        raw = self.raw("full_calendar.html")
        with tempfile.TemporaryDirectory() as directory:
            capture = self.capture(directory, raw)
            provenance = replace(
                self.provenance(raw),
                observed_at=capture.retrieved_at,
                retrieved_at=capture.retrieved_at,
                known_at=capture.retrieved_at,
                usable_from=datetime(2023, 12, 4, tzinfo=timezone.utc),
            )
            descriptor = write_calendar_parse_descriptor(
                directory,
                capture=capture,
                provenance=provenance,
                parser_version=CALENDAR_HTML_PARSER_VERSION,
            )
            path = Path(directory) / descriptor.parse_descriptor_key
            value = json.loads(path.read_text(encoding="utf-8"))
            value["provenance"]["notice_id"] = "tampered"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(CalendarAdapterError, "parse_descriptor_id"):
                load_calendar_parse_descriptor(
                    directory,
                    parse_descriptor_key=descriptor.parse_descriptor_key,
                )

    def test_attachment_and_notice_html_are_independent_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            html = self.capture(directory, self.raw("annual_calendar.html"))
            pdf = self.capture(
                directory,
                b"%PDF-1.7\nfixture attachment\n%%EOF\n",
                url="https://www.sse.com.cn/files/calendar-2024.pdf",
                raw_format=RawCalendarFormat.PDF,
                content_type="application/pdf",
                source_family=CalendarSourceFamily.SSE_OFFICIAL_NOTICE_ATTACHMENT,
            )
            self.assertNotEqual(html.artifact_id, pdf.artifact_id)
            self.assertTrue(html.storage_key.endswith(".html"))
            self.assertTrue(pdf.storage_key.endswith(".pdf"))

    def test_html_error_page_and_http_error_fail_closed(self) -> None:
        raw = self.raw("error_page.html")
        for status, message in ((200, "HTML error page"), (503, "HTTP error")):
            with self.subTest(status=status):
                provenance = self.provenance(raw, response_status=status)
                with self.assertRaisesRegex(CalendarAdapterError, message):
                    parse_calendar_document(
                        raw,
                        provenance=provenance,
                        parser_version=CALENDAR_HTML_PARSER_VERSION,
                    )

    def test_missing_duplicate_unordered_and_unknown_fields_fail_closed(self) -> None:
        cases = (
            ("missing_day.html", "missing civil dates"),
            ("duplicate_date.html", "duplicate calendar date"),
            ("unordered_dates.html", "strictly chronological"),
            ("unknown_field.html", "unknown fields"),
        )
        for fixture, message in cases:
            with self.subTest(fixture=fixture):
                raw = self.raw(fixture)
                end = date(2024, 1, 3) if fixture == "missing_day.html" else date(2024, 1, 2)
                if fixture in {"duplicate_date.html", "unknown_field.html"}:
                    end = date(2024, 1, 1)
                with self.assertRaisesRegex(CalendarAdapterError, message):
                    parse_calendar_document(
                        raw,
                        provenance=self.provenance(raw, effective_to=end),
                        parser_version=CALENDAR_HTML_PARSER_VERSION,
                    )

    def test_wrong_timezone_is_rejected(self) -> None:
        raw = self.raw("full_calendar.html")
        with self.assertRaisesRegex(CalendarAdapterError, "retrieved_at must be represented in UTC"):
            replace(
                self.provenance(raw),
                retrieved_at=datetime(2024, 1, 10, tzinfo=ZoneInfo("Asia/Shanghai")),
            )

    def test_date_publication_does_not_fabricate_second_precision(self) -> None:
        document = self.parse(
            "full_calendar.html",
            known_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
            usable_from=datetime(2024, 1, 3, tzinfo=timezone.utc),
            source_published_at=date(2023, 12, 31),
            source_published_granularity=PublishedGranularity.DATE,
        )
        fact = document.facts[0]
        self.assertIs(type(fact.source_published_at), date)
        self.assertEqual(fact.source_published_granularity, PublishedGranularity.DATE)
        self.assertEqual(fact.known_at, datetime(2024, 1, 2, tzinfo=timezone.utc))
        self.assertEqual(fact.usable_from, datetime(2024, 1, 3, tzinfo=timezone.utc))

    def test_date_granularity_rejects_fabricated_midnight_datetime(self) -> None:
        raw = self.raw("full_calendar.html")
        with self.assertRaisesRegex(CalendarAdapterError, "without fabricated time"):
            self.provenance(
                raw,
                source_published_at=datetime(2023, 12, 1, tzinfo=timezone.utc),
                source_published_granularity=PublishedGranularity.DATE,
            )

    def test_known_at_must_not_follow_usable_policy_boundary(self) -> None:
        raw = self.raw("full_calendar.html")
        with self.assertRaisesRegex(CalendarAdapterError, "usable_from cannot precede"):
            self.provenance(
                raw,
                known_at=datetime(2024, 1, 3, tzinfo=timezone.utc),
                usable_from=datetime(2024, 1, 2, tzinfo=timezone.utc),
            )

    def test_known_at_cannot_be_backfilled_before_first_observation(self) -> None:
        raw = self.raw("full_calendar.html")
        base = self.provenance(raw)
        with self.assertRaisesRegex(CalendarAdapterError, "known_at == observed_at"):
            replace(
                base,
                known_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
                usable_from=base.usable_from,
            )

    def test_cross_family_documents_cannot_be_assembled_into_one_core_stream(self) -> None:
        detail = self.parse("full_calendar.html")
        attachment = self.parse(
            "full_calendar.html",
            source_family=CalendarSourceFamily.SSE_OFFICIAL_NOTICE_ATTACHMENT,
        )
        with self.assertRaisesRegex(CalendarAdapterError, "source families"):
            assemble_calendar_candidates((detail, attachment))

    def test_cross_calendar_source_or_version_is_rejected(self) -> None:
        annual = self.parse("full_calendar.html")
        raw = self.raw("holiday_notice.html")
        other_version = parse_calendar_document(
            raw,
            provenance=self.provenance(
                raw,
                notice_type=NoticeType.HOLIDAY,
                effective_from=date(2024, 1, 3),
                effective_to=date(2024, 1, 3),
                revision_id="holiday-r1",
                supersedes_revision_id="annual-r1",
                source_version="sse-calendar-2024-v2",
            ),
            parser_version=CALENDAR_HTML_PARSER_VERSION,
        )
        with self.assertRaisesRegex(CalendarAdapterError, "source/version"):
            assemble_calendar_candidates((annual, other_version))

    def test_raw_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            captured = self.capture(directory, self.raw("annual_calendar.html"))
            raw_path = Path(directory) / captured.storage_key
            raw = raw_path.read_bytes()
            raw_path.write_bytes(b"X" + raw[1:])
            with self.assertRaisesRegex(CalendarAdapterError, "hash changed"):
                load_calendar_raw(directory, descriptor_key=captured.descriptor_key)

    def test_parser_version_changes_descriptor_but_not_exact_raw(self) -> None:
        raw = self.raw("annual_calendar.html")
        with tempfile.TemporaryDirectory() as directory:
            first = self.capture(directory, raw, parser_version="parser-v1")
            second = self.capture(directory, raw, parser_version="parser-v2")
            self.assertEqual(first.artifact_id, second.artifact_id)
            self.assertEqual(first.storage_key, second.storage_key)
            self.assertNotEqual(first.descriptor_id, second.descriptor_id)

    def test_unknown_descriptor_fields_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            captured = self.capture(directory, self.raw("annual_calendar.html"))
            descriptor = Path(directory) / captured.descriptor_key
            value = json.loads(descriptor.read_text(encoding="utf-8"))
            value["verified"] = True
            descriptor.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(CalendarAdapterError, "unknown fields: verified"):
                load_calendar_raw(directory, descriptor_key=captured.descriptor_key)

    def test_adapter_result_has_no_complete_or_trust_tier_surface(self) -> None:
        result = assemble_calendar_candidates((self.parse("full_calendar.html"),))
        self.assertNotIn("complete", result.__dataclass_fields__)
        self.assertNotIn("trust_tier", result.__dataclass_fields__)
        self.assertFalse(result.coverage.verified)
        self.assertIn("LICENSE_PENDING", result.gaps)
        self.assertIn("SINGLE_SOURCE_NOT_RECONCILED", result.gaps)

    def test_cli_requires_output_root_and_network_failure_is_nonzero(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                capture_cli.main(["--url", SSE_URL])
        argv = self.cli_argv("D:/explicit-fixture-output")
        with patch.object(capture_cli, "_fetch", side_effect=URLError("offline")):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(capture_cli.main(argv), 1)

    def test_cli_offline_capture_reports_required_identity_and_gaps(self) -> None:
        raw = self.raw("annual_calendar.html")
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(
                capture_cli,
                "_fetch",
                return_value=(
                    raw,
                    200,
                    {"Content-Type": "text/html; charset=utf-8"},
                    (),
                ),
            ):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    exit_code = capture_cli.main(self.cli_argv(directory))
            self.assertEqual(exit_code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(len(payload["artifact_id"]), 64)
            self.assertEqual(len(payload["descriptor_id"]), 64)
            self.assertEqual(len(payload["parse_descriptor_id"]), 64)
            self.assertTrue(payload["parse_descriptor_key"].startswith("parse-descriptors/"))
            self.assertTrue((Path(directory) / payload["parse_descriptor_key"]).is_file())
            replay = parse_calendar_from_descriptor(
                directory,
                parse_descriptor_key=payload["parse_descriptor_key"],
            )
            self.assertEqual(replay.document_id, payload["document_id"])
            self.assertEqual(payload["source"], "SSE/SSE_OFFICIAL_NOTICE_DETAIL")
            self.assertEqual(payload["gap_status"], "HAS_GAPS")
            self.assertIn("LICENSE_PENDING", payload["gaps"])

    @staticmethod
    def cli_argv(output_root: str) -> list[str]:
        return [
            "--output-root",
            output_root,
            "--url",
            SSE_URL,
            "--source-owner",
            "SSE",
            "--source-family",
            "SSE_OFFICIAL_NOTICE_DETAIL",
            "--source-version",
            "sse-calendar-2024-v1",
            "--raw-format",
            "HTML",
            "--notice-id",
            "annual-2024",
            "--notice-type",
            "ANNUAL",
            "--source-published-granularity",
            "DATE",
            "--source-published-at",
            "2023-12-01",
            "--usable-from",
            "2099-01-01T00:00:00+00:00",
            "--effective-from",
            "2024-01-01",
            "--effective-to",
            "2024-01-07",
            "--revision-id",
            "annual-r1",
            "--coverage-mode",
            "ANNUAL_EXCEPTIONS",
        ]


if __name__ == "__main__":
    unittest.main()

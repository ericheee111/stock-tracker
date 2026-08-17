from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import cast

from scripts import report_stage2_coverage as coverage_cli
from stock_tracker.core.types import Market
from stock_tracker.quant.core.calendar import TradingCalendar
from stock_tracker.quant.core.fingerprint import fingerprint
from stock_tracker.quant.data.calendar_adapter import (
    CALENDAR_HTML_PARSER_VERSION,
    CalendarCoverageMode,
    CalendarProvenance,
    CalendarSourceFamily,
    Exchange,
    NoticeType,
    PublishedGranularity,
    RawCalendarFormat,
    assemble_calendar_candidates,
    capture_calendar_raw,
    digest_request_payload,
    write_calendar_parse_descriptor,
)
from stock_tracker.quant.data.reconciliation import (
    DEFAULT_RECONCILIATION_POLICY_VERSION,
    BlockerClosureRequest,
    CalendarReconciliationInput,
    ClosureEvidenceKind,
    ExternalClosureEvidence,
    Finding,
    FindingSeverity,
    InheritedTrustBlocker,
    ReconciliationContractError,
    ReconciliationInputError,
    SecurityUniverseReconciliationInput,
    reconcile_stage2,
    validate_reconciliation_output_payload,
)
from stock_tracker.quant.data.security_universe_adapter import (
    SecurityUniverseAdapterError,
    SecurityUniverseCandidateBundle,
    parse_security_universe_artifact,
    read_security_universe_descriptor,
)

ROOT = Path(__file__).resolve().parents[1]
CALENDAR_FIXTURES = Path(__file__).parent / "fixtures" / "calendar"
SECURITY_FIXTURES = Path(__file__).parent / "fixtures" / "security_universe"
SECURITY_ARTIFACT = SECURITY_FIXTURES / "golden_sse.json"
SECURITY_DESCRIPTOR = SECURITY_FIXTURES / "golden_sse.descriptor.json"
SSE_URL = (
    "https://www.sse.com.cn/disclosure/announcement/general/"
    "c/c_20231201_00000001.shtml"
)
AS_OF = datetime(2024, 1, 16, 8, tzinfo=timezone.utc)


class ReconciliationFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.security_raw = SECURITY_ARTIFACT.read_bytes()
        cls.security_document = json.loads(cls.security_raw.decode("utf-8"))
        cls.security_descriptor = read_security_universe_descriptor(SECURITY_DESCRIPTOR)

    def calendar_html(
        self,
        start: date,
        end: date,
        overrides: dict[date, str] | None = None,
        *,
        sparse: bool = False,
    ) -> bytes:
        overrides = overrides or {}
        rows = []
        current = start
        while current <= end:
            if not sparse or current in overrides:
                status = overrides.get(
                    current,
                    "CLOSED" if current.weekday() >= 5 else "OPEN",
                )
                rows.append(f"<tr><td>{current.isoformat()}</td><td>{status}</td></tr>")
            current += timedelta(days=1)
        return (
            "<html><body><table data-calendar-facts=\"v1\">"
            "<tr><th>date</th><th>status</th></tr>"
            + "".join(rows)
            + "</table></body></html>"
        ).encode("utf-8")

    def calendar_input(
        self,
        root: str | Path,
        *,
        start: date = date(2024, 1, 1),
        end: date = date(2024, 1, 14),
        exchange: Exchange = Exchange.SSE,
        source_family: CalendarSourceFamily | None = None,
        source_version: str | None = None,
        notice_type: NoticeType = NoticeType.ANNUAL,
        coverage_mode: CalendarCoverageMode = CalendarCoverageMode.EXPLICIT_DAILY,
        revision_id: str = "annual-r1",
        supersedes_revision_id: str | None = None,
        overrides: dict[date, str] | None = None,
        sparse: bool = False,
        known_at: datetime = datetime(2024, 1, 1, tzinfo=timezone.utc),
        usable_from: datetime = datetime(2024, 1, 1, tzinfo=timezone.utc),
    ) -> CalendarReconciliationInput:
        raw = self.calendar_html(start, end, overrides, sparse=sparse)
        request_url = (
            SSE_URL
            if exchange is Exchange.SSE
            else "https://www.szse.cn/disclosure/notice/general/t20231231_000000.html"
        )
        target_family = source_family or (
            CalendarSourceFamily.SSE_OFFICIAL_NOTICE_DETAIL
            if exchange is Exchange.SSE
            else CalendarSourceFamily.SZSE_OFFICIAL_NOTICE_DETAIL
        )
        target_version = source_version or (
            "fixture-sse-calendar-v1"
            if exchange is Exchange.SSE
            else "fixture-szse-calendar-v1"
        )
        capture = capture_calendar_raw(
            root,
            raw_bytes=raw,
            request_url=request_url,
            request_method="GET",
            request_payload_digest=digest_request_payload(None),
            response_status=200,
            response_headers={"Content-Type": "text/html; charset=utf-8"},
            redirect_chain=(),
            retrieved_at=known_at,
            source_owner=exchange,
            source_family=target_family,
            source_version=target_version,
            parser_version=CALENDAR_HTML_PARSER_VERSION,
            raw_format=RawCalendarFormat.HTML,
        )
        provenance = CalendarProvenance(
            exchange=exchange,
            source_owner=exchange,
            source_family=target_family,
            source_version=target_version,
            notice_id=f"fixture-{revision_id}",
            notice_type=notice_type,
            source_uri=request_url,
            source_published_at=date(2023, 12, 31),
            source_published_granularity=PublishedGranularity.DATE,
            observed_at=known_at,
            retrieved_at=known_at,
            known_at=known_at,
            usable_from=usable_from,
            effective_from=start,
            effective_to=end,
            revision_id=revision_id,
            supersedes_revision_id=supersedes_revision_id,
            raw_artifact_id=capture.artifact_id,
            response_status=200,
            content_type="text/html; charset=utf-8",
            coverage_mode=coverage_mode,
        )
        descriptor = write_calendar_parse_descriptor(
            root,
            capture=capture,
            provenance=provenance,
            parser_version=CALENDAR_HTML_PARSER_VERSION,
        )
        return CalendarReconciliationInput.from_parse_descriptor(
            root,
            descriptor.parse_descriptor_key,
        )

    def security_bundle(
        self,
        document: dict[str, object] | None = None,
        *,
        source: str | None = None,
        source_version: str | None = None,
        exchange: str | None = None,
        synthetic: bool = True,
    ) -> SecurityUniverseCandidateBundle:
        value = copy.deepcopy(document if document is not None else self.security_document)
        target_exchange = exchange or self.security_descriptor.exchange
        if target_exchange == "SZSE":
            def convert(item: object) -> object:
                if isinstance(item, str):
                    return item.replace(".SH", ".SZ").replace("fixture://sse", "fixture://szse")
                if isinstance(item, list):
                    return [convert(child) for child in item]
                if isinstance(item, dict):
                    return {key: convert(child) for key, child in item.items()}
                return item

            value = cast(dict[str, object], convert(value))
            value["exchange"] = "SZSE"
            value["universe_id"] = "A_SHARE_SZSE_ALL"
            value["source"] = source or "fixture-szse-official-like"
            value["source_version"] = source_version or "fixture-szse-v1"
        else:
            if source is not None:
                value["source"] = source
            if source_version is not None:
                value["source_version"] = source_version
        raw = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        descriptor = replace(
            self.security_descriptor,
            source=cast(str, value["source"]),
            source_version=cast(str, value["source_version"]),
            exchange=target_exchange,
            artifact_sha256=hashlib.sha256(raw).hexdigest(),
            byte_size=len(raw),
            synthetic=synthetic,
        )
        return parse_security_universe_artifact(raw, descriptor)

    def security_input(
        self,
        document: dict[str, object] | None = None,
        **kwargs: object,
    ) -> SecurityUniverseReconciliationInput:
        return SecurityUniverseReconciliationInput.from_bundle(
            self.security_bundle(document, **kwargs)
        )

    def report(
        self,
        calendar_inputs: tuple[CalendarReconciliationInput, ...],
        security_inputs: tuple[SecurityUniverseReconciliationInput, ...],
        **kwargs: object,
    ):
        return reconcile_stage2(
            calendar_inputs=calendar_inputs,
            security_universe_inputs=security_inputs,
            as_of=AS_OF,
            **kwargs,
        )


class TestTrustBlockerGovernance(ReconciliationFixtures):
    def test_two_synthetic_sources_agree_but_trust_blockers_stay_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            calendar = self.calendar_input(directory)
            first = self.security_input()
            second = self.security_input(source="fixture-workbuddy-like")
            report = self.report((calendar,), (first, second))
        self.assertIn("SYNTHETIC_EVIDENCE_NOT_CORROBORATION", report.open_inherited_blockers)
        self.assertIn("ADAPTER_UNVERIFIED_INCOMPLETE", report.open_inherited_blockers)
        self.assertIn("LICENSE_PENDING", report.open_inherited_blockers)
        self.assertIn("T3_NOT_REACHED", report.open_inherited_blockers)
        self.assertTrue(report.has_trust_blocks)
        self.assertFalse(report.has_hard_blocks)

    def test_999_of_1000_quantity_continuity_with_one_unclosed_delisting_blocks(self) -> None:
        document = copy.deepcopy(self.security_document)
        continuity = document["coverage_evidence"]["quantity_continuity"][0]
        continuity.update(
            {
                "begin_count": 1000,
                "listings": 0,
                "relistings": 0,
                "delistings": 1,
                "scope_changes": 0,
                "end_count": 999,
            }
        )
        document["coverage_evidence"]["delisting_closures"][0][
            "chinaclear_termination_ids"
        ] = []
        with tempfile.TemporaryDirectory() as directory:
            report = self.report(
                (self.calendar_input(directory),),
                (self.security_input(document),),
            )
        finding = next(item for item in report.findings if item.code == "UNCLOSED_DELISTINGS")
        self.assertEqual(finding.severity, FindingSeverity.TRUST_BLOCK)
        self.assertEqual(len(report.coverage_metrics.unclosed_delisted_instrument_ids), 1)

    def test_snapshot_constructible_does_not_clear_c_trust_blockers(self) -> None:
        security = self.security_input()
        self.assertFalse(security.bundle.coverage_report.has_snapshot_blockers)
        with tempfile.TemporaryDirectory() as directory:
            report = self.report((self.calendar_input(directory),), (security,))
        self.assertEqual(report.candidate_snapshot_state, "STRUCTURALLY_CONSTRUCTIBLE")
        self.assertIn("SOURCE_SECURITY_ID_STABILITY_UNPROVEN", report.open_inherited_blockers)
        self.assertIn("UPSTREAM_RAW_PROVENANCE_INCOMPLETE", report.open_inherited_blockers)

    def test_synthetic_closure_evidence_cannot_close_stability_blocker(self) -> None:
        code = "SOURCE_SECURITY_ID_STABILITY_UNPROVEN"
        payload = {
            "schema": "stage2-external-closure-evidence-v1",
            "kind": ClosureEvidenceKind.SOURCE_ID_STABILITY_CONTRACT.value,
            "supported_blocker_codes": [code],
            "source_owner": "fixture-owner",
            "source_version": "fixture-v1",
            "synthetic": True,
            "independently_approved": True,
            "upstream_raw_artifact_ids": [],
            "details": ["synthetic-only"],
        }
        evidence = ExternalClosureEvidence(
            evidence_id=fingerprint(payload),
            kind=ClosureEvidenceKind.SOURCE_ID_STABILITY_CONTRACT,
            supported_blocker_codes=(code,),
            source_owner="fixture-owner",
            source_version="fixture-v1",
            synthetic=True,
            independently_approved=True,
            details=("synthetic-only",),
        )
        request = BlockerClosureRequest(
            code,
            (evidence.evidence_id,),
            "attempted synthetic closure",
            DEFAULT_RECONCILIATION_POLICY_VERSION,
        )
        with tempfile.TemporaryDirectory() as directory:
            report = self.report(
                (self.calendar_input(directory),),
                (self.security_input(),),
                closure_requests=(request,),
                external_closure_evidence=(evidence,),
            )
        self.assertIn(code, report.open_inherited_blockers)
        self.assertTrue(any(item.code == "BLOCKER_CLOSURE_REJECTED" for item in report.findings))

    def test_non_synthetic_self_asserted_approval_still_cannot_close_blocker(self) -> None:
        code = "SOURCE_SECURITY_ID_STABILITY_UNPROVEN"
        payload = {
            "schema": "stage2-external-closure-evidence-v1",
            "kind": ClosureEvidenceKind.SOURCE_ID_STABILITY_CONTRACT.value,
            "supported_blocker_codes": [code],
            "source_owner": "self-asserted-owner",
            "source_version": "self-asserted-v1",
            "synthetic": False,
            "independently_approved": True,
            "upstream_raw_artifact_ids": [],
            "details": ["caller claims independent approval"],
        }
        evidence = ExternalClosureEvidence(
            evidence_id=fingerprint(payload),
            kind=ClosureEvidenceKind.SOURCE_ID_STABILITY_CONTRACT,
            supported_blocker_codes=(code,),
            source_owner="self-asserted-owner",
            source_version="self-asserted-v1",
            synthetic=False,
            independently_approved=True,
            details=("caller claims independent approval",),
        )
        request = BlockerClosureRequest(
            code,
            (evidence.evidence_id,),
            "caller attempts to close blocker",
            DEFAULT_RECONCILIATION_POLICY_VERSION,
        )
        with tempfile.TemporaryDirectory() as directory:
            report = self.report(
                (self.calendar_input(directory),),
                (self.security_input(),),
                closure_requests=(request,),
                external_closure_evidence=(evidence,),
            )
        self.assertIn(code, report.open_inherited_blockers)
        rejection = next(
            item for item in report.findings if item.code == "BLOCKER_CLOSURE_REJECTED"
        )
        self.assertTrue(any("trusted external closure authority" in item for item in rejection.details))

    def test_synthetic_fixture_cannot_be_relabelled_non_synthetic(self) -> None:
        with self.assertRaisesRegex(
            SecurityUniverseAdapterError,
            "observed_at must equal descriptor retrieved_at",
        ):
            self.security_input(source="fake-real-source", synthetic=False)

    def test_rejected_closure_reason_is_bound_to_report_id(self) -> None:
        code = "SOURCE_SECURITY_ID_STABILITY_UNPROVEN"
        payload = {
            "schema": "stage2-external-closure-evidence-v1",
            "kind": ClosureEvidenceKind.SOURCE_ID_STABILITY_CONTRACT.value,
            "supported_blocker_codes": [code],
            "source_owner": "self-asserted-owner",
            "source_version": "self-asserted-v1",
            "synthetic": False,
            "independently_approved": True,
            "upstream_raw_artifact_ids": [],
            "details": ["caller claims independent approval"],
        }
        evidence = ExternalClosureEvidence(
            evidence_id=fingerprint(payload),
            kind=ClosureEvidenceKind.SOURCE_ID_STABILITY_CONTRACT,
            supported_blocker_codes=(code,),
            source_owner="self-asserted-owner",
            source_version="self-asserted-v1",
            synthetic=False,
            independently_approved=True,
            details=("caller claims independent approval",),
        )
        with tempfile.TemporaryDirectory() as directory:
            calendar = self.calendar_input(directory)
            security = self.security_input()
            first = self.report(
                (calendar,),
                (security,),
                closure_requests=(
                    BlockerClosureRequest(
                        code,
                        (evidence.evidence_id,),
                        "first rejected reason",
                        DEFAULT_RECONCILIATION_POLICY_VERSION,
                    ),
                ),
                external_closure_evidence=(evidence,),
            )
            second = self.report(
                (calendar,),
                (security,),
                closure_requests=(
                    BlockerClosureRequest(
                        code,
                        (evidence.evidence_id,),
                        "second rejected reason",
                        DEFAULT_RECONCILIATION_POLICY_VERSION,
                    ),
                ),
                external_closure_evidence=(evidence,),
            )
        self.assertNotEqual(first.report_id, second.report_id)

    def test_normalized_json_without_upstream_raw_keeps_provenance_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = self.report(
                (self.calendar_input(directory),),
                (self.security_input(),),
            )
        self.assertIn("UPSTREAM_RAW_PROVENANCE_INCOMPLETE", report.open_inherited_blockers)

    def test_direct_report_construction_cannot_bypass_derived_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            hard_report = self.report(
                (self.calendar_input(directory, exchange=Exchange.SSE),),
                (self.security_input(exchange="SZSE"),),
            )
        self.assertTrue(hard_report.has_hard_blocks)
        self.assertEqual(hard_report.candidate_snapshot_state, "HARD_BLOCKED")
        for field_name, forged in (
            ("findings", ()),
            ("inherited_trust_blockers", ()),
            ("unresolved_gaps", ()),
            (
                "coverage_metrics",
                replace(
                    hard_report.coverage_metrics,
                    calendar_observed_civil_dates=0,
                    calendar_open_dates=(),
                    security_bundle_count=0,
                ),
            ),
        ):
            with self.subTest(field=field_name), self.assertRaises(TypeError):
                replace(hard_report, **{field_name: forged})

        recomputed = replace(hard_report, reconciliation_policy_version="policy-recomputed-v2")
        self.assertTrue(recomputed.has_hard_blocks)
        self.assertEqual(recomputed.candidate_snapshot_state, "HARD_BLOCKED")
        self.assertNotEqual(recomputed.report_id, hard_report.report_id)

    def test_promotion_fields_and_ambiguous_blocker_status_are_rejected(self) -> None:
        for payload in (
            {"verified": True},
            {"complete": True},
            {"trust_tier": "T2"},
            {"trust_tier": "T3"},
        ):
            with self.subTest(payload=payload), self.assertRaises(ReconciliationContractError):
                validate_reconciliation_output_payload(payload)
        with self.assertRaises(ReconciliationContractError):
            InheritedTrustBlocker("X", "CLOSED")
        with self.assertRaises(ReconciliationContractError):
            BlockerClosureRequest("X", (), "no evidence", "policy-v1")


class TestIdentityStatusUniverseReconciliation(ReconciliationFixtures):
    def test_non_overlapping_symbol_reuse_is_legal(self) -> None:
        document = copy.deepcopy(self.security_document)
        old = next(
            item
            for item in document["identities"]
            if item["source_security_id"] == "SSECID-BETA"
        )
        reused = copy.deepcopy(old)
        reused.update(
            {
                "source_security_id": "SSECID-BETA-REUSED",
                "name": "代码复用新证券",
                "effective_from": "2024-01-13",
                "effective_to": None,
            }
        )
        reused["provenance"].update(
            {
                "source_published_at": "2024-01-13",
                "observed_at": "2024-01-13T08:00:00+08:00",
                "known_at": "2024-01-13T08:00:00+08:00",
                "usable_from": "2024-01-13T09:30:00+08:00",
                "revision": 1,
                "supersedes": None,
                "evidence_ids": ["fixture-code-reuse"],
            }
        )
        document["identities"].append(reused)
        with tempfile.TemporaryDirectory() as directory:
            report = self.report(
                (self.calendar_input(directory),),
                (self.security_input(document),),
            )
        codes = {item.code for item in report.findings}
        self.assertIn("LEGITIMATE_NON_OVERLAP_CODE_REUSE", codes)
        self.assertNotIn("SYMBOL_IDENTITY_INTERVAL_OVERLAP", codes)

    def test_same_symbol_overlapping_included_instruments_is_hard_block(self) -> None:
        second = copy.deepcopy(self.security_document)
        for section in ("identities", "statuses", "memberships"):
            for item in second[section]:
                if item["source_security_id"] == "SSECID-GAMMA":
                    item["source_security_id"] = "SSECID-GAMMA-SECOND"
        with tempfile.TemporaryDirectory() as directory:
            report = self.report(
                (self.calendar_input(directory),),
                (self.security_input(), self.security_input(second)),
            )
        hard_codes = {
            item.code
            for item in report.findings
            if item.severity is FindingSeverity.HARD_BLOCK
        }
        self.assertIn("SYMBOL_IDENTITY_INTERVAL_OVERLAP", hard_codes)
        self.assertIn("SYMBOL_OVERLAPS_INCLUDED_INSTRUMENTS", hard_codes)

    def test_excluded_old_instrument_does_not_need_future_target_status(self) -> None:
        document = copy.deepcopy(self.security_document)
        document["coverage"]["end_date"] = "2024-01-13"
        document["coverage"]["required_session_dates"] = ["2024-01-13"]
        document["identities"] = [
            item
            for item in document["identities"]
            if item["source_security_id"] == "SSECID-BETA"
        ]
        document["statuses"] = [
            item
            for item in document["statuses"]
            if item["source_security_id"] == "SSECID-BETA"
        ]
        document["memberships"] = [
            item
            for item in document["memberships"]
            if item["source_security_id"] == "SSECID-BETA"
        ]
        with tempfile.TemporaryDirectory() as directory:
            report = self.report(
                (self.calendar_input(directory),),
                (self.security_input(document),),
            )
        codes = {item.code for item in report.findings}
        self.assertNotIn("EXCLUDED_WITHOUT_EXIT_OR_LAST_VISIBLE_STATUS", codes)
        self.assertNotIn("INCLUDED_WITHOUT_TARGET_SESSION_STATUS", codes)

    def test_excluded_exit_status_requirement_is_counted_once_across_later_sessions(self) -> None:
        document = copy.deepcopy(self.security_document)
        document["coverage"]["end_date"] = "2024-01-14"
        document["coverage"]["required_session_dates"] = [
            "2024-01-12",
            "2024-01-13",
            "2024-01-14",
        ]
        document["identities"] = [
            item
            for item in document["identities"]
            if item["source_security_id"] == "SSECID-BETA"
        ]
        document["statuses"] = [
            item
            for item in document["statuses"]
            if item["source_security_id"] == "SSECID-BETA"
        ]
        document["memberships"] = [
            item
            for item in document["memberships"]
            if item["source_security_id"] == "SSECID-BETA"
        ]
        with tempfile.TemporaryDirectory() as directory:
            report = self.report(
                (self.calendar_input(directory),),
                (self.security_input(document),),
            )
        self.assertEqual(report.coverage_metrics.required_status_count, 1)
        self.assertEqual(report.coverage_metrics.observed_required_status_count, 1)

    def test_cross_source_membership_state_conflict_is_hard_block(self) -> None:
        first = copy.deepcopy(self.security_document)
        second = copy.deepcopy(self.security_document)
        second["memberships"] = [
            item
            for item in second["memberships"]
            if not (
                item["source_security_id"] == "SSECID-ALPHA"
                and item["effective_date"] == "2024-01-08"
                and item["state"] == "EXCLUDED"
            )
        ]
        with tempfile.TemporaryDirectory() as directory:
            report = self.report(
                (self.calendar_input(directory),),
                (
                    self.security_input(first, source="source-with-correction"),
                    self.security_input(second, source="source-without-correction"),
                ),
            )
        finding = next(
            item
            for item in report.findings
            if item.code == "UNIVERSE_MEMBERSHIP_STATE_CONFLICT"
        )
        self.assertEqual(finding.severity, FindingSeverity.HARD_BLOCK)

    def test_unknown_risk_is_not_silently_resolved(self) -> None:
        first = copy.deepcopy(self.security_document)
        target = next(
            item
            for item in first["statuses"]
            if item["source_security_id"] == "SSECID-GAMMA"
            and item["session_date"] == "2024-01-12"
        )
        target["risk_designation"] = "UNKNOWN"
        second = copy.deepcopy(first)
        second_target = next(
            item
            for item in second["statuses"]
            if item["source_security_id"] == "SSECID-GAMMA"
            and item["session_date"] == "2024-01-12"
        )
        second_target["risk_designation"] = "NORMAL"
        with tempfile.TemporaryDirectory() as directory:
            report = self.report(
                (self.calendar_input(directory),),
                (
                    self.security_input(first, source="fixture-source-one"),
                    self.security_input(second, source="fixture-source-two"),
                ),
            )
        finding = next(
            item
            for item in report.findings
            if item.code == "STATUS_UNKNOWN_CANNOT_BE_SILENTLY_RESOLVED"
        )
        self.assertEqual(finding.severity, FindingSeverity.TRUST_BLOCK)


class TestExchangeAndSourceScoping(ReconciliationFixtures):
    def test_szse_universe_cannot_borrow_sse_calendar_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = self.report(
                (self.calendar_input(directory, exchange=Exchange.SSE),),
                (self.security_input(exchange="SZSE"),),
            )
        hard_codes = {
            item.code
            for item in report.findings
            if item.severity is FindingSeverity.HARD_BLOCK
        }
        self.assertIn("REQUIRED_SESSION_CALENDAR_MISSING", hard_codes)

    def test_szse_closed_session_is_not_overridden_by_sse_open_session(self) -> None:
        target = date(2024, 1, 12)
        with tempfile.TemporaryDirectory() as directory:
            sse_calendar = self.calendar_input(directory, exchange=Exchange.SSE)
            szse_calendar = self.calendar_input(
                directory,
                exchange=Exchange.SZSE,
                overrides={target: "CLOSED"},
            )
            report = self.report(
                (sse_calendar, szse_calendar),
                (self.security_input(exchange="SZSE"),),
            )
        hard_codes = {
            item.code
            for item in report.findings
            if item.severity is FindingSeverity.HARD_BLOCK
        }
        self.assertIn("REQUIRED_SESSION_CALENDAR_CLOSED", hard_codes)

    def test_future_calendar_facts_do_not_inflate_as_of_coverage_metrics(self) -> None:
        future = datetime(2024, 1, 17, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            calendar = self.calendar_input(
                directory,
                known_at=future,
                usable_from=future,
            )
            report = self.report((calendar,), (self.security_input(),))
        self.assertEqual(report.coverage_metrics.calendar_observed_civil_dates, 0)
        self.assertEqual(
            report.coverage_metrics.calendar_expected_civil_dates,
            len(report.coverage_metrics.calendar_missing_civil_dates),
        )
        self.assertTrue(
            any(item.code == "REQUIRED_SESSION_CALENDAR_MISSING" for item in report.findings)
        )

    def test_future_security_artifact_does_not_pollute_past_candidate_coverage(self) -> None:
        past_as_of = datetime(2024, 1, 14, 8, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            report = reconcile_stage2(
                calendar_inputs=(self.calendar_input(directory),),
                security_universe_inputs=(self.security_input(),),
                as_of=past_as_of,
            )
        self.assertEqual(report.coverage_metrics.security_bundle_count, 0)
        self.assertEqual(report.coverage_metrics.identity_candidate_count, 0)
        self.assertEqual(report.coverage_metrics.status_candidate_count, 0)
        self.assertEqual(report.coverage_metrics.membership_candidate_count, 0)
        self.assertTrue(
            any(item.code == "SECURITY_ARTIFACT_NOT_VISIBLE_AS_OF" for item in report.findings)
        )
        self.assertFalse(
            any(item.code == "MISSING_SOURCE_EVIDENCE_IDS" for item in report.findings)
        )

    def test_same_version_different_calendar_families_are_independent_streams(self) -> None:
        shared_version = "shared-family-version-v1"
        with tempfile.TemporaryDirectory() as directory:
            detail = self.calendar_input(
                directory,
                source_family=CalendarSourceFamily.SSE_OFFICIAL_NOTICE_DETAIL,
                source_version=shared_version,
            )
            attachment = self.calendar_input(
                directory,
                source_family=CalendarSourceFamily.SSE_OFFICIAL_NOTICE_ATTACHMENT,
                source_version=shared_version,
            )
            security = self.security_input()
            forward = self.report((detail, attachment), (security,))
            reverse = self.report((attachment, detail), (security,))
        calendar_hard_codes = {
            item.code
            for item in forward.findings
            if item.severity is FindingSeverity.HARD_BLOCK
            and item.code.startswith("CALENDAR_")
        }
        self.assertEqual(calendar_hard_codes, set())
        self.assertEqual(forward.report_id, reverse.report_id)

    def test_different_calendar_families_disagree_after_independent_resolution(self) -> None:
        target = date(2024, 1, 3)
        shared_version = "shared-family-version-v1"
        with tempfile.TemporaryDirectory() as directory:
            detail = self.calendar_input(
                directory,
                source_family=CalendarSourceFamily.SSE_OFFICIAL_NOTICE_DETAIL,
                source_version=shared_version,
            )
            attachment = self.calendar_input(
                directory,
                source_family=CalendarSourceFamily.SSE_OFFICIAL_NOTICE_ATTACHMENT,
                source_version=shared_version,
                overrides={target: "CLOSED"},
            )
            report = self.report(
                (detail, attachment),
                (self.security_input(),),
            )
        finding = next(
            item
            for item in report.findings
            if item.code == "CALENDAR_OPEN_CLOSED_CONFLICT"
        )
        self.assertEqual(finding.severity, FindingSeverity.HARD_BLOCK)
        self.assertFalse(
            any(
                item.code == "CALENDAR_REVISION_BRANCH_CONFLICT"
                for item in report.findings
            )
        )
        self.assertIn(
            "SSE_OFFICIAL_NOTICE_DETAIL/shared-family-version-v1",
            finding.details,
        )
        self.assertIn(
            "SSE_OFFICIAL_NOTICE_ATTACHMENT/shared-family-version-v1",
            finding.details,
        )

    def test_independent_sources_may_have_independent_versions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = self.report(
                (self.calendar_input(directory),),
                (
                    self.security_input(source="source-one", source_version="source-one-v1"),
                    self.security_input(source="source-two", source_version="source-two-v9"),
                ),
            )
        self.assertFalse(
            any(item.code == "UNIVERSE_SOURCE_VERSION_MIXING" for item in report.findings)
        )

    def test_one_source_identity_cannot_mix_versions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = self.report(
                (self.calendar_input(directory),),
                (
                    self.security_input(source="same-source", source_version="same-source-v1"),
                    self.security_input(source="same-source", source_version="same-source-v2"),
                ),
            )
        finding = next(
            item for item in report.findings if item.code == "UNIVERSE_SOURCE_VERSION_MIXING"
        )
        self.assertEqual(finding.severity, FindingSeverity.HARD_BLOCK)


class TestCalendarAndReportIdentity(ReconciliationFixtures):
    def test_explicit_revision_overrides_inferred_weekday_with_audit_warning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            annual = self.calendar_input(
                directory,
                coverage_mode=CalendarCoverageMode.ANNUAL_EXCEPTIONS,
                overrides={date(2024, 1, 1): "CLOSED"},
                sparse=True,
            )
            holiday = self.calendar_input(
                directory,
                start=date(2024, 1, 3),
                end=date(2024, 1, 3),
                notice_type=NoticeType.HOLIDAY,
                revision_id="holiday-r1",
                supersedes_revision_id="annual-r1",
                overrides={date(2024, 1, 3): "CLOSED"},
                sparse=True,
                known_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
                usable_from=datetime(2024, 1, 3, tzinfo=timezone.utc),
            )
            report = self.report((annual, holiday), (self.security_input(),))
        warning = next(
            item
            for item in report.findings
            if item.code == "CALENDAR_EXPLICIT_REVISION_OVERRIDES_INFERENCE"
        )
        self.assertEqual(warning.severity, FindingSeverity.WARNING)
        self.assertNotIn("2024-01-03", report.coverage_metrics.calendar_open_dates)
        self.assertFalse(
            any(
                item.code == "CALENDAR_OPEN_CLOSED_CONFLICT"
                and item.severity is FindingSeverity.HARD_BLOCK
                for item in report.findings
            )
        )

    def test_revision_chain_terminal_not_revision_id_lexical_order_wins(self) -> None:
        target = date(2024, 1, 3)
        same_known_at = datetime(2024, 1, 2, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            annual = self.calendar_input(
                directory,
                coverage_mode=CalendarCoverageMode.ANNUAL_EXCEPTIONS,
                overrides={date(2024, 1, 1): "CLOSED"},
                sparse=True,
            )
            r2 = self.calendar_input(
                directory,
                start=target,
                end=target,
                notice_type=NoticeType.HOLIDAY,
                revision_id="r2",
                supersedes_revision_id="annual-r1",
                overrides={target: "CLOSED"},
                sparse=True,
                known_at=same_known_at,
                usable_from=datetime(2024, 1, 3, tzinfo=timezone.utc),
            )
            r10 = self.calendar_input(
                directory,
                start=target,
                end=target,
                notice_type=NoticeType.REVISION,
                revision_id="r10",
                supersedes_revision_id="r2",
                overrides={target: "OPEN"},
                sparse=True,
                known_at=same_known_at,
                usable_from=datetime(2024, 1, 3, tzinfo=timezone.utc),
            )
            report = self.report((annual, r2, r10), (self.security_input(),))
            assembled = assemble_calendar_candidates(
                (annual.document, r2.document, r10.document)
            )
            core_snapshot = TradingCalendar(
                (assembled.coverage,),
                assembled.days,
            ).snapshot(
                Market.A,
                date(2024, 1, 1),
                date(2024, 1, 14),
                AS_OF,
                require_verified=False,
            )
        self.assertIn(target.isoformat(), report.coverage_metrics.calendar_open_dates)
        self.assertIn(target, core_snapshot.open_dates)
        self.assertFalse(
            any(
                item.severity is FindingSeverity.HARD_BLOCK
                and item.code.startswith("CALENDAR_")
                and target.isoformat() in item.details
                for item in report.findings
            )
        )
        chain = next(
            item
            for item in report.findings
            if item.code == "CALENDAR_EXPLICIT_REVISION_OVERRIDES_INFERENCE"
            and target.isoformat() in item.details
        )
        self.assertIn("r10", chain.details)

    def test_disconnected_same_payload_terminals_are_hard_blocked(self) -> None:
        target = date(2024, 1, 3)
        with tempfile.TemporaryDirectory() as directory:
            annual = self.calendar_input(
                directory,
                coverage_mode=CalendarCoverageMode.ANNUAL_EXCEPTIONS,
                overrides={date(2024, 1, 1): "CLOSED"},
                sparse=True,
            )
            orphan = self.calendar_input(
                directory,
                start=target,
                end=target,
                notice_type=NoticeType.REVISION,
                revision_id="orphan-r1",
                supersedes_revision_id=None,
                overrides={target: "OPEN"},
                sparse=True,
            )
            report = self.report((annual, orphan), (self.security_input(),))
        finding = next(
            item
            for item in report.findings
            if item.code == "CALENDAR_REVISION_BRANCH_CONFLICT"
        )
        self.assertEqual(finding.severity, FindingSeverity.HARD_BLOCK)

    def test_disconnected_same_payload_calendar_cycle_is_hard_blocked(self) -> None:
        target = date(2024, 1, 3)
        with tempfile.TemporaryDirectory() as directory:
            annual = self.calendar_input(
                directory,
                coverage_mode=CalendarCoverageMode.ANNUAL_EXCEPTIONS,
                overrides={date(2024, 1, 1): "CLOSED"},
                sparse=True,
            )
            r2 = self.calendar_input(
                directory,
                start=target,
                end=target,
                notice_type=NoticeType.REVISION,
                revision_id="cycle-r2",
                supersedes_revision_id="cycle-r3",
                overrides={target: "OPEN"},
                sparse=True,
            )
            r3 = self.calendar_input(
                directory,
                start=target,
                end=target,
                notice_type=NoticeType.REVISION,
                revision_id="cycle-r3",
                supersedes_revision_id="cycle-r2",
                overrides={target: "OPEN"},
                sparse=True,
            )
            report = self.report((annual, r2, r3), (self.security_input(),))
        finding = next(
            item for item in report.findings if item.code == "CALENDAR_REVISION_CYCLE"
        )
        self.assertEqual(finding.severity, FindingSeverity.HARD_BLOCK)
        self.assertTrue(report.has_hard_blocks)

    def test_input_order_does_not_change_report_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first_calendar = self.calendar_input(directory)
            second_calendar = self.calendar_input(
                directory,
                start=date(2024, 1, 12),
                end=date(2024, 1, 12),
                notice_type=NoticeType.HOLIDAY,
                revision_id="same-day-info-r1",
                overrides={date(2024, 1, 12): "OPEN"},
                sparse=True,
            )
            first_security = self.security_input()
            second_security = self.security_input(source="fixture-second-source")
            first = self.report(
                (first_calendar, second_calendar),
                (first_security, second_security),
            )
            second = self.report(
                (second_calendar, first_calendar),
                (second_security, first_security),
            )
        self.assertEqual(first.report_id, second.report_id)

    def test_finding_message_or_severity_changes_report_id(self) -> None:
        base = Finding("CUSTOM_AUDIT", FindingSeverity.INFO, "test", "message one")
        changed_message = replace(base, message="message two")
        changed_severity = replace(base, severity=FindingSeverity.WARNING)
        with tempfile.TemporaryDirectory() as directory:
            calendar = self.calendar_input(directory)
            security = self.security_input()
            first = self.report(
                (calendar,),
                (security,),
                additional_findings=(base,),
            )
            second = self.report(
                (calendar,),
                (security,),
                additional_findings=(changed_message,),
            )
            third = self.report(
                (calendar,),
                (security,),
                additional_findings=(changed_severity,),
            )
        self.assertNotEqual(first.report_id, second.report_id)
        self.assertNotEqual(first.report_id, third.report_id)

    def test_policy_version_changes_report_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            calendar = self.calendar_input(directory)
            security = self.security_input()
            first = self.report((calendar,), (security,))
            second = self.report(
                (calendar,),
                (security,),
                reconciliation_policy_version="stage2-reconciliation-policy-v2-test",
            )
        self.assertNotEqual(first.report_id, second.report_id)

    def test_tampered_parse_descriptor_is_rejected_before_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            calendar = self.calendar_input(directory)
            path = Path(directory) / calendar.parse_descriptor_key
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["provenance"]["notice_id"] = "tampered"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ReconciliationInputError) as raised:
                CalendarReconciliationInput.from_parse_descriptor(
                    directory,
                    calendar.parse_descriptor_key,
                )
        self.assertEqual(raised.exception.severity, FindingSeverity.HARD_BLOCK)
        self.assertEqual(
            raised.exception.code,
            "CALENDAR_PARSE_DESCRIPTOR_MISMATCH",
        )


class TestCoverageCli(ReconciliationFixtures):
    def test_cli_generates_json_and_markdown_but_does_not_claim_trust_passed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calendar = self.calendar_input(root / "calendar")
            json_output = root / "report.json"
            markdown_output = root / "report.md"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = coverage_cli.main(
                    [
                        "--calendar-root",
                        str(root / "calendar"),
                        "--calendar-parse-descriptor",
                        calendar.parse_descriptor_key,
                        "--security-artifact",
                        str(SECURITY_ARTIFACT),
                        "--security-descriptor",
                        str(SECURITY_DESCRIPTOR),
                        "--as-of",
                        AS_OF.isoformat(),
                        "--json-output",
                        str(json_output),
                        "--markdown-output",
                        str(markdown_output),
                    ]
                )
            summary = json.loads(stdout.getvalue())
            payload = json.loads(json_output.read_text(encoding="utf-8"))
            markdown = markdown_output.read_text(encoding="utf-8")
        self.assertEqual(exit_code, 0)
        self.assertTrue(summary["report_generated"])
        self.assertFalse(summary["trust_passed"])
        self.assertEqual(payload["license_status"], "LICENSE_PENDING")
        self.assertEqual(payload["evidence_tier_status"], "T3_NOT_REACHED")
        self.assertIn(payload["report_id"], markdown)
        self.assertNotIn("trust_tier", payload)
        self.assertNotIn("complete", payload)
        self.assertNotIn("verified", payload)

    def test_cli_refuses_to_overwrite_security_input_artifact(self) -> None:
        original_hash = hashlib.sha256(SECURITY_ARTIFACT.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calendar = self.calendar_input(root / "calendar")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = coverage_cli.main(
                    [
                        "--calendar-root",
                        str(root / "calendar"),
                        "--calendar-parse-descriptor",
                        calendar.parse_descriptor_key,
                        "--security-artifact",
                        str(SECURITY_ARTIFACT),
                        "--security-descriptor",
                        str(SECURITY_DESCRIPTOR),
                        "--as-of",
                        AS_OF.isoformat(),
                        "--json-output",
                        str(SECURITY_ARTIFACT),
                        "--markdown-output",
                        str(root / "report.md"),
                    ]
                )
            error = json.loads(stderr.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(error["severity"], "HARD_BLOCK")
        self.assertIn("overwrite input", error["message"])
        self.assertEqual(
            hashlib.sha256(SECURITY_ARTIFACT.read_bytes()).hexdigest(),
            original_hash,
        )

    def test_cli_refuses_same_json_and_markdown_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calendar = self.calendar_input(root / "calendar")
            output = root / "same-output"
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = coverage_cli.main(
                    [
                        "--calendar-root",
                        str(root / "calendar"),
                        "--calendar-parse-descriptor",
                        calendar.parse_descriptor_key,
                        "--security-artifact",
                        str(SECURITY_ARTIFACT),
                        "--security-descriptor",
                        str(SECURITY_DESCRIPTOR),
                        "--as-of",
                        AS_OF.isoformat(),
                        "--json-output",
                        str(output),
                        "--markdown-output",
                        str(output),
                    ]
                )
            error = json.loads(stderr.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(error["severity"], "HARD_BLOCK")
        self.assertIn("must be different files", error["message"])
        self.assertFalse(output.exists())

    def test_cli_refuses_to_overwrite_calendar_parse_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calendar_root = root / "calendar"
            calendar = self.calendar_input(calendar_root)
            parse_path = calendar_root / calendar.parse_descriptor_key
            original_hash = hashlib.sha256(parse_path.read_bytes()).hexdigest()
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = coverage_cli.main(
                    [
                        "--calendar-root",
                        str(calendar_root),
                        "--calendar-parse-descriptor",
                        calendar.parse_descriptor_key,
                        "--security-artifact",
                        str(SECURITY_ARTIFACT),
                        "--security-descriptor",
                        str(SECURITY_DESCRIPTOR),
                        "--as-of",
                        AS_OF.isoformat(),
                        "--json-output",
                        str(parse_path),
                        "--markdown-output",
                        str(root / "report.md"),
                    ]
                )
            error = json.loads(stderr.getvalue())
            self.assertEqual(
                hashlib.sha256(parse_path.read_bytes()).hexdigest(),
                original_hash,
            )
        self.assertEqual(exit_code, 2)
        self.assertEqual(error["severity"], "HARD_BLOCK")
        self.assertIn("overwrite input", error["message"])

    def test_cli_tampered_parse_descriptor_is_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calendar = self.calendar_input(root / "calendar")
            descriptor = root / "calendar" / calendar.parse_descriptor_key
            payload = json.loads(descriptor.read_text(encoding="utf-8"))
            payload["parser_version"] = "tampered-parser"
            descriptor.write_text(json.dumps(payload), encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = coverage_cli.main(
                    [
                        "--calendar-root",
                        str(root / "calendar"),
                        "--calendar-parse-descriptor",
                        calendar.parse_descriptor_key,
                        "--security-artifact",
                        str(SECURITY_ARTIFACT),
                        "--security-descriptor",
                        str(SECURITY_DESCRIPTOR),
                        "--as-of",
                        AS_OF.isoformat(),
                        "--json-output",
                        str(root / "report.json"),
                        "--markdown-output",
                        str(root / "report.md"),
                    ]
                )
            error = json.loads(stderr.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(error["severity"], "HARD_BLOCK")
        self.assertFalse((root / "report.json").exists())


if __name__ == "__main__":
    unittest.main()

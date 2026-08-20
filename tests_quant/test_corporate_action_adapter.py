from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

from _helpers import utc_datetime

from stock_tracker.quant.core.corporate_actions import (
    AdjustmentBasis,
    CorporateActionLifecycle,
)
from stock_tracker.quant.data.corporate_action_adapter import (
    CandidateCorporateActionLifecycle,
    CorporateActionAdapterError,
    CorporateActionSourceFamily,
    CorporateActionSourceOwner,
    ExtractionStatus,
    RawCorporateActionFormat,
    RedirectHop,
    capture_corporate_action_raw,
    digest_request_payload,
    load_corporate_action_parse_descriptor,
    load_corporate_action_raw,
    parse_corporate_action_document,
    parse_corporate_action_from_descriptor,
    resolve_corporate_action_candidates,
    write_corporate_action_parse_descriptor,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "corporate_actions"
VALID_FIXTURE = FIXTURES / "valid_effective.json"
PROPOSAL_FIXTURE = FIXTURES / "proposed_incomplete.json"
PDF_FIXTURE = FIXTURES / "sample.pdf"
ERROR_HTML_FIXTURE = FIXTURES / "error_page.html"
SCRIPT = ROOT / "scripts" / "capture_a_share_corporate_actions.py"
SSE_JSON_URL = (
    "https://www.sse.com.cn/disclosure/listedinfo/announcement/"
    "c/new/2025-01-10/fixture.json"
)
SSE_PDF_URL = (
    "https://www.sse.com.cn/disclosure/listedinfo/announcement/"
    "c/new/2025-01-10/fixture.pdf"
)
RETRIEVED = utc_datetime(2025, 1, 12)


class CorporateActionAdapterFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.valid_raw = VALID_FIXTURE.read_bytes()
        cls.valid_document = json.loads(cls.valid_raw.decode("utf-8"))
        cls.proposal_raw = PROPOSAL_FIXTURE.read_bytes()

    @staticmethod
    def encode(document: dict[str, object]) -> bytes:
        return json.dumps(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    def capture(
        self,
        root: str | Path,
        *,
        raw_bytes: bytes | None = None,
        document: dict[str, object] | None = None,
        request_url: str = SSE_JSON_URL,
        request_method: str = "GET",
        response_status: int = 200,
        content_type: str = "application/json",
        redirect_chain: tuple[RedirectHop, ...] = (),
        retrieved_at=RETRIEVED,
        source_owner: CorporateActionSourceOwner = CorporateActionSourceOwner.SSE,
        source_family: CorporateActionSourceFamily = (
            CorporateActionSourceFamily.SSE_LISTED_COMPANY_ANNOUNCEMENT
        ),
        source_version: str = "fixture-corporate-actions-v1",
        raw_format: RawCorporateActionFormat = RawCorporateActionFormat.JSON,
    ):
        if raw_bytes is not None and document is not None:
            raise AssertionError("choose raw_bytes or document")
        payload = (
            raw_bytes
            if raw_bytes is not None
            else self.encode(document)
            if document is not None
            else self.valid_raw
        )
        return capture_corporate_action_raw(
            root,
            raw_bytes=payload,
            request_url=request_url,
            request_method=request_method,
            request_payload_digest=digest_request_payload({}),
            response_status=response_status,
            response_headers={"Content-Type": content_type},
            redirect_chain=redirect_chain,
            retrieved_at=retrieved_at,
            source_owner=source_owner,
            source_family=source_family,
            source_version=source_version,
            raw_format=raw_format,
        )

    def parsed(
        self,
        root: str | Path,
        *,
        document: dict[str, object] | None = None,
        raw_bytes: bytes | None = None,
        retrieved_at=RETRIEVED,
    ):
        capture = self.capture(
            root,
            document=document,
            raw_bytes=raw_bytes,
            retrieved_at=retrieved_at,
        )
        payload = (
            raw_bytes
            if raw_bytes is not None
            else self.encode(document)
            if document is not None
            else self.valid_raw
        )
        parsed = parse_corporate_action_document(payload, capture=capture)
        return capture, parsed


class TestExactRawCapture(CorporateActionAdapterFixtures):
    def test_exact_bytes_and_descriptor_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capture = self.capture(directory)
            loaded, raw = load_corporate_action_raw(
                directory,
                descriptor_key=capture.descriptor_key,
            )
        self.assertEqual(raw, self.valid_raw)
        self.assertEqual(loaded, capture)
        self.assertEqual(capture.artifact_id, hashlib.sha256(raw).hexdigest())
        self.assertEqual(capture.known_at, capture.retrieved_at)
        self.assertEqual(capture.observed_at, capture.retrieved_at)
        self.assertFalse(hasattr(capture, "verified"))
        self.assertFalse(hasattr(capture, "complete"))
        self.assertFalse(hasattr(capture, "trust_tier"))

    def test_same_url_changed_bytes_create_new_artifact(self) -> None:
        changed = self.valid_raw.replace(b"fixture-action-1", b"fixture-action-2")
        with tempfile.TemporaryDirectory() as directory:
            first = self.capture(directory)
            second = self.capture(directory, raw_bytes=changed)
        self.assertNotEqual(first.artifact_id, second.artifact_id)
        self.assertNotEqual(first.descriptor_id, second.descriptor_id)
        self.assertNotEqual(first.storage_key, second.storage_key)

    def test_same_raw_different_provenance_reuses_raw_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = self.capture(directory, source_version="fixture-v1")
            second = self.capture(
                directory,
                source_version="fixture-v2",
                retrieved_at=utc_datetime(2025, 1, 13),
            )
        self.assertEqual(first.artifact_id, second.artifact_id)
        self.assertEqual(first.storage_key, second.storage_key)
        self.assertNotEqual(first.descriptor_id, second.descriptor_id)
        self.assertNotEqual(first.descriptor_key, second.descriptor_key)

    def test_same_size_raw_tamper_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = self.capture(root)
            raw_path = root / capture.storage_key
            tampered = bytearray(raw_path.read_bytes())
            tampered[0] ^= 1
            raw_path.write_bytes(bytes(tampered))
            with self.assertRaisesRegex(CorporateActionAdapterError, "hash changed"):
                load_corporate_action_raw(
                    root,
                    descriptor_key=capture.descriptor_key,
                )

    def test_descriptor_and_descriptor_key_tamper_are_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = self.capture(root)
            descriptor_path = root / capture.descriptor_key
            value = json.loads(descriptor_path.read_text(encoding="utf-8"))
            value["source_version"] = "tampered-v2"
            descriptor_path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(CorporateActionAdapterError, "descriptor_id"):
                load_corporate_action_raw(
                    root,
                    descriptor_key=capture.descriptor_key,
                )
        with self.assertRaisesRegex(CorporateActionAdapterError, "descriptor_key"):
            replace(
                capture,
                descriptor_key=(
                    "descriptors/corporate-actions/"
                    + "f" * 64
                    + ".json"
                ),
            )

    def test_domain_family_redirect_status_and_content_type_fail_closed(self) -> None:
        cases = (
            {
                "request_url": "http://www.sse.com.cn/disclosure/listedinfo/announcement/x",
                "pattern": "official HTTPS",
            },
            {
                "source_family": (
                    CorporateActionSourceFamily.CNINFO_DISCLOSURE_ATTACHMENT
                ),
                "pattern": "owner and source family",
            },
            {
                "redirect_chain": (
                    RedirectHop(
                        302,
                        SSE_JSON_URL,
                        "https://evil.example/disclosure.json",
                    ),
                ),
                "pattern": "official HTTPS",
            },
            {"response_status": 500, "pattern": "HTTP error"},
            {"content_type": "text/html", "pattern": "incompatible"},
        )
        for index, case in enumerate(cases):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory:
                pattern = case.pop("pattern")
                try:
                    with self.assertRaisesRegex(CorporateActionAdapterError, pattern):
                        self.capture(directory, **case)
                finally:
                    case["pattern"] = pattern

    def test_content_length_and_redirect_chain_must_match_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(
            CorporateActionAdapterError,
            "Content-Length",
        ):
            capture_corporate_action_raw(
                directory,
                raw_bytes=self.valid_raw,
                request_url=SSE_JSON_URL,
                request_method="GET",
                request_payload_digest=digest_request_payload({}),
                response_status=200,
                response_headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(self.valid_raw) + 1),
                },
                redirect_chain=(),
                retrieved_at=RETRIEVED,
                source_owner=CorporateActionSourceOwner.SSE,
                source_family=(
                    CorporateActionSourceFamily.SSE_LISTED_COMPANY_ANNOUNCEMENT
                ),
                source_version="fixture-v1",
                raw_format=RawCorporateActionFormat.JSON,
            )

        alternate = (
            "https://www.sse.com.cn/disclosure/listedinfo/announcement/"
            "c/new/2025-01-10/alternate.json"
        )
        cases = (
            (
                RedirectHop(302, alternate, SSE_JSON_URL),
            ),
            (
                RedirectHop(302, SSE_JSON_URL, alternate),
                RedirectHop(302, alternate, SSE_JSON_URL),
            ),
        )
        for redirects in cases:
            with (
                self.subTest(redirects=redirects),
                tempfile.TemporaryDirectory() as directory,
                self.assertRaisesRegex(
                    CorporateActionAdapterError,
                    "discontinuous|cycle",
                ),
            ):
                self.capture(directory, redirect_chain=redirects)

    def test_html_error_page_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(
            CorporateActionAdapterError,
            "error page",
        ):
            self.capture(
                directory,
                raw_bytes=ERROR_HTML_FIXTURE.read_bytes(),
                content_type="text/html; charset=utf-8",
                raw_format=RawCorporateActionFormat.HTML,
            )


class TestCandidateParsing(CorporateActionAdapterFixtures):
    def test_valid_fixture_is_deterministic_unverified_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capture, document = self.parsed(directory)
            replay = parse_corporate_action_document(
                self.valid_raw,
                capture=capture,
            )
        self.assertEqual(document.document_id, replay.document_id)
        self.assertEqual(len(document.candidates), 1)
        candidate = document.candidates[0]
        self.assertEqual(candidate.automatic_share_ratio, Decimal("1.2"))
        self.assertEqual(candidate.known_at, RETRIEVED)
        self.assertEqual(candidate.observed_at, RETRIEVED)
        self.assertIs(type(candidate.source_published_at), date)
        self.assertEqual(
            candidate.gaps,
            ("DATE_ONLY_PUBLICATION_NO_INTRADAY_PRECISION",),
        )
        core = candidate.to_core_fact()
        self.assertFalse(core.verified)
        self.assertEqual(core.lifecycle, CorporateActionLifecycle.EFFECTIVE)
        self.assertFalse(hasattr(candidate, "verified"))
        self.assertFalse(hasattr(candidate, "complete"))
        self.assertFalse(hasattr(candidate, "trust_tier"))
        self.assertFalse(hasattr(candidate, "promotion"))

    def test_parser_requires_the_exact_bytes_bound_to_capture(self) -> None:
        changed = self.valid_raw.replace(b"fixture-action-1", b"fixture-action-2")
        self.assertEqual(len(changed), len(self.valid_raw))
        with tempfile.TemporaryDirectory() as directory:
            capture = self.capture(directory)
            with self.assertRaisesRegex(CorporateActionAdapterError, "capture SHA-256"):
                parse_corporate_action_document(changed, capture=capture)

    def test_nonfinite_json_and_request_payload_are_rejected(self) -> None:
        malformed = self.valid_raw.replace(b'"automatic_share_ratio": "1.2"', b'"automatic_share_ratio": NaN  ')
        with tempfile.TemporaryDirectory() as directory:
            capture = self.capture(directory, raw_bytes=malformed)
            with self.assertRaisesRegex(CorporateActionAdapterError, "non-finite"):
                parse_corporate_action_document(malformed, capture=capture)
        with self.assertRaisesRegex(CorporateActionAdapterError, "canonical"):
            digest_request_payload({"value": float("nan")})

    def test_parse_descriptor_round_trip_and_tamper_detection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = self.capture(root)
            descriptor = write_corporate_action_parse_descriptor(
                root,
                capture=capture,
            )
            loaded, loaded_capture, raw = load_corporate_action_parse_descriptor(
                root,
                parse_descriptor_key=descriptor.parse_descriptor_key,
            )
            replay = parse_corporate_action_from_descriptor(
                root,
                parse_descriptor_key=descriptor.parse_descriptor_key,
            )
            self.assertEqual(loaded, descriptor)
            self.assertEqual(loaded_capture, capture)
            self.assertEqual(raw, self.valid_raw)
            self.assertEqual(replay.document_id, descriptor.document_id)

            path = root / descriptor.parse_descriptor_key
            value = json.loads(path.read_text(encoding="utf-8"))
            value["candidate_ids"] = ["f" * 64]
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(
                CorporateActionAdapterError,
                "parse_descriptor_id",
            ):
                load_corporate_action_parse_descriptor(
                    root,
                    parse_descriptor_key=descriptor.parse_descriptor_key,
                )

    def test_pdf_is_captured_but_never_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capture = self.capture(
                directory,
                raw_bytes=PDF_FIXTURE.read_bytes(),
                request_url=SSE_PDF_URL,
                content_type="application/pdf",
                source_family=(
                    CorporateActionSourceFamily.SSE_ANNOUNCEMENT_ATTACHMENT
                ),
                raw_format=RawCorporateActionFormat.PDF,
            )
            first = write_corporate_action_parse_descriptor(
                directory,
                capture=capture,
                parser_version="pdf-extraction-v1",
            )
            second = write_corporate_action_parse_descriptor(
                directory,
                capture=capture,
                parser_version="pdf-extraction-v2",
            )
            self.assertEqual(first.extraction_status, ExtractionStatus.EXTRACTION_REQUIRED)
            self.assertEqual(first.gaps, ("EXTRACTION_REQUIRED_PDF",))
            self.assertNotEqual(first.parse_descriptor_id, second.parse_descriptor_id)
            self.assertEqual(first.raw_artifact_id, second.raw_artifact_id)
            with self.assertRaisesRegex(CorporateActionAdapterError, "extraction required"):
                parse_corporate_action_from_descriptor(
                    directory,
                    parse_descriptor_key=first.parse_descriptor_key,
                )

    def test_unknown_missing_and_malformed_values_fail_closed(self) -> None:
        mutations: list[tuple[str, object, str]] = []
        top_unknown = copy.deepcopy(self.valid_document)
        top_unknown["unexpected"] = True
        mutations.append(("top unknown", top_unknown, "unknown fields"))

        row_unknown = copy.deepcopy(self.valid_document)
        row_unknown["actions"][0]["unexpected"] = "x"
        mutations.append(("row unknown", row_unknown, "unknown fields"))

        missing = copy.deepcopy(self.valid_document)
        del missing["actions"][0]["ex_date"]
        mutations.append(("missing", missing, "missing fields"))

        for name, value in (
            ("bool", True),
            ("number", 1.2),
            ("noncanonical", "1.20"),
            ("negative", "-1"),
        ):
            malformed = copy.deepcopy(self.valid_document)
            malformed["actions"][0]["automatic_share_ratio"] = value
            mutations.append((name, malformed, "decimal|positive"))

        future_publication = copy.deepcopy(self.valid_document)
        future_publication["actions"][0]["source_published_at"] = "2025-01-13"
        mutations.append(
            ("future publication", future_publication, "cannot follow known_at")
        )

        for name, document, pattern in mutations:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                capture = self.capture(directory, document=document)
                with self.assertRaisesRegex(CorporateActionAdapterError, pattern):
                    parse_corporate_action_document(
                        self.encode(document),
                        capture=capture,
                    )

    def test_duplicate_action_revision_is_rejected(self) -> None:
        duplicated = copy.deepcopy(self.valid_document)
        duplicated["actions"].append(copy.deepcopy(duplicated["actions"][0]))
        with tempfile.TemporaryDirectory() as directory:
            capture = self.capture(directory, document=duplicated)
            with self.assertRaisesRegex(CorporateActionAdapterError, "duplicate"):
                parse_corporate_action_document(
                    self.encode(duplicated),
                    capture=capture,
                )

    def test_resolver_is_input_order_independent(self) -> None:
        document = copy.deepcopy(self.valid_document)
        second = copy.deepcopy(document["actions"][0])
        second.update(
            {
                "action_id": "fixture-action-2",
                "ex_date": "2025-01-25",
                "record_date": "2025-01-24",
                "payment_date": "2025-01-30",
                "share_listing_date": "2025-01-25",
                "effective_date": "2025-01-25",
                "automatic_share_ratio": "2",
                "cash_dividend_per_share": "0",
                "rights_entitlement_ratio": "0",
                "rights_subscription_price": None,
                "currency": None,
                "reference_price": None,
                "reference_price_snapshot_id": None,
            }
        )
        document["actions"].append(second)
        with tempfile.TemporaryDirectory() as directory:
            _, parsed = self.parsed(directory, document=document)
        forward = resolve_corporate_action_candidates(parsed.candidates, as_of=RETRIEVED)
        reverse = resolve_corporate_action_candidates(
            tuple(reversed(parsed.candidates)),
            as_of=RETRIEVED,
        )
        self.assertEqual(
            tuple(item.candidate_id for item in forward),
            tuple(item.candidate_id for item in reverse),
        )

    def test_future_cancellation_does_not_rewrite_earlier_as_of(self) -> None:
        cancellation = copy.deepcopy(self.valid_document)
        row = cancellation["actions"][0]
        row.update(
            {
                "lifecycle": "CANCELLED",
                "source_published_at": "2025-01-19",
                "automatic_share_ratio": None,
                "cash_dividend_per_share": None,
                "rights_entitlement_ratio": None,
                "rights_subscription_price": None,
                "currency": None,
                "reference_price": None,
                "reference_price_snapshot_id": None,
                "effective_date": None,
                "revision_id": "r2",
                "supersedes_revision_id": "r1",
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            _, first = self.parsed(directory)
            _, second = self.parsed(
                directory,
                document=cancellation,
                retrieved_at=utc_datetime(2025, 1, 20),
            )
        all_candidates = first.candidates + second.candidates
        before = resolve_corporate_action_candidates(
            all_candidates,
            as_of=utc_datetime(2025, 1, 15),
        )
        after = resolve_corporate_action_candidates(
            all_candidates,
            as_of=utc_datetime(2025, 1, 21),
        )
        self.assertEqual(before[0].lifecycle, CandidateCorporateActionLifecycle.EFFECTIVE)
        self.assertEqual(after[0].lifecycle, CandidateCorporateActionLifecycle.CANCELLED)

    def test_revision_graph_conflicts_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, document = self.parsed(directory)
        base = document.candidates[0]
        missing = replace(
            base,
            revision_id="r2",
            supersedes_revision_id="missing-r1",
        )
        cycle_a = replace(base, revision_id="r2", supersedes_revision_id="r3")
        cycle_b = replace(base, revision_id="r3", supersedes_revision_id="r2")
        disconnected = replace(
            base,
            revision_id="other-root",
            supersedes_revision_id=None,
        )
        for name, candidates in (
            ("missing predecessor", (missing,)),
            ("cycle", (cycle_a, cycle_b)),
            ("disconnected terminal", (base, disconnected)),
        ):
            with self.subTest(name=name), self.assertRaises(
                CorporateActionAdapterError
            ):
                resolve_corporate_action_candidates(candidates, as_of=RETRIEVED)

    def test_proposal_cannot_generate_adjustment_factor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capture = self.capture(directory, raw_bytes=self.proposal_raw)
            proposal_document = parse_corporate_action_document(
                self.proposal_raw,
                capture=capture,
            )
            _, effective_document = self.parsed(directory)
        proposal = proposal_document.candidates[0]
        self.assertIn("ACTION_NOT_IMPLEMENTED", proposal.gaps)
        self.assertIn("MISSING_EX_DATE", proposal.gaps)
        with self.assertRaisesRegex(CorporateActionAdapterError, "requires ex_date"):
            proposal.to_core_fact()

        full_proposal = replace(
            effective_document.candidates[0],
            lifecycle=CandidateCorporateActionLifecycle.PROPOSED,
        )
        self.assertIn("ACTION_NOT_IMPLEMENTED", full_proposal.gaps)
        core = full_proposal.to_core_fact()
        self.assertEqual(core.lifecycle, CorporateActionLifecycle.ANNOUNCED)
        with self.assertRaisesRegex(
            Exception,
            "only EFFECTIVE",
        ):
            core.backward_price_multiplier(AdjustmentBasis.TOTAL_RETURN)

    def test_missing_terms_are_explicit_derived_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, document = self.parsed(directory)
        base = document.candidates[0]
        cases = (
            (
                replace(base, rights_subscription_price=None),
                "MISSING_RIGHTS_SUBSCRIPTION_PRICE",
            ),
            (
                replace(
                    base,
                    reference_price=None,
                    reference_price_snapshot_id=None,
                ),
                "MISSING_REFERENCE_PRICE",
            ),
            (replace(base, share_listing_date=None), "MISSING_SHARE_LISTING_DATE"),
            (replace(base, record_date=None), "MISSING_RECORD_DATE"),
            (replace(base, effective_date=None), "MISSING_EFFECTIVE_DATE"),
        )
        for candidate, gap in cases:
            with self.subTest(gap=gap):
                self.assertIn(gap, candidate.gaps)
        with self.assertRaisesRegex(TypeError, "init=False"):
            replace(base, gaps=())
        with self.assertRaisesRegex(TypeError, "init=False"):
            replace(document, gaps=())

    def test_noop_effective_candidate_is_explicitly_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, document = self.parsed(directory)
        no_op = replace(
            document.candidates[0],
            automatic_share_ratio=Decimal(1),
            cash_dividend_per_share=Decimal(0),
            rights_entitlement_ratio=Decimal(0),
            rights_subscription_price=None,
            currency=None,
            reference_price=None,
            reference_price_snapshot_id=None,
            share_listing_date=None,
        )
        self.assertIn("NO_EFFECTIVE_ECONOMIC_TERMS", no_op.gaps)
        with self.assertRaisesRegex(CorporateActionAdapterError, "no-op"):
            no_op.to_core_fact()

    def test_symbol_change_keeps_stable_instrument_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, document = self.parsed(directory)
        original = document.candidates[0]
        renamed = replace(
            original,
            action_id="after-symbol-change",
            identity_fact_id="c" * 64,
            symbol="600001.SH",
        )
        self.assertEqual(original.instrument_id, renamed.instrument_id)
        self.assertNotEqual(original.symbol, renamed.symbol)
        self.assertNotEqual(original.identity_fact_id, renamed.identity_fact_id)

    def test_synthetic_candidate_and_document_cannot_be_relabelled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, document = self.parsed(directory)
        with self.assertRaisesRegex(CorporateActionAdapterError, "cannot be relabelled"):
            replace(document.candidates[0], synthetic_fixture=False)
        with self.assertRaisesRegex(CorporateActionAdapterError, "cannot be relabelled"):
            replace(document, synthetic_fixture=False)


class TestOfflineCli(CorporateActionAdapterFixtures):
    def command(self, output_root: str | Path) -> list[str]:
        return [
            sys.executable,
            str(SCRIPT),
            "--output-root",
            str(output_root),
            "--input-file",
            str(VALID_FIXTURE),
            "--url",
            SSE_JSON_URL,
            "--source-owner",
            "SSE",
            "--source-family",
            "SSE_LISTED_COMPANY_ANNOUNCEMENT",
            "--source-version",
            "fixture-cli-v1",
            "--raw-format",
            "JSON",
        ]

    def test_cli_offline_capture_writes_candidate_artifacts_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                self.command(directory),
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["candidate_count"], 1)
            self.assertEqual(payload["extraction_status"], "PARSED")
            self.assertEqual(
                payload["evidence_boundary"],
                "CONTRACT_ONLY / SYNTHETIC_VALIDATED / LICENSE_PENDING / T3_NOT_REACHED",
            )
            output_files = tuple(Path(directory).rglob("*"))
            self.assertTrue(any(path.is_file() for path in output_files))
            self.assertFalse(any(path.suffix == ".db" for path in output_files))

    def test_cli_refuses_unsafe_output_and_has_no_database_or_trust_switch(self) -> None:
        help_result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(help_result.returncode, 0)
        lowered = help_result.stdout.lower()
        for forbidden in (
            "database",
            "--apply",
            "trust-tier",
            "verified",
            "complete",
            "research-grade",
        ):
            self.assertNotIn(forbidden, lowered)

        with tempfile.TemporaryDirectory() as directory:
            output_file = Path(directory) / "not-a-directory"
            output_file.write_text("occupied", encoding="utf-8")
            result = subprocess.run(
                self.command(output_file),
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be a directory", result.stderr)


if __name__ == "__main__":
    unittest.main()

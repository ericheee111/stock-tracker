from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

from _helpers import utc_datetime

from stock_tracker.core.types import Market
from stock_tracker.quant.core.universe import (
    InstrumentIdentityFact,
    SecurityType,
)
from stock_tracker.quant.data.corporate_action_adapter import (
    CorporateActionSourceFamily,
    CorporateActionSourceOwner,
    RawCorporateActionFormat,
    capture_corporate_action_raw,
    digest_request_payload,
)
from stock_tracker.quant.data.corporate_action_extraction import (
    CorporateActionExtractionError,
    ExtractionMethod,
    IdentityBindingStatus,
    SourceSecurityIdentityMapping,
    bind_extracted_document,
    parse_frozen_html_document,
    parse_structured_extraction_document,
    resolve_extracted_rows_as_of,
    write_extraction_descriptor,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "corporate_action_extraction"
HTML_FIXTURE = FIXTURES / "valid_table.html"
MANUAL_FIXTURE = FIXTURES / "valid_manual.json"
PDF_FIXTURE = FIXTURES / "sample.pdf"
SCRIPT = ROOT / "scripts" / "extract_a_share_corporate_actions.py"
RETRIEVED = utc_datetime(2025, 1, 12)
AS_OF = utc_datetime(2025, 2, 1)
IDENTITY_KNOWN_AT = utc_datetime(2024, 12, 1)
SSE_HTML_URL = (
    "https://www.sse.com.cn/disclosure/listedinfo/announcement/"
    "c/new/2025-01-10/fixture.html"
)
SSE_PDF_URL = (
    "https://www.sse.com.cn/disclosure/listedinfo/announcement/"
    "c/new/2025-01-10/fixture.pdf"
)


class ExtractionFixtures(unittest.TestCase):
    def capture_html(self, root: str | Path):
        return capture_corporate_action_raw(
            root,
            raw_bytes=HTML_FIXTURE.read_bytes(),
            request_url=SSE_HTML_URL,
            request_method="GET",
            request_payload_digest=digest_request_payload({}),
            response_status=200,
            response_headers={"Content-Type": "text/html; charset=utf-8"},
            redirect_chain=(),
            retrieved_at=RETRIEVED,
            source_owner=CorporateActionSourceOwner.SSE,
            source_family=(
                CorporateActionSourceFamily.SSE_LISTED_COMPANY_ANNOUNCEMENT
            ),
            source_version="sse-fixture-v1",
            raw_format=RawCorporateActionFormat.HTML,
        )

    def capture_pdf(self, root: str | Path):
        return capture_corporate_action_raw(
            root,
            raw_bytes=PDF_FIXTURE.read_bytes(),
            request_url=SSE_PDF_URL,
            request_method="GET",
            request_payload_digest=digest_request_payload({}),
            response_status=200,
            response_headers={"Content-Type": "application/pdf"},
            redirect_chain=(),
            retrieved_at=RETRIEVED,
            source_owner=CorporateActionSourceOwner.SSE,
            source_family=CorporateActionSourceFamily.SSE_ANNOUNCEMENT_ATTACHMENT,
            source_version="sse-fixture-v1",
            raw_format=RawCorporateActionFormat.PDF,
        )

    def parse_html(self, root: str | Path):
        capture = self.capture_html(root)
        document = parse_frozen_html_document(
            HTML_FIXTURE.read_bytes(),
            capture=capture,
            extractor_version="html-fixture-v1",
            reviewer_note="synthetic frozen HTML table",
            extracted_at=utc_datetime(2025, 1, 13),
        )
        descriptor = write_extraction_descriptor(
            root,
            capture=capture,
            extraction_payload=HTML_FIXTURE.read_bytes(),
            document=document,
        )
        return capture, document, descriptor

    def identity(
        self,
        *,
        instrument_id: str = "CN:SSE:fixture-security-1",
        symbol: str = "600000.SH",
        effective_from: date = date(2020, 1, 1),
        effective_to: date | None = None,
        known_at=IDENTITY_KNOWN_AT,
    ) -> InstrumentIdentityFact:
        return InstrumentIdentityFact(
            instrument_id=instrument_id,
            symbol=symbol,
            market=Market.A,
            exchange="SSE",
            security_type=SecurityType.COMMON_EQUITY,
            effective_from=effective_from,
            effective_to=effective_to,
            known_at=known_at,
            usable_from=known_at,
            source="fixture-identity",
            revision="identity-r1",
            verified=True,
            source_note="synthetic identity fixture",
        )

    def mapping(
        self,
        identity: InstrumentIdentityFact,
        *,
        source_security_id: str = "SSE:600000",
        known_at=IDENTITY_KNOWN_AT,
    ) -> SourceSecurityIdentityMapping:
        return SourceSecurityIdentityMapping(
            source_owner="SSE",
            source_security_id=source_security_id,
            identity_fact_id=identity.fact_id,
            mapping_policy_version="stage2d-identity-binding-v1",
            known_at=known_at,
            usable_from=known_at,
        )


class TestFrozenExtraction(ExtractionFixtures):
    def test_html_rows_are_source_native_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capture, document, _ = self.parse_html(directory)
            replay = parse_frozen_html_document(
                HTML_FIXTURE.read_bytes(),
                capture=capture,
                extractor_version="html-fixture-v1",
                reviewer_note="synthetic frozen HTML table",
                extracted_at=utc_datetime(2025, 1, 13),
            )
        self.assertEqual(document.document_id, replay.document_id)
        self.assertEqual(len(document.rows), 1)
        row = document.rows[0]
        self.assertEqual(row.source_security_id, "SSE:600000")
        self.assertFalse(hasattr(row, "instrument_id"))
        self.assertFalse(hasattr(row, "identity_fact_id"))
        self.assertEqual(row.gaps, ())
        with self.assertRaisesRegex(TypeError, "init=False"):
            replace(row, gaps=())
        with self.assertRaisesRegex(TypeError, "init=False"):
            replace(document, document_id="a" * 64)

    def test_parser_rejects_unknown_headers_and_duplicate_revision(self) -> None:
        original = HTML_FIXTURE.read_bytes()
        unknown_header = original.replace(
            b"<th>source_locator</th>",
            b"<th>instrument_id</th>",
        )
        row_html = original.split(b"<tbody>", 1)[1].split(b"</tbody>", 1)[0]
        duplicate = original.replace(
            b"</tbody>",
            row_html + b"</tbody>",
            1,
        )
        for name, payload, pattern in (
            ("unknown", unknown_header, "headers"),
            ("duplicate", duplicate, "duplicate"),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                capture = capture_corporate_action_raw(
                    directory,
                    raw_bytes=payload,
                    request_url=SSE_HTML_URL,
                    request_method="GET",
                    request_payload_digest=digest_request_payload({}),
                    response_status=200,
                    response_headers={"Content-Type": "text/html"},
                    redirect_chain=(),
                    retrieved_at=RETRIEVED,
                    source_owner=CorporateActionSourceOwner.SSE,
                    source_family=(
                        CorporateActionSourceFamily.SSE_LISTED_COMPANY_ANNOUNCEMENT
                    ),
                    source_version="sse-fixture-v1",
                    raw_format=RawCorporateActionFormat.HTML,
                )
                with self.assertRaisesRegex(CorporateActionExtractionError, pattern):
                    parse_frozen_html_document(
                        payload,
                        capture=capture,
                        extractor_version="v1",
                        reviewer_note="fixture",
                        extracted_at=utc_datetime(2025, 1, 13),
                    )

    def test_structured_extraction_rejects_invalid_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capture = self.capture_pdf(directory)
            with self.assertRaisesRegex(CorporateActionExtractionError, "UTF-8"):
                parse_structured_extraction_document(
                    b"\xff",
                    capture=capture,
                    extraction_method=ExtractionMethod.STRUCTURED_MANUAL,
                    extracted_at=utc_datetime(2025, 1, 13),
                )

    def test_parser_requires_exact_captured_bytes(self) -> None:
        changed = HTML_FIXTURE.read_bytes().replace(
            b"sse-fixture-action-1",
            b"sse-fixture-action-2",
        )
        with tempfile.TemporaryDirectory() as directory:
            capture = self.capture_html(directory)
            with self.assertRaisesRegex(CorporateActionExtractionError, "raw capture"):
                parse_frozen_html_document(
                    changed,
                    capture=capture,
                    extractor_version="v1",
                    reviewer_note="fixture",
                    extracted_at=utc_datetime(2025, 1, 13),
                )

    def test_structured_binary_extraction_is_explicit_and_strict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capture = self.capture_pdf(directory)
            document = parse_structured_extraction_document(
                MANUAL_FIXTURE.read_bytes(),
                capture=capture,
                extraction_method=ExtractionMethod.STRUCTURED_MANUAL,
                extracted_at=utc_datetime(2025, 1, 13),
            )
            descriptor = write_extraction_descriptor(
                directory,
                capture=capture,
                extraction_payload=MANUAL_FIXTURE.read_bytes(),
                document=document,
            )
            changed_payload_value = json.loads(
                MANUAL_FIXTURE.read_text(encoding="utf-8")
            )
            changed_payload_value["reviewer_note"] = (
                "different synthetic reviewer note"
            )
            changed_payload = (
                json.dumps(
                    changed_payload_value,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
            changed_document = parse_structured_extraction_document(
                changed_payload,
                capture=capture,
                extraction_method=ExtractionMethod.STRUCTURED_MANUAL,
                extracted_at=utc_datetime(2025, 1, 13),
            )
            second = write_extraction_descriptor(
                directory,
                capture=capture,
                extraction_payload=changed_payload,
                document=changed_document,
            )
            with self.assertRaisesRegex(
                CorporateActionExtractionError,
                "deterministic extraction replay",
            ):
                write_extraction_descriptor(
                    directory,
                    capture=capture,
                    extraction_payload=MANUAL_FIXTURE.read_bytes(),
                    document=changed_document,
                )
        self.assertNotEqual(descriptor.descriptor_id, second.descriptor_id)
        self.assertNotEqual(
            descriptor.extraction_payload_id,
            second.extraction_payload_id,
        )
        self.assertNotEqual(
            descriptor.extracted_document_id,
            second.extracted_document_id,
        )

    def test_structured_rows_cannot_self_assert_internal_identity(self) -> None:
        value = json.loads(MANUAL_FIXTURE.read_text(encoding="utf-8"))
        value["rows"][0]["instrument_id"] = "forged"
        payload = json.dumps(value).encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            capture = self.capture_pdf(directory)
            with self.assertRaisesRegex(CorporateActionExtractionError, "unknown fields"):
                parse_structured_extraction_document(
                    payload,
                    capture=capture,
                    extraction_method=ExtractionMethod.STRUCTURED_MANUAL,
                    extracted_at=utc_datetime(2025, 1, 13),
                )

    def test_structured_decimal_bool_float_nonfinite_fail_closed(self) -> None:
        base = json.loads(MANUAL_FIXTURE.read_text(encoding="utf-8"))
        cases = (
            (True, "canonical decimal"),
            (1.2, "canonical decimal"),
            ("1.20", "canonical decimal"),
        )
        for value, pattern in cases:
            document = copy.deepcopy(base)
            document["rows"][0]["automatic_share_ratio"] = value
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                capture = self.capture_pdf(directory)
                with self.assertRaisesRegex(CorporateActionExtractionError, pattern):
                    parse_structured_extraction_document(
                        json.dumps(document).encode("utf-8"),
                        capture=capture,
                        extraction_method=ExtractionMethod.STRUCTURED_MANUAL,
                        extracted_at=utc_datetime(2025, 1, 13),
                    )
        nonfinite = MANUAL_FIXTURE.read_bytes().replace(
            b'"automatic_share_ratio": "1.2"',
            b'"automatic_share_ratio": NaN  ',
        )
        with tempfile.TemporaryDirectory() as directory:
            capture = self.capture_pdf(directory)
            with self.assertRaisesRegex(CorporateActionExtractionError, "non-finite"):
                parse_structured_extraction_document(
                    nonfinite,
                    capture=capture,
                    extraction_method=ExtractionMethod.STRUCTURED_MANUAL,
                    extracted_at=utc_datetime(2025, 1, 13),
                )


class TestIdentityBinding(ExtractionFixtures):
    def test_explicit_mapping_binds_active_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capture, document, descriptor = self.parse_html(directory)
            identity = self.identity()
            bundle = bind_extracted_document(
                document,
                extraction_descriptor=descriptor,
                capture=capture,
                identities=(identity,),
                mappings=(self.mapping(identity),),
                as_of=AS_OF,
            )
        self.assertEqual(bundle.gaps, ())
        self.assertEqual(len(bundle.candidates), 1)
        self.assertEqual(bundle.candidates[0].instrument_id, identity.instrument_id)
        self.assertEqual(bundle.candidates[0].identity_fact_id, identity.fact_id)
        self.assertEqual(bundle.bindings[0].status, IdentityBindingStatus.BOUND)
        self.assertFalse(hasattr(bundle, "verified"))
        self.assertFalse(hasattr(bundle, "complete"))
        self.assertFalse(hasattr(bundle, "trust_tier"))
        with self.assertRaisesRegex(TypeError, "init=False"):
            replace(bundle, gaps=())

    def test_missing_ambiguous_future_inactive_and_mismatch_are_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capture, document, descriptor = self.parse_html(directory)
            active = self.identity()
            alternative = self.identity(instrument_id="CN:SSE:other-security")
            future = self.identity(known_at=utc_datetime(2025, 3, 1))
            inactive = self.identity(effective_from=date(2025, 1, 20))
            wrong_symbol = self.identity(symbol="600001.SH")
            cases = (
                ((), (active,), IdentityBindingStatus.UNBOUND),
                (
                    (self.mapping(active), self.mapping(alternative)),
                    (active, alternative),
                    IdentityBindingStatus.AMBIGUOUS,
                ),
                ((self.mapping(future),), (future,), IdentityBindingStatus.FUTURE),
                ((self.mapping(inactive),), (inactive,), IdentityBindingStatus.INACTIVE),
                (
                    (self.mapping(wrong_symbol),),
                    (wrong_symbol,),
                    IdentityBindingStatus.IDENTITY_MISMATCH,
                ),
            )
            for mappings, identities, expected in cases:
                with self.subTest(expected=expected):
                    bundle = bind_extracted_document(
                        document,
                        extraction_descriptor=descriptor,
                        capture=capture,
                        identities=identities,
                        mappings=mappings,
                        as_of=AS_OF,
                    )
                    self.assertEqual(bundle.candidates, ())
                    self.assertEqual(bundle.bindings[0].status, expected)
                    self.assertTrue(bundle.gaps)

    def test_symbol_rename_reuse_and_historical_delisting_are_not_conflated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capture, document, descriptor = self.parse_html(directory)
            historical = self.identity(effective_to=date(2025, 1, 31))
            bundle = bind_extracted_document(
                document,
                extraction_descriptor=descriptor,
                capture=capture,
                identities=(historical,),
                mappings=(self.mapping(historical),),
                as_of=AS_OF,
            )
            self.assertEqual(bundle.candidates[0].instrument_id, historical.instrument_id)

            reused = self.identity(
                instrument_id="CN:SSE:new-security-reusing-code",
                effective_from=date(2025, 2, 1),
            )
            wrong = bind_extracted_document(
                document,
                extraction_descriptor=descriptor,
                capture=capture,
                identities=(reused,),
                mappings=(self.mapping(reused),),
                as_of=AS_OF,
            )
            self.assertEqual(wrong.candidates, ())
            self.assertEqual(wrong.bindings[0].status, IdentityBindingStatus.INACTIVE)

            forged_row = replace(document.rows[0], symbol="600001.SH")
            forged_document = replace(document, rows=(forged_row,))
            renamed_identity = self.identity(symbol="600001.SH")
            with self.assertRaisesRegex(
                CorporateActionExtractionError,
                "does not bind extracted document",
            ):
                bind_extracted_document(
                    forged_document,
                    extraction_descriptor=descriptor,
                    capture=capture,
                    identities=(renamed_identity,),
                    mappings=(self.mapping(renamed_identity),),
                    as_of=AS_OF,
                )

            renamed_payload = HTML_FIXTURE.read_bytes().replace(
                b"600000.SH",
                b"600001.SH",
            )
            renamed_capture = capture_corporate_action_raw(
                directory,
                raw_bytes=renamed_payload,
                request_url=SSE_HTML_URL,
                request_method="GET",
                request_payload_digest=digest_request_payload({}),
                response_status=200,
                response_headers={"Content-Type": "text/html; charset=utf-8"},
                redirect_chain=(),
                retrieved_at=RETRIEVED,
                source_owner=CorporateActionSourceOwner.SSE,
                source_family=(
                    CorporateActionSourceFamily.SSE_LISTED_COMPANY_ANNOUNCEMENT
                ),
                source_version="sse-fixture-v1",
                raw_format=RawCorporateActionFormat.HTML,
            )
            renamed_document = parse_frozen_html_document(
                renamed_payload,
                capture=renamed_capture,
                extractor_version="html-fixture-v1",
                reviewer_note="synthetic renamed-symbol fixture",
                extracted_at=utc_datetime(2025, 1, 13),
            )
            renamed_descriptor = write_extraction_descriptor(
                directory,
                capture=renamed_capture,
                extraction_payload=renamed_payload,
                document=renamed_document,
            )
            renamed_bundle = bind_extracted_document(
                renamed_document,
                extraction_descriptor=renamed_descriptor,
                capture=renamed_capture,
                identities=(renamed_identity,),
                mappings=(self.mapping(renamed_identity),),
                as_of=AS_OF,
            )
            self.assertEqual(
                renamed_bundle.candidates[0].instrument_id,
                renamed_identity.instrument_id,
            )

    def test_revision_graph_resolves_terminal_before_date_use(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, document, _ = self.parse_html(directory)
        first = document.rows[0]
        moved = replace(
            first,
            revision_id="r2",
            supersedes_revision_id="r1",
            ex_date=date(2025, 2, 15),
        )
        selected = resolve_extracted_rows_as_of((first, moved))
        self.assertEqual(selected, (moved,))
        missing = replace(
            first,
            revision_id="r2",
            supersedes_revision_id="missing",
        )
        with self.assertRaises(CorporateActionExtractionError):
            resolve_extracted_rows_as_of((missing,))


class TestExtractionCli(ExtractionFixtures):
    def test_cli_is_offline_and_has_no_database_or_trust_switches(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lowered = result.stdout.lower()
        for forbidden in (
            "--database",
            "--apply",
            "--verified",
            "--complete",
            "--trust",
            "--promote",
            "--url",
        ):
            self.assertNotIn(forbidden, lowered)

    def test_cli_extracts_existing_raw_descriptor_only(self) -> None:
        with tempfile.TemporaryDirectory() as artifact_directory, tempfile.TemporaryDirectory() as input_directory:
            capture = self.capture_html(artifact_directory)
            input_path = Path(input_directory) / "fixture.html"
            input_path.write_bytes(HTML_FIXTURE.read_bytes())
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--artifact-root",
                    artifact_directory,
                    "--raw-descriptor-key",
                    capture.descriptor_key,
                    "--extraction-input",
                    str(input_path),
                    "--method",
                    "FROZEN_HTML_TABLE",
                    "--extractor-version",
                    "fixture-v1",
                    "--reviewer-note",
                    "synthetic CLI fixture",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["row_count"], 1)
        self.assertIn("T3_NOT_REACHED", payload["evidence_boundary"])


if __name__ == "__main__":
    unittest.main()

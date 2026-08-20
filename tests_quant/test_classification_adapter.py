from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

from stock_tracker.core.types import Market
from stock_tracker.quant.data.classification_adapter import (
    CLASSIFICATION_ADAPTER_VERSION,
    CLASSIFICATION_SOURCE_SCHEMA,
    ClassificationAdapterError,
    ClassificationArtifactDescriptor,
    ClassificationBindingReport,
    ClassificationCandidateBundle,
    ClassificationSourceDefinition,
    ClassificationSourceMembership,
    parse_classification_artifact,
    read_classification_descriptor,
)
from stock_tracker.quant.data.security_universe_adapter import (
    parse_security_universe_artifact,
    read_security_universe_descriptor,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "classification"
ARTIFACT = FIXTURES / "golden_capco.json"
DESCRIPTOR = FIXTURES / "golden_capco.descriptor.json"
SECURITY_ARTIFACT = (
    Path(__file__).parent / "fixtures" / "security_universe" / "golden_sse.json"
)
SECURITY_DESCRIPTOR = (
    Path(__file__).parent
    / "fixtures"
    / "security_universe"
    / "golden_sse.descriptor.json"
)
SESSION = date(2024, 1, 12)
AS_OF = datetime.fromisoformat("2024-01-12T16:00:00+08:00")
LATE = datetime.fromisoformat("2024-02-12T16:00:00+08:00")


class ClassificationAdapterFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = ARTIFACT.read_bytes()
        cls.document = json.loads(cls.raw.decode("utf-8"))
        cls.descriptor = read_classification_descriptor(DESCRIPTOR)
        security_raw = SECURITY_ARTIFACT.read_bytes()
        security_descriptor = read_security_universe_descriptor(
            SECURITY_DESCRIPTOR
        )
        cls.security_bundle = parse_security_universe_artifact(
            security_raw,
            security_descriptor,
        )

    @staticmethod
    def encode(document: dict[str, object]) -> bytes:
        return json.dumps(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    def descriptor_for(
        self,
        raw: bytes,
        *,
        retrieved_at: datetime | None = None,
        source: str = "CAPCO",
        source_version: str = "2024H1-fixture",
    ) -> ClassificationArtifactDescriptor:
        return replace(
            self.descriptor,
            source=source,
            source_version=source_version,
            retrieved_at=retrieved_at or self.descriptor.retrieved_at,
            artifact_sha256=hashlib.sha256(raw).hexdigest(),
            byte_size=len(raw),
        )

    def parse(
        self,
        *,
        document: dict[str, object] | None = None,
        identities=None,
        retrieved_at: datetime | None = None,
        source: str = "CAPCO",
        source_version: str = "2024H1-fixture",
    ) -> ClassificationCandidateBundle:
        raw = self.raw if document is None else self.encode(document)
        descriptor = self.descriptor_for(
            raw,
            retrieved_at=retrieved_at,
            source=source,
            source_version=source_version,
        )
        return parse_classification_artifact(
            raw,
            descriptor,
            self.security_bundle.identities if identities is None else identities,
        )


class TestClassificationExactRaw(ClassificationAdapterFixtures):
    def test_descriptor_binds_exact_bytes_and_round_trips(self) -> None:
        self.assertEqual(
            self.descriptor.artifact_sha256,
            hashlib.sha256(self.raw).hexdigest(),
        )
        self.assertEqual(self.descriptor.byte_size, len(self.raw))
        self.assertEqual(
            self.descriptor.schema_version,
            CLASSIFICATION_SOURCE_SCHEMA,
        )
        self.assertEqual(
            self.descriptor.parser_version,
            CLASSIFICATION_ADAPTER_VERSION,
        )
        self.assertEqual(
            self.descriptor,
            read_classification_descriptor(DESCRIPTOR),
        )

    def test_raw_and_descriptor_tamper_fail_closed(self) -> None:
        tampered = bytearray(self.raw)
        tampered[-2] = ord(" ") if tampered[-2] != ord(" ") else ord("\n")
        with self.assertRaisesRegex(ClassificationAdapterError, "SHA-256"):
            parse_classification_artifact(
                bytes(tampered),
                self.descriptor,
                self.security_bundle.identities,
            )
        with self.assertRaisesRegex(ClassificationAdapterError, "SHA-256"):
            parse_classification_artifact(
                self.raw,
                replace(self.descriptor, artifact_sha256="f" * 64),
                self.security_bundle.identities,
            )
        with self.assertRaisesRegex(ClassificationAdapterError, "byte_size"):
            parse_classification_artifact(
                self.raw,
                replace(self.descriptor, byte_size=len(self.raw) + 1),
                self.security_bundle.identities,
            )

    def test_descriptor_unknown_fields_and_synthetic_relabel_are_rejected(self) -> None:
        payload = json.loads(DESCRIPTOR.read_text(encoding="utf-8"))
        payload["verified"] = True
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "descriptor.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ClassificationAdapterError, "unknown"):
                read_classification_descriptor(path)
        with self.assertRaisesRegex(ClassificationAdapterError, "synthetic-only"):
            replace(self.descriptor, synthetic=False)

    def test_same_semantics_different_row_order_preserves_normalized_identity(self) -> None:
        reverse = copy.deepcopy(self.document)
        reverse["classifications"] = list(reversed(reverse["classifications"]))
        reverse["memberships"] = list(reversed(reverse["memberships"]))
        forward_bundle = self.parse()
        reverse_bundle = self.parse(document=reverse)
        self.assertEqual(
            forward_bundle.normalized_dataset_id,
            reverse_bundle.normalized_dataset_id,
        )
        self.assertNotEqual(
            forward_bundle.descriptor.descriptor_id,
            reverse_bundle.descriptor.descriptor_id,
        )
        self.assertNotEqual(forward_bundle.bundle_id, reverse_bundle.bundle_id)


class TestClassificationSourceBoundary(ClassificationAdapterFixtures):
    def test_source_membership_cannot_assert_stable_identity_or_trust(self) -> None:
        for field_name, value in (
            ("instrument_id", "CN:SSE:FORGED"),
            ("identity_fact_id", "f" * 64),
            ("verified", True),
            ("complete", True),
            ("trust_tier", "RESEARCH_GRADE"),
        ):
            document = copy.deepcopy(self.document)
            document["memberships"][0][field_name] = value
            with self.subTest(field=field_name), self.assertRaisesRegex(
                ClassificationAdapterError,
                "unknown fields",
            ):
                self.parse(document=document)

    def test_source_and_taxonomy_version_must_match_descriptor(self) -> None:
        with self.assertRaisesRegex(ClassificationAdapterError, "owner"):
            self.parse(source="OTHER")
        with self.assertRaisesRegex(ClassificationAdapterError, "version"):
            self.parse(source_version="other-v1")

    def test_synthetic_artifact_cannot_be_relabelled_real(self) -> None:
        document = copy.deepcopy(self.document)
        document["synthetic_fixture"] = False
        with self.assertRaisesRegex(ClassificationAdapterError, "synthetic"):
            self.parse(document=document)

    def test_unknown_duplicate_invalid_dates_and_direct_constructor_bypass(self) -> None:
        unknown = copy.deepcopy(self.document)
        unknown["taxonomy"]["unexpected"] = 1
        with self.assertRaisesRegex(ClassificationAdapterError, "unknown"):
            self.parse(document=unknown)

        malformed = self.raw.replace(
            b'"taxonomy_id": "CAPCO-INDUSTRY",',
            b'"taxonomy_id": "CAPCO-INDUSTRY",\n    "taxonomy_id": "DUP",',
        )
        with self.assertRaisesRegex(ClassificationAdapterError, "duplicate JSON"):
            parse_classification_artifact(
                malformed,
                self.descriptor_for(malformed),
                self.security_bundle.identities,
            )

        with self.assertRaisesRegex(ClassificationAdapterError, "precede"):
            ClassificationSourceDefinition(
                classification_id="C39",
                name="fixture",
                parent_classification_id=None,
                effective_from=date(2024, 2, 1),
                effective_to=date(2024, 1, 1),
                revision="r1",
                supersedes=None,
            )
        with self.assertRaisesRegex(ClassificationAdapterError, "supersede itself"):
            ClassificationSourceMembership(
                classification_id="C39",
                source_security_id="SSECID-ALPHA",
                symbol="600101.SH",
                exchange="SSE",
                effective_from=date(2024, 1, 1),
                effective_to=None,
                state=__import__(
                    "stock_tracker.quant.core.classification",
                    fromlist=["ClassificationMembershipState"],
                ).ClassificationMembershipState.INCLUDED,
                revision="r1",
                supersedes="r1",
            )


class TestClassificationIdentityBinding(ClassificationAdapterFixtures):
    def test_golden_bundle_is_snapshot_compatible_but_never_promoted(self) -> None:
        bundle = self.parse()
        self.assertFalse(bundle.report.has_snapshot_blockers)
        self.assertIn("LICENSE_PENDING", bundle.report.trust_blocker_codes)
        self.assertIn("T3_NOT_REACHED", bundle.report.trust_blocker_codes)
        self.assertFalse(bundle.coverage.complete)
        self.assertFalse(bundle.coverage.verified)
        self.assertTrue(all(not item.verified for item in bundle.classifications))
        self.assertTrue(all(not item.verified for item in bundle.memberships))
        self.assertFalse(bundle.as_dict()["complete"])
        self.assertFalse(bundle.as_dict()["verified"])
        self.assertEqual(bundle.as_dict()["trust_state"], "T3_NOT_REACHED")

        snapshot = bundle.historical_classification().snapshot(
            Market.A,
            SESSION,
            AS_OF,
            require_verified=False,
            require_complete=False,
        )
        self.assertEqual(snapshot.classification_members("C39"), ("600101.SH",))
        self.assertEqual(snapshot.classification_members("I65"), ("600300.SH",))

    def test_missing_hidden_inactive_symbol_and_ambiguous_identity_are_reported(self) -> None:
        alpha = next(
            item
            for item in self.security_bundle.identities
            if item.source_security_id == "SSECID-ALPHA"
            and item.symbol == "600101.SH"
        )
        gamma = next(
            item
            for item in self.security_bundle.identities
            if item.source_security_id == "SSECID-GAMMA"
        )
        future = datetime.fromisoformat("2024-02-01T08:00:00+08:00")
        cases = (
            ((gamma,), "missing_identity"),
            (
                (
                    replace(
                        alpha,
                        provenance=replace(
                            alpha.provenance,
                            observed_at=future,
                            retrieved_at=future,
                            known_at=future,
                            usable_from=future,
                        ),
                    ),
                    gamma,
                ),
                "missing_identity",
            ),
            (
                (
                    replace(alpha, effective_from=date(2024, 1, 9)),
                    gamma,
                ),
                "inactive_identity",
            ),
            ((replace(alpha, symbol="600102.SH"), gamma), "symbol_mismatch"),
            ((alpha, alpha, gamma), "ambiguous_identity"),
        )
        for identities, field_name in cases:
            with self.subTest(field=field_name):
                bundle = self.parse(identities=identities)
                self.assertTrue(bundle.report.has_snapshot_blockers)
                self.assertGreaterEqual(len(getattr(bundle.report, field_name)), 1)
                self.assertFalse(bundle.as_dict()["snapshot_constructible"])

    def test_binding_report_cannot_be_replaced_to_hide_unbound_rows(self) -> None:
        gamma = next(
            item
            for item in self.security_bundle.identities
            if item.source_security_id == "SSECID-GAMMA"
        )
        blocked = self.parse(identities=(gamma,))
        forged = ClassificationBindingReport(
            missing_identity=(),
            inactive_identity=(),
            symbol_mismatch=(),
            ambiguous_identity=(),
            declared_gaps=blocked.declared_gaps,
        )
        with self.assertRaisesRegex(
            ClassificationAdapterError,
            "every unbound source membership",
        ):
            replace(blocked, report=forged)
        with self.assertRaisesRegex(
            ClassificationAdapterError,
            "strict parser",
        ):
            replace(
                blocked,
                declared_gaps=(),
                report=replace(blocked.report, declared_gaps=()),
            )

    def test_symbol_rename_and_reused_code_follow_stage2a_identity(self) -> None:
        alpha = [
            item
            for item in self.security_bundle.identities
            if item.source_security_id == "SSECID-ALPHA"
        ]
        old = next(item for item in alpha if item.symbol == "600100.SH")
        current = next(item for item in alpha if item.symbol == "600101.SH")
        self.assertEqual(old.instrument_id, current.instrument_id)
        bundle = self.parse(identities=(old, current))
        membership = bundle.memberships[0]
        self.assertEqual(membership.instrument_id, current.instrument_id)
        self.assertEqual(membership.symbol, current.symbol)

        reused = replace(
            current,
            source_security_id="SSECID-REUSED",
            instrument_id="CN:SSE:SSECID-REUSED",
        )
        bundle_with_reuse = self.parse(identities=(old, current, reused))
        self.assertEqual(
            bundle_with_reuse.memberships[0].instrument_id,
            current.instrument_id,
        )
        self.assertNotEqual(
            bundle_with_reuse.memberships[0].instrument_id,
            reused.instrument_id,
        )

    def test_bundle_direct_construction_cannot_promote_or_relabel_stream(self) -> None:
        bundle = self.parse()
        with self.assertRaisesRegex(ClassificationAdapterError, "promote"):
            replace(
                bundle,
                coverage=replace(bundle.coverage, complete=True, source_note="forged"),
            )
        with self.assertRaisesRegex(ClassificationAdapterError, "stream"):
            replace(
                bundle,
                taxonomy=replace(bundle.taxonomy, owner="OTHER"),
            )
        with self.assertRaisesRegex(ClassificationAdapterError, "sorted"):
            replace(bundle, classifications=tuple(reversed(bundle.classifications)))

    def test_adapter_does_not_depend_on_runtime_static_sector_map(self) -> None:
        source = (
            ROOT / "stock_tracker" / "quant" / "data" / "classification_adapter.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("_SECTOR_MAP", source)
        self.assertNotIn("stock_tracker.core.sector", source)
        self.assertNotIn("stock_tracker.services.sector", source)


if __name__ == "__main__":
    unittest.main()

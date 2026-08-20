from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from _helpers import utc_datetime
from test_corporate_action_extraction import AS_OF, ExtractionFixtures

from stock_tracker.quant.data.corporate_action_adapter import (
    CorporateActionSourceFamily,
    CorporateActionSourceOwner,
)
from stock_tracker.quant.data.corporate_action_extraction import (
    BoundCorporateActionCandidateBundle,
)
from stock_tracker.quant.data.corporate_action_reconciliation import (
    CandidateActionMapping,
    ConflictKind,
    CorporateActionReconciliationError,
    CoverageClaimCandidate,
    LicenseStatus,
    PromotionEligibilityStatus,
    ReconciliationPolicy,
    reconcile_corporate_actions,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "reconcile_a_share_corporate_actions.py"


def _time_text(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _date_text(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if value == 0 else text


class ReconciliationFixtures(ExtractionFixtures):
    def sse_bundle(self) -> BoundCorporateActionCandidateBundle:
        import tempfile

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        capture, document, descriptor = self.parse_html(directory.name)
        identity = self.identity()
        return self._bind(
            document=document,
            extraction_descriptor=descriptor,
            capture=capture,
            identity=identity,
        )

    def _bind(self, *, document, extraction_descriptor, capture, identity):
        from stock_tracker.quant.data.corporate_action_extraction import (
            bind_extracted_document,
        )

        return bind_extracted_document(
            document,
            extraction_descriptor=extraction_descriptor,
            capture=capture,
            identities=(identity,),
            mappings=(self.mapping(identity),),
            as_of=AS_OF,
        )

    def cninfo_bundle(
        self,
        base: BoundCorporateActionCandidateBundle,
        *,
        candidate_change: dict[str, object] | None = None,
    ) -> BoundCorporateActionCandidateBundle:
        candidate = base.candidates[0]
        changes: dict[str, object] = {
            "action_id": "cninfo-fixture-action-1",
            "source_uri": "https://www.cninfo.com.cn/finalpage/2025-01-10/fixture.PDF",
            "raw_artifact_id": "c" * 64,
            "raw_descriptor_id": "d" * 64,
            "source_owner": CorporateActionSourceOwner.CNINFO,
            "source_family": (
                CorporateActionSourceFamily.CNINFO_DISCLOSURE_ATTACHMENT
            ),
            "source_version": "cninfo-fixture-v1",
        }
        if candidate_change:
            changes.update(candidate_change)
        candidate = replace(candidate, **changes)
        return replace(
            base,
            document_id="e" * 64,
            extraction_descriptor_id="9" * 64,
            raw_artifact_id="c" * 64,
            raw_descriptor_id="d" * 64,
            candidates=(candidate,),
        )

    @staticmethod
    def mappings(*bundles: BoundCorporateActionCandidateBundle):
        return tuple(
            CandidateActionMapping(
                candidate_id=candidate.candidate_id,
                logical_action_id="logical-action-1",
                mapping_policy_version="reconciliation-v1",
                mapping_note="explicit synthetic cross-source mapping",
            )
            for bundle in bundles
            for candidate in bundle.candidates
        )

    @staticmethod
    def claims(
        sse: BoundCorporateActionCandidateBundle,
        cninfo: BoundCorporateActionCandidateBundle,
        *,
        license_status: LicenseStatus = LicenseStatus.PENDING,
    ) -> tuple[CoverageClaimCandidate, ...]:
        return (
            CoverageClaimCandidate(
                instrument_id=sse.candidates[0].instrument_id,
                source_owner=CorporateActionSourceOwner.SSE,
                source_version=sse.candidates[0].source_version,
                start_date=date(2025, 1, 1),
                end_date=date(2025, 1, 31),
                known_at=utc_datetime(2025, 1, 31),
                usable_from=utc_datetime(2025, 1, 31),
                surveyed_source_event_ids=(sse.candidates[0].action_id,),
                coverage_note="synthetic SSE coverage claim",
                license_status=license_status,
            ),
            CoverageClaimCandidate(
                instrument_id=cninfo.candidates[0].instrument_id,
                source_owner=CorporateActionSourceOwner.CNINFO,
                source_version=cninfo.candidates[0].source_version,
                start_date=date(2025, 1, 1),
                end_date=date(2025, 1, 31),
                known_at=utc_datetime(2025, 1, 31),
                usable_from=utc_datetime(2025, 1, 31),
                surveyed_source_event_ids=(cninfo.candidates[0].action_id,),
                coverage_note="synthetic CNINFO coverage claim",
                license_status=license_status,
            ),
        )

    @staticmethod
    def policy(
        *,
        allow_synthetic: bool = False,
        require_license: bool = True,
        minimum_sources: int = 2,
    ) -> ReconciliationPolicy:
        return ReconciliationPolicy(
            policy_version="reconciliation-v1",
            required_primary_owners=(CorporateActionSourceOwner.SSE,),
            minimum_independent_sources=minimum_sources,
            require_reference_price_evidence=True,
            require_license_clearance=require_license,
            allow_synthetic_eligibility_test=allow_synthetic,
        )

    @staticmethod
    def candidate_dict(candidate) -> dict[str, object]:
        published = candidate.source_published_at
        if isinstance(published, datetime):
            published_value: str | None = _time_text(published)
        elif isinstance(published, date):
            published_value = published.isoformat()
        else:
            published_value = None
        return {
            "action_id": candidate.action_id,
            "instrument_id": candidate.instrument_id,
            "identity_fact_id": candidate.identity_fact_id,
            "symbol": candidate.symbol,
            "market": candidate.market.value,
            "exchange": candidate.exchange,
            "action_type": candidate.action_type.value,
            "lifecycle": candidate.lifecycle.value,
            "source_published_at": published_value,
            "source_published_granularity": (
                candidate.source_published_granularity.value
            ),
            "observed_at": _time_text(candidate.observed_at),
            "retrieved_at": _time_text(candidate.retrieved_at),
            "known_at": _time_text(candidate.known_at),
            "usable_from": _time_text(candidate.usable_from),
            "ex_date": _date_text(candidate.ex_date),
            "record_date": _date_text(candidate.record_date),
            "payment_date": _date_text(candidate.payment_date),
            "share_listing_date": _date_text(candidate.share_listing_date),
            "effective_date": _date_text(candidate.effective_date),
            "automatic_share_ratio": _decimal_text(
                candidate.automatic_share_ratio
            ),
            "cash_dividend_per_share": _decimal_text(
                candidate.cash_dividend_per_share
            ),
            "rights_entitlement_ratio": _decimal_text(
                candidate.rights_entitlement_ratio
            ),
            "rights_subscription_price": _decimal_text(
                candidate.rights_subscription_price
            ),
            "currency": candidate.currency,
            "reference_price": _decimal_text(candidate.reference_price),
            "reference_price_snapshot_id": (
                candidate.reference_price_snapshot_id
            ),
            "revision_id": candidate.revision_id,
            "supersedes_revision_id": candidate.supersedes_revision_id,
            "source_uri": candidate.source_uri,
            "raw_artifact_id": candidate.raw_artifact_id,
            "raw_descriptor_id": candidate.raw_descriptor_id,
            "parser_version": candidate.parser_version,
            "source_owner": candidate.source_owner.value,
            "source_family": candidate.source_family.value,
            "source_version": candidate.source_version,
            "synthetic_fixture": candidate.synthetic_fixture,
        }

    @classmethod
    def bundle_dict(cls, bundle) -> dict[str, object]:
        return {
            "document_id": bundle.document_id,
            "extraction_descriptor_id": bundle.extraction_descriptor_id,
            "raw_artifact_id": bundle.raw_artifact_id,
            "raw_descriptor_id": bundle.raw_descriptor_id,
            "mapping_policy_version": bundle.mapping_policy_version,
            "as_of": _time_text(bundle.as_of),
            "bindings": [
                {
                    "row_id": binding.row_id,
                    "status": binding.status.value,
                    "mapping_id": binding.mapping_id,
                    "identity_fact_id": binding.identity_fact_id,
                    "instrument_id": binding.instrument_id,
                    "reason": binding.reason,
                }
                for binding in bundle.bindings
            ],
            "candidates": [
                cls.candidate_dict(candidate) for candidate in bundle.candidates
            ],
            "synthetic_fixture": bundle.synthetic_fixture,
        }

    @staticmethod
    def mapping_dict(mapping) -> dict[str, object]:
        return {
            "candidate_id": mapping.candidate_id,
            "logical_action_id": mapping.logical_action_id,
            "mapping_policy_version": mapping.mapping_policy_version,
            "mapping_note": mapping.mapping_note,
        }

    @staticmethod
    def claim_dict(claim) -> dict[str, object]:
        return {
            "instrument_id": claim.instrument_id,
            "source_owner": claim.source_owner.value,
            "source_version": claim.source_version,
            "start_date": claim.start_date.isoformat(),
            "end_date": claim.end_date.isoformat(),
            "known_at": _time_text(claim.known_at),
            "usable_from": _time_text(claim.usable_from),
            "surveyed_source_event_ids": list(
                claim.surveyed_source_event_ids
            ),
            "coverage_note": claim.coverage_note,
            "license_status": claim.license_status.value,
            "synthetic_fixture": claim.synthetic_fixture,
        }

    @staticmethod
    def policy_dict(policy) -> dict[str, object]:
        return {
            "policy_version": policy.policy_version,
            "required_primary_owners": [
                owner.value for owner in policy.required_primary_owners
            ],
            "minimum_independent_sources": policy.minimum_independent_sources,
            "require_reference_price_evidence": (
                policy.require_reference_price_evidence
            ),
            "require_license_clearance": policy.require_license_clearance,
            "require_attachment_evidence": policy.require_attachment_evidence,
            "allow_synthetic_eligibility_test": (
                policy.allow_synthetic_eligibility_test
            ),
            "synthetic_fixture": policy.synthetic_fixture,
        }


class TestReconciliation(ReconciliationFixtures):
    def test_matching_sources_remain_not_eligible_while_license_pending(self) -> None:
        sse = self.sse_bundle()
        cninfo = self.cninfo_bundle(sse)
        report = reconcile_corporate_actions(
            bundles=(sse, cninfo),
            action_mappings=self.mappings(sse, cninfo),
            coverage_claims=self.claims(sse, cninfo),
            policy=self.policy(),
            as_of=AS_OF,
        )
        self.assertEqual(report.conflicts, ())
        self.assertEqual(
            report.eligibility.status,
            PromotionEligibilityStatus.NOT_ELIGIBLE,
        )
        self.assertIn("LICENSE_NOT_CLEARED", report.global_gaps)
        self.assertIn("SYNTHETIC_ONLY_NOT_PROMOTABLE", report.global_gaps)
        self.assertFalse(hasattr(report, "verified"))
        self.assertFalse(hasattr(report, "complete"))
        self.assertFalse(hasattr(report, "trust_tier"))

    def test_synthetic_policy_can_only_reach_independent_verification_queue(self) -> None:
        sse = self.sse_bundle()
        cninfo = self.cninfo_bundle(sse)
        report = reconcile_corporate_actions(
            bundles=(sse, cninfo),
            action_mappings=self.mappings(sse, cninfo),
            coverage_claims=self.claims(
                sse,
                cninfo,
                license_status=LicenseStatus.CLEARED_FOR_INTERNAL_RESEARCH,
            ),
            policy=self.policy(allow_synthetic=True),
            as_of=AS_OF,
        )
        self.assertEqual(report.global_gaps, ())
        self.assertEqual(
            report.eligibility.status,
            PromotionEligibilityStatus.ELIGIBLE_FOR_INDEPENDENT_VERIFICATION,
        )
        self.assertFalse(hasattr(report.eligibility, "verified"))
        self.assertFalse(hasattr(report.eligibility, "research_grade"))

    def test_conflicting_economic_or_date_fields_are_explicit(self) -> None:
        cases = (
            ({"ex_date": date(2025, 1, 16)}, ConflictKind.EX_DATE),
            ({"cash_dividend_per_share": 2}, ConflictKind.CASH_DIVIDEND),
            (
                {"lifecycle": "CANCELLED"},
                ConflictKind.LIFECYCLE,
            ),
        )
        for changes, expected_kind in cases:
            with self.subTest(kind=expected_kind):
                sse = self.sse_bundle()
                if changes.get("lifecycle") == "CANCELLED":
                    from stock_tracker.quant.data.corporate_action_adapter import (
                        CandidateCorporateActionLifecycle,
                    )

                    changes = {
                        "lifecycle": CandidateCorporateActionLifecycle.CANCELLED,
                        "automatic_share_ratio": None,
                        "cash_dividend_per_share": None,
                        "rights_entitlement_ratio": None,
                        "rights_subscription_price": None,
                        "currency": None,
                        "reference_price": None,
                        "reference_price_snapshot_id": None,
                        "effective_date": None,
                    }
                elif "cash_dividend_per_share" in changes:
                    changes = {"cash_dividend_per_share": Decimal(2)}
                cninfo = self.cninfo_bundle(sse, candidate_change=changes)
                report = reconcile_corporate_actions(
                    bundles=(sse, cninfo),
                    action_mappings=self.mappings(sse, cninfo),
                    coverage_claims=self.claims(sse, cninfo),
                    policy=self.policy(),
                    as_of=AS_OF,
                )
                kinds = {item.kind for item in report.conflicts}
                self.assertIn(expected_kind, kinds)
                self.assertEqual(
                    report.eligibility.status,
                    PromotionEligibilityStatus.NOT_ELIGIBLE,
                )

    def test_missing_primary_corroboration_coverage_and_reference_evidence_block(self) -> None:
        sse = self.sse_bundle()
        only_sse = reconcile_corporate_actions(
            bundles=(sse,),
            action_mappings=self.mappings(sse),
            coverage_claims=(),
            policy=self.policy(),
            as_of=AS_OF,
        )
        self.assertIn("NO_VISIBLE_COVERAGE_CLAIMS", only_sse.global_gaps)
        self.assertTrue(
            any(
                gap.startswith("INSUFFICIENT_INDEPENDENT_SOURCES")
                for gap in only_sse.global_gaps
            )
        )

        no_reference_candidate = replace(
            sse.candidates[0],
            reference_price=None,
            reference_price_snapshot_id=None,
        )
        no_reference = replace(sse, candidates=(no_reference_candidate,))
        report = reconcile_corporate_actions(
            bundles=(no_reference,),
            action_mappings=self.mappings(no_reference),
            coverage_claims=(),
            policy=self.policy(minimum_sources=1),
            as_of=AS_OF,
        )
        self.assertTrue(
            any(
                gap.startswith("MISSING_REFERENCE_PRICE_EVIDENCE")
                for gap in report.global_gaps
            )
        )

    def test_same_symbol_different_instrument_is_not_silently_merged(self) -> None:
        sse = self.sse_bundle()
        other_candidate = replace(
            sse.candidates[0],
            instrument_id="CN:SSE:different-instrument",
            identity_fact_id="f" * 64,
            action_id="other-source-action",
            raw_artifact_id="1" * 64,
            raw_descriptor_id="2" * 64,
        )
        other_binding = replace(
            sse.bindings[0],
            identity_fact_id="f" * 64,
            instrument_id="CN:SSE:different-instrument",
        )
        other = replace(
            sse,
            document_id="3" * 64,
            extraction_descriptor_id="8" * 64,
            raw_artifact_id="1" * 64,
            raw_descriptor_id="2" * 64,
            bindings=(other_binding,),
            candidates=(other_candidate,),
        )
        report = reconcile_corporate_actions(
            bundles=(sse, other),
            action_mappings=self.mappings(sse, other),
            coverage_claims=(),
            policy=self.policy(minimum_sources=1),
            as_of=AS_OF,
        )
        self.assertIn(ConflictKind.IDENTITY, {item.kind for item in report.conflicts})

    def test_future_correction_does_not_rewrite_earlier_report(self) -> None:
        sse = self.sse_bundle()
        original = sse.candidates[0]
        future = replace(
            original,
            revision_id="r2",
            supersedes_revision_id="r1",
            observed_at=utc_datetime(2025, 3, 1),
            retrieved_at=utc_datetime(2025, 3, 1),
            known_at=utc_datetime(2025, 3, 1),
            usable_from=utc_datetime(2025, 3, 1),
            ex_date=date(2025, 1, 16),
            raw_artifact_id="4" * 64,
            raw_descriptor_id="5" * 64,
        )
        future_bundle = replace(
            sse,
            document_id="6" * 64,
            extraction_descriptor_id="7" * 64,
            raw_artifact_id="4" * 64,
            raw_descriptor_id="5" * 64,
            as_of=utc_datetime(2025, 3, 2),
            candidates=(future,),
        )
        report = reconcile_corporate_actions(
            bundles=(sse, future_bundle),
            action_mappings=self.mappings(sse, future_bundle),
            coverage_claims=(),
            policy=self.policy(minimum_sources=1),
            as_of=AS_OF,
        )
        action = report.logical_actions[0]
        self.assertEqual(action.candidate_ids, (original.candidate_id,))

    def test_order_and_policy_identity_are_deterministic(self) -> None:
        sse = self.sse_bundle()
        cninfo = self.cninfo_bundle(sse)
        forward = reconcile_corporate_actions(
            bundles=(sse, cninfo),
            action_mappings=self.mappings(sse, cninfo),
            coverage_claims=self.claims(sse, cninfo),
            policy=self.policy(),
            as_of=AS_OF,
        )
        reverse = reconcile_corporate_actions(
            bundles=(cninfo, sse),
            action_mappings=tuple(reversed(self.mappings(sse, cninfo))),
            coverage_claims=tuple(reversed(self.claims(sse, cninfo))),
            policy=self.policy(),
            as_of=AS_OF,
        )
        self.assertEqual(forward.report_id, reverse.report_id)
        changed_policy = replace(self.policy(), policy_version="reconciliation-v2")
        self.assertNotEqual(self.policy().policy_id, changed_policy.policy_id)

    def test_mapping_and_derived_identity_bypass_fail_closed(self) -> None:
        sse = self.sse_bundle()
        mapping = self.mappings(sse)[0]
        with self.assertRaises(CorporateActionReconciliationError):
            reconcile_corporate_actions(
                bundles=(sse,),
                action_mappings=(mapping, mapping),
                coverage_claims=(),
                policy=self.policy(minimum_sources=1),
                as_of=AS_OF,
            )
        report = reconcile_corporate_actions(
            bundles=(sse,),
            action_mappings=(mapping,),
            coverage_claims=(),
            policy=self.policy(minimum_sources=1),
            as_of=AS_OF,
        )
        with self.assertRaisesRegex(TypeError, "init=False"):
            replace(report, report_id="a" * 64)
        with self.assertRaisesRegex(TypeError, "init=False"):
            replace(report.eligibility, eligibility_id="b" * 64)
        with self.assertRaisesRegex(CorporateActionReconciliationError, "relabelled"):
            replace(report, synthetic_fixture=False)


class TestReconciliationCli(ReconciliationFixtures):
    def test_cli_is_offline_report_only(self) -> None:
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

    def test_cli_reconciles_and_rejects_nested_unknown_field(self) -> None:
        sse = self.sse_bundle()
        cninfo = self.cninfo_bundle(sse)
        mappings = self.mappings(sse, cninfo)
        claims = self.claims(sse, cninfo)
        policy = self.policy()
        request = {
            "schema": "stage2e-corporate-action-reconciliation-request-v1",
            "synthetic_fixture": True,
            "as_of": _time_text(AS_OF),
            "policy": self.policy_dict(policy),
            "bundles": [self.bundle_dict(sse), self.bundle_dict(cninfo)],
            "action_mappings": [
                self.mapping_dict(mapping) for mapping in mappings
            ],
            "coverage_claims": [self.claim_dict(claim) for claim in claims],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "request.json"
            output_path = root / "report.json"
            input_path.write_text(
                json.dumps(request, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--input",
                    str(input_path),
                    "--output",
                    str(output_path),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["eligibility_status"], "NOT_ELIGIBLE")
            self.assertIn("T3_NOT_REACHED", payload["evidence_boundary"])

            request["bundles"][0]["verified"] = True
            input_path.write_text(
                json.dumps(request, ensure_ascii=False),
                encoding="utf-8",
            )
            blocked = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--input",
                    str(input_path),
                    "--output",
                    str(root / "blocked.json"),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("unknown", blocked.stderr)


if __name__ == "__main__":
    unittest.main()

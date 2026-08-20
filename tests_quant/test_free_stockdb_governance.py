from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from stock_tracker.core.types import Market
from stock_tracker.quant.data.bar_artifact import DataTrustTier
from stock_tracker.quant.data.free_stockdb_governance import (
    FreeStockDbComparisonReport,
    FreeStockDbGovernanceError,
    FreeStockDbReleaseAudit,
    FreeStockDbShadowAssessment,
    SidecarAuditFile,
    SidecarAuditFileKind,
    SidecarBarSeriesEvidence,
    SidecarComparisonPolicy,
    SidecarComparisonSample,
    SidecarComparisonState,
    SidecarFindingSeverity,
    SidecarLicenseStatus,
    SidecarNetworkObservation,
    SidecarNetworkProtocol,
    SidecarNormalizedBarPoint,
    SidecarReleaseAuditState,
    SidecarSampleCategory,
    SidecarShadowState,
    compare_sidecar_sample,
)

_NOW = datetime(2026, 8, 20, 2, 0, tzinfo=timezone.utc)
_START = datetime(2026, 8, 18, 7, 0, tzinfo=timezone.utc)
_END = datetime(2026, 8, 18, 8, 0, tzinfo=timezone.utc)


def _hash(character: str) -> str:
    return character * 64


def _audit(
    *,
    synthetic: bool = True,
    license_status: SidecarLicenseStatus = SidecarLicenseStatus.PENDING,
    project_data_isolated: bool = True,
    production_store_isolated: bool = True,
    loopback_listener_only: bool = True,
    updater_behavior_observed: bool = True,
    destination_host: str = "127.0.0.1",
    network_approved: bool = False,
) -> FreeStockDbReleaseAudit:
    executable = SidecarAuditFile(
        relative_path="bin/stockdb.exe",
        kind=SidecarAuditFileKind.EXECUTABLE,
        byte_size=100,
        sha256=_hash("1"),
    )
    manifest = SidecarAuditFile(
        relative_path="data/manifest.json",
        kind=SidecarAuditFileKind.MANIFEST,
        byte_size=200,
        sha256=_hash("2"),
    )
    observation = SidecarNetworkObservation(
        process_sha256=executable.sha256,
        observed_at=_NOW,
        destination_host=destination_host,
        destination_port=7899,
        protocol=SidecarNetworkProtocol.TCP,
        purpose="synthetic network observation",
        approved=network_approved,
        approval_evidence_id=_hash("d") if network_approved else None,
    )
    return FreeStockDbReleaseAudit(
        release_version="synthetic-v1",
        source_locator="fixture-release",
        asset_sha256=_hash("3"),
        data_snapshot_manifest_sha256=_hash("5"),
        sync_manifest_sha256=_hash("6"),
        captured_at=_NOW,
        files=(executable, manifest),
        network_observations=(observation,),
        low_privilege_process=True,
        project_data_isolated=project_data_isolated,
        production_store_isolated=production_store_isolated,
        loopback_listener_only=loopback_listener_only,
        updater_behavior_observed=updater_behavior_observed,
        license_status=license_status,
        license_evidence_ids=(
            ()
            if license_status is SidecarLicenseStatus.PENDING
            else (_hash("c"),)
        ),
        synthetic_fixture_only=synthetic,
        source_note="synthetic audit evidence only",
    )


def _point(
    *,
    close: str = "10.00",
    timestamp: datetime = _START,
    volume: int = 1000,
) -> SidecarNormalizedBarPoint:
    close_value = Decimal(close)
    return SidecarNormalizedBarPoint(
        timestamp=timestamp,
        open=Decimal("9.90"),
        high=max(Decimal("10.10"), close_value),
        low=Decimal("9.80"),
        close=close_value,
        volume=volume,
        amount=Decimal(100000),
        turnover=Decimal("1.20"),
    )


def _series(
    *,
    source_name: str,
    audit_id: str | None,
    points: tuple[SidecarNormalizedBarPoint, ...] = (_point(),),
    synthetic: bool = True,
    trust_tier: DataTrustTier = DataTrustTier.BEST_EFFORT,
    verified: bool = False,
    complete: bool = False,
    snapshot_character: str = "7",
) -> SidecarBarSeriesEvidence:
    return SidecarBarSeriesEvidence(
        source_name=source_name,
        symbol="600001.SH",
        market=Market.A,
        interval="1m",
        start_at=_START,
        end_at=_END,
        snapshot_id=_hash(snapshot_character),
        sidecar_release_audit_id=audit_id,
        trust_tier=trust_tier,
        verified=verified,
        complete=complete,
        synthetic_fixture_only=synthetic,
        provenance_ids=(_hash("e"),) if verified else (),
        points=points,
    )


def _sample(
    audit: FreeStockDbReleaseAudit,
    *,
    reference_points: tuple[SidecarNormalizedBarPoint, ...] = (_point(),),
    sidecar_points: tuple[SidecarNormalizedBarPoint, ...] = (_point(),),
    synthetic: bool = True,
    reference_trust: DataTrustTier = DataTrustTier.OPERATIONAL_VERIFIED,
    categories: tuple[SidecarSampleCategory, ...] = (
        SidecarSampleCategory.SH_MAIN,
    ),
) -> SidecarComparisonSample:
    effective_reference_trust = (
        DataTrustTier.BEST_EFFORT if synthetic else reference_trust
    )
    return SidecarComparisonSample(
        instrument_id="CN:SSE:synthetic-600001",
        identity_fact_id=_hash("8"),
        symbol="600001.SH",
        categories=categories,
        category_evidence_ids=tuple(
            sorted(_hash(format(index, "x")) for index in range(1, len(categories) + 1))
        ),
        reference=_series(
            source_name="authoritative_fixture",
            audit_id=None,
            points=reference_points,
            synthetic=synthetic,
            trust_tier=effective_reference_trust,
            verified=not synthetic,
            complete=True,
            snapshot_character="9",
        ),
        sidecar=_series(
            source_name="free_stockdb",
            audit_id=audit.audit_id,
            points=sidecar_points,
            synthetic=synthetic,
            trust_tier=DataTrustTier.BEST_EFFORT,
            complete=True,
            snapshot_character="a",
        ),
    )


def _policy(
    *,
    required_categories: tuple[SidecarSampleCategory, ...] = (
        SidecarSampleCategory.SH_MAIN,
    ),
) -> SidecarComparisonPolicy:
    return SidecarComparisonPolicy(
        policy_version="test-policy-v1",
        minimum_samples=1,
        maximum_samples=100,
        required_categories=required_categories,
        price_relative_tolerance=Decimal("0.001"),
        volume_relative_tolerance=Decimal("0.001"),
        amount_relative_tolerance=Decimal("0.001"),
        turnover_relative_tolerance=Decimal("0.001"),
    )


class TestFreeStockDbReleaseAudit(unittest.TestCase):
    def test_synthetic_release_never_becomes_sandbox_audited(self) -> None:
        audit = _audit(synthetic=True)
        self.assertEqual(audit.state, SidecarReleaseAuditState.CONTRACT_ONLY)
        self.assertIn("LICENSE_PENDING", audit.trust_blockers)

    def test_real_release_fails_closed_on_isolation_and_network(self) -> None:
        audit = _audit(
            synthetic=False,
            license_status=SidecarLicenseStatus.CLEARED,
            project_data_isolated=False,
            production_store_isolated=False,
            destination_host="203.0.113.10",
            network_approved=False,
        )
        self.assertEqual(audit.state, SidecarReleaseAuditState.BLOCKED)
        self.assertIn("PROJECT_DATA_NOT_ISOLATED", audit.engineering_blockers)
        self.assertIn("PRODUCTION_STORE_NOT_ISOLATED", audit.engineering_blockers)
        self.assertTrue(
            any(
                blocker.startswith("UNAPPROVED_NETWORK:")
                for blocker in audit.engineering_blockers
            )
        )

    def test_complete_real_release_reaches_engineering_audit_only(self) -> None:
        audit = _audit(
            synthetic=False,
            license_status=SidecarLicenseStatus.CLEARED,
        )
        self.assertEqual(audit.state, SidecarReleaseAuditState.SANDBOX_AUDITED)
        self.assertEqual(audit.engineering_blockers, ())
        self.assertEqual(audit.trust_blockers, ())

    def test_derived_audit_state_and_id_cannot_be_injected(self) -> None:
        audit = _audit()
        with self.assertRaises(TypeError):
            replace(audit, state=SidecarReleaseAuditState.SANDBOX_AUDITED)
        with self.assertRaises(TypeError):
            FreeStockDbReleaseAudit(
                **{
                    field: getattr(audit, field)
                    for field in audit.__dataclass_fields__
                    if field
                    not in {
                        "binary_inventory_sha256",
                        "engineering_blockers",
                        "trust_blockers",
                        "state",
                        "audit_id",
                    }
                },
                state=SidecarReleaseAuditState.SANDBOX_AUDITED,
            )

    def test_binary_inventory_network_process_and_license_evidence_are_bound(self) -> None:
        audit = _audit(
            synthetic=False,
            license_status=SidecarLicenseStatus.CLEARED,
        )
        self.assertRegex(audit.binary_inventory_sha256, r"^[0-9a-f]{64}$")
        changed_binary = replace(audit.files[0], sha256=_hash("b"))
        changed_audit = replace(
            audit,
            files=(changed_binary, audit.files[1]),
            network_observations=(
                replace(
                    audit.network_observations[0],
                    process_sha256=changed_binary.sha256,
                ),
            ),
        )
        self.assertNotEqual(
            audit.binary_inventory_sha256,
            changed_audit.binary_inventory_sha256,
        )
        with self.assertRaises(FreeStockDbGovernanceError):
            replace(
                audit,
                network_observations=(
                    replace(
                        audit.network_observations[0],
                        process_sha256=_hash("b"),
                    ),
                ),
            )
        with self.assertRaises(FreeStockDbGovernanceError):
            replace(audit, license_evidence_ids=())
        with self.assertRaises(FreeStockDbGovernanceError):
            replace(
                audit.network_observations[0],
                approved=True,
                approval_evidence_id=None,
            )

    def test_observed_updater_requires_network_observations(self) -> None:
        audit = _audit(
            synthetic=False,
            license_status=SidecarLicenseStatus.CLEARED,
        )
        blocked = replace(audit, network_observations=())
        self.assertEqual(blocked.state, SidecarReleaseAuditState.BLOCKED)
        self.assertIn("NETWORK_OBSERVATIONS_MISSING", blocked.engineering_blockers)

    def test_audit_capture_cannot_precede_observation(self) -> None:
        audit = _audit(
            synthetic=False,
            license_status=SidecarLicenseStatus.CLEARED,
        )
        with self.assertRaises(FreeStockDbGovernanceError):
            replace(audit, captured_at=_NOW - timedelta(seconds=1))

    def test_paths_hashes_and_booleans_are_strict(self) -> None:
        with self.assertRaises(FreeStockDbGovernanceError):
            SidecarAuditFile(
                relative_path="../stockdb.exe",
                kind=SidecarAuditFileKind.EXECUTABLE,
                byte_size=1,
                sha256=_hash("b"),
            )
        audit = _audit()
        with self.assertRaises(FreeStockDbGovernanceError):
            replace(audit, project_data_isolated=1)


class TestFreeStockDbComparison(unittest.TestCase):
    def test_matching_series_has_no_findings(self) -> None:
        audit = _audit()
        self.assertEqual(compare_sidecar_sample(_sample(audit), _policy()), ())

    def test_missing_and_mismatched_bars_are_explicit(self) -> None:
        audit = _audit()
        sample = _sample(
            audit,
            reference_points=(
                _point(),
                _point(timestamp=_START + timedelta(minutes=1)),
            ),
            sidecar_points=(_point(close="10.05"),),
        )
        findings = compare_sidecar_sample(sample, _policy())
        self.assertTrue(
            any(item.code == "SIDECAR_BAR_MISSING" for item in findings)
        )
        self.assertTrue(any(item.code == "PRICE_MISMATCH" for item in findings))
        self.assertTrue(
            any(item.severity is SidecarFindingSeverity.HARD_BLOCK for item in findings)
        )

    def test_sidecar_series_must_bind_exact_release_audit(self) -> None:
        audit = _audit()
        sample = _sample(audit)
        other_audit = replace(audit, release_version="synthetic-v2")
        with self.assertRaises(FreeStockDbGovernanceError):
            FreeStockDbComparisonReport(
                release_audit=other_audit,
                policy=_policy(),
                generated_at=_NOW + timedelta(minutes=1),
                samples=(sample,),
            )

    def test_synthetic_report_stays_contract_only(self) -> None:
        audit = _audit(synthetic=True)
        sample = _sample(audit, synthetic=True)
        report = FreeStockDbComparisonReport(
            release_audit=audit,
            policy=_policy(),
            generated_at=_NOW + timedelta(minutes=1),
            samples=(sample,),
        )
        self.assertEqual(report.state, SidecarComparisonState.CONTRACT_ONLY)
        self.assertEqual(report.evidence_tier_status, "T3_NOT_REACHED")

    def test_license_pending_and_low_reference_trust_block_real_shadow(self) -> None:
        audit = _audit(synthetic=False)
        sample = _sample(
            audit,
            synthetic=False,
            reference_trust=DataTrustTier.BEST_EFFORT,
        )
        report = FreeStockDbComparisonReport(
            release_audit=audit,
            policy=_policy(),
            generated_at=_NOW + timedelta(minutes=1),
            samples=(sample,),
        )
        self.assertEqual(report.state, SidecarComparisonState.BLOCKED)
        self.assertIn("LICENSE_PENDING", report.trust_blockers)
        self.assertTrue(
            any(
                item.startswith("REFERENCE_TRUST_INSUFFICIENT:")
                for item in report.trust_blockers
            )
        )

    def test_real_clean_comparison_is_shadow_eligible_not_research_grade(self) -> None:
        audit = _audit(
            synthetic=False,
            license_status=SidecarLicenseStatus.CLEARED,
        )
        sample = _sample(audit, synthetic=False)
        report = FreeStockDbComparisonReport(
            release_audit=audit,
            policy=_policy(),
            generated_at=_NOW + timedelta(minutes=1),
            samples=(sample,),
        )
        self.assertEqual(report.state, SidecarComparisonState.SHADOW_ELIGIBLE)
        self.assertEqual(report.evidence_tier_status, "T3_NOT_REACHED")
        self.assertEqual(report.findings, ())

    def test_sample_count_and_category_coverage_are_fail_closed(self) -> None:
        audit = _audit(
            synthetic=False,
            license_status=SidecarLicenseStatus.CLEARED,
        )
        sample = _sample(audit, synthetic=False)
        report = FreeStockDbComparisonReport(
            release_audit=audit,
            policy=replace(
                _policy(
                    required_categories=(
                        SidecarSampleCategory.ETF,
                        SidecarSampleCategory.SH_MAIN,
                    )
                ),
                minimum_samples=2,
            ),
            generated_at=_NOW + timedelta(minutes=1),
            samples=(sample,),
        )
        self.assertEqual(report.state, SidecarComparisonState.BLOCKED)
        self.assertIn("SAMPLE_COUNT_BELOW_MINIMUM", report.engineering_blockers)
        self.assertIn("MISSING_CATEGORY:ETF", report.engineering_blockers)

    def test_category_claims_require_evidence_and_one_primary_venue(self) -> None:
        audit = _audit()
        sample = _sample(audit)
        with self.assertRaises(FreeStockDbGovernanceError):
            replace(sample, category_evidence_ids=())
        with self.assertRaises(FreeStockDbGovernanceError):
            replace(
                sample,
                categories=(
                    SidecarSampleCategory.SH_MAIN,
                    SidecarSampleCategory.SZ_MAIN,
                ),
                category_evidence_ids=(_hash("1"), _hash("2")),
            )

    def test_reference_completeness_and_unique_instruments_are_required(self) -> None:
        audit = _audit(
            synthetic=False,
            license_status=SidecarLicenseStatus.CLEARED,
        )
        sample = _sample(audit, synthetic=False)
        incomplete = replace(
            sample,
            reference=replace(sample.reference, complete=False),
        )
        report = FreeStockDbComparisonReport(
            release_audit=audit,
            policy=_policy(),
            generated_at=_NOW + timedelta(minutes=1),
            samples=(incomplete,),
        )
        self.assertEqual(report.state, SidecarComparisonState.BLOCKED)
        self.assertTrue(
            any(
                item.startswith("REFERENCE_INCOMPLETE:")
                for item in report.trust_blockers
            )
        )
        sidecar_incomplete = replace(
            sample,
            sidecar=replace(sample.sidecar, complete=False),
        )
        sidecar_report = FreeStockDbComparisonReport(
            release_audit=audit,
            policy=_policy(),
            generated_at=_NOW + timedelta(minutes=1),
            samples=(sidecar_incomplete,),
        )
        self.assertEqual(sidecar_report.state, SidecarComparisonState.BLOCKED)
        self.assertTrue(
            any(
                item.startswith("SIDECAR_SERIES_INCOMPLETE:")
                for item in sidecar_report.engineering_blockers
            )
        )
        second = replace(
            sample,
            sidecar=replace(
                sample.sidecar,
                snapshot_id=_hash("b"),
            ),
        )
        ordered = tuple(sorted((sample, second), key=lambda item: item.sample_id))
        with self.assertRaises(FreeStockDbGovernanceError):
            FreeStockDbComparisonReport(
                release_audit=audit,
                policy=_policy(),
                generated_at=_NOW + timedelta(minutes=1),
                samples=ordered,
            )

    def test_findings_state_and_report_id_are_derived(self) -> None:
        audit = _audit()
        report = FreeStockDbComparisonReport(
            release_audit=audit,
            policy=_policy(),
            generated_at=_NOW + timedelta(minutes=1),
            samples=(_sample(audit),),
        )
        for changes in (
            {"findings": ()},
            {"state": SidecarComparisonState.SHADOW_ELIGIBLE},
            {"report_id": _hash("f")},
        ):
            with self.subTest(changes=changes), self.assertRaises(TypeError):
                replace(report, **changes)


class TestFreeStockDbShadowAssessment(unittest.TestCase):
    def _eligible_report(self) -> FreeStockDbComparisonReport:
        audit = _audit(
            synthetic=False,
            license_status=SidecarLicenseStatus.CLEARED,
        )
        return FreeStockDbComparisonReport(
            release_audit=audit,
            policy=_policy(),
            generated_at=_NOW + timedelta(minutes=1),
            samples=(_sample(audit, synthetic=False),),
        )

    def test_disabled_blocked_and_shadow_only_states(self) -> None:
        eligible = self._eligible_report()
        disabled = FreeStockDbShadowAssessment(
            comparison_report=eligible,
            requested_enabled=False,
            evaluated_at=_NOW + timedelta(minutes=2),
        )
        self.assertEqual(disabled.state, SidecarShadowState.DISABLED)

        synthetic_audit = _audit()
        blocked_report = FreeStockDbComparisonReport(
            release_audit=synthetic_audit,
            policy=_policy(),
            generated_at=_NOW + timedelta(minutes=1),
            samples=(_sample(synthetic_audit),),
        )
        blocked = FreeStockDbShadowAssessment(
            comparison_report=blocked_report,
            requested_enabled=True,
            evaluated_at=_NOW + timedelta(minutes=2),
        )
        self.assertEqual(blocked.state, SidecarShadowState.BLOCKED)

        shadow = FreeStockDbShadowAssessment(
            comparison_report=eligible,
            requested_enabled=True,
            evaluated_at=_NOW + timedelta(minutes=2),
        )
        self.assertEqual(shadow.state, SidecarShadowState.SHADOW_ONLY)
        self.assertEqual(shadow.trust_tier, DataTrustTier.BEST_EFFORT)
        self.assertFalse(shadow.affects_live_decision)
        self.assertFalse(shadow.affects_model_training)
        self.assertFalse(shadow.allows_public_redistribution)
        self.assertFalse(shadow.formal_research_eligible)

    def test_shadow_safety_fields_cannot_be_relabelled(self) -> None:
        shadow = FreeStockDbShadowAssessment(
            comparison_report=self._eligible_report(),
            requested_enabled=True,
            evaluated_at=_NOW + timedelta(minutes=2),
        )
        for changes in (
            {"affects_live_decision": True},
            {"affects_model_training": True},
            {"formal_research_eligible": True},
            {"state": SidecarShadowState.SHADOW_ONLY},
        ):
            with self.subTest(changes=changes), self.assertRaises(TypeError):
                replace(shadow, **changes)


if __name__ == "__main__":
    unittest.main()

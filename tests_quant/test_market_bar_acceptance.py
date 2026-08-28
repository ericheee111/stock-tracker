from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

from stock_tracker.core.types import Bar, Market
from stock_tracker.quant.data.bar_artifact import capture_market_bars
from stock_tracker.quant.data.market_bar_acceptance import (
    CORPORATE_ACTION_BINDING_NOT_INDEPENDENTLY_VERIFIED,
    CORPORATE_ACTION_REFERENCE_MISSING,
    NO_TRUSTED_ASSURANCE_AUTHORITY,
    SECURITY_STATUS_UNIVERSE_BINDING_NOT_INDEPENDENTLY_VERIFIED,
    SECURITY_STATUS_UNIVERSE_REFERENCE_MISSING,
    MarketBarAcceptanceCase,
    MarketBarAcceptanceError,
    MarketBarAcceptanceManifest,
    MarketBarAcceptanceReport,
    MarketBarAcceptanceState,
    MarketBarAssuranceCoverage,
    MarketBarAssuranceDeclaration,
    MarketBarAssuranceKind,
    MarketBarAuxiliaryBindings,
    MarketBarCaptureReference,
    MarketBarT3PreflightState,
    materialize_market_bar_acceptance,
    write_market_bar_acceptance_json,
    write_market_bar_acceptance_markdown,
)
from stock_tracker.quant.data.market_bar_golden import MarketBarParserBinding
from stock_tracker.quant.data.market_bar_reconciliation import (
    MarketBarField,
    MarketBarReconciliationPolicy,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parser(source: str):
    def parse(raw: bytes, symbol: str, market: Market, interval: str) -> list[Bar]:
        value = json.loads(raw.decode("utf-8"))
        return [
            Bar(
                symbol=symbol,
                market=market,
                timestamp=datetime.fromisoformat(item["date"]),
                interval=interval,
                open=float(item["open"]),
                high=float(item["high"]),
                low=float(item["low"]),
                close=float(item["close"]),
                volume=int(item["volume"]),
                amount=float(item["amount"]),
                turnover=0.0,
                source=source,
                adjustment_factor=1.0,
            )
            for item in value["rows"]
        ]

    return parse


_FIELDS = tuple(
    sorted(
        (
            MarketBarField.OPEN,
            MarketBarField.HIGH,
            MarketBarField.LOW,
            MarketBarField.CLOSE,
            MarketBarField.VOLUME,
        ),
        key=lambda item: item.value,
    )
)


class MarketBarAcceptanceFixture(unittest.TestCase):
    def _capture(
        self,
        root: str | Path,
        *,
        source: str,
        close: float = 105.0,
        synthetic: bool,
    ):
        raw = json.dumps(
            {
                "rows": [
                    {
                        "date": "2024-01-02",
                        "open": 100.0,
                        "high": max(110.0, close),
                        "low": 95.0,
                        "close": close,
                        "volume": 1_200_000,
                        "amount": 150_000_000.0,
                    }
                ]
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        parser = _parser(source)
        captured = capture_market_bars(
            root,
            raw_bytes=raw,
            parser=parser,
            symbol="600519.SH",
            market=Market.A,
            interval="1d",
            retrieved_at=datetime(2024, 1, 3, tzinfo=timezone.utc),
            source=source,
            source_dataset=f"{source}-acceptance-fixture",
            provider_version=f"{source}-provider-v1",
            schema_version=f"{source}-schema-v1",
            parser_version=f"{source}-parser-v1",
            request_parameters={
                "adjustment": "qfq",
                "requested_start": "2024-01-02",
                "requested_end": "2024-01-02",
                "endpoint": f"https://{source}.invalid/bars",
                "interval": "1d",
                "synthetic_fixture": synthetic,
            },
            source_note="acceptance test fixture",
        )
        binding = MarketBarParserBinding(
            source=source,
            schema_version=f"{source}-schema-v1",
            parser_version=f"{source}-parser-v1",
            parser=parser,
        )
        reference = MarketBarCaptureReference(
            source=source,
            descriptor_key=captured.descriptor_key,
            parser_binding_id=binding.binding_id,
        )
        return binding, reference

    def _declarations(
        self,
        sources: tuple[str, ...],
        *,
        synthetic: bool = False,
    ):
        values = []
        for kind in MarketBarAssuranceKind:
            values.append(
                MarketBarAssuranceDeclaration(
                    kind=kind,
                    source_owner=f"review-owner-{kind.value.lower()}",
                    source_version="v1",
                    known_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
                    usable_from=datetime(2024, 1, 2, tzinfo=timezone.utc),
                    markets=(Market.A,),
                    sources=sources,
                    evidence_artifact_ids=(_sha(f"evidence-{kind.value}"),),
                    synthetic=synthetic,
                    details=("declaration-only; no trusted closure authority",),
                )
            )
        return tuple(values)

    def _manifest(
        self,
        root: str | Path,
        *,
        synthetic: bool,
        second_close: float = 105.0,
        include_declarations: bool,
        include_auxiliary: bool,
        assurance_synthetic: bool = False,
    ):
        first_binding, first_reference = self._capture(
            root,
            source="source_one",
            synthetic=synthetic,
        )
        second_binding, second_reference = self._capture(
            root,
            source="source_two",
            close=second_close,
            synthetic=synthetic,
        )
        registry = {
            first_binding.source: first_binding,
            second_binding.source: second_binding,
        }
        declarations = (
            self._declarations(
                ("source_one", "source_two"),
                synthetic=assurance_synthetic,
            )
            if include_declarations
            else ()
        )
        case = MarketBarAcceptanceCase(
            case_name="A_600519",
            market=Market.A,
            symbol="600519.SH",
            interval="1d",
            adjustment="qfq",
            as_of=datetime(2024, 1, 4, tzinfo=timezone.utc),
            expected_open_sessions=(date(2024, 1, 2),),
            calendar_snapshot_id=_sha("calendar"),
            captures=(first_reference, second_reference),
            comparable_fields=_FIELDS,
            assurance_declaration_ids=tuple(
                item.declaration_id for item in declarations
            ),
            auxiliary_bindings=(
                MarketBarAuxiliaryBindings(
                    stage2_reconciliation_report_id=_sha("stage2-report"),
                    corporate_action_report_id=_sha("corporate-action-report"),
                )
                if include_auxiliary
                else MarketBarAuxiliaryBindings()
            ),
        )
        manifest = MarketBarAcceptanceManifest(
            acceptance_version="stage2h-test-v1",
            created_at=datetime(2024, 1, 5, tzinfo=timezone.utc),
            cases=(case,),
            assurance_declarations=declarations,
        )
        return manifest, registry


class TestMarketBarAcceptance(MarketBarAcceptanceFixture):
    def test_synthetic_inputs_remain_contract_only_and_t3_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest, registry = self._manifest(
                directory,
                synthetic=True,
                include_declarations=False,
                include_auxiliary=False,
            )
            report = materialize_market_bar_acceptance(
                manifest=manifest,
                artifact_root=directory,
                parser_registry=registry,
            )
        self.assertIs(
            report.acceptance_state,
            MarketBarAcceptanceState.SYNTHETIC_CONTRACT_ONLY,
        )
        self.assertIs(
            report.t3_preflight_state,
            MarketBarT3PreflightState.EVIDENCE_PACKAGE_INCOMPLETE,
        )
        self.assertIn(NO_TRUSTED_ASSURANCE_AUTHORITY, report.open_blockers)
        self.assertIn(
            SECURITY_STATUS_UNIVERSE_REFERENCE_MISSING,
            report.open_blockers,
        )
        self.assertIn(CORPORATE_ACTION_REFERENCE_MISSING, report.open_blockers)
        self.assertIn(
            SECURITY_STATUS_UNIVERSE_BINDING_NOT_INDEPENDENTLY_VERIFIED,
            report.open_blockers,
        )
        self.assertIn(
            CORPORATE_ACTION_BINDING_NOT_INDEPENDENTLY_VERIFIED,
            report.open_blockers,
        )
        self.assertIn("SYNTHETIC_MARKET_BAR_EVIDENCE", report.open_blockers)
        payload = report.as_dict()
        self.assertFalse(payload["research_grade"])
        self.assertFalse(payload["t3_reached"])
        self.assertFalse(payload["production_database_modified"])

    def test_complete_live_declaration_package_still_waits_for_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest, registry = self._manifest(
                directory,
                synthetic=False,
                include_declarations=True,
                include_auxiliary=True,
            )
            report = materialize_market_bar_acceptance(
                manifest=manifest,
                artifact_root=directory,
                parser_registry=registry,
            )
        case = report.cases[0]
        self.assertTrue(case.non_synthetic_declared)
        self.assertFalse(case.assurance_coverage.missing_kinds)
        self.assertIs(
            report.acceptance_state,
            (
                MarketBarAcceptanceState
                .NON_SYNTHETIC_DECLARED_STRUCTURALLY_CONSTRUCTIBLE
            ),
        )
        self.assertIs(
            report.t3_preflight_state,
            MarketBarT3PreflightState.PENDING_INDEPENDENT_AUTHORITY,
        )
        self.assertIn(NO_TRUSTED_ASSURANCE_AUTHORITY, report.open_blockers)
        self.assertNotIn(
            SECURITY_STATUS_UNIVERSE_REFERENCE_MISSING,
            report.open_blockers,
        )
        self.assertNotIn(CORPORATE_ACTION_REFERENCE_MISSING, report.open_blockers)
        self.assertIn(
            SECURITY_STATUS_UNIVERSE_BINDING_NOT_INDEPENDENTLY_VERIFIED,
            report.open_blockers,
        )
        self.assertIn(
            CORPORATE_ACTION_BINDING_NOT_INDEPENDENTLY_VERIFIED,
            report.open_blockers,
        )
        self.assertIn("LICENSE_PENDING", report.open_blockers)
        self.assertIn("T3_NOT_REACHED", report.open_blockers)
        with self.assertRaises(TypeError):
            replace(
                report,
                t3_preflight_state=MarketBarT3PreflightState.PENDING_INDEPENDENT_AUTHORITY,
            )

    def test_synthetic_assurance_declarations_do_not_complete_real_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest, registry = self._manifest(
                directory,
                synthetic=False,
                include_declarations=True,
                include_auxiliary=True,
                assurance_synthetic=True,
            )
            report = materialize_market_bar_acceptance(
                manifest=manifest,
                artifact_root=directory,
                parser_registry=registry,
            )
        coverage = report.cases[0].assurance_coverage
        self.assertTrue(coverage.missing_kinds)
        self.assertEqual(
            set(coverage.synthetic_declaration_ids),
            set(coverage.declaration_ids),
        )
        self.assertIs(
            report.t3_preflight_state,
            MarketBarT3PreflightState.EVIDENCE_PACKAGE_INCOMPLETE,
        )

    def test_source_scoped_assurances_can_cover_providers_in_separate_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base_manifest, registry = self._manifest(
                directory,
                synthetic=False,
                include_declarations=False,
                include_auxiliary=True,
            )
            declarations = tuple(
                MarketBarAssuranceDeclaration(
                    kind=MarketBarAssuranceKind.LICENSE_APPROVAL,
                    source_owner=f"license-owner-{source}",
                    source_version="v1",
                    known_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
                    usable_from=datetime(2024, 1, 2, tzinfo=timezone.utc),
                    markets=(Market.A,),
                    sources=(source,),
                    evidence_artifact_ids=(_sha(f"license-{source}"),),
                    synthetic=False,
                )
                for source in ("source_one", "source_two")
            )
            case = replace(
                base_manifest.cases[0],
                assurance_declaration_ids=tuple(
                    item.declaration_id for item in declarations
                ),
            )
            manifest = MarketBarAcceptanceManifest(
                acceptance_version=base_manifest.acceptance_version,
                created_at=base_manifest.created_at,
                cases=(case,),
                assurance_declarations=declarations,
            )
            report = materialize_market_bar_acceptance(
                manifest=manifest,
                artifact_root=directory,
                parser_registry=registry,
            )
        coverage = report.cases[0].assurance_coverage
        self.assertIn(
            MarketBarAssuranceKind.LICENSE_APPROVAL,
            coverage.declared_kinds,
        )
        self.assertNotIn(
            MarketBarAssuranceKind.LICENSE_APPROVAL,
            coverage.missing_kinds,
        )
        self.assertIn(
            MarketBarAssuranceKind.FIELD_UNIT_POLICY,
            coverage.missing_kinds,
        )

    def test_report_rejects_forged_coverage_duplicate_case_and_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest, registry = self._manifest(
                directory,
                synthetic=False,
                include_declarations=True,
                include_auxiliary=True,
            )
            report = materialize_market_bar_acceptance(
                manifest=manifest,
                artifact_root=directory,
                parser_registry=registry,
            )
        case_report = report.cases[0]
        required = case_report.assurance_coverage.required_kinds
        forged_coverage = MarketBarAssuranceCoverage(
            required_kinds=required,
            declared_kinds=(),
            missing_kinds=required,
            declaration_ids=case_report.assurance_coverage.declaration_ids,
            synthetic_declaration_ids=(),
        )
        with self.assertRaisesRegex(
            MarketBarAcceptanceError,
            "not derived from its declarations",
        ):
            replace(
                case_report,
                assurance_coverage=forged_coverage,
            )
        with self.assertRaisesRegex(
            MarketBarAcceptanceError,
            "declarations disagree with the case",
        ):
            replace(case_report, assurance_declarations=())
        with self.assertRaisesRegex(MarketBarAcceptanceError, "case IDs must be unique"):
            MarketBarAcceptanceReport(
                manifest=manifest,
                policy=report.policy,
                cases=(case_report, case_report),
            )
        with self.assertRaisesRegex(
            MarketBarAcceptanceError,
            "policy disagrees",
        ):
            MarketBarAcceptanceReport(
                manifest=manifest,
                policy=MarketBarReconciliationPolicy(price_tolerance_bps=0),
                cases=report.cases,
            )

    def test_case_report_rejects_reconciliation_case_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest, registry = self._manifest(
                directory,
                synthetic=False,
                include_declarations=True,
                include_auxiliary=True,
            )
            report = materialize_market_bar_acceptance(
                manifest=manifest,
                artifact_root=directory,
                parser_registry=registry,
            )
        mismatched_case = replace(
            report.cases[0].case,
            as_of=datetime(2024, 1, 5, tzinfo=timezone.utc),
        )
        with self.assertRaisesRegex(MarketBarAcceptanceError, "as_of disagrees"):
            replace(report.cases[0], case=mismatched_case)

    def test_assurance_scope_and_manifest_evidence_time_fail_closed(self) -> None:
        with self.assertRaisesRegex(MarketBarAcceptanceError, "covered sources"):
            MarketBarAssuranceDeclaration(
                kind=MarketBarAssuranceKind.LICENSE_APPROVAL,
                source_owner="license-review",
                source_version="v1",
                known_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
                usable_from=datetime(2024, 1, 2, tzinfo=timezone.utc),
                markets=(Market.A,),
                sources=(),
                evidence_artifact_ids=(_sha("license-evidence"),),
                synthetic=False,
            )
        with tempfile.TemporaryDirectory() as directory:
            manifest, _ = self._manifest(
                directory,
                synthetic=False,
                include_declarations=True,
                include_auxiliary=True,
            )
        unreferenced_case = replace(
            manifest.cases[0],
            assurance_declaration_ids=(),
        )
        with self.assertRaisesRegex(
            MarketBarAcceptanceError,
            "unreferenced assurance declaration",
        ):
            MarketBarAcceptanceManifest(
                acceptance_version=manifest.acceptance_version,
                created_at=manifest.created_at,
                cases=(unreferenced_case,),
                assurance_declarations=manifest.assurance_declarations,
            )
        future_declaration = replace(
            manifest.assurance_declarations[0],
            known_at=datetime(2024, 1, 6, tzinfo=timezone.utc),
            usable_from=datetime(2024, 1, 6, tzinfo=timezone.utc),
        )
        future_declarations = (future_declaration, *manifest.assurance_declarations[1:])
        future_case = replace(
            manifest.cases[0],
            assurance_declaration_ids=tuple(
                item.declaration_id for item in future_declarations
            ),
        )
        with self.assertRaisesRegex(MarketBarAcceptanceError, "after created_at"):
            MarketBarAcceptanceManifest(
                acceptance_version=manifest.acceptance_version,
                created_at=manifest.created_at,
                cases=(future_case,),
                assurance_declarations=future_declarations,
            )

    def test_acceptance_identity_tokens_and_adjustment_fail_closed(self) -> None:
        with self.assertRaisesRegex(MarketBarAcceptanceError, "at least two sources"):
            MarketBarAssuranceDeclaration(
                kind=MarketBarAssuranceKind.SOURCE_FAMILY_INDEPENDENCE,
                source_owner="independence-review",
                source_version="v1",
                known_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
                usable_from=datetime(2024, 1, 2, tzinfo=timezone.utc),
                markets=(Market.A,),
                sources=("source_one",),
                evidence_artifact_ids=(_sha("independence-evidence"),),
                synthetic=False,
            )
        with self.assertRaisesRegex(MarketBarAcceptanceError, "source is unsafe"):
            MarketBarCaptureReference(
                source="../source",
                descriptor_key="manifests/market-bars/" + _sha("descriptor") + ".json",
                parser_binding_id=_sha("binding"),
            )
        with tempfile.TemporaryDirectory() as directory:
            manifest, _ = self._manifest(
                directory,
                synthetic=False,
                include_declarations=False,
                include_auxiliary=False,
            )
        with self.assertRaisesRegex(MarketBarAcceptanceError, "qfq"):
            replace(manifest.cases[0], adjustment="raw")
        with self.assertRaisesRegex(MarketBarAcceptanceError, "A shares only"):
            replace(
                manifest.cases[0],
                market=Market.HK,
                symbol="00700.HK",
            )

    def test_cross_source_conflict_is_hard_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest, registry = self._manifest(
                directory,
                synthetic=False,
                second_close=108.0,
                include_declarations=True,
                include_auxiliary=True,
            )
            report = materialize_market_bar_acceptance(
                manifest=manifest,
                artifact_root=directory,
                parser_registry=registry,
            )
        self.assertIs(report.acceptance_state, MarketBarAcceptanceState.HARD_BLOCKED)
        self.assertIs(
            report.t3_preflight_state,
            MarketBarT3PreflightState.HARD_BLOCKED,
        )
        self.assertIn("MARKET_BAR_CLOSE_CONFLICT", report.open_blockers)

    def test_manifest_round_trip_and_tamper_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, _ = self._manifest(
                root / "artifacts",
                synthetic=False,
                include_declarations=True,
                include_auxiliary=True,
            )
            path = root / "manifest.json"
            manifest.write_json(path)
            loaded = MarketBarAcceptanceManifest.read_json(path)
            self.assertEqual(loaded.manifest_id, manifest.manifest_id)
            value = json.loads(path.read_text(encoding="utf-8"))
            value["cases"][0]["symbol"] = "000001.SZ"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(MarketBarAcceptanceError):
                MarketBarAcceptanceManifest.read_json(path)

            duplicate = root / "duplicate.json"
            duplicate.write_text(
                '{"schema":"one","schema":"two"}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                MarketBarAcceptanceError,
                "duplicate JSON keys",
            ):
                MarketBarAcceptanceManifest.read_json(duplicate)

    def test_parser_binding_mismatch_and_declaration_tamper_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest, registry = self._manifest(
                directory,
                synthetic=False,
                include_declarations=True,
                include_auxiliary=True,
            )
            broken = dict(registry)
            binding = broken["source_one"]
            broken["source_one"] = replace(
                binding,
                parser_version="source-one-parser-v2",
            )
            with self.assertRaisesRegex(
                MarketBarAcceptanceError,
                "binding ID",
            ):
                materialize_market_bar_acceptance(
                    manifest=manifest,
                    artifact_root=directory,
                    parser_registry=broken,
                )

            declaration = manifest.assurance_declarations[0]
            value = declaration.as_dict()
            value["source_version"] = "tampered"
            with self.assertRaisesRegex(
                MarketBarAcceptanceError,
                "declaration_id",
            ):
                MarketBarAssuranceDeclaration.from_dict(value)

    def test_report_outputs_are_content_addressed_and_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, registry = self._manifest(
                root / "artifacts",
                synthetic=False,
                include_declarations=True,
                include_auxiliary=True,
            )
            report = materialize_market_bar_acceptance(
                manifest=manifest,
                artifact_root=root / "artifacts",
                parser_registry=registry,
            )
            json_path = root / f"{report.report_id}.json"
            markdown_path = root / f"{report.report_id}.md"
            write_market_bar_acceptance_json(report, json_path)
            write_market_bar_acceptance_markdown(report, markdown_path)
            write_market_bar_acceptance_json(report, json_path)
            self.assertEqual(
                json.loads(json_path.read_text(encoding="utf-8"))["report_id"],
                report.report_id,
            )
            self.assertIn(report.report_id, markdown_path.read_text(encoding="utf-8"))
            json_path.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(
                MarketBarAcceptanceError,
                "immutable acceptance path",
            ):
                write_market_bar_acceptance_json(report, json_path)


if __name__ == "__main__":
    unittest.main()

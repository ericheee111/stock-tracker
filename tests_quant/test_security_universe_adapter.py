from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

from stock_tracker.core.types import Market
from stock_tracker.quant.core.universe import (
    ListingState,
    RiskDesignation,
    TradingState,
    UniverseContractError,
    UniverseMembershipState,
)
from stock_tracker.quant.data.security_universe_adapter import (
    CoverageKind,
    MembershipReason,
    PublishedGranularity,
    SecurityUniverseAdapterError,
    SourceListingState,
    SourceRiskDesignation,
    SourceTradingState,
    StatusScope,
    parse_security_universe_artifact,
    read_security_universe_descriptor,
    stable_instrument_id,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "security_universe"
ARTIFACT = FIXTURES / "golden_sse.json"
DESCRIPTOR = FIXTURES / "golden_sse.descriptor.json"
SESSION = date(2024, 1, 12)
VISIBLE_AS_OF = datetime.fromisoformat("2024-01-14T16:00:00+08:00")
CORRECTED_AS_OF = datetime.fromisoformat("2024-01-16T16:00:00+08:00")


class SecurityUniverseAdapterFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = ARTIFACT.read_bytes()
        cls.document = json.loads(cls.raw.decode("utf-8"))
        cls.descriptor = read_security_universe_descriptor(DESCRIPTOR)

    def parse_document(self, document: dict[str, object]):
        raw = json.dumps(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        descriptor = replace(
            self.descriptor,
            artifact_sha256=hashlib.sha256(raw).hexdigest(),
            byte_size=len(raw),
        )
        return parse_security_universe_artifact(raw, descriptor)

    def bundle(self):
        return parse_security_universe_artifact(self.raw, self.descriptor)

    def snapshot(self, bundle=None, *, as_of: datetime = VISIBLE_AS_OF):
        candidate = bundle or self.bundle()
        return candidate.historical_universe().snapshot(
            "A_SHARE_SSE_ALL",
            Market.A,
            SESSION,
            as_of,
            require_verified=False,
            require_complete=False,
        )


class TestStableSecurityIdentity(SecurityUniverseAdapterFixtures):
    def test_golden_candidate_is_snapshot_compatible_and_never_promoted(self) -> None:
        bundle = self.bundle()
        snapshot = self.snapshot(bundle)
        self.assertFalse(bundle.coverage.complete)
        self.assertFalse(bundle.coverage.verified)
        self.assertFalse(bundle.coverage_report.has_snapshot_blockers)
        self.assertTrue(bundle.coverage_report.has_trust_blockers)
        self.assertIn(
            "ADAPTER_UNVERIFIED_INCOMPLETE",
            bundle.coverage_report.trust_blocker_codes,
        )
        self.assertIn(
            "SOURCE_SECURITY_ID_STABILITY_UNPROVEN",
            bundle.coverage_report.trust_blocker_codes,
        )
        self.assertIn(
            "UPSTREAM_RAW_PROVENANCE_INCOMPLETE",
            bundle.coverage_report.trust_blocker_codes,
        )
        self.assertEqual(snapshot.member_symbols, ("600101.SH", "600300.SH"))
        self.assertEqual(snapshot.delisted_symbols, ("600200.SH",))
        self.assertTrue(all(not item.fact.verified for item in bundle.identities))
        self.assertTrue(all(not item.fact.verified for item in bundle.memberships))

    def test_symbol_change_keeps_same_stable_instrument_id(self) -> None:
        alpha = [
            item
            for item in self.bundle().identities
            if item.source_security_id == "SSECID-ALPHA"
        ]
        self.assertEqual([item.symbol for item in alpha], ["600100.SH", "600101.SH"])
        self.assertEqual(len({item.instrument_id for item in alpha}), 1)
        self.assertEqual(alpha[0].name, "甲公司")
        self.assertEqual(alpha[1].name, "甲股份")

    def test_same_symbol_reuse_gets_a_new_instrument_id(self) -> None:
        document = copy.deepcopy(self.document)
        reused = copy.deepcopy(document["identities"][0])
        reused["source_security_id"] = "SSECID-REUSED-CODE"
        reused["name"] = "新证券"
        reused["effective_from"] = "2025-01-01"
        reused["effective_to"] = None
        document["identities"].append(reused)
        bundle = self.parse_document(document)
        matches = [item for item in bundle.identities if item.symbol == "600100.SH"]
        self.assertEqual(len(matches), 2)
        self.assertNotEqual(matches[0].instrument_id, matches[1].instrument_id)

    def test_reused_symbol_after_delisting_keeps_old_exit_evidence_without_future_status(self) -> None:
        document = copy.deepcopy(self.document)
        beta_id = "SSECID-BETA"
        new_id = "SSECID-BETA-CODE-REUSE"
        old_identity = next(
            item for item in document["identities"] if item["source_security_id"] == beta_id
        )
        old_status = next(
            item
            for item in document["statuses"]
            if item["source_security_id"] == beta_id
            and item["session_date"] == "2024-01-12"
        )
        old_memberships = [
            item for item in document["memberships"] if item["source_security_id"] == beta_id
        ]

        def provenance(day: str, suffix: str) -> dict[str, object]:
            return {
                "source_published_at": day,
                "source_published_granularity": "DATE",
                "observed_at": day + "T08:00:00+08:00",
                "known_at": day + "T08:00:00+08:00",
                "usable_from": day + "T09:30:00+08:00",
                "revision": 1,
                "supersedes": None,
                "source_uri": "fixture://sse/reuse/" + suffix,
                "evidence_ids": ["fixture-reuse-" + suffix],
            }

        new_identity = copy.deepcopy(old_identity)
        new_identity.update(
            {
                "source_security_id": new_id,
                "name": "复用代码新证券",
                "effective_from": "2024-01-13",
                "effective_to": None,
                "provenance": provenance("2024-01-13", "identity"),
            }
        )
        new_status = copy.deepcopy(old_status)
        new_status.update(
            {
                "source_security_id": new_id,
                "session_date": "2024-01-13",
                "effective_start": "2024-01-13",
                "effective_end": "2024-01-13",
                "listing_state": "LISTED",
                "trading_state": "TRADABLE",
                "risk_designation": "NORMAL",
                "reason_code": "NEW_LISTING",
                "provenance": provenance("2024-01-13", "status"),
            }
        )
        new_membership = copy.deepcopy(old_memberships[-1])
        new_membership.update(
            {
                "source_security_id": new_id,
                "effective_date": "2024-01-13",
                "state": "INCLUDED",
                "reason": "LISTED",
                "evidence_ids": ["fixture-reuse-listing"],
                "provenance": provenance("2024-01-13", "membership"),
            }
        )
        document["coverage"]["end_date"] = "2024-01-13"
        document["coverage"]["required_session_dates"] = ["2024-01-13"]
        document["identities"] = [old_identity, new_identity]
        document["statuses"] = [
            item for item in document["statuses"] if item["source_security_id"] == beta_id
        ] + [new_status]
        document["memberships"] = old_memberships + [new_membership]

        bundle = self.parse_document(document)
        old_instrument_id = stable_instrument_id("SSE", beta_id)
        self.assertFalse(
            any(
                item.startswith(old_instrument_id + "@2024-01-13")
                for item in bundle.coverage_report.missing_daily_session_status
            )
        )
        snapshot = bundle.historical_universe().snapshot(
            "A_SHARE_SSE_ALL",
            Market.A,
            date(2024, 1, 13),
            CORRECTED_AS_OF,
            require_verified=False,
            require_complete=False,
        )
        self.assertEqual(snapshot.member_symbols, ("600200.SH",))
        self.assertIn(old_instrument_id, snapshot.delisted_instrument_ids)
        self.assertEqual(
            len([item for item in snapshot.identities if item.symbol == "600200.SH"]),
            2,
        )

    def test_unproved_overlapping_continuity_fails_closed(self) -> None:
        document = copy.deepcopy(self.document)
        reused = copy.deepcopy(document["identities"][0])
        reused["source_security_id"] = "SSECID-UNPROVED"
        reused["name"] = "冲突证券"
        document["identities"].append(reused)
        with self.assertRaisesRegex(SecurityUniverseAdapterError, "overlaps"):
            self.parse_document(document)

    def test_instrument_id_never_depends_on_symbol(self) -> None:
        first = stable_instrument_id("SSE", "SOURCE-42")
        second = stable_instrument_id("SSE", "SOURCE-42")
        other = stable_instrument_id("SSE", "SOURCE-43")
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertNotIn("600000", first)

    def test_sse_and_szse_universes_remain_separate(self) -> None:
        document = copy.deepcopy(self.document)

        def convert(value: object) -> object:
            if isinstance(value, str):
                return value.replace(".SH", ".SZ").replace("fixture://sse", "fixture://szse")
            if isinstance(value, list):
                return [convert(item) for item in value]
            if isinstance(value, dict):
                return {key: convert(item) for key, item in value.items()}
            return value

        document = convert(document)
        assert isinstance(document, dict)
        document["source"] = "fixture-szse-official-like"
        document["source_version"] = "fixture-szse-v1"
        document["exchange"] = "SZSE"
        document["universe_id"] = "A_SHARE_SZSE_ALL"
        raw = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()
        descriptor = replace(
            self.descriptor,
            source="fixture-szse-official-like",
            source_version="fixture-szse-v1",
            exchange="SZSE",
            artifact_sha256=hashlib.sha256(raw).hexdigest(),
            byte_size=len(raw),
        )
        bundle = parse_security_universe_artifact(raw, descriptor)
        self.assertEqual(bundle.coverage.universe_id, "A_SHARE_SZSE_ALL")
        self.assertTrue(all(item.symbol.endswith(".SZ") for item in bundle.identities))
        self.assertTrue(all(item.instrument_id.startswith("CN:SZSE:") for item in bundle.identities))


class TestStatusAndMembershipSemantics(SecurityUniverseAdapterFixtures):
    def test_st_to_normal_is_an_explicit_daily_transition(self) -> None:
        statuses = {
            item.session_date: item
            for item in self.bundle().statuses
            if item.source_security_id == "SSECID-ALPHA"
            and item.scope is StatusScope.DAILY
        }
        self.assertEqual(statuses[date(2024, 1, 2)].risk_designation, SourceRiskDesignation.ST)
        self.assertEqual(statuses[date(2024, 1, 3)].risk_designation, SourceRiskDesignation.NORMAL)

    def test_normal_to_st_is_an_explicit_daily_transition(self) -> None:
        statuses = {
            item.session_date: item
            for item in self.bundle().statuses
            if item.source_security_id == "SSECID-GAMMA"
            and item.scope is StatusScope.DAILY
        }
        self.assertEqual(statuses[date(2024, 1, 2)].fact.risk_designation, RiskDesignation.NORMAL)
        self.assertEqual(statuses[date(2024, 1, 3)].fact.risk_designation, RiskDesignation.ST)

    def test_suspension_and_resumption_stay_distinct(self) -> None:
        statuses = {
            item.session_date: item
            for item in self.bundle().statuses
            if item.source_security_id == "SSECID-ALPHA"
            and item.scope is StatusScope.DAILY
        }
        self.assertEqual(statuses[date(2024, 1, 4)].trading_state, SourceTradingState.SUSPENDED)
        self.assertEqual(statuses[date(2024, 1, 4)].fact.trading_state, TradingState.SUSPENDED)
        self.assertEqual(statuses[date(2024, 1, 5)].trading_state, SourceTradingState.RESUMED)
        self.assertEqual(statuses[date(2024, 1, 5)].fact.trading_state, TradingState.TRADABLE)

    def test_intraday_halt_keeps_exact_interval_and_is_not_daily_status(self) -> None:
        statuses = [
            item
            for item in self.bundle().statuses
            if item.source_security_id == "SSECID-ALPHA"
            and item.session_date == date(2024, 1, 8)
        ]
        daily = [item for item in statuses if item.scope is StatusScope.DAILY]
        intraday = [item for item in statuses if item.scope is StatusScope.INTRADAY]
        self.assertEqual(daily[0].fact.trading_state, TradingState.TRADABLE)
        self.assertEqual(len(intraday), 2)
        halted = next(item for item in intraday if item.trading_state is SourceTradingState.HALTED)
        resumed = next(item for item in intraday if item.trading_state is SourceTradingState.RESUMED)
        self.assertIsNone(halted.fact)
        self.assertIsNone(resumed.fact)
        self.assertEqual(halted.effective_end - halted.effective_start, timedelta(minutes=30))
        self.assertEqual(halted.effective_end, resumed.effective_start)

    def test_unknown_status_is_preserved_without_fabricating_core_listing_state(self) -> None:
        unknown = next(
            item
            for item in self.bundle().statuses
            if item.listing_state is SourceListingState.UNKNOWN
        )
        self.assertEqual(unknown.trading_state, SourceTradingState.UNKNOWN)
        self.assertEqual(unknown.risk_designation, SourceRiskDesignation.UNKNOWN)
        self.assertIsNone(unknown.fact)

    def test_unknown_risk_designation_is_not_collapsed_to_other(self) -> None:
        document = copy.deepcopy(self.document)
        status = copy.deepcopy(document["statuses"][0])
        status["risk_designation"] = "UNKNOWN"
        status["reason_code"] = "RISK_VALUE_NOT_PROVEN"
        document["statuses"][0] = status
        bundle = self.parse_document(document)
        candidate = next(
            item
            for item in bundle.statuses
            if item.source_security_id == status["source_security_id"]
            and item.session_date == date.fromisoformat(status["session_date"])
            and item.scope is StatusScope.DAILY
        )
        self.assertEqual(candidate.risk_designation, SourceRiskDesignation.UNKNOWN)
        self.assertEqual(candidate.fact.risk_designation, RiskDesignation.UNKNOWN)

    def test_delisting_period_and_formal_delisting_are_distinct(self) -> None:
        beta = {
            item.session_date: item
            for item in self.bundle().statuses
            if item.source_security_id == "SSECID-BETA"
        }
        self.assertEqual(beta[date(2024, 1, 11)].fact.listing_state, ListingState.DELISTING)
        self.assertEqual(beta[date(2024, 1, 11)].reason_code, "DELISTING_PERIOD")
        self.assertEqual(beta[date(2024, 1, 12)].fact.listing_state, ListingState.DELISTED)
        self.assertEqual(beta[date(2024, 1, 12)].reason_code, "FORMAL_DELISTING")

    def test_universe_included_to_excluded_and_relisting_are_explicit(self) -> None:
        beta = [
            item
            for item in self.bundle().memberships
            if item.source_security_id == "SSECID-BETA"
        ]
        states = [(item.state, item.reason) for item in beta]
        self.assertIn((UniverseMembershipState.INCLUDED, MembershipReason.LISTED), states)
        self.assertIn((UniverseMembershipState.EXCLUDED, MembershipReason.OUT_OF_SCOPE), states)
        self.assertIn((UniverseMembershipState.INCLUDED, MembershipReason.RELISTED), states)
        self.assertIn((UniverseMembershipState.EXCLUDED, MembershipReason.DELISTED), states)

    def test_delisted_security_remains_in_historical_snapshot(self) -> None:
        snapshot = self.snapshot()
        beta = stable_instrument_id("SSE", "SSECID-BETA")
        self.assertIn(beta, [item.instrument_id for item in snapshot.identities])
        self.assertEqual(snapshot.delisted_symbols, ("600200.SH",))

    def test_current_anchor_does_not_infer_absent_exclusions_or_completeness(self) -> None:
        document = copy.deepcopy(self.document)
        document["coverage"]["coverage_kind"] = "CURRENT_ANCHOR"
        document["memberships"] = [
            item
            for item in document["memberships"]
            if item["source_security_id"] != "SSECID-BETA"
            and item["state"] == "INCLUDED"
        ]
        document["identities"] = [
            item for item in document["identities"] if item["source_security_id"] != "SSECID-BETA"
        ]
        document["statuses"] = [
            item for item in document["statuses"] if item["source_security_id"] != "SSECID-BETA"
        ]
        bundle = self.parse_document(document)
        self.assertEqual(bundle.coverage_kind, CoverageKind.CURRENT_ANCHOR)
        self.assertTrue(bundle.coverage_report.current_anchor_only)
        self.assertFalse(bundle.coverage.complete)
        self.assertFalse(bundle.coverage.verified)
        self.assertFalse(
            any(item.state is UniverseMembershipState.EXCLUDED for item in bundle.memberships)
        )


class TestPointInTimeAndFailureClosure(SecurityUniverseAdapterFixtures):
    def test_future_correction_does_not_change_earlier_snapshot(self) -> None:
        bundle = self.bundle()
        before = self.snapshot(bundle, as_of=VISIBLE_AS_OF)
        after = self.snapshot(bundle, as_of=CORRECTED_AS_OF)
        self.assertIn("600101.SH", before.member_symbols)
        self.assertNotIn("600101.SH", after.member_symbols)
        self.assertNotEqual(before.snapshot_id, after.snapshot_id)

    def test_same_known_at_and_revision_conflict_fails(self) -> None:
        document = copy.deepcopy(self.document)
        conflict = copy.deepcopy(document["memberships"][0])
        conflict["state"] = "EXCLUDED"
        conflict["reason"] = "OUT_OF_SCOPE"
        document["memberships"].append(conflict)
        with self.assertRaisesRegex(SecurityUniverseAdapterError, "same known_at and revision"):
            self.parse_document(document)

    def test_missing_identity_is_reported_and_snapshot_fails(self) -> None:
        document = copy.deepcopy(self.document)
        document["identities"] = [
            item for item in document["identities"] if item["source_security_id"] != "SSECID-GAMMA"
        ]
        bundle = self.parse_document(document)
        gamma = stable_instrument_id("SSE", "SSECID-GAMMA")
        self.assertTrue(any(gamma in item for item in bundle.coverage_report.missing_identity))
        with self.assertRaisesRegex(UniverseContractError, "lacks visible instrument identity"):
            self.snapshot(bundle)

    def test_missing_daily_status_is_reported_and_snapshot_fails(self) -> None:
        document = copy.deepcopy(self.document)
        document["statuses"] = [
            item
            for item in document["statuses"]
            if not (
                item["source_security_id"] == "SSECID-GAMMA"
                and item["session_date"] == "2024-01-12"
            )
        ]
        bundle = self.parse_document(document)
        gamma = stable_instrument_id("SSE", "SSECID-GAMMA")
        self.assertTrue(
            any(gamma in item for item in bundle.coverage_report.missing_daily_session_status)
        )
        with self.assertRaisesRegex(UniverseContractError, "lacks visible security status"):
            self.snapshot(bundle)

    def test_duplicate_row_fails(self) -> None:
        document = copy.deepcopy(self.document)
        document["identities"].append(copy.deepcopy(document["identities"][0]))
        with self.assertRaisesRegex(SecurityUniverseAdapterError, "duplicate identity row"):
            self.parse_document(document)

    def test_input_order_randomization_keeps_fact_and_snapshot_identity(self) -> None:
        first = self.bundle()
        document = copy.deepcopy(self.document)
        document["identities"].reverse()
        document["statuses"].reverse()
        document["memberships"].reverse()
        second = self.parse_document(document)
        self.assertEqual(
            [item.candidate_id for item in first.identities],
            [item.candidate_id for item in second.identities],
        )
        self.assertEqual(
            [item.candidate_id for item in first.statuses],
            [item.candidate_id for item in second.statuses],
        )
        self.assertEqual(
            [item.candidate_id for item in first.memberships],
            [item.candidate_id for item in second.memberships],
        )
        self.assertEqual(self.snapshot(first).snapshot_id, self.snapshot(second).snapshot_id)

    def test_source_version_mismatch_fails(self) -> None:
        descriptor = replace(self.descriptor, source_version="other-version")
        with self.assertRaisesRegex(SecurityUniverseAdapterError, "source or version mismatch"):
            parse_security_universe_artifact(self.raw, descriptor)

    def test_invalid_complete_true_attempt_fails(self) -> None:
        document = copy.deepcopy(self.document)
        document["coverage"]["complete"] = True
        with self.assertRaisesRegex(SecurityUniverseAdapterError, "cannot set adapter trust"):
            self.parse_document(document)

    def test_invalid_verified_false_input_is_also_rejected(self) -> None:
        document = copy.deepcopy(self.document)
        document["verified"] = False
        with self.assertRaisesRegex(SecurityUniverseAdapterError, "cannot set adapter trust"):
            self.parse_document(document)

    def test_missing_exclusion_reason_is_reported(self) -> None:
        document = copy.deepcopy(self.document)
        final_exit = next(
            item
            for item in document["memberships"]
            if item["source_security_id"] == "SSECID-BETA"
            and item["effective_date"] == "2024-01-12"
        )
        final_exit["reason"] = "UNKNOWN"
        bundle = self.parse_document(document)
        self.assertEqual(len(bundle.coverage_report.missing_exclusion_reason), 1)

    def test_quantity_continuity_and_declared_gaps_are_reported(self) -> None:
        document = copy.deepcopy(self.document)
        document["coverage_evidence"]["quantity_continuity"][0]["end_count"] = 4
        document["coverage_evidence"]["unparsed_attachments"] = ["fixture-unparsed.pdf"]
        document["coverage_evidence"]["cross_source_conflicts"] = ["fixture-conflict"]
        bundle = self.parse_document(document)
        self.assertEqual(len(bundle.coverage_report.quantity_continuity_gaps), 1)
        self.assertEqual(bundle.coverage_report.unparsed_attachments, ("fixture-unparsed.pdf",))
        self.assertEqual(bundle.coverage_report.cross_source_conflicts, ("fixture-conflict",))
        self.assertTrue(bundle.coverage_report.has_snapshot_blockers)
        self.assertTrue(bundle.coverage_report.has_trust_blockers)
        self.assertIn(
            "QUANTITY_CONTINUITY_GAPS",
            bundle.coverage_report.trust_blocker_codes,
        )
        self.assertIn(
            "UNPARSED_ATTACHMENTS",
            bundle.coverage_report.trust_blocker_codes,
        )

    def test_unclosed_delisting_is_reported_without_promoting_completeness(self) -> None:
        document = copy.deepcopy(self.document)
        document["coverage_evidence"]["delisting_closures"][0][
            "chinaclear_termination_ids"
        ] = []
        bundle = self.parse_document(document)
        beta = stable_instrument_id("SSE", "SSECID-BETA")
        self.assertEqual(bundle.coverage_report.unclosed_delistings, (beta,))
        self.assertFalse(bundle.coverage.complete)

    def test_publication_granularity_and_pit_times_stay_distinct(self) -> None:
        bundle = self.bundle()
        identity = bundle.identities[0]
        intraday = next(item for item in bundle.statuses if item.scope is StatusScope.INTRADAY)
        self.assertEqual(
            identity.provenance.source_published_granularity,
            PublishedGranularity.DATE,
        )
        self.assertEqual(
            intraday.provenance.source_published_granularity,
            PublishedGranularity.SECOND,
        )
        self.assertNotEqual(intraday.provenance.observed_at, intraday.provenance.retrieved_at)
        self.assertLessEqual(intraday.provenance.known_at, intraday.provenance.usable_from)

    def test_known_at_cannot_follow_observed_at(self) -> None:
        document = copy.deepcopy(self.document)
        provenance = document["identities"][0]["provenance"]
        provenance["observed_at"] = "2024-01-02T08:00:00+08:00"
        provenance["known_at"] = "2024-01-02T08:01:00+08:00"
        with self.assertRaisesRegex(SecurityUniverseAdapterError, "known_at must equal observed_at"):
            self.parse_document(document)

    def test_non_synthetic_candidate_cannot_backdate_first_observation(self) -> None:
        descriptor = replace(self.descriptor, synthetic=False)
        with self.assertRaisesRegex(
            SecurityUniverseAdapterError,
            "observed_at must equal descriptor retrieved_at",
        ):
            parse_security_universe_artifact(self.raw, descriptor)

    def test_date_publication_cannot_follow_known_at_date(self) -> None:
        document = copy.deepcopy(self.document)
        provenance = document["identities"][0]["provenance"]
        provenance["source_published_at"] = "2024-01-03"
        provenance["source_published_granularity"] = "DATE"
        provenance["observed_at"] = "2024-01-02T08:00:00+08:00"
        provenance["known_at"] = "2024-01-02T08:00:00+08:00"
        with self.assertRaisesRegex(SecurityUniverseAdapterError, "date cannot follow known_at"):
            self.parse_document(document)

    def test_artifact_hash_and_strict_utf8_are_enforced(self) -> None:
        tampered = self.raw[:-1] + b" "
        with self.assertRaisesRegex(SecurityUniverseAdapterError, "SHA-256"):
            parse_security_universe_artifact(tampered, self.descriptor)
        raw = b"\xff"
        descriptor = replace(
            self.descriptor,
            artifact_sha256=hashlib.sha256(raw).hexdigest(),
            byte_size=len(raw),
        )
        with self.assertRaisesRegex(SecurityUniverseAdapterError, "strict UTF-8"):
            parse_security_universe_artifact(raw, descriptor)


class TestIdentityImportCli(SecurityUniverseAdapterFixtures):
    def test_cli_writes_only_candidate_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "import_a_share_identity.py"),
                    "--artifact",
                    str(ARTIFACT),
                    "--descriptor",
                    str(DESCRIPTOR),
                    "--output-dir",
                    directory,
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["complete"])
            self.assertFalse(payload["verified"])
            self.assertFalse(payload["database_modified"])
            self.assertEqual(payload["trust_state"], "T3_NOT_REACHED")
            output = Path(directory)
            self.assertEqual(
                {path.name for path in output.iterdir()},
                {
                    "candidate_bundle.json",
                    "instrument_identities.jsonl",
                    "security_statuses.jsonl",
                    "universe_memberships.jsonl",
                    "coverage.json",
                    "coverage_report.json",
                },
            )
            report = json.loads((output / "coverage_report.json").read_text(encoding="utf-8"))
            self.assertFalse(report["complete"])
            self.assertFalse(report["verified"])

    def test_cli_has_no_trust_tier_or_database_switch(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "import_a_share_identity.py"),
                "--help",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("trust-tier", result.stdout)
        self.assertNotIn("database", result.stdout.lower())


if __name__ == "__main__":
    unittest.main()

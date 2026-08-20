from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, datetime

from stock_tracker.core.types import Market
from stock_tracker.quant.core.classification import (
    ClassificationAuthority,
    ClassificationContractError,
    ClassificationCoverage,
    ClassificationFact,
    ClassificationKind,
    ClassificationMembershipFact,
    ClassificationMembershipState,
    ClassificationTaxonomy,
    HistoricalClassification,
)
from stock_tracker.quant.core.universe import InstrumentIdentityFact, SecurityType

KNOWN = datetime.fromisoformat("2024-01-10T08:00:00+08:00")
AS_OF = datetime.fromisoformat("2024-01-12T16:00:00+08:00")
LATE_AS_OF = datetime.fromisoformat("2024-02-12T16:00:00+08:00")
SESSION = date(2024, 1, 12)
TAXONOMY_ID = "CAPCO-INDUSTRY"
VERSION = "2024H1-fixture"
SOURCE = "CAPCO"
INSTRUMENT = "CN:SSE:SSECID-ALPHA"
CLASSIFICATION = "C39"


class ClassificationFixtures(unittest.TestCase):
    def taxonomy(self, **changes):
        values = {
            "taxonomy_id": TAXONOMY_ID,
            "name": "上市公司行业分类",
            "kind": ClassificationKind.INDUSTRY,
            "authority": ClassificationAuthority.OFFICIAL_REGULATOR,
            "owner": SOURCE,
            "taxonomy_version": VERSION,
            "commercial_definition": False,
            "verified": False,
            "source_note": "",
        }
        values.update(changes)
        return ClassificationTaxonomy(**values)

    def coverage(self, **changes):
        values = {
            "taxonomy_id": TAXONOMY_ID,
            "market": Market.A,
            "start_date": date(2024, 1, 1),
            "end_date": date(2024, 6, 30),
            "source": SOURCE,
            "taxonomy_version": VERSION,
            "known_at": KNOWN,
            "usable_from": KNOWN,
            "revision": "coverage-r1",
            "supersedes_revision": None,
            "verified": False,
            "complete": False,
            "source_note": "",
        }
        values.update(changes)
        return ClassificationCoverage(**values)

    def classification(self, **changes):
        values = {
            "taxonomy_id": TAXONOMY_ID,
            "classification_id": CLASSIFICATION,
            "name": "计算机、通信和其他电子设备制造业",
            "parent_classification_id": "C",
            "effective_from": date(2024, 1, 1),
            "effective_to": None,
            "known_at": KNOWN,
            "usable_from": KNOWN,
            "source": SOURCE,
            "taxonomy_version": VERSION,
            "revision": "class-r1",
            "supersedes_revision": None,
            "verified": False,
            "source_note": "",
        }
        values.update(changes)
        return ClassificationFact(**values)

    def identity(self, **changes):
        values = {
            "instrument_id": INSTRUMENT,
            "symbol": "600101.SH",
            "market": Market.A,
            "exchange": "SSE",
            "security_type": SecurityType.COMMON_EQUITY,
            "effective_from": date(2024, 1, 8),
            "effective_to": None,
            "known_at": KNOWN,
            "usable_from": KNOWN,
            "source": "fixture-identity",
            "revision": "identity-r1",
            "verified": False,
            "source_note": "",
        }
        values.update(changes)
        return InstrumentIdentityFact(**values)

    def membership(self, identity=None, **changes):
        identity = identity or self.identity()
        values = {
            "taxonomy_id": TAXONOMY_ID,
            "classification_id": CLASSIFICATION,
            "instrument_id": identity.instrument_id,
            "identity_fact_id": identity.fact_id,
            "symbol": identity.symbol,
            "market": Market.A,
            "effective_from": SESSION,
            "effective_to": None,
            "state": ClassificationMembershipState.INCLUDED,
            "known_at": KNOWN,
            "usable_from": KNOWN,
            "source": SOURCE,
            "taxonomy_version": VERSION,
            "revision": "membership-r1",
            "supersedes_revision": None,
            "verified": False,
            "source_note": "",
        }
        values.update(changes)
        return ClassificationMembershipFact(**values)

    def historical(
        self,
        *,
        taxonomy=None,
        coverages=None,
        classifications=None,
        memberships=None,
        identities=None,
    ):
        identity = self.identity()
        return HistoricalClassification(
            taxonomy or self.taxonomy(),
            coverages or (self.coverage(),),
            classifications or (self.classification(),),
            memberships or (self.membership(identity),),
            identities or (identity,),
        )

    def snapshot(self, **kwargs):
        return self.historical(**kwargs).snapshot(
            Market.A,
            SESSION,
            AS_OF,
            require_verified=False,
            require_complete=False,
        )


class TestClassificationContracts(ClassificationFixtures):
    def test_taxonomy_authority_and_kind_cannot_be_mislabelled(self) -> None:
        with self.assertRaisesRegex(ClassificationContractError, "theme"):
            self.taxonomy(kind=ClassificationKind.THEME)
        commercial = self.taxonomy(
            kind=ClassificationKind.THEME,
            authority=ClassificationAuthority.SECONDARY_VENDOR,
            owner="secondary-fixture",
            commercial_definition=True,
        )
        self.assertTrue(commercial.commercial_definition)
        self.assertNotEqual(
            commercial.taxonomy_identity,
            self.taxonomy().taxonomy_identity,
        )

    def test_incomplete_candidate_snapshot_requires_explicit_opt_out(self) -> None:
        with self.assertRaisesRegex(ClassificationContractError, "complete"):
            self.historical().snapshot(
                Market.A,
                SESSION,
                AS_OF,
                require_verified=False,
            )
        snapshot = self.snapshot()
        self.assertFalse(snapshot.coverage.complete)
        self.assertEqual(snapshot.classification_members(CLASSIFICATION), ("600101.SH",))
        self.assertEqual(snapshot.instrument_classifications(INSTRUMENT), (CLASSIFICATION,))

    def test_snapshot_id_and_derived_identity_cannot_be_relabelled(self) -> None:
        snapshot = self.snapshot()
        with self.assertRaisesRegex(TypeError, "init=False"):
            replace(snapshot, snapshot_id="f" * 64)
        reordered = self.snapshot(
            classifications=tuple(reversed(self.historical().classifications)),
            memberships=tuple(reversed(self.historical().memberships)),
        )
        self.assertEqual(snapshot.snapshot_id, reordered.snapshot_id)

    def test_membership_requires_identity_active_on_session_date(self) -> None:
        identity = self.identity(effective_from=date(2024, 1, 13))
        membership = self.membership(identity)
        with self.assertRaisesRegex(ClassificationContractError, "active"):
            self.snapshot(memberships=(membership,), identities=(identity,))

    def test_symbol_change_keeps_instrument_membership_via_revision(self) -> None:
        old_identity = self.identity(
            symbol="600100.SH",
            effective_from=date(2020, 1, 1),
            effective_to=date(2024, 1, 7),
            revision="identity-old",
        )
        current_identity = self.identity(
            symbol="600101.SH",
            effective_from=date(2024, 1, 8),
            effective_to=None,
            revision="identity-new",
        )
        old_membership = self.membership(
            old_identity,
            effective_from=date(2024, 1, 1),
            effective_to=date(2024, 1, 7),
            revision="membership-old",
        )
        current_membership = self.membership(
            current_identity,
            effective_from=date(2024, 1, 8),
            effective_to=None,
            revision="membership-new",
            supersedes_revision="membership-old",
        )
        snapshot = self.snapshot(
            memberships=(old_membership, current_membership),
            identities=(old_identity, current_identity),
        )
        self.assertEqual(
            snapshot.instrument_classifications(INSTRUMENT),
            (CLASSIFICATION,),
        )
        self.assertEqual(
            snapshot.classification_members(CLASSIFICATION),
            ("600101.SH",),
        )

    def test_reused_symbol_does_not_inherit_old_instrument_membership(self) -> None:
        old_identity = self.identity(
            instrument_id="CN:SSE:OLD",
            symbol="600101.SH",
            effective_from=date(2020, 1, 1),
            effective_to=date(2024, 1, 10),
            revision="old-id",
        )
        new_identity = self.identity(
            instrument_id="CN:SSE:NEW",
            symbol="600101.SH",
            effective_from=date(2024, 1, 11),
            effective_to=None,
            revision="new-id",
        )
        old_membership = self.membership(
            old_identity,
            instrument_id=old_identity.instrument_id,
            identity_fact_id=old_identity.fact_id,
            effective_from=date(2024, 1, 1),
            effective_to=date(2024, 1, 10),
            revision="old-membership",
        )
        new_membership = self.membership(
            new_identity,
            instrument_id=new_identity.instrument_id,
            identity_fact_id=new_identity.fact_id,
            effective_from=date(2024, 1, 11),
            effective_to=None,
            revision="new-membership",
        )
        snapshot = self.snapshot(
            memberships=(old_membership, new_membership),
            identities=(old_identity, new_identity),
        )
        self.assertEqual(
            snapshot.instrument_classifications(old_identity.instrument_id),
            (),
        )
        self.assertEqual(
            snapshot.instrument_classifications(new_identity.instrument_id),
            (CLASSIFICATION,),
        )


class TestClassificationPointInTime(ClassificationFixtures):
    def test_future_membership_revision_does_not_rewrite_earlier_snapshot(self) -> None:
        identity = self.identity()
        included = self.membership(identity)
        excluded = self.membership(
            identity,
            state=ClassificationMembershipState.EXCLUDED,
            known_at=datetime.fromisoformat("2024-02-01T08:00:00+08:00"),
            usable_from=datetime.fromisoformat("2024-02-01T08:00:00+08:00"),
            revision="membership-r2",
            supersedes_revision="membership-r1",
        )
        historical = self.historical(
            memberships=(included, excluded),
            identities=(identity,),
        )
        before = historical.snapshot(
            Market.A,
            SESSION,
            AS_OF,
            require_verified=False,
            require_complete=False,
        )
        after = historical.snapshot(
            Market.A,
            SESSION,
            LATE_AS_OF,
            require_verified=False,
            require_complete=False,
        )
        self.assertEqual(len(before.included_memberships), 1)
        self.assertEqual(len(after.included_memberships), 0)

    def test_terminal_coverage_narrowing_removes_old_claim(self) -> None:
        broad = self.coverage()
        narrow = self.coverage(
            start_date=date(2024, 2, 1),
            end_date=date(2024, 6, 30),
            known_at=datetime.fromisoformat("2024-02-01T08:00:00+08:00"),
            usable_from=datetime.fromisoformat("2024-02-01T08:00:00+08:00"),
            revision="coverage-r2",
            supersedes_revision="coverage-r1",
        )
        historical = self.historical(coverages=(broad, narrow))
        historical.snapshot(
            Market.A,
            SESSION,
            AS_OF,
            require_verified=False,
            require_complete=False,
        )
        with self.assertRaisesRegex(ClassificationContractError, "coverage"):
            historical.snapshot(
                Market.A,
                SESSION,
                LATE_AS_OF,
                require_verified=False,
                require_complete=False,
            )

    def test_revision_cycle_and_disconnected_terminals_fail_closed(self) -> None:
        base = self.membership()
        cycle_a = replace(
            base,
            revision="r2",
            supersedes_revision="r3",
        )
        cycle_b = replace(
            base,
            revision="r3",
            supersedes_revision="r2",
        )
        disconnected = replace(
            base,
            revision="other-root",
            supersedes_revision=None,
        )
        for name, memberships in (
            ("cycle", (cycle_a, cycle_b)),
            ("disconnected", (base, disconnected)),
        ):
            with self.subTest(name=name), self.assertRaises(
                ClassificationContractError
            ):
                self.snapshot(memberships=memberships)

    def test_taxonomy_version_and_source_streams_cannot_mix(self) -> None:
        other = replace(
            self.coverage(),
            source="OTHER",
            taxonomy_version="other-v1",
            revision="other-coverage",
        )
        with self.assertRaisesRegex(ClassificationContractError, "multiple"):
            self.snapshot(coverages=(self.coverage(), other))


if __name__ == "__main__":
    unittest.main()

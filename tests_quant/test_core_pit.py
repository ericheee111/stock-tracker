from __future__ import annotations

import math
import unittest
from datetime import datetime
from typing import cast

from _helpers import utc_datetime

from stock_tracker.quant.core.fingerprint import (
    FingerprintError,
    canonical_json,
    fingerprint,
)
from stock_tracker.quant.core.point_in_time import (
    PITConflictError,
    PITFact,
    PointInTimeStore,
    decode_revision,
    encode_revision,
    revision_key,
)
from stock_tracker.quant.core.time import TimeContractError, to_utc


class TestStrictTime(unittest.TestCase):
    def test_naive_datetime_rejected(self) -> None:
        with self.assertRaises(TimeContractError):
            to_utc(datetime(2025, 1, 1))  # noqa: DTZ001 - intentional naive input

    def test_aware_datetime_normalized(self) -> None:
        value = utc_datetime(2025, 1, 1, 8)
        self.assertEqual(to_utc(value), value)


class TestFingerprint(unittest.TestCase):
    def test_set_order_is_stable(self) -> None:
        self.assertEqual(fingerprint({"x": {3, 1, 2}}), fingerprint({"x": {2, 3, 1}}))

    def test_mapping_order_is_stable(self) -> None:
        self.assertEqual(canonical_json({"b": 2, "a": 1}), canonical_json({"a": 1, "b": 2}))

    def test_non_string_mapping_keys_are_rejected(self) -> None:
        for value in ({1: "x"}, {"nested": {1: "x"}}):
            with self.subTest(value=value), self.assertRaisesRegex(
                FingerprintError,
                "mapping keys must be strings",
            ):
                fingerprint(value)

    def test_nan_and_infinity_rejected(self) -> None:
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaises(FingerprintError):
                fingerprint(value)


class TestRevisionEncoding(unittest.TestCase):
    def test_integer_revision_round_trip(self) -> None:
        kind, value = encode_revision(10)
        self.assertEqual((kind, value), ("INTEGER", "10"))
        self.assertEqual(decode_revision(kind, value), 10)

    def test_string_revision_round_trip(self) -> None:
        kind, value = encode_revision("r2")
        self.assertEqual(decode_revision(kind, value), "r2")

    def test_noncanonical_integer_rejected(self) -> None:
        with self.assertRaises(ValueError):
            decode_revision("INTEGER", "010")

    def test_boolean_revision_rejected(self) -> None:
        with self.assertRaises(TypeError):
            revision_key(True)

    def test_integer_orders_above_string(self) -> None:
        self.assertGreater(revision_key(1), revision_key("999"))


class TestPointInTimeStore(unittest.TestCase):
    @staticmethod
    def fact(
        *,
        known_at: datetime,
        revision: int | str,
        payload: object,
        event_time: datetime | None = None,
        usable_from: datetime | None = None,
        source: str = "fixture",
        verified: bool = True,
    ) -> PITFact:
        return PITFact(
            namespace="fundamental",
            entity_id="600000.SH",
            field="eps",
            event_time=event_time or utc_datetime(2024, 12, 31),
            known_at=known_at,
            usable_from=usable_from or known_at,
            revision=revision,
            payload=payload,
            source=source,
            verified=verified,
        )

    def test_future_revision_not_visible(self) -> None:
        old = self.fact(known_at=utc_datetime(2025, 1, 2), revision=1, payload=1.0)
        future = self.fact(known_at=utc_datetime(2025, 1, 10), revision=2, payload=2.0)
        snapshot = PointInTimeStore((old, future)).snapshot(utc_datetime(2025, 1, 5))
        self.assertEqual(snapshot.facts[0].payload, 1.0)

    def test_latest_known_revision_selected(self) -> None:
        first = self.fact(known_at=utc_datetime(2025, 1, 2), revision=1, payload=1.0)
        latest = self.fact(known_at=utc_datetime(2025, 1, 3), revision=1, payload=1.1)
        snapshot = PointInTimeStore((first, latest)).snapshot(utc_datetime(2025, 1, 5))
        self.assertEqual(snapshot.facts[0].payload, 1.1)

    def test_snapshot_get_returns_latest_event_time(self) -> None:
        older = self.fact(
            event_time=utc_datetime(2024, 9, 30),
            known_at=utc_datetime(2025, 1, 2),
            revision=1,
            payload=0.9,
        )
        newer = self.fact(
            event_time=utc_datetime(2024, 12, 31),
            known_at=utc_datetime(2025, 1, 3),
            revision=1,
            payload=1.1,
        )
        snapshot = PointInTimeStore((older, newer)).snapshot(utc_datetime(2025, 1, 5))
        self.assertEqual(
            snapshot.get("fundamental", "600000.SH", "eps"),
            newer,
        )

    def test_verified_requires_real_boolean(self) -> None:
        with self.assertRaisesRegex(TypeError, "verified must be a boolean"):
            self.fact(
                known_at=utc_datetime(2025, 1, 2),
                revision=1,
                payload=1.0,
                verified=cast(bool, "false"),
            )

    def test_snapshot_gate_requires_real_boolean(self) -> None:
        fact = self.fact(
            known_at=utc_datetime(2025, 1, 2),
            revision=1,
            payload=1.0,
        )
        with self.assertRaisesRegex(TypeError, "require_verified must be a boolean"):
            PointInTimeStore((fact,)).snapshot(
                utc_datetime(2025, 1, 5),
                require_verified=cast(bool, 0),
            )

    def test_highest_revision_breaks_same_known_at_tie(self) -> None:
        first = self.fact(known_at=utc_datetime(2025, 1, 2), revision=1, payload=1.0)
        latest = self.fact(known_at=utc_datetime(2025, 1, 2), revision=2, payload=1.1)
        snapshot = PointInTimeStore((first, latest)).snapshot(utc_datetime(2025, 1, 5))
        self.assertEqual(snapshot.facts[0].payload, 1.1)

    def test_conflicting_latest_payloads_fail_closed(self) -> None:
        left = self.fact(known_at=utc_datetime(2025, 1, 2), revision=2, payload=1.0)
        right = self.fact(known_at=utc_datetime(2025, 1, 2), revision=2, payload=2.0)
        with self.assertRaises(PITConflictError):
            PointInTimeStore((left, right)).snapshot(utc_datetime(2025, 1, 5))

    def test_unverified_fact_excluded_by_default(self) -> None:
        fact = self.fact(
            known_at=utc_datetime(2025, 1, 2),
            revision=1,
            payload=1.0,
            verified=False,
        )
        self.assertEqual(PointInTimeStore((fact,)).snapshot(utc_datetime(2025, 1, 5)).facts, ())

    def test_usable_from_cannot_precede_known_at(self) -> None:
        with self.assertRaises(ValueError):
            self.fact(
                known_at=utc_datetime(2025, 1, 3),
                usable_from=utc_datetime(2025, 1, 2),
                revision=1,
                payload=1.0,
            )

    def test_snapshot_identity_is_order_independent(self) -> None:
        one = self.fact(known_at=utc_datetime(2025, 1, 2), revision=1, payload=1.0)
        two = PITFact(
            namespace="fundamental",
            entity_id="000001.SZ",
            field="eps",
            event_time=utc_datetime(2024, 12, 31),
            known_at=utc_datetime(2025, 1, 2),
            usable_from=utc_datetime(2025, 1, 2),
            revision=1,
            payload=2.0,
            source="fixture",
        )
        cutoff = utc_datetime(2025, 1, 5)
        left = PointInTimeStore((one, two)).snapshot(cutoff)
        right = PointInTimeStore((two, one)).snapshot(cutoff)
        self.assertEqual(left.snapshot_id, right.snapshot_id)


if __name__ == "__main__":
    unittest.main()

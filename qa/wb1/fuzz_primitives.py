"""WB1 batch C: bounded seed-mutation probe over frozen primitive inputs.

For every frozen primitive field of ``MarketEventTransportRecord``, inject
explicitly illegal values (wrong exact type, out-of-range, malformed hash/text,
naive datetime, non-empty payload) and assert the contract rejects them with
``MarketEventSourceContractError`` (AGENTS.md §8: fail closed, exact type).

Structure:
- PART A: deterministic exhaustive (field, illegal-value) sweep (exact coverage).
- PART B: 5 fixed seeds x 10 random mutations from the same pool.

Total calls are bounded well under 100. Any value that is NOT rejected is
reported as ``unexpected_pass`` with a minimal reproduction (field + value) —
such an outcome would be reported as FIXTURE_BUG / SUSPECTED_PRODUCT_BUG, never
turned into a passing assertion.

Run from the clone root as a module (package context is required so the
``stock_tracker`` import resolves):

    py -3.14 -B -m qa.wb1.fuzz_primitives
"""

from __future__ import annotations

import json
import random
import sys
from datetime import datetime

from qa.wb1._helpers import valid_record
from stock_tracker.runtime_evidence.source_snapshot_contracts import (
    MarketEventSourceContractError,
)

# (field, illegal values) — each value must violate the frozen contract.
_POOL: list[tuple[str, list]] = [
    ("transport_append_order", [True, 1.0, "1", 0, None]),
    ("connection_epoch", ["1", 0, True, None]),
    ("reconnect_epoch", [True, -1, "0"]),
    ("queue_overflow_count", [True, 1.5, "0"]),
    ("dropped_callback_count", [True, -1, "0"]),
    ("source_store_id", [None, "not-a-hash", "A" * 64, "z" * 63]),
    ("transport_stream_id", [None, "not-a-hash", "A" * 64]),
    ("previous_transport_record_hash", [None, "g" * 64, "A" * 64]),
    ("session_id", ["", " wb1 ", "wb1\x00session", "x" * 300]),
    ("kind", ["CONNECTED", None, 1]),
    ("observed_at", [None, datetime(2026, 9, 2, 1, 30)]),  # noqa: DTZ001 - deliberately invalid naive-time fixture
    ("durable_known_at", [None, datetime(2026, 9, 2, 1, 30)]),  # noqa: DTZ001 - deliberately invalid naive-time fixture
    ("canonical_payload", [123, '{"x":1}', "{}extra", None]),
]

_SEEDS = [1, 7, 42, 2026, 90909]


def _mutations() -> list[tuple[str, object]]:
    pairs: list[tuple[str, object]] = []
    for field, values in _POOL:
        for value in values:
            pairs.append((field, value))
    return pairs


def main() -> int:
    pairs = _mutations()
    rejected = 0
    unexpected: list[dict] = []

    def probe(field: str, value: object) -> bool:
        try:
            valid_record(**{field: value})
            return False  # not rejected -> unexpected pass
        except MarketEventSourceContractError:
            return True  # rejected as required

    # PART A: deterministic exhaustive sweep.
    for field, value in pairs:
        if probe(field, value):
            rejected += 1
        else:
            unexpected.append({"field": field, "value": repr(value)})

    # PART B: 5 fixed seeds x 10 random mutations from the pool.
    random_calls = 0
    for seed in _SEEDS:
        rng = random.Random(seed)
        for _ in range(10):
            field, value = rng.choice(pairs)
            random_calls += 1
            if probe(field, value):
                rejected += 1
            else:
                unexpected.append(
                    {"field": field, "value": repr(value), "seed": seed}
                )

    report = {
        "deterministic_pool_size": len(pairs),
        "random_calls": random_calls,
        "total_calls": len(pairs) + random_calls,
        "rejected": rejected,
        "unexpected_pass": unexpected,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if unexpected else 0


if __name__ == "__main__":
    sys.exit(main())

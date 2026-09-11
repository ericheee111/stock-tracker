"""Probe: observe exact behavior of clock boundary values.

Determines whether snapshot construction raises or produces findings for
ahead=1/2/3 seconds and durability delay=2/300/301 seconds. Output is used to
fix the WB1 assertions against frozen semantics (strict ``>`` boundaries),
NOT to auto-generate assertions from return values.

Run from the clone root as a module (package context required):

    py -3.14 -B -m qa.wb1.probe_clock_boundary
"""

from __future__ import annotations

import sys
from dataclasses import fields
from datetime import timedelta

from qa.wb1._helpers import MarketEventSourceSnapshotTestCase
from stock_tracker.runtime_evidence.source_snapshot_contracts import (
    MarketEventSourceRecord,
)


def main() -> int:
    case = MarketEventSourceSnapshotTestCase("run")
    case.setUp()
    first = case.records()[0]

    def clock_record(*, ahead_seconds=0, delay_seconds=2):
        args = {
            f.name: getattr(first, f.name)
            for f in fields(first)
            if f.init
            and f.name
            not in {
                "payload_json",
                "payload_sha256",
                "record_storage_key",
                "record_file_sha256",
                "record_content_hash",
            }
        }
        args.update(
            received_at=first.source_time - timedelta(seconds=ahead_seconds),
            durable_known_at=first.source_time + timedelta(seconds=delay_seconds),
        )
        return MarketEventSourceRecord.create(
            **args, payload={"last_price": 11, "quantity": 100}
        )

    for label, rec in (
        ("ahead=1s", clock_record(ahead_seconds=1)),
        ("ahead=2s", clock_record(ahead_seconds=2)),
        ("ahead=3s", clock_record(ahead_seconds=3)),
        ("delay=300s", clock_record(ahead_seconds=0, delay_seconds=300)),
        ("delay=301s", clock_record(ahead_seconds=0, delay_seconds=301)),
    ):
        try:
            snap = case.snapshot((rec,))
            sel = case.select(snap, (rec,))
            print(f"{label}: SNAPSHOT_OK completeness={sel.completeness.value} "
                  f"covers={sel.covers_interval(case.base_time, case.base_time + timedelta(minutes=10))}")
        except Exception as exc:  # noqa: BLE001 - probe, report type+message
            print(f"{label}: RAISED {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

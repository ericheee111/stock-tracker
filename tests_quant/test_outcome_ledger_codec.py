from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from stock_tracker.quant.storage.outcome_ledger import (
    OutcomeLedgerError,
    OutcomeLedgerLane,
    OutcomeLedgerRecord,
    read_signal_outcome_json,
    signal_outcome_from_dict,
    signal_outcome_from_json_bytes,
    signal_outcome_to_dict,
    signal_outcome_to_json_bytes,
)
from tests_quant.test_outcomes import _complete_outcome, _open_outcome


class TestSignalOutcomeLedgerCodec(unittest.TestCase):
    def test_round_trip_recomputes_all_derived_identity(self) -> None:
        outcome = _complete_outcome(signal_suffix="codec")
        raw = signal_outcome_to_json_bytes(outcome)
        rebuilt = signal_outcome_from_json_bytes(raw)
        self.assertEqual(rebuilt, outcome)
        self.assertEqual(signal_outcome_to_json_bytes(rebuilt), raw)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outcome.json"
            path.write_bytes(raw)
            self.assertEqual(read_signal_outcome_json(path), outcome)

    def test_derived_and_nested_tampering_fails_closed(self) -> None:
        baseline = signal_outcome_to_dict(_complete_outcome(signal_suffix="tamper"))

        def change_metrics(value):
            value["metrics"]["realized_r"] = "999"

        def change_fill(value):
            value["entry_fill"]["implicit_cost"] = "999"

        def change_path(value):
            value["path"][0]["high"] = "99"

        mutations = (
            ("outcome_id", lambda value: value.__setitem__("outcome_id", "f" * 64)),
            ("state", lambda value: value.__setitem__("state", "OPEN")),
            (
                "eligibility",
                lambda value: value.__setitem__("real_scoreboard_eligible", False),
            ),
            ("metrics", change_metrics),
            ("fill", change_fill),
            ("path", change_path),
        )
        for name, mutate in mutations:
            with self.subTest(name=name):
                value = json.loads(json.dumps(baseline))
                mutate(value)
                with self.assertRaises(OutcomeLedgerError):
                    signal_outcome_from_dict(value)

    def test_json_type_and_canonical_decimal_boundaries_fail_closed(self) -> None:
        for raw, message in (
            (b'\xef\xbb\xbf{"x":1}', "BOM"),
            (b'{"x":1,"x":2}', "duplicate JSON keys"),
            (b'{"x":NaN}', "non-finite"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(
                OutcomeLedgerError,
                message,
            ):
                signal_outcome_from_json_bytes(raw)

        value = signal_outcome_to_dict(_complete_outcome(signal_suffix="decimal"))
        value["entry_fill"]["fill_price"] = "10.00"
        with self.assertRaisesRegex(OutcomeLedgerError, "canonical"):
            signal_outcome_from_dict(value)

        value = signal_outcome_to_dict(_complete_outcome(signal_suffix="exponent"))
        value["entry_fill"]["fill_price"] = "1e999999999"
        with self.assertRaisesRegex(OutcomeLedgerError, "canonical"):
            signal_outcome_from_dict(value)

        value = signal_outcome_to_dict(_complete_outcome(signal_suffix="time"))
        self.assertTrue(value["recorded_at"].endswith(".000000Z"))
        value["recorded_at"] = value["recorded_at"].replace(".000000Z", "Z")
        with self.assertRaisesRegex(OutcomeLedgerError, "canonical UTC"):
            signal_outcome_from_dict(value)

        value = signal_outcome_to_dict(_complete_outcome(signal_suffix="unicode-control"))
        value["strategy_id"] = "S1_BREAKOUT\u202e"
        with self.assertRaisesRegex(OutcomeLedgerError, "safe non-empty"):
            signal_outcome_from_dict(value)

        value = signal_outcome_to_dict(_complete_outcome(signal_suffix="extra"))
        value["unexpected"] = True
        with self.assertRaisesRegex(OutcomeLedgerError, "field set"):
            signal_outcome_from_dict(value)

        canonical = signal_outcome_to_json_bytes(
            _complete_outcome(signal_suffix="noncanonical-bytes")
        )
        pretty = json.dumps(
            json.loads(canonical),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8") + b"\n"
        with self.assertRaisesRegex(OutcomeLedgerError, "canonical Stage 4F form"):
            signal_outcome_from_json_bytes(pretty)

    def test_record_derived_lane_and_hash_cannot_be_injected(self) -> None:
        outcome = _complete_outcome(signal_suffix="record")
        record = OutcomeLedgerRecord(
            append_order=1,
            recorded_by="reviewed-fixture",
            ingested_at=outcome.recorded_at,
            previous_record_hash="0" * 64,
            outcome=outcome,
        )
        with self.assertRaises(TypeError):
            replace(record, lane=OutcomeLedgerLane.DIAGNOSTIC_ONLY)
        with self.assertRaises(TypeError):
            replace(record, record_hash="f" * 64)

        value = record.as_dict()
        value["lane"] = OutcomeLedgerLane.DIAGNOSTIC_ONLY.value
        with self.assertRaisesRegex(OutcomeLedgerError, "lane"):
            OutcomeLedgerRecord.from_dict(value)

        value = record.as_dict()
        value["record_hash"] = "f" * 64
        with self.assertRaisesRegex(OutcomeLedgerError, "hash mismatch"):
            OutcomeLedgerRecord.from_dict(value)

    def test_deeply_nested_json_fails_closed_without_recursion_escape(self) -> None:
        raw = ("[" * 2000 + "0" + "]" * 2000).encode("utf-8")
        with self.assertRaises(OutcomeLedgerError):
            signal_outcome_from_json_bytes(raw)

    def test_file_reader_rejects_oversized_input_before_json_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oversized.json"
            path.write_bytes(b"{" + b" " * (16 * 1024 * 1024))
            with self.assertRaisesRegex(OutcomeLedgerError, "size"):
                read_signal_outcome_json(path)

    def test_open_outcome_can_be_serialized_but_is_not_terminal_ledger_evidence(self) -> None:
        outcome = _open_outcome()
        self.assertEqual(
            signal_outcome_from_json_bytes(signal_outcome_to_json_bytes(outcome)),
            outcome,
        )


if __name__ == "__main__":
    unittest.main()

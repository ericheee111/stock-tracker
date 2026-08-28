from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

from stock_tracker.market_events.shadow import (
    ComparisonStatus,
    ShadowContractError,
    ShadowObservation,
    ShadowThresholds,
    build_shadow_fixture,
    compare_observations,
    representative_symbols,
    run_shadow_acceptance,
)

ROOT = Path(__file__).resolve().parents[1]


def _sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


class TestXtpShadowAcceptance(unittest.TestCase):
    def test_fixture_is_representative_deterministic_and_synthetic_only(self) -> None:
        symbols = representative_symbols()
        self.assertEqual(len(symbols), 64)
        self.assertEqual(len(set(symbols)), 64)
        boards = {board for _, board in symbols}
        self.assertEqual(boards, {"SH_MAIN", "SZ_MAIN", "CHINEXT", "STAR"})
        first = run_shadow_acceptance()
        second = run_shadow_acceptance()
        self.assertEqual(first["fixture_id"], second["fixture_id"])
        self.assertEqual(first["comparisons"], second["comparisons"])
        self.assertTrue(first["engineering_passed"])
        self.assertTrue(first["synthetic_fixture_only"])
        self.assertTrue(first["operational_live_account_pending"])
        self.assertEqual(
            first["stock_test_account_registration"],
            "USER_REPORTED_NOT_MACHINE_VERIFIED",
        )
        self.assertEqual(
            first["algorithm_test_account_registration"],
            "USER_REPORTED_NOT_MACHINE_VERIFIED",
        )
        self.assertTrue(first["no_real_strategy_claim"])
        self.assertFalse(first["allow_live_decision"])
        self.assertFalse(first["allow_model_training"])
        self.assertFalse(first["allow_public_redistribution"])
        self.assertFalse(first["auto_trade"])
        self.assertFalse(first["source_promotion_performed"])
        self.assertEqual(first["evidence_tier_status"], "T3_NOT_REACHED")

    def test_conflicts_unavailable_and_frequency_mismatch_are_preserved(self) -> None:
        report = run_shadow_acceptance()
        counts = report["comparison_status_counts"]
        self.assertGreater(counts[ComparisonStatus.CONFLICT.value], 0)
        self.assertGreater(counts[ComparisonStatus.SOURCE_UNAVAILABLE.value], 0)
        self.assertEqual(
            counts[ComparisonStatus.NON_OVERLAPPING_FREQUENCY.value],
            report["symbol_count"] * 2,
        )
        conflicts = [
            item
            for item in report["comparisons"]
            if item["status"] == ComparisonStatus.CONFLICT.value
        ]
        self.assertTrue(conflicts)
        self.assertTrue(all(item["conflict_reasons"] for item in conflicts))
        self.assertTrue(all(item["source_winner"] is None for item in conflicts))

    def test_non_overlapping_daily_source_is_not_compared_as_live(self) -> None:
        xtp_rows, references, _ = build_shadow_fixture()
        xtp = xtp_rows[0]
        daily = next(
            row
            for row in references
            if row.symbol == xtp.symbol and row.source == "hithink_finance"
        )
        comparison = compare_observations(xtp, daily, ShadowThresholds())
        self.assertEqual(
            comparison.status,
            ComparisonStatus.NON_OVERLAPPING_FREQUENCY,
        )
        self.assertIsNone(comparison.price_difference_bps)

    def test_thresholds_reject_bool_nonfinite_and_overclaiming_observation(self) -> None:
        with self.assertRaises(ShadowContractError):
            ShadowThresholds(maximum_timestamp_delta_ms=True)  # type: ignore[arg-type]
        with self.assertRaises(ShadowContractError):
            ShadowThresholds(maximum_price_difference_bps=float("nan"))
        with self.assertRaisesRegex(ShadowContractError, "cannot relabel"):
            ShadowObservation(
                "xtp",
                "600001.SH",
                "SH_MAIN",
                "NORMAL_TRADING",
                datetime.now(timezone.utc),
                10.0,
                100,
                "SNAPSHOT",
                synthetic_fixture=False,
            )

    def test_cli_preserves_production_database_and_emits_no_account_values(self) -> None:
        production = ROOT / "data" / "stock_tracker.db"
        before = _sha(production)
        completed = subprocess.run(
            [sys.executable, "scripts/run_xtp_shadow_acceptance.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        after = _sha(production)
        self.assertEqual(before, after)
        self.assertFalse(report["production_database_modified"])
        self.assertEqual(
            report["production_database_sha256_before"],
            report["production_database_sha256_after"],
        )
        rendered = completed.stdout.lower()
        self.assertNotIn("quote_access", rendered)
        self.assertNotIn("quote_password", rendered)
        self.assertNotIn("sidecar_access", rendered)
        self.assertFalse(report["algorithm_account_used"])


if __name__ == "__main__":
    unittest.main()

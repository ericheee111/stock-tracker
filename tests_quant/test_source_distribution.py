from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from stock_tracker.quant.data import DataSnapshotManifest, RawDataArtifact


class TestSourceDistribution(unittest.TestCase):
    """Guard against source packages being present locally but omitted by Git."""

    def test_quant_data_contract_imports(self) -> None:
        self.assertTrue(callable(RawDataArtifact))
        self.assertTrue(callable(DataSnapshotManifest))

    def test_critical_quant_data_files_are_tracked(self) -> None:
        root = Path(__file__).resolve().parents[1]
        if not (root / ".git").exists():
            self.skipTest("source-distribution check requires a Git checkout")

        critical = (
            ".gitattributes",
            "scripts/capture_quant_bars.py",
            "scripts/capture_hithink_bars.py",
            "scripts/capture_a_share_corporate_actions.py",
            "scripts/extract_a_share_corporate_actions.py",
            "scripts/materialize_adjusted_market_data.py",
            "scripts/reconcile_a_share_corporate_actions.py",
            "scripts/report_stage2g_market_bars.py",
            "scripts/build_stage2h_market_bar_acceptance.py",
            "scripts/report_stage2h_market_bar_acceptance.py",
            "scripts/verify_free_stockdb_sidecar.py",
            "stock_tracker/collector/free_stockdb.py",
            "stock_tracker/collector/hithink_finance.py",
            "stock_tracker/collector/tencent.py",
            "stock_tracker/quant/core/big_trend.py",
            "stock_tracker/quant/core/classification.py",
            "stock_tracker/quant/core/corporate_actions.py",
            "stock_tracker/quant/core/events.py",
            "stock_tracker/quant/core/market_isolation.py",
            "stock_tracker/quant/core/outcomes.py",
            "stock_tracker/quant/data/__init__.py",
            "stock_tracker/quant/data/adjusted_market_data.py",
            "stock_tracker/quant/data/bar_artifact.py",
            "stock_tracker/quant/data/classification_adapter.py",
            "stock_tracker/quant/data/corporate_action_adapter.py",
            "stock_tracker/quant/data/corporate_action_extraction.py",
            "stock_tracker/quant/data/corporate_action_reconciliation.py",
            "stock_tracker/quant/data/free_stockdb_governance.py",
            "stock_tracker/quant/data/manifest.py",
            "stock_tracker/quant/data/market_bar_acceptance.py",
            "stock_tracker/quant/data/market_bar_golden.py",
            "stock_tracker/quant/data/market_bar_reconciliation.py",
            "stock_tracker/quant/evaluation/attribution.py",
            "stock_tracker/quant/evaluation/decision_quality.py",
            "stock_tracker/quant/evaluation/shadow_lifecycle.py",
            "stock_tracker/quant/research/replay.py",
            "stock_tracker/quant/storage/migrations/0004_corporate_action_identity.sql",
            "tests_quant/test_adjusted_market_data.py",
            "tests_quant/test_attribution.py",
            "tests_quant/test_big_trend.py",
            "tests_quant/test_classification.py",
            "tests_quant/test_classification_adapter.py",
            "tests_quant/test_corporate_actions.py",
            "tests_quant/test_corporate_action_adapter.py",
            "tests_quant/test_corporate_action_extraction.py",
            "tests_quant/test_corporate_action_reconciliation.py",
            "tests_quant/test_capture_hithink_bars_cli.py",
            "tests_quant/test_decision_quality.py",
            "tests_quant/test_events.py",
            "tests_quant/test_free_stockdb_governance.py",
            "tests_quant/test_market_isolation.py",
            "tests_quant/test_market_bar_acceptance.py",
            "tests_quant/test_market_bar_reconciliation.py",
            "tests_quant/test_outcomes.py",
            "tests_quant/test_replay.py",
            "tests_quant/test_shadow_lifecycle.py",
            "tests_quant/test_stage2g_market_bar_cli.py",
            "tests_quant/test_stage2h_market_bar_acceptance_cli.py",
            "tests_quant/fixtures/market_bar_golden/v1/manifest.json",
            "tests_quant/fixtures/market_bar_golden/v1/a/600519_eastmoney.json",
            "tests_quant/fixtures/market_bar_golden/v1/a/600519_tencent.json",
            "tests_quant/fixtures/market_bar_golden/v1/hk/00700_eastmoney.json",
            "tests_quant/fixtures/market_bar_golden/v1/hk/00700_tencent.json",
            "tests_quant/fixtures/market_bar_golden/v1/us/AAPL_eastmoney.json",
            "tests_quant/fixtures/market_bar_golden/v1/us/AAPL_tencent.json",
            "tests_quant/fixtures/market_bar_golden/v2/manifest.json",
            "tests_quant/fixtures/market_bar_golden/v2/a/600519_eastmoney.json",
            "tests_quant/fixtures/market_bar_golden/v2/a/600519_tencent.json",
            "tests_quant/fixtures/market_bar_golden/v2/hk/00700_eastmoney.json",
            "tests_quant/fixtures/market_bar_golden/v2/hk/00700_tencent.json",
            "tests_quant/fixtures/market_bar_golden/v2/us/AAPL_eastmoney.json",
            "tests_quant/fixtures/market_bar_golden/v2/us/AAPL_tencent.json",
            "tests/test_free_stockdb_provider.py",
            "tests/test_hithink_finance_provider.py",
            "tests/test_provider_research_request.py",
        )
        for relative_path in critical:
            with self.subTest(path=relative_path):
                result = subprocess.run(
                    ["git", "ls-files", "--error-unmatch", relative_path],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    msg=(
                        f"critical source file is not tracked: {relative_path}\n"
                        f"stdout={result.stdout}\nstderr={result.stderr}"
                    ),
                )


if __name__ == "__main__":
    unittest.main()

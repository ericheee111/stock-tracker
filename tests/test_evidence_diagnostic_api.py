"""No-write, one-read detail adapter and fixed CLI failure semantics."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import run_evidence_comparison as runner
from stock_tracker.api import evidence_handlers
from stock_tracker.api.handlers import AppContext, get_quote_detail
from tests.test_api_indicators import _bundle
from tests.test_evidence_comparison import AT, context, row


class TestEvidenceDetailAPI(unittest.TestCase):
    def make_context(self):
        source = context()
        source.quote.quality = source.dq
        store = Mock()
        store.get_quote.return_value = source.quote
        for name in ("get_regime", "get_sectors", "get_positions", "get_portfolio_profile", "get_signals"):
            getattr(store, name).side_effect = AssertionError("unrelated state queried")
        repo = Mock()
        repo.load_recent_bars.return_value = source.recent_bars
        return AppContext(bundle=_bundle(), store=store, repo=repo, router=Mock(),
                          signal_manager=Mock(), sse_hub=SimpleNamespace()), source

    def test_reuses_one_bounded_read_and_keeps_old_detail(self):
        ctx, source = self.make_context()
        before = copy.deepcopy(source)
        result = get_quote_detail(ctx, source.symbol)
        ctx.repo.load_recent_bars.assert_called_once_with(source.symbol, "1d", n=260)
        ctx.store.get_quote.assert_called_once_with(source.symbol)
        self.assertEqual(source, before)
        self.assertEqual(result["bar_count"], 80)
        self.assertEqual(len(result["recent_bars"]), 30)
        self.assertIsNotNone(result["indicators"])
        comparison = result["evidence_comparison"]
        self.assertEqual(comparison["status"], "PARTIAL")
        self.assertEqual(comparison["computed_at"], result["indicator_diagnostics"]["computed_at"])
        self.assertIsNone(row(comparison, "scores", "opportunity")["value"])
        self.assertIsNotNone(row(comparison, "families", "trend")["value"])
        self.assertFalse(ctx.router.mock_calls)
        self.assertFalse(ctx.signal_manager.mock_calls)
        self.assertEqual(len(ctx.repo.mock_calls), 1)

    def test_optional_failure_logged_without_raw_exception_and_old_detail_survives(self):
        ctx, source = self.make_context()
        private_marker = "[REDACTED_SECRET]"
        with patch.object(evidence_handlers, "compare_evidence", side_effect=RuntimeError(private_marker)), self.assertLogs(evidence_handlers._LOG, "ERROR") as logs:
            result = get_quote_detail(ctx, source.symbol)
        self.assertIsNotNone(result["indicators"])
        self.assertEqual(result["evidence_comparison"]["issues"], ["DIAGNOSTIC_INTERNAL_ERROR"])
        self.assertNotIn(private_marker, "\n".join(logs.output))

    def test_no_quote_retains_bars(self):
        ctx, source = self.make_context();ctx.store.get_quote.return_value = None
        result = get_quote_detail(ctx, source.symbol)
        self.assertEqual(result["evidence_comparison"]["status"], "NO_QUOTE")
        self.assertEqual(len(result["recent_bars"]), 30)

    def test_mixed_identity_not_filtered_into_valid_candidate(self):
        ctx, source = self.make_context();source.recent_bars[0].symbol = "AAPL.US"
        result = get_quote_detail(ctx, source.symbol)
        self.assertEqual(result["evidence_comparison"]["status"], "INVALID_INPUT")
        self.assertEqual(result["evidence_comparison"]["candidate"]["families"], [])

    def test_quote_aware_future_not_used(self):
        source = context();source.quote.received_at = AT + timedelta(seconds=1)
        report = evidence_handlers.evidence_detail(source.quote, source.recent_bars, source.symbol, source.market, AT)
        self.assertEqual(report["status"], "INVALID_INPUT")

    def test_invalid_symbol_does_not_trigger_any_reads(self):
        ctx, _ = self.make_context()
        self.assertIsNone(get_quote_detail(ctx, "bad"))
        self.assertFalse(ctx.store.mock_calls)
        self.assertFalse(ctx.repo.mock_calls)


class TestEvidenceFixtureCLI(unittest.TestCase):
    def test_twelve_fixed_scenarios_and_determinism(self):
        a = runner.build_report(details=True)
        self.assertEqual(a, runner.build_report(details=True))
        self.assertTrue(a["passed"])
        self.assertEqual(a["executed"], 12)
        self.assertEqual([r["case"] for r in a["cases"]], list(runner.CASES))
        self.assertFalse(a["strategy_promoted"])
        self.assertFalse(a["real_data_trial"])
        json.dumps(a, allow_nan=False)

    def test_empty_duplicate_and_unknown_plans_never_pass(self):
        for cases in ((), ("complete", "complete"), ("typo",)):
            with self.subTest(cases=cases), self.assertRaises(ValueError):
                runner.build_report(cases)

    def test_deliberately_wrong_result_rejected_by_fixed_expectation(self):
        original = runner.compare_evidence
        def bad(ctx, at):
            result = original(ctx, at)
            result["auto_trade"] = True
            return result
        with patch.object(runner, "compare_evidence", side_effect=bad):
            self.assertFalse(runner.build_report(("complete",))["passed"])

    def test_cli_real_exit_codes_and_json(self):
        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        def call(args):
            return subprocess.run([sys.executable, "-X", "utf8", "-B", "scripts/run_evidence_comparison.py", *args],
                                  cwd=root, capture_output=True, timeout=20, env=env, check=False, encoding="utf-8")
        good = call(["--case", "rsi-zero", "--details"])
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertEqual(json.loads(good.stdout)["executed"], 1)
        for args in (["--case", "typo"], ["--case", "complete", "--case", "complete"], ["--database", "production.db"]):
            with self.subTest(args=args):
                bad = call(args)
                self.assertEqual(bad.returncode, 2)
                self.assertNotIn('"passed": true', bad.stdout)


if __name__ == "__main__":
    unittest.main()

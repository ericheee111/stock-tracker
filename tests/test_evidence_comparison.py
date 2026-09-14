"""Independent fixed examples for a diagnostic candidate, never production scores."""
from __future__ import annotations

import copy
import json
import math
import random
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from stock_tracker.core import types as T
from stock_tracker.features import evidence as E
from stock_tracker.features.evidence_comparison import (
    compare_evidence,
    compare_snapshot,
)
from stock_tracker.features.evidence_snapshot import (
    EvidenceInputError,
    EvidenceSnapshot,
    canonical,
)
from stock_tracker.signals import scoring

AT = datetime(2026, 9, 14, 6, tzinfo=timezone.utc)


def context(n=80, *, falling=False, full_context=True):
    values = [100 - i * .01 if falling else 100 + i * .05 + (i % 4) * .02 for i in range(n)]
    bars = [T.Bar("600000.SH", T.Market.A, AT - timedelta(days=n-i), open=v, high=v+1, low=v-1,
                  close=v, volume=1000+i, amount=100000, source="synthetic") for i, v in enumerate(values)]
    last = values[-1] if values else 100.0
    q = T.Quote("600000.SH", T.Market.A, AT-timedelta(seconds=2), open=last, high=last+1, low=last-1,
                last=last, prev_close=last, volume=2000, amount=100000, turnover=2.0,
                received_at=AT-timedelta(seconds=1), computed_at=AT, source="synthetic", data_status=T.DataStatus.LIVE)
    return T.ScanContext(symbol=q.symbol, market=q.market, quote=q, recent_bars=bars,
                         dq=T.DataQuality(T.QualityStatus.VALID, 80, []),
                         regime=T.MarketRegime(T.RegimeState.ROTATION, 60) if full_context else None,
                         sector=T.SectorSnapshot(sector="synthetic-sector", score=60, relative_strength=60,
                                                 persistence=50, crowding=10, catalyst="fixture") if full_context else None)


def row(report, section, key):
    return next(item for item in report["candidate"][section] if item["key"] == key)


class TestEvidenceComparison(unittest.TestCase):
    def test_full_normal_input_equals_actual_legacy_functions(self):
        ctx = context()
        before = copy.deepcopy(ctx)
        report = compare_evidence(ctx, AT)
        legacy = scoring.score(ctx)
        self.assertEqual(report["status"], "COMPARABLE_NUMERIC")
        for key in ("opportunity", "timing", "risk", "confidence"):
            self.assertEqual(row(report, "scores", key)["value"], getattr(legacy, key))
        expected = E.compute_evidence(ctx.quote, ctx.recent_bars, ctx.regime, ctx.sector)
        for family in report["candidate"]["families"]:
            self.assertEqual(family["value"], getattr(expected, family["key"]))
        self.assertEqual(ctx, before)
        self.assertIsNone(report["success_probability"])
        for key in ("auto_trade", "execution_authorized", "affects_live_scores", "investment_performance_claim"):
            self.assertIs(report[key], False)

    def test_rsi_true_zero_is_not_replaced_with_fifty(self):
        report = compare_evidence(context(falling=True), AT)
        self.assertEqual(report["indicators"]["rsi14"], 0.0)
        self.assertIn("RSI_ZERO_WAS_NEUTRALIZED", report["findings"])
        delta = next(x for x in report["differences"] if x["key"] == "momentum")
        self.assertEqual(delta["delta"], -25)

    def test_ma60_missing_does_not_use_last_price(self):
        report = compare_evidence(context(20), AT)
        self.assertIsNone(row(report, "families", "trend")["value"])
        self.assertIn("MA60_WAS_LAST_PRICE_FALLBACK", report["findings"])
        self.assertIn("MA60_REQUIRED", row(report, "families", "trend")["missing"])
        self.assertIsInstance(report["legacy"]["families"]["trend"], int)

    def test_fourteen_is_not_enough_for_rsi(self):
        report = compare_evidence(context(14), AT)
        self.assertIsNone(report["indicators"]["rsi14"])
        self.assertIsNone(row(report, "families", "momentum")["value"])
        self.assertIn("RSI14_REQUIRED", row(report, "families", "momentum")["missing"])

    def test_missing_macd_has_no_downward_description(self):
        report = compare_evidence(context(33), AT)
        self.assertIsNone(report["indicators"]["macd_hist"])
        self.assertEqual(report["candidate"]["macd_direction"], "UNKNOWN")
        self.assertIn("MACD_MISSING_WAS_DESCRIBED_DOWN", report["findings"])
        self.assertIsNone(row(report, "scores", "timing")["value"])

    def test_no_partial_roc_average(self):
        report = compare_evidence(context(15), AT)
        term = next(t for t in row(report, "families", "momentum")["terms"] if t["key"] == "roc_mean")
        self.assertIsNone(term["value"])
        self.assertIn("ROC20_REQUIRED", term["missing"])

    def test_missing_dq_is_not_high_confidence(self):
        ctx = context();ctx.dq = None
        report = compare_evidence(ctx, AT)
        self.assertIsNone(row(report, "scores", "confidence")["value"])
        self.assertIn("DQ_MISSING_WAS_100", report["findings"])
        self.assertIsInstance(report["legacy"]["scores"]["confidence"], int)

    def test_true_zero_quality_is_preserved(self):
        ctx = context();ctx.dq.score = 0
        report = compare_evidence(ctx, AT)
        self.assertEqual(row(report, "scores", "confidence")["value"], scoring.score(ctx).confidence)
        self.assertNotIn("DQ_MISSING_WAS_100", report["findings"])

    def test_degraded_quality_has_no_candidate_confidence(self):
        ctx = context();ctx.dq.status = T.QualityStatus.DEGRADED
        report = compare_evidence(ctx, AT)
        self.assertIn("DQ_NOT_VALID", row(report, "scores", "confidence")["missing"])

    def test_context_missing_is_unknown_not_renormalized(self):
        report = compare_evidence(context(full_context=False), AT)
        self.assertEqual(report["status"], "PARTIAL")
        self.assertIsNone(row(report, "families", "relative_strength")["value"])
        for key in ("opportunity", "timing", "risk", "confidence"):
            self.assertIsNone(row(report, "scores", key)["value"])
        self.assertIsNotNone(row(report, "families", "trend")["value"])

    def test_quote_missing_fields_do_not_crash(self):
        for key in ("open", "high", "low", "last", "prev_close", "amount", "turnover"):
            ctx = context();setattr(ctx.quote, key, None)
            with self.subTest(key=key):
                report = compare_evidence(ctx, AT)
                self.assertEqual(report["status"], "PARTIAL")
                json.dumps(report, allow_nan=False)

    def test_no_bars_does_not_become_intraday_swing_evidence(self):
        report = compare_evidence(context(0), AT)
        self.assertEqual(report["sample_info"]["selected_count"], 0)
        for key in ("trend", "momentum", "price_structure"):
            self.assertIsNone(row(report, "families", key)["value"])

    def test_same_day_excluded_before_both_calculations(self):
        ctx = context()
        ctx.recent_bars.append(replace(ctx.recent_bars[-1], timestamp=AT, close=200.0, high=201.0, low=99.0))
        report = compare_evidence(ctx, AT)
        self.assertEqual(report["sample_info"]["same_day_excluded"], 1)
        self.assertEqual(report["sample_info"]["selected_count"], 80)
        self.assertEqual(report["legacy"]["scores"], compare_evidence(context(), AT)["legacy"]["scores"])

    def test_bounded_latest_eighty_from_same_snapshot(self):
        report = compare_evidence(context(260), AT)
        self.assertEqual(report["sample_info"]["selected_count"], 80)
        self.assertEqual(report["sample_info"]["older_excluded"], 180)
        self.assertEqual(compare_evidence(context(261), AT)["status"], "INVALID_INPUT")

    def test_invalid_and_cross_security_series_fail_without_partial_rescue(self):
        for mutation in (lambda c:setattr(c.recent_bars[0], "symbol", "AAPL.US"),
                         lambda c:setattr(c.recent_bars[0], "timestamp", AT+timedelta(days=1)),
                         lambda c:setattr(c.recent_bars[1], "timestamp", c.recent_bars[0].timestamp),
                         lambda c:setattr(c.recent_bars[0], "high", .01),
                         lambda c:setattr(c.recent_bars[0], "source", "other")):
            ctx = context();mutation(ctx)
            report = compare_evidence(ctx, AT)
            self.assertEqual(report["status"], "INVALID_INPUT")
            self.assertEqual(report["candidate"]["families"], [])

    def test_bad_actual_types_never_become_finite_scores(self):
        for value in (True, "1", math.nan, math.inf, -1, 0, 10**400):
            ctx = context();ctx.quote.last = value
            with self.subTest(value=type(value).__name__):
                self.assertEqual(compare_evidence(ctx, AT)["status"], "INVALID_INPUT")
        ctx = context();ctx.dq.score = True
        self.assertEqual(compare_evidence(ctx, AT)["status"], "INVALID_INPUT")

    def test_subclasses_rejected_before_custom_conversion(self):
        class Hostile(float):
            def __float__(self):raise AssertionError("must not coerce")
        class FakeQuote(T.Quote):pass
        ctx = context();ctx.quote.last = Hostile(100)
        self.assertEqual(compare_evidence(ctx, AT)["status"], "INVALID_INPUT")
        ctx = context();ctx.quote = FakeQuote("600000.SH", T.Market.A, AT)
        self.assertEqual(compare_evidence(ctx, AT)["status"], "INVALID_INPUT")

    def test_naive_times_explicitly_not_pit(self):
        ctx = context()
        for b in ctx.recent_bars:b.timestamp = b.timestamp.replace(tzinfo=None)
        for name in ("timestamp", "received_at", "computed_at"):
            setattr(ctx.quote, name, getattr(ctx.quote, name).replace(tzinfo=None))
        report = compare_evidence(ctx, AT)
        self.assertEqual(report["sample_info"]["time_basis"], "LEGACY_NAIVE_DATE")
        self.assertIn("NAIVE_TIME_NOT_PIT", report["warnings"])

    def test_snapshot_roundtrip_and_mutation_isolation(self):
        ctx = context();snapshot = EvidenceSnapshot.capture(ctx, AT)
        before = compare_snapshot(snapshot)
        ctx.quote.last = 999;ctx.recent_bars[0].close = 10;ctx.dq.reasons.append("changed")
        self.assertEqual(compare_snapshot(snapshot), before)
        thaw, _ = snapshot.select();thaw.quote.last = 999
        self.assertEqual(compare_snapshot(snapshot), before)
        with self.assertRaises(FrozenInstanceError):snapshot.payload = "{}"
        self.assertEqual(EvidenceSnapshot(snapshot.payload).input_id, snapshot.input_id)

    def test_extra_fields_fake_identity_and_duplicate_json_fail(self):
        snap = EvidenceSnapshot.capture(context(), AT)
        data = json.loads(snap.payload);data["verified"] = True
        with self.assertRaises(EvidenceInputError):EvidenceSnapshot(canonical(data))
        with self.assertRaises(EvidenceInputError):EvidenceSnapshot('{"schema":"x","schema":"y"}')
        data = json.loads(snap.payload);data["quote"]["last"] = True
        with self.assertRaises(EvidenceInputError):EvidenceSnapshot(canonical(data))

    def test_source_input_mutation_changes_identity(self):
        ctx = context();left = EvidenceSnapshot.capture(ctx, AT).input_id
        ctx.quote.amount += 1
        self.assertNotEqual(EvidenceSnapshot.capture(ctx, AT).input_id, left)

    def test_unrelated_account_and_configuration_never_read(self):
        class Secret:
            def __deepcopy__(self, memo):raise AssertionError("private state read")
        ctx = context();ctx.position = Secret();ctx.cfg = Secret();ctx.watch = Secret()
        self.assertEqual(compare_evidence(ctx, AT)["status"], "COMPARABLE_NUMERIC")

    def test_candidate_calculates_when_legacy_cannot_accept_missing_quote(self):
        ctx = context();ctx.quote.open = None
        report = compare_evidence(ctx, AT)
        self.assertEqual(report["legacy"]["status"], "UNAVAILABLE")
        self.assertIsNotNone(row(report, "families", "momentum")["value"])

    def test_random_normal_full_context_matches_legacy(self):
        rng = random.Random(614)
        for _ in range(25):
            ctx = context()
            for b in ctx.recent_bars:
                v = rng.uniform(90, 110);b.open=b.close=v;b.high=v+2;b.low=v-2
            report = compare_evidence(ctx, AT)
            original = scoring.score(ctx)
            for key in ("opportunity", "timing", "risk", "confidence"):
                self.assertEqual(row(report, "scores", key)["value"], getattr(original, key))

    def test_calls_live_legacy_but_never_patches_or_changes_it(self):
        ctx = context();method = scoring.score
        with patch.object(scoring, "score", wraps=method) as probe:
            report = compare_evidence(ctx, AT)
            probe.assert_called_once()
        self.assertIs(scoring.score, method)
        self.assertIs(report["affects_live_scores"], False)


if __name__ == "__main__":
    unittest.main()

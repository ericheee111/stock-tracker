"""Run fixed synthetic N4 comparisons; stdout only, no database/network/config I/O."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_tracker.core import types as T
from stock_tracker.features.evidence_comparison import (
    CANDIDATE_POLICY,
    compare_evidence,
)

AT = datetime(2026, 9, 14, 6, tzinfo=timezone.utc)
CASES = ("complete", "rsi-zero", "ma60-short", "rsi-short", "macd-short", "dq-missing",
         "dq-degraded", "contexts-missing", "no-bars", "quote-missing", "zero-turnover", "invalid-identity")


def fixture_context(case: str) -> T.ScanContext:
    """A deliberately synthetic numerical sequence, including non-trading dates."""
    if case not in CASES:
        raise ValueError("unknown fixed scenario")
    n = {"ma60-short": 20, "rsi-short": 14, "macd-short": 33, "no-bars": 0}.get(case, 80)
    values = [100.0-i*.01 if case == "rsi-zero" else 100+i*.05+(i % 4)*.02 for i in range(n)]
    bars = [T.Bar("600000.SH", T.Market.A, AT-timedelta(days=n-i), open=value, high=value+1,
                  low=value-1, close=value, volume=1000, amount=100000, source="synthetic-fixture")
            for i, value in enumerate(values)]
    last = values[-1] if values else 100.0
    q = T.Quote("600000.SH", T.Market.A, AT-timedelta(seconds=2), open=last, high=last+1, low=last-1,
                last=last, prev_close=last, amount=100000, turnover=2,
                received_at=AT-timedelta(seconds=1), computed_at=AT, source="synthetic-fixture", data_status=T.DataStatus.LIVE)
    ctx = T.ScanContext(symbol=q.symbol, market=q.market, quote=q, recent_bars=bars,
                        dq=T.DataQuality(T.QualityStatus.VALID, 80, []),
                        regime=T.MarketRegime(T.RegimeState.ROTATION, 60),
                        sector=T.SectorSnapshot(sector="synthetic-sector", score=60, relative_strength=60,
                                                persistence=50, crowding=10, catalyst="fixture"))
    if case == "dq-missing":
        ctx.dq = None
    elif case == "dq-degraded":
        ctx.dq = T.DataQuality(T.QualityStatus.DEGRADED, 50, ["fixture"])
    elif case == "contexts-missing":
        ctx.regime = None;ctx.sector = None
    elif case == "quote-missing":
        q.open = None
    elif case == "zero-turnover":
        q.turnover = 0;q.amount = 0
    elif case == "invalid-identity":
        bars[0].symbol = "AAPL.US"
    return ctx


def verify_case(case: str, report: dict[str, Any]) -> list[bool]:
    expected = "INVALID_INPUT" if case == "invalid-identity" else "COMPARABLE_NUMERIC" if case in ("complete", "rsi-zero", "zero-turnover") else "PARTIAL"
    checks = [report["status"] == expected,
              all(report[k] is False for k in ("auto_trade", "execution_authorized", "affects_live_scores", "investment_performance_claim")),
              report["success_probability"] is None]
    families = {r["key"]: r["value"] for r in report["candidate"]["families"]}
    scores = {r["key"]: r["value"] for r in report["candidate"]["scores"]}
    if case == "complete":
        checks.append(all(d["delta"] == 0 for d in report["differences"]))
    elif case == "rsi-zero":
        checks.append(report["indicators"]["rsi14"] == 0 and "RSI_ZERO_WAS_NEUTRALIZED" in report["findings"])
        checks.append(next(x["delta"] for x in report["differences"] if x["key"] == "momentum") == -25)
    elif case == "ma60-short":
        checks.append(families["trend"] is None)
    elif case in ("rsi-short", "macd-short"):
        checks.append(families["momentum"] is None and report["candidate"]["macd_direction"] == "UNKNOWN")
    elif case.startswith("dq-"):
        checks.append(scores["confidence"] is None)
    elif case == "contexts-missing":
        checks.append(all(v is None for v in scores.values()))
    elif case == "no-bars":
        checks.append(report["sample_info"]["selected_count"] == 0 and families["trend"] is None)
    elif case == "quote-missing":
        checks.append(report["legacy"]["status"] == "UNAVAILABLE")
    elif case == "zero-turnover":
        checks.append(families["volume_liquidity"] == 50)
    elif case == "invalid-identity":
        checks.append(not families and not scores)
    return checks


def build_report(cases: tuple[str, ...] = CASES, *, details: bool = False) -> dict[str, Any]:
    if not cases or len(set(cases)) != len(cases) or any(c not in CASES for c in cases):
        raise ValueError("nonempty unique known scenarios required")
    results = []
    for case in cases:
        report = compare_evidence(fixture_context(case), AT)
        checks = verify_case(case, report)
        row = {"case": case, "passed": all(checks), "checks": checks, "status": report["status"],
               "input_id": report["input_id"], "findings": report["findings"], "differences": report["differences"]}
        if details:
            row["report"] = report
        results.append(row)
    return {"schema": "evidence-comparison-fixture-report-v1", "candidate_policy_id": CANDIDATE_POLICY,
            "expected_cases": list(cases), "executed": len(results), "passed": all(x["passed"] for x in results),
            "synthetic_fixture_only": True, "calendar_sessions_proven": False,
            "real_data_trial": False, "strategy_promoted": False, "auto_trade": False, "cases": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=CASES, action="append", help="Choose fixed synthetic case(s); default all")
    parser.add_argument("--details", action="store_true", help="Include full comparison documents, no account facts")
    args = parser.parse_args()
    try:
        result = build_report(tuple(args.case) if args.case else CASES, details=args.details)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, allow_nan=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

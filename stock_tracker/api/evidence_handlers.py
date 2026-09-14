"""Optional N4 detail enhancement, using already-read local values only."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ..core import types as T
from ..features.evidence_comparison import compare_evidence, unavailable

_LOG = logging.getLogger(__name__)


def evidence_detail(quote: T.Quote | None, bars: list[T.Bar], symbol: str,
                    market: T.Market, computed_at: datetime) -> dict[str, Any]:
    """No provider, account, global regime or guessed sector lookup here.

    Runtime global regime/sector objects lack a verified per-market/member/time
    binding. Keep them missing rather than borrowing a different market context.
    A failure in this optional explanation must not destroy the existing detail.
    """
    if quote is None:
        return unavailable(symbol, market.value, computed_at.isoformat(), "QUOTE_UNAVAILABLE", no_quote=True)
    try:
        context = T.ScanContext(symbol=symbol, market=market, quote=quote, recent_bars=bars,
                                dq=quote.quality, regime=None, sector=None)
        return compare_evidence(context, computed_at)
    except Exception as exc:  # noqa: BLE001 - logged optional API isolation, never a numeric fallback
        # Optional API/process boundary only. Never swallow core errors silently,
        # echo private object repr, or turn failure into plausible numeric values.
        _LOG.error("Evidence detail unavailable (%s)", type(exc).__name__)
        return unavailable(symbol, market.value, computed_at.isoformat(), "DIAGNOSTIC_INTERNAL_ERROR")

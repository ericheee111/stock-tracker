"""Run the Stage 1 Today page against the real Python API and Web assets.

The runner uses a temporary SQLite database and synthetic runtime facts.  It
never opens or modifies ``data/stock_tracker.db`` and it does not call an
external market-data provider.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tracker.api.handlers import AppContext
from stock_tracker.api.server import APIServer
from stock_tracker.api.sse import SSEHub
from stock_tracker.core import types as T
from stock_tracker.core.config import load_configs
from stock_tracker.core.store import MarketStore
from stock_tracker.decision.types import RiskMode, UserPortfolioProfile
from stock_tracker.storage.db import close_all
from stock_tracker.storage.repository import Repository


class _LocalBus:
    def subscribe(self, callback) -> None:
        self.callback = callback


class _RouterStub:
    def health_list(self) -> list:
        return []


class _SignalManagerStub:
    def _portfolio_heat(self) -> float:
        return 0.0


@contextmanager
def _sqlite_lifecycle():
    try:
        yield
    finally:
        close_all()


def _quote(
    symbol: str,
    *,
    name: str,
    last: float,
) -> T.Quote:
    now = datetime.now()
    return T.Quote(
        symbol=symbol,
        market=T.market_from_symbol(symbol),
        timestamp=now,
        name=name,
        open=last,
        high=last * 1.02,
        low=last * 0.98,
        close=last,
        last=last,
        prev_close=last * 0.99,
        volume=1_000_000,
        amount=100_000_000,
        turnover=1.5,
        source="stage1-integration-fixture",
        received_at=now,
        computed_at=now,
        observed_age_ms=100,
        data_status=T.DataStatus.LIVE,
    )


def _signal(
    symbol: str,
    state: T.SignalState,
    *,
    strategy_id: str,
    entry_low: float,
    entry_high: float,
    trigger: float,
    invalidation: float,
    target_1: float,
    target_2: float,
    opportunity: int,
) -> T.Signal:
    return T.Signal(
        signal_id=f"{symbol}:{strategy_id}",
        symbol=symbol,
        market=T.market_from_symbol(symbol),
        strategy_id=strategy_id,
        state=state,
        state_changed_at=datetime.now(),
        reason="Stage 1 real API integration fixture",
        entry_low=entry_low,
        entry_high=entry_high,
        trigger_price=trigger,
        invalidation_price=invalidation,
        target_1=target_1,
        target_2=target_2,
        reward_risk=2.0,
        freshness=0.95,
        next_trigger="等待已冻结的结构条件",
        data_status=T.DataStatus.LIVE,
        scores=T.ScoreSet(
            opportunity=opportunity,
            timing=75,
            risk=35,
            confidence=70,
            positive_reasons=["趋势结构完整", "数据状态可用"],
            negative_reasons=["仍需遵守不追价和失效位"],
        ),
    )


def main() -> int:
    with (
        tempfile.TemporaryDirectory(prefix="stock-tracker-stage1-") as temp_dir,
        _sqlite_lifecycle(),
    ):
        db_path = os.path.join(temp_dir, "stage1-integration.db")
        bundle = load_configs(str(ROOT / "config"))
        store = MarketStore()
        repo = Repository(db_path)

        profile = UserPortfolioProfile(
            account_equity=100_000,
            available_cash=50_000,
            risk_mode=RiskMode.BALANCED,
            per_trade_risk_pct=0.01,
            max_position_pct=0.30,
            max_portfolio_heat_pct=0.10,
            max_sector_pct=0.50,
            max_theme_pct=0.50,
            updated_at=datetime.now(timezone.utc),
        )
        repo.save_portfolio_profile(profile)
        store.set_portfolio_profile(profile)

        core_quote = _quote("600000.SH", name="浦发银行", last=10.5)
        holding_quote = _quote("000001.SZ", name="平安银行", last=9.65)
        store.update_quote(core_quote)
        store.update_quote(holding_quote)
        store.upsert_instrument("600000.SH", {"sector": "金融"})
        store.upsert_instrument("000001.SZ", {"sector": "金融"})
        store.set_regime(
            T.MarketRegime(
                regime=T.RegimeState.ROTATION,
                market_score=55.0,
                sub_factors={},
            )
        )
        store.update_sector(
            T.SectorSnapshot(
                sector="金融",
                score=62.0,
                stage=T.SectorStage.LEADING,
                relative_strength=58.0,
                breadth=60.0,
                volume=55.0,
                leader_quality=60.0,
                catalyst="",
                persistence=50.0,
                crowding=20.0,
            )
        )

        core_signal = _signal(
            "600000.SH",
            T.SignalState.TRIGGERED,
            strategy_id="S1",
            entry_low=10.0,
            entry_high=10.4,
            trigger=10.5,
            invalidation=9.5,
            target_1=11.5,
            target_2=12.2,
            opportunity=84,
        )
        holding_signal = _signal(
            "000001.SZ",
            T.SignalState.ACTIVE,
            strategy_id="S2",
            entry_low=9.8,
            entry_high=10.0,
            trigger=10.1,
            invalidation=9.5,
            target_1=11.0,
            target_2=11.8,
            opportunity=72,
        )
        store.upsert_signal(core_signal)
        store.upsert_signal(holding_signal)

        position = repo.create_position(
            symbol="000001.SZ",
            market=T.Market.A,
            shares=37,
            average_cost=9.8,
            added_at=datetime.now(timezone.utc),
        )
        store.set_positions([position])

        sse_hub = SSEHub(_LocalBus())
        ctx = AppContext(
            bundle=bundle,
            store=store,
            repo=repo,
            router=_RouterStub(),
            signal_manager=_SignalManagerStub(),
            sse_hub=sse_hub,
            web_root=str(ROOT / "web"),
        )
        server = APIServer("127.0.0.1", 0, ctx, None)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            env = os.environ.copy()
            env["TODAY_QA_BASE_URL"] = f"http://127.0.0.1:{port}"
            result = subprocess.run(
                ["node", "qa/ui/today_action_qa.cjs"],
                cwd=ROOT,
                env=env,
                check=False,
            )
            return result.returncode
        finally:
            server.shutdown_wait()
            thread.join(timeout=5)
            close_all()


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timezone
from types import SimpleNamespace

from stock_tracker.api.handlers import AppContext
from stock_tracker.api.server import APIServer
from stock_tracker.core import types as T
from stock_tracker.core.config import load_configs
from stock_tracker.core.store import MarketStore
from stock_tracker.decision.types import RiskMode, UserPortfolioProfile
from stock_tracker.storage.db import close_all
from stock_tracker.storage.repository import Repository


class ExplodingRouter:
    def __getattr__(self, name):
        raise AssertionError(f"Today API accessed Provider/Router attribute: {name}")


class TestTodayBriefAPI(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MarketStore()
        self.repo = Repository(os.path.join(self.tmp.name, "today.db"))
        self.bundle = load_configs("config")
        self.ctx = AppContext(
            bundle=self.bundle,
            store=self.store,
            repo=self.repo,
            router=ExplodingRouter(),
            signal_manager=None,
            sse_hub=SimpleNamespace(),
            web_root=self.tmp.name,
        )
        self.store.set_regime(
            T.MarketRegime(
                regime=T.RegimeState.ROTATION,
                market_score=55,
                sub_factors={},
            )
        )
        self.server = APIServer("127.0.0.1", 0, self.ctx, None)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown_wait()
        self.thread.join(timeout=5)
        close_all()
        self.tmp.cleanup()

    def get_brief(self) -> dict:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/api/brief/today",
            timeout=5,
        ) as response:
            self.assertEqual(response.status, 200)
            return json.loads(response.read().decode("utf-8"))

    def save_profile(self) -> UserPortfolioProfile:
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
        self.repo.save_portfolio_profile(profile)
        return profile

    def quote(
        self,
        symbol: str,
        *,
        status: T.DataStatus = T.DataStatus.LIVE,
        last: float = 10.5,
        name: str = "测试股票",
    ) -> T.Quote:
        market = T.market_from_symbol(symbol)
        quote = T.Quote(
            symbol=symbol,
            market=market,
            timestamp=datetime.now(),
            name=name,
            open=10.0,
            high=max(10.8, last),
            low=min(9.9, last),
            close=last,
            last=last,
            prev_close=10.0,
            volume=1_000_000,
            received_at=datetime.now(),
            computed_at=datetime.now(),
            observed_age_ms=100,
            data_status=status,
        )
        self.store.update_quote(quote)
        self.store.upsert_instrument(symbol, {"sector": "金融"})
        return quote

    def signal(
        self,
        symbol: str,
        state: T.SignalState,
        *,
        status: T.DataStatus = T.DataStatus.LIVE,
        strategy_id: str = "S1",
        opportunity: int = 82,
    ) -> T.Signal:
        signal = T.Signal(
            signal_id=f"{symbol}:{strategy_id}",
            symbol=symbol,
            market=T.market_from_symbol(symbol),
            strategy_id=strategy_id,
            state=state,
            state_changed_at=datetime.now(),
            entry_low=10.0,
            entry_high=10.4,
            trigger_price=10.5,
            invalidation_price=9.5,
            target_1=11.5,
            target_2=12.2,
            reward_risk=2.1,
            freshness=0.9,
            next_trigger="等待结构确认",
            data_status=status,
            scores=T.ScoreSet(
                opportunity=opportunity,
                timing=75,
                risk=35,
                confidence=70,
                positive_reasons=["趋势结构完整"],
                negative_reasons=["临近前高"],
            ),
        )
        self.store.upsert_signal(signal)
        return signal

    def test_live_trigger_returns_real_stage1_contract_without_provider_access(self) -> None:
        self.save_profile()
        self.quote("600000.SH", status=T.DataStatus.LIVE)
        self.signal("600000.SH", T.SignalState.TRIGGERED)

        brief = self.get_brief()

        self.assertEqual(brief["schema_version"], "stage1-v1")
        self.assertEqual(brief["ranking_mode"], "RULE_EVIDENCE")
        self.assertEqual(brief["actions"]["executable_count"], 1)
        self.assertEqual(len(brief["core_opportunities"]), 1)
        item = brief["core_opportunities"][0]
        self.assertEqual(item["action_state"], "EXECUTABLE")
        self.assertIsNone(item["model"]["calibrated_probability"])
        self.assertEqual(
            item["model"]["probability_evidence_level"],
            "INSUFFICIENT",
        )
        self.assertGreater(item["trade_plan"]["suggested_shares"], 0)
        self.assertEqual(item["trade_plan"]["suggested_shares"] % 100, 0)
        self.assertEqual(brief["big_trend"]["status"], "NOT_AVAILABLE")
        self.assertEqual(
            brief["strategy_evidence"]["status"],
            "INSUFFICIENT_REAL_EVIDENCE",
        )

    def test_delayed_trigger_is_not_executable(self) -> None:
        self.quote("600000.SH", status=T.DataStatus.DELAYED)
        self.signal(
            "600000.SH",
            T.SignalState.TRIGGERED,
            status=T.DataStatus.DELAYED,
        )

        brief = self.get_brief()

        self.assertEqual(brief["actions"]["executable_count"], 0)
        self.assertEqual(
            brief["core_opportunities"][0]["action_state"],
            "DATA_BLOCKED",
        )
        self.assertIsNone(brief["core_opportunities"][0]["trade_plan"])

    def test_holding_is_separate_and_odd_lot_is_preserved(self) -> None:
        self.save_profile()
        self.quote("600000.SH", last=9.65)
        self.signal("600000.SH", T.SignalState.ACTIVE)
        position = self.repo.create_position(
            symbol="600000.SH",
            market=T.Market.A,
            shares=37,
            average_cost=9.8,
            added_at=datetime.now(timezone.utc),
        )

        brief = self.get_brief()

        self.assertEqual(brief["core_opportunities"], [])
        self.assertEqual(len(brief["holding_actions"]), 1)
        holding = brief["holding_actions"][0]
        self.assertEqual(holding["position_id"], position.id)
        self.assertEqual(holding["shares"], 37)
        self.assertEqual(holding["action_state"], "WARNING")
        self.assertGreater(holding["distance_to_invalidation_pct"], 0)

    def test_unbound_position_blocks_new_sizing_and_is_visible(self) -> None:
        self.save_profile()
        self.quote("600000.SH")
        self.repo.create_position(
            symbol="600000.SH",
            market=T.Market.A,
            shares=37,
            average_cost=9.8,
            added_at=datetime.now(timezone.utc),
        )
        self.quote("000001.SZ")
        self.signal("000001.SZ", T.SignalState.TRIGGERED)

        brief = self.get_brief()

        self.assertEqual(brief["holding_actions"][0]["action_state"], "DATA_BLOCKED")
        opportunity = brief["core_opportunities"][0]
        self.assertEqual(opportunity["action_state"], "AVOID")
        self.assertTrue(opportunity["hard_blockers"])
        self.assertIsNone(opportunity["trade_plan"]["suggested_shares"])

    def test_no_profile_does_not_invent_shares(self) -> None:
        self.quote("600000.SH")
        self.signal("600000.SH", T.SignalState.TRIGGERED)

        brief = self.get_brief()

        plan = brief["core_opportunities"][0]["trade_plan"]
        self.assertIsNone(plan["suggested_shares"])
        self.assertIsNone(plan["suggested_position_pct"])
        self.assertIn("尚未设置账户", " ".join(brief["summary"]["facts"]))

    def test_core_is_capped_at_five_and_deduplicated_by_symbol(self) -> None:
        for index in range(7):
            symbol = f"X{index}.US"
            self.quote(symbol)
            self.signal(
                symbol,
                T.SignalState.WATCH,
                opportunity=90 - index,
            )
        self.signal(
            "X0.US",
            T.SignalState.WATCH,
            strategy_id="S2",
            opportunity=99,
        )

        brief = self.get_brief()

        symbols = [item["symbol"] for item in brief["core_opportunities"]]
        self.assertLessEqual(len(symbols), 5)
        self.assertEqual(len(symbols), len(set(symbols)))

    def test_invalid_signal_contract_is_skipped_not_promoted(self) -> None:
        self.quote("600000.SH")
        signal = self.signal("600000.SH", T.SignalState.TRIGGERED)
        signal.invalidation_price = 10.2

        brief = self.get_brief()

        self.assertEqual(brief["core_opportunities"], [])
        self.assertEqual(brief["actions"]["executable_count"], 0)
        self.assertTrue(
            any("因合同不完整被跳过" in fact for fact in brief["summary"]["facts"])
        )


if __name__ == "__main__":
    unittest.main()

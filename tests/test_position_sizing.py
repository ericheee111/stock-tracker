from __future__ import annotations

import math
import unittest

from stock_tracker.core.types import Market
from stock_tracker.decision.position_sizing import PositionSizer, size_position
from stock_tracker.decision.types import (
    BlockerSeverity,
    DecisionBlocker,
    DecisionContractError,
    UserPortfolioProfile,
)


class TestPositionSizer(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = UserPortfolioProfile(
            account_equity=100_000,
            available_cash=100_000,
            per_trade_risk_pct=0.01,
            max_position_pct=1.0,
            max_portfolio_heat_pct=0.10,
            max_sector_pct=1.0,
            max_theme_pct=1.0,
        )

    def size(self, **overrides: object):
        values = {
            "market": Market.A,
            "entry_price": 10.0,
            "invalidation_price": 9.0,
        }
        values.update(overrides)
        return size_position(self.profile, **values)

    def test_a_share_defaults_to_100_share_lots(self) -> None:
        result = self.size(entry_price=10.0, invalidation_price=9.4)
        self.assertEqual(result.lot_size, 100)
        self.assertEqual(result.shares, 1600)

    def test_hk_requires_explicit_lot_size(self) -> None:
        with self.assertRaisesRegex(DecisionContractError, "required for HK"):
            self.size(market=Market.HK)

    def test_us_defaults_to_one_share(self) -> None:
        result = self.size(market=Market.US, entry_price=333, invalidation_price=300)
        self.assertEqual(result.lot_size, 1)
        self.assertEqual(result.shares, 30)

    def test_cash_can_be_the_limiting_factor(self) -> None:
        profile = UserPortfolioProfile(
            account_equity=100_000,
            available_cash=2_550,
            per_trade_risk_pct=0.10,
            max_position_pct=1.0,
            max_portfolio_heat_pct=0.20,
            max_sector_pct=1.0,
            max_theme_pct=1.0,
        )
        result = size_position(
            profile, market=Market.A, entry_price=10, invalidation_price=9
        )
        self.assertEqual(result.shares, 200)
        self.assertIn("AVAILABLE_CASH", result.limiting_factors)

    def test_position_limit_can_be_the_limiting_factor(self) -> None:
        profile = UserPortfolioProfile(
            account_equity=100_000,
            available_cash=100_000,
            per_trade_risk_pct=0.10,
            max_position_pct=0.05,
            max_portfolio_heat_pct=0.20,
            max_sector_pct=1.0,
            max_theme_pct=1.0,
        )
        result = size_position(
            profile, market=Market.A, entry_price=10, invalidation_price=9
        )
        self.assertEqual(result.shares, 500)
        self.assertIn("MAX_POSITION", result.limiting_factors)

    def test_heat_limit_can_be_the_limiting_factor(self) -> None:
        result = self.size(current_portfolio_heat_pct=0.095)
        self.assertEqual(result.shares, 500)
        self.assertIn("PORTFOLIO_HEAT", result.limiting_factors)

    def test_sector_and_theme_limits_apply(self) -> None:
        sector_result = self.size(current_sector_exposure_pct=0.95)
        theme_result = self.size(current_theme_exposure_pct=0.97)
        self.assertEqual(sector_result.shares, 500)
        self.assertIn("SECTOR_EXPOSURE", sector_result.limiting_factors)
        self.assertEqual(theme_result.shares, 300)
        self.assertIn("THEME_EXPOSURE", theme_result.limiting_factors)

    def test_hard_blocker_forces_zero_shares(self) -> None:
        blocker = DecisionBlocker("HALT", "Security is halted", BlockerSeverity.HARD)
        result = self.size(hard_blockers=(blocker,))
        self.assertFalse(result.allowed)
        self.assertEqual(result.shares, 0)
        self.assertEqual(result.blockers, (blocker,))

    def test_less_than_one_lot_is_blocked(self) -> None:
        result = self.size(liquidity_max_shares=99)
        self.assertFalse(result.allowed)
        self.assertEqual(result.shares, 0)
        self.assertEqual(result.blockers[0].code, "BELOW_MINIMUM_LOT")

    def test_equal_entry_and_invalidation_is_rejected(self) -> None:
        with self.assertRaisesRegex(DecisionContractError, "greater than"):
            self.size(entry_price=9, invalidation_price=9)

    def test_bool_nonfinite_negative_and_bad_percentages_are_rejected(self) -> None:
        invalid_cases = (
            {"entry_price": True},
            {"entry_price": math.nan},
            {"entry_price": math.inf},
            {"entry_price": -1},
            {"current_portfolio_heat_pct": -0.1},
            {"current_sector_exposure_pct": 1.1},
            {"liquidity_max_shares": True},
        )
        for kwargs in invalid_cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(DecisionContractError):
                    self.size(**kwargs)

    def test_facade_uses_bound_profile(self) -> None:
        result = PositionSizer(self.profile).size(
            market=Market.US, entry_price=100, invalidation_price=90
        )
        self.assertEqual(result.shares, 100)


if __name__ == "__main__":
    unittest.main()

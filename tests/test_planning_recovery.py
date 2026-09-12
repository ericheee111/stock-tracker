"""Regression probes for legacy Portfolio compatibility and recovery isolation."""
from __future__ import annotations

import copy
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from stock_tracker.core.types import Market
from tests import test_planning_api as api_fixture
from tests.test_portfolio_planning import NOW, PARENT, allocation, envelope


class TestPlanningRecovery(unittest.TestCase):
    def setUp(self):
        self.api = api_fixture.TestPlanningAPI(methodName="test_get_no_write_no_provider_and_unclassified")
        self.api.setUp()
        self.addCleanup(self.api.doCleanups)

    def test_existing_seven_decimal_cost_does_not_block_book_or_cancel(self):
        plan = self.api.ready()
        self.assertEqual(self.api.command("RESERVE", plan)[0], 200)
        self.api.repo.create_position(symbol="000001.SZ", market=Market.A, shares=100,
                                      average_cost=10.1234567, added_at=datetime.now(timezone.utc))
        status, book = self.api.request("GET", "/api/planning/book")
        self.assertEqual(status, 200)
        self.assertEqual(next(p for p in book["positions"] if p["symbol"] == "000001.SZ")["cost"], "10.1234567")
        self.assertEqual(self.api.command("CANCEL", {"plan_id": "plan1", "reason": "synthetic unexecuted",
                                                    "no_execution_confirmed": True, "no_open_orders_confirmed": True})[0], 200)

    def test_legacy_cost_precision_preserved_in_parent_fingerprint(self):
        self.api.repo.update_position(self.api.position.id, average_cost=10.1234567890123)
        plan = self.api.ready()
        status, result = self.api.command("RESERVE", plan)
        self.assertEqual(status, 200)
        self.assertEqual(result["book"]["plans"]["plan1"]["parent"]["cost"], "10.1234567890123")

    def test_unrelated_unsupported_row_visible_without_blocking_reconciliation(self):
        plan = self.api.ready()
        self.api.command("RESERVE", plan)
        other = copy.copy(self.api.position)
        other.id, other.symbol, other.shares = "legacy-fractional", "000001.SZ", 10.5
        with patch.object(self.api.repo, "load_positions", return_value=[self.api.position, other]):
            code, result = self.api.request("GET", "/api/planning/book")
            self.assertEqual(code, 200)
            self.assertEqual(len(result["positions"]), 1)
            self.assertEqual(result["position_issues"][0]["code"], "UNSUPPORTED_POSITION")
            code, result = self.api.command("RECONCILE", {"currency": "CNY", "plan_ids": ["plan1"],
                        "no_open_orders_confirmed": True, "accounting_checked": True, "reason": "fixture accounting"})
            self.assertEqual(code, 200)
            self.assertEqual(result["book"]["plans"]["plan1"]["status"], "CLOSED_MANUAL_RECONCILIATION")

    def test_duplicate_symbol_does_not_leave_first_row_usable(self):
        other = copy.copy(self.api.position)
        other.id = "duplicate-row"
        with patch.object(self.api.repo, "load_positions", return_value=[self.api.position, other]):
            code, result = self.api.request("GET", "/api/planning/book")
            self.assertEqual(code, 200)
            self.assertEqual(result["positions"], [])
            self.assertEqual(result["position_issues"][0]["code"], "DUPLICATE_POSITION")

    def test_store_idempotency_does_not_read_parent_source_again(self):
        store = self.api.ctx.planning_store
        cmd = envelope(store.read(), "SET_ALLOCATION", allocation())
        store.apply(cmd, {"p1": PARENT}, NOW)
        def unavailable():
            raise AssertionError("source must not be consulted for an exact retry")
        self.assertTrue(store.apply(cmd, unavailable, NOW)["idempotent"])

    def test_close_out_does_not_read_parent_source(self):
        plan = self.api.ready()
        self.api.command("RESERVE", plan)
        store = self.api.ctx.planning_store
        cmd = envelope(store.read(), "CANCEL", {"plan_id": "plan1", "reason": "not executed",
                           "no_execution_confirmed": True, "no_open_orders_confirmed": True})
        def unavailable():
            raise AssertionError("source must not be consulted for a cancellation")
        result = store.apply(cmd, unavailable, datetime.now(timezone.utc))
        self.assertEqual(result["book"]["plans"]["plan1"]["status"], "CANCELLED")


if __name__ == "__main__":
    unittest.main()

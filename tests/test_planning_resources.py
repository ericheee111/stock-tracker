"""Read-only manual resource/reminder tests; no model or trading claims."""
from __future__ import annotations

import copy
import unittest
from datetime import timedelta
from decimal import localcontext

from stock_tracker.portfolio_planning.domain import empty_book, reduce_command
from stock_tracker.portfolio_planning.resources import resource_summary
from tests.test_portfolio_planning import (
    NOW,
    PARENT,
    allocation,
    cash,
    envelope,
    inventory,
    ready,
    reserve,
)


class TestPlanningResources(unittest.TestCase):
    def setUp(self):
        self.book = ready()
        self.parents = {"p1": copy.deepcopy(PARENT)}

    def apply(self, kind, data):
        self.book = reduce_command(self.book, envelope(self.book, kind, data), self.parents, NOW)

    def summary(self, now=NOW):
        return resource_summary(self.book, self.parents, now)

    def test_empty_inputs_are_unknown_not_zero_capacity(self):
        result = resource_summary(empty_book(), self.parents, NOW)
        self.assertTrue(all(c["remaining_cash"] is None for c in result["currency_pools"]))
        self.assertEqual(result["positions"][0]["unclassified_quantity"], 1000)
        self.assertIsNone(result["positions"][0]["remaining_old_quantity"])
        self.assertFalse(result["execution_authorized"])

    def test_reserved_resources_are_subtracted_exactly_once(self):
        self.apply("RESERVE", reserve())
        result = self.summary()
        pool = next(p for p in result["currency_pools"] if p["currency"] == "CNY")
        self.assertEqual((pool["reserved_cash"], pool["remaining_cash"]), ("2010", "7990"))
        self.assertEqual(result["positions"][0]["remaining_old_quantity"], 700)
        self.assertEqual(result["positions"][0]["remaining_tactical_quantity"], 400)
        self.assertEqual(result["positions"][0]["core_quantity"], 400)

    def test_expired_confirmation_nulls_capacity_but_reservation_persists(self):
        self.apply("RESERVE", reserve())
        result = self.summary(NOW + timedelta(minutes=11))
        pool = next(p for p in result["currency_pools"] if p["currency"] == "CNY")
        self.assertEqual(pool["reserved_cash"], "2010")
        self.assertIsNone(pool["remaining_cash"])
        self.assertIsNone(result["positions"][0]["remaining_old_quantity"])
        self.assertEqual(result["positions"][0]["reserved_old_quantity"], 200)

    def test_two_symbols_share_one_currency_pool(self):
        parent = {**PARENT, "position_id": "p2", "symbol": "000001.SZ"}
        self.parents["p2"] = parent
        self.apply("SET_ALLOCATION", allocation(parent))
        self.apply("CONFIRM_INVENTORY", inventory(parent))
        self.apply("RESERVE", reserve())
        self.apply("RESERVE", reserve(parent))
        result = self.summary()
        pool = next(p for p in result["currency_pools"] if p["currency"] == "CNY")
        self.assertEqual(pool["reserved_cash"], "4020")
        self.assertEqual(pool["remaining_cash"], "5980")
        self.assertEqual(len(result["positions"]), 2)

    def test_currency_cash_cannot_be_merged(self):
        self.apply("CONFIRM_CASH", cash(currency="USD", available_cash="200"))
        self.apply("RESERVE", reserve())
        result = {r["currency"]: r for r in self.summary()["currency_pools"]}
        self.assertEqual(result["USD"]["remaining_cash"], "200")
        self.assertIsNone(result["HKD"]["remaining_cash"])
        self.assertNotIn("total_cash", self.summary())

    def test_started_plan_blocks_remaining_until_reconciliation(self):
        self.apply("RESERVE", reserve(plan_id="started"))
        self.apply("MARK_EXECUTED", {"plan_id": "started", "reason": "manual started"})
        result = self.summary()
        self.assertIsNone(result["currency_pools"][0]["remaining_cash"])
        self.assertIsNone(result["positions"][0]["remaining_old_quantity"])
        self.assertEqual(result["positions"][0]["state"], "RECONCILIATION_REQUIRED")

    def test_changed_parent_does_not_reuse_old_allocation(self):
        self.parents["p1"]["shares"] = 900
        p = self.summary()["positions"][0]
        self.assertIsNone(p["allocated_quantity"])
        self.assertIsNone(p["unclassified_quantity"])
        self.assertIsNone(p["remaining_old_quantity"])

    def test_recreated_position_retains_same_symbol_reservations(self):
        self.apply("RESERVE", reserve())
        new = {**PARENT, "position_id": "new-position"}
        self.parents = {"new-position": new}
        p = self.summary()["positions"][0]
        self.assertEqual(p["reserved_old_quantity"], 200)
        self.assertIsNone(p["remaining_old_quantity"])
        self.assertIsNone(self.summary()["currency_pools"][0]["remaining_cash"])

    def test_review_due_boundary_is_reminder_not_exit(self):
        a = allocation(); a["sleeves"][0]["review_at"] = NOW.isoformat()
        self.apply("SET_ALLOCATION", a)
        reminder = self.summary()["review_items"][0]
        self.assertTrue(reminder["due"])
        self.assertEqual(reminder["action"], "REVIEW_ONLY")
        self.assertFalse(self.summary(NOW - timedelta(seconds=1))["review_items"][0]["due"])

    def test_source_changes_do_not_create_a_stale_reminder(self):
        self.parents["p1"]["cost"] = "12"
        self.assertEqual(self.summary()["review_items"], [])

    def test_read_does_not_mutate_book(self):
        before = copy.deepcopy(self.book)
        self.summary(NOW + timedelta(days=300))
        self.assertEqual(self.book, before)

    def test_global_decimal_precision_does_not_change_resources(self):
        self.apply("RESERVE", reserve(buy_limit="10.123456", fee_buffer="1.234567"))
        expected = self.summary()
        with localcontext() as context:
            context.prec = 2
            self.assertEqual(self.summary(), expected)

    def test_cancel_frees_internal_reservation_only(self):
        self.apply("RESERVE", reserve(plan_id="cancelled"))
        self.apply("CANCEL", {"plan_id": "cancelled", "reason": "not executed",
                              "no_execution_confirmed": True, "no_open_orders_confirmed": True})
        self.assertEqual(self.summary()["currency_pools"][0]["reserved_cash"], "0")
        self.assertEqual(self.summary()["positions"][0]["recorded_quantity"], 1000)

    def test_reconciliation_invalidates_both_resource_inputs(self):
        self.apply("RESERVE", reserve(plan_id="done"))
        self.apply("RECONCILE", {"currency": "CNY", "plan_ids": ["done"], "reason": "checked",
                                 "no_open_orders_confirmed": True, "accounting_checked": True})
        result = self.summary()
        self.assertIsNone(result["currency_pools"][0]["remaining_cash"])
        self.assertIsNone(result["positions"][0]["remaining_old_quantity"])


if __name__ == "__main__":
    unittest.main()

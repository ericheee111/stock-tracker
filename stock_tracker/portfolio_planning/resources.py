"""Read model from an audited manual planning book, not broker balances or orders."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, localcontext
from typing import Any

from .domain import CURRENCIES, MONEY_CONTEXT, active_plans, clock, money, when


def resource_summary(book: dict[str, Any], parents: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Derive remaining planning capacity without modifying inputs or releasing plans.

    The caller must use a replayed PlanningStore book and one Portfolio read.
    Counts describe internal bookkeeping. No cross-database atomicity or broker
    availability is asserted. Invalid/stale prerequisites produce null capacity.
    """
    now = clock(now)
    active = active_plans(book)

    def fresh(item: dict[str, Any] | None) -> bool:
        return item is not None and when(item["observed_at"], "observed_at") <= now < when(item["expires_at"], "expires_at")

    pools: list[dict[str, Any]] = []
    pool_state: dict[str, str] = {}
    with localcontext(MONEY_CONTEXT):
        for currency in CURRENCIES.values():
            plans = [p for p in active if p["currency"] == currency]
            reserved = sum((Decimal(p["reserved_cash"]) for p in plans), Decimal(0))
            cash = book["cash"].get(currency)
            needs_reconcile = any(p["status"] == "RECONCILIATION_REQUIRED" or
                                  parents.get(p["position_id"]) != p["parent"] for p in plans)
            state = "RECONCILIATION_REQUIRED" if needs_reconcile else (
                "UNCONFIRMED" if cash is None else "STALE" if not fresh(cash) else "MANUAL_CONFIRMED")
            remaining = Decimal(cash["available_cash"]) - reserved if state == "MANUAL_CONFIRMED" and cash is not None else None
            if remaining is not None and remaining < 0:
                state, remaining = "INCONSISTENT", None
            pool_state[currency] = state
            pools.append({"currency": currency, "state": state,
                          "confirmed_cash": cash["available_cash"] if cash else None,
                          "reserved_cash": money(reserved), "remaining_cash": money(remaining) if remaining is not None else None,
                          "active_plan_count": len(plans),
                          "expires_at": cash["expires_at"] if cash else None})

    positions: list[dict[str, Any]] = []
    reminders: list[dict[str, Any]] = []
    for pid, parent in sorted(parents.items()):
        allocation, inventory = book["allocations"].get(pid), book["inventory"].get(pid)
        allocation_matches = allocation is not None and allocation["parent"] == parent
        inventory_matches = inventory is not None and inventory["parent"] == parent
        plans = [p for p in active if p["parent"]["symbol"] == parent["symbol"] and p["parent"]["market"] == parent["market"]]
        reserved = sum(p["quantity"] for p in plans)
        currency = CURRENCIES[parent["market"]]
        old_plan = any(p["parent"] != parent for p in plans)
        state = "RECONCILIATION_REQUIRED" if old_plan or (allocation is not None and not allocation_matches) or (
            inventory is not None and not inventory_matches) or pool_state[currency] == "RECONCILIATION_REQUIRED" else (
            "UNCLASSIFIED" if allocation is None else "UNCONFIRMED" if inventory is None else
            "STALE" if not fresh(inventory) else "MANUAL_CONFIRMED")
        sleeves = allocation["sleeves"] if allocation_matches and allocation is not None else []
        allocated = sum(s["quantity"] for s in sleeves) if allocation_matches else 0 if allocation is None else None
        core = sum(s["core_quantity"] for s in sleeves) if allocation_matches else 0 if allocation is None else None
        unclassified = parent["shares"] - allocated if allocated is not None else None
        remainder = inventory["sellable_gross"] - inventory["external_reserved_sell"] - reserved if state == "MANUAL_CONFIRMED" and inventory is not None else None
        tactical = allocated - core - reserved if state == "MANUAL_CONFIRMED" and allocated is not None and core is not None else None
        if (remainder is not None and remainder < 0) or (tactical is not None and tactical < 0):
            state, remainder, tactical = "INCONSISTENT", None, None
        positions.append({"position_id": pid, "symbol": parent["symbol"], "market": parent["market"],
                          "currency": currency, "state": state, "recorded_quantity": parent["shares"],
                          "allocated_quantity": allocated, "core_quantity": core, "unclassified_quantity": unclassified,
                          "reserved_old_quantity": reserved, "remaining_old_quantity": remainder,
                          "remaining_tactical_quantity": tactical,
                          "expires_at": inventory["expires_at"] if inventory else None})
        for sleeve in sleeves:
            if sleeve["review_at"] is not None:
                date = when(sleeve["review_at"], "review_at")
                reminders.append({"position_id": pid, "symbol": parent["symbol"], "purpose": sleeve["purpose"],
                                  "review_at": date.isoformat(), "due": date <= now,
                                  "action": "REVIEW_ONLY"})
    reminders.sort(key=lambda r: (r["review_at"], r["position_id"], r["purpose"]))
    return {"schema": "manual-planning-resources-v1", "revision": book["revision"], "as_of": now.isoformat(),
            "currency_pools": pools, "positions": positions, "review_items": reminders,
            "assurance": "MANUAL_UNVERIFIED", "execution_authorized": False, "auto_trade": False}

"""Manual multi-horizon planning, NOT orders, broker inventory or performance.

All commands are replayable pure reductions. Currency cash and old-share capacity
are shared across every sleeve and plan. No expired reservation is auto-released.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, localcontext
from typing import Any

SCHEMA = "manual-plan-book-v1"
POLICY = "manual-planning-conservative-v1"
PURPOSES = ("SWING", "LONG_TERM", "SHORT_TERM")
CURRENCIES = {"A": "CNY", "HK": "HKD", "US": "USD"}
ACTIVE = ("RESERVED", "RECONCILIATION_REQUIRED")
MONEY_CONTEXT = Context(prec=50)
MAX_COMMANDS = 5000
MAX_POSITIONS = 128
MAX_PLANS = 1000


class PlanningError(ValueError):
    def __init__(self, code: str, message: str, *, status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


def require(condition: bool, code: str, message: str, status: int = 400) -> None:
    if not condition:
        raise PlanningError(code, message, status=status)


def fields(value: Any, names: set[str]) -> dict[str, Any]:
    require(type(value) is dict and set(value) == names, "INVALID_FIELDS", "字段缺失或含不支持字段")
    return value


def text(value: Any, name: str, maximum: int = 1000) -> str:
    require(type(value) is str and 0 < len(value) <= maximum and value == value.strip()
            and not any(ord(c) < 32 or ord(c) == 127 for c in value),
            "INVALID_TEXT", f"{name} 必须是有界非空文本")
    return value


def identifier(value: Any, name: str) -> str:
    text(value, name, 120)
    require(re.fullmatch(r"[A-Za-z0-9_.:-]+", value) is not None, "INVALID_ID", f"{name} 不合法")
    return value


def integer(value: Any, name: str, *, minimum: int = 0) -> int:
    require(type(value) is int and minimum <= value <= 1_000_000_000,
            "INVALID_QUANTITY", f"{name} 必须是合法整数")
    return value


def amount(value: Any, name: str, *, positive: bool = False) -> Decimal:
    require(type(value) is str and re.fullmatch(r"(?:0|[1-9][0-9]{0,12})(?:\.[0-9]{1,6})?", value) is not None,
            "INVALID_AMOUNT", f"{name} 必须是最多6位小数的非负十进制字符串")
    number = Decimal(value)
    require(not positive or number > 0, "INVALID_AMOUNT", f"{name} 必须大于0")
    return number


def money(value: Decimal) -> str:
    return format(value, "f")


def when(value: Any, name: str) -> datetime:
    require(type(value) is str and len(value) <= 40, "INVALID_TIME", f"{name} 必须带时区")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PlanningError("INVALID_TIME", f"{name} 不是ISO时间") from exc
    require(parsed.tzinfo is not None and parsed.utcoffset() is not None,
            "INVALID_TIME", f"{name} 必须带时区")
    return parsed.astimezone(timezone.utc)


def clock(value: datetime) -> datetime:
    require(type(value) is datetime and value.tzinfo is not None and value.utcoffset() is not None,
            "INVALID_TIME", "内部时钟必须带时区")
    return value.astimezone(timezone.utc)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def empty_book() -> dict[str, Any]:
    return {"schema": SCHEMA, "policy_id": POLICY, "revision": 0,
            "allocations": {}, "inventory": {}, "cash": {}, "plans": {}}


def validate_parent(parent: Any) -> dict[str, Any]:
    fields(parent, {"position_id", "symbol", "market", "shares", "cost", "added_at"})
    identifier(parent["position_id"], "position_id")
    identifier(parent["symbol"], "symbol")
    market = parent["market"]
    require(type(market) is str and market in CURRENCIES, "INVALID_MARKET", "不支持的市场")
    suffix = parent["symbol"].rpartition(".")[2]
    require(suffix in {"A": ("SH", "SZ"), "HK": ("HK",), "US": ("US",)}[market],
            "IDENTITY_MISMATCH", "证券与市场不一致")
    integer(parent["shares"], "shares", minimum=1)
    # A parent cost is a source fingerprint, not a six-decimal order amount.
    # Preserve the complete finite legacy float's decimal representation.
    cost = parent["cost"]
    require(type(cost) is str and len(cost) <= 400
            and re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", cost) is not None,
            "INVALID_PARENT_COST", "原持仓成本必须是有界十进制文本")
    require(Decimal(cost).is_finite() and Decimal(cost) > 0,
            "INVALID_PARENT_COST", "原持仓成本必须是有限正数，不做隐式舍入")
    # Legacy added_at remains an opaque source fingerprint; never attach a guessed timezone.
    text(parent["added_at"], "added_at", 64)
    return parent


def fresh_interval(data: dict[str, Any], now: datetime) -> None:
    start, end = when(data["observed_at"], "observed_at"), when(data["expires_at"], "expires_at")
    require(start <= now < end <= start + timedelta(minutes=15), "SNAPSHOT_STALE",
            "人工确认需在15分钟有效窗口内，不得使用未来时间")


def active_plans(book: dict[str, Any], *, position_id: str | None = None, currency: str | None = None) -> list[dict[str, Any]]:
    return [p for p in book["plans"].values() if p["status"] in ACTIVE
            and (position_id is None or p["position_id"] == position_id)
            and (currency is None or p["currency"] == currency)]


def parent_for(parents: dict[str, Any], pid: str) -> dict[str, Any]:
    require(pid in parents, "POSITION_NOT_FOUND", "原持仓不存在或已关闭", 409)
    return validate_parent(parents[pid])


def assert_anchor(saved: dict[str, Any], parent: dict[str, Any]) -> None:
    require(saved["parent"] == parent, "POSITION_RECONCILIATION_REQUIRED",
            "原持仓已变化，请重新核对分配与库存", 409)


def reserve_plan(book: dict[str, Any], data: dict[str, Any], parents: dict[str, Any], now: datetime) -> dict[str, Any]:
    fields(data, {"plan_id", "position_id", "parent_hash", "purpose", "direction", "quantity", "buy_limit",
                  "sell_limit", "fee_buffer", "valid_until", "manual_conditions_acknowledged"})
    plan_id, pid = identifier(data["plan_id"], "plan_id"), identifier(data["position_id"], "position_id")
    require(plan_id not in book["plans"], "PLAN_EXISTS", "计划ID已存在", 409)
    require(len(book["plans"]) < MAX_PLANS, "CAPACITY_LIMIT", "计划库已达到本版本容量", 409)
    require(data["manual_conditions_acknowledged"] is True, "MANUAL_CONFIRMATION_REQUIRED", "需确认这不是执行授权")
    parent = parent_for(parents, pid)
    require(data["parent_hash"] == digest(parent), "POSITION_RECONCILIATION_REQUIRED", "持仓在页面加载后已变化", 409)
    allocation, inv = book["allocations"].get(pid), book["inventory"].get(pid)
    if allocation is None or inv is None:
        raise PlanningError("PLAN_INPUT_MISSING", "请先分配持仓并确认可卖库存", status=409)
    assert_anchor(allocation, parent)
    assert_anchor(inv, parent)
    fresh_interval(inv, now)
    purpose = data["purpose"]
    require(type(purpose) is str and purpose in PURPOSES, "INVALID_PURPOSE", "用途不合法")
    sleeve = next((s for s in allocation["sleeves"] if s["purpose"] == purpose), None)
    if sleeve is None:
        raise PlanningError("PLAN_INPUT_MISSING", "尚未为该用途分配持仓", status=409)
    direction = data["direction"]
    require(direction in ("BUY_THEN_SELL", "SELL_THEN_BUY") and type(direction) is str,
            "INVALID_DIRECTION", "方向不合法")
    qty = integer(data["quantity"], "quantity", minimum=1)
    require(qty % inv["lot_size"] == 0, "LOT_SIZE_MISMATCH", "计划数量不符合人工确认的交易单位")
    buy, sell, fees = amount(data["buy_limit"], "buy_limit", positive=True), amount(data["sell_limit"], "sell_limit", positive=True), amount(data["fee_buffer"], "fee_buffer")
    end = when(data["valid_until"], "valid_until")
    currency = inv["currency"]
    cash = book["cash"].get(currency)
    if cash is None:
        raise PlanningError("CASH_UNCONFIRMED", "请确认对应币种可用现金", status=409)
    fresh_interval(cash, now)
    require(now < end <= min(when(inv["expires_at"], "inventory expiry"), when(cash["expires_at"], "cash expiry")),
            "PLAN_EXPIRED", "计划有效期不得超出库存和现金确认窗口")
    pending = active_plans(book, position_id=pid)
    require(not any(p["parent"]["symbol"] == parent["symbol"]
                    and p["parent"]["market"] == parent["market"] and p["parent"] != parent
                    for p in active_plans(book)),
            "POSITION_RECONCILIATION_REQUIRED", "同一证券存在旧持仓版本的未关闭计划", 409)
    require(not any(p["status"] == "RECONCILIATION_REQUIRED" for p in active_plans(book, currency=currency)),
            "RECONCILIATION_REQUIRED", "该币种有实际操作待对账，不能新增预留", 409)
    available_tactical = sleeve["quantity"] - sleeve["core_quantity"] - sum(p["quantity"] for p in pending if p["purpose"] == purpose)
    sellable = inv["sellable_gross"] - inv["external_reserved_sell"] - sum(p["quantity"] for p in pending)
    require(qty <= available_tactical, "TACTICAL_CAPACITY_EXCEEDED", "不可动用核心仓或重复分配机动量", 409)
    require(qty <= sellable, "SELLABLE_EXCEEDED", "多个计划共享可卖旧仓，当前额度不足", 409)
    with localcontext(MONEY_CONTEXT):
        cash_required = qty * buy + fees
        cash_left = amount(cash["available_cash"], "available_cash") - sum((Decimal(p["reserved_cash"]) for p in active_plans(book, currency=currency)), Decimal(0))
        require(cash_required <= cash_left, "CASH_EXCEEDED", "现金不足，不预支预计卖出款", 409)
        peak = parent["shares"] + sum(p["quantity"] for p in pending if p["direction"] == "BUY_THEN_SELL") + (qty if direction == "BUY_THEN_SELL" else 0)
        require(peak <= inv["maximum_position_quantity"], "PEAK_EXPOSURE_EXCEEDED", "中途峰值数量超过人工上限", 409)
        return {**copy.deepcopy(data), "parent": copy.deepcopy(parent), "currency": currency,
                "status": "RESERVED", "reserved_cash": money(cash_required), "peak_quantity": peak,
                "scenario_net_spread": money(qty * (sell - buy) - fees),
                "created_at": now.isoformat(), "inventory_id": inv["snapshot_id"], "cash_id": cash["snapshot_id"],
                "assurance": "MANUAL_UNVERIFIED", "execution_authorized": False}


def reduce_command(book: dict[str, Any], command: dict[str, Any], parents: dict[str, Any], recorded_at: datetime) -> dict[str, Any]:
    """Rebuild the next book; does not mutate inputs or access external services."""
    now = clock(recorded_at)
    fields(command, {"command_id", "expected_revision", "kind", "data"})
    identifier(command["command_id"], "command_id")
    integer(command["expected_revision"], "expected_revision")
    require(command["expected_revision"] == book["revision"], "REVISION_CONFLICT", "页面版本已变化，请刷新后重试", 409)
    require(book["revision"] < MAX_COMMANDS, "CAPACITY_LIMIT", "命令日志已满，需显式导出升级", 409)
    kind = text(command["kind"], "kind", 40)
    # Reserve one final reconciliation command per supported currency. Never strand
    # an active plan merely because unrelated notes consumed the log budget.
    require(kind == "RECONCILE" or book["revision"] < MAX_COMMANDS - len(CURRENCIES),
            "CAPACITY_RECONCILIATION_ONLY", "日志接近容量；仅允许按币种对账关闭，不再新建或逐笔取消", 409)
    data = command["data"]
    result = copy.deepcopy(book)
    if kind in ("SET_ALLOCATION", "CONFIRM_INVENTORY"):
        require(type(data) is dict, "INVALID_FIELDS", "data必须是对象")
        pid = identifier(data.get("position_id"), "position_id")
        parent = parent_for(parents, pid)
        require(not active_plans(book, position_id=pid), "ACTIVE_RESERVATIONS", "请先处理此持仓未关闭计划", 409)
        require(pid in book["allocations"] or len(book["allocations"]) < MAX_POSITIONS, "CAPACITY_LIMIT", "持仓计划容量已满", 409)
        if kind == "SET_ALLOCATION":
            fields(data, {"position_id", "parent_hash", "sleeves"})
            require(data["parent_hash"] == digest(parent), "POSITION_RECONCILIATION_REQUIRED", "持仓在页面加载后已变化", 409)
            sleeves = data["sleeves"]
            require(type(sleeves) is list and 0 <= len(sleeves) <= 3, "INVALID_SLEEVES", "最多三类持仓用途")
            seen = set()
            total_quantity = 0
            for sleeve in sleeves:
                sleeve = fields(sleeve, {"purpose", "quantity", "core_quantity", "thesis", "invalidation", "review_at"})
                purpose = sleeve["purpose"]
                require(type(purpose) is str and purpose in PURPOSES and purpose not in seen, "INVALID_PURPOSE", "用途不合法或重复")
                seen.add(purpose)
                q = integer(sleeve["quantity"], "quantity", minimum=1)
                total_quantity += q
                require(integer(sleeve["core_quantity"], "core_quantity") <= q, "CORE_EXCEEDS_QUANTITY", "核心保留量超过分配量")
                text(sleeve["thesis"], "thesis")
                text(sleeve["invalidation"], "invalidation")
                if sleeve["review_at"] is not None:
                    when(sleeve["review_at"], "review_at")  # Reminder only, never mandatory exit.
            require(total_quantity <= parent["shares"], "ALLOCATION_EXCEEDED", "分配之和不能超过真实总仓")
            result["allocations"][pid] = {"parent": copy.deepcopy(parent), "sleeves": copy.deepcopy(sleeves), "updated_at": now.isoformat()}
        else:
            fields(data, {"position_id", "parent_hash", "sellable_gross", "external_reserved_sell", "lot_size", "maximum_position_quantity", "currency", "observed_at", "expires_at", "rule_note"})
            require(data["parent_hash"] == digest(parent), "POSITION_RECONCILIATION_REQUIRED", "持仓在页面加载后已变化", 409)
            fresh_interval(data, now)
            sellable = integer(data["sellable_gross"], "sellable_gross")
            reserved = integer(data["external_reserved_sell"], "external_reserved_sell")
            require(reserved <= sellable <= parent["shares"], "INVALID_SELLABLE", "可卖旧量/外部预留与真实总仓不一致")
            integer(data["lot_size"], "lot_size", minimum=1)
            integer(data["maximum_position_quantity"], "maximum_position_quantity", minimum=1)
            require(data["currency"] == CURRENCIES[parent["market"]], "CURRENCY_MISMATCH", "币种与本版支持的市场股票范围不一致")
            text(data["rule_note"], "rule_note", 400)
            result["inventory"][pid] = {**copy.deepcopy(data), "parent": copy.deepcopy(parent), "snapshot_id": digest({"data": data, "parent": parent, "recorded_at": now.isoformat()})}
    elif kind == "CONFIRM_CASH":
        fields(data, {"currency", "available_cash", "observed_at", "expires_at"})
        currency = data["currency"]
        require(type(currency) is str and currency in CURRENCIES.values(), "INVALID_CURRENCY", "不支持的币种")
        require(not active_plans(book, currency=currency), "ACTIVE_RESERVATIONS", "该币种有预留，不能用新现金快照覆盖", 409)
        amount(data["available_cash"], "available_cash")
        fresh_interval(data, now)
        result["cash"][currency] = {**copy.deepcopy(data), "snapshot_id": digest({"data": data, "recorded_at": now.isoformat()})}
    elif kind == "RESERVE":
        plan = reserve_plan(book, data, parents, now)
        result["plans"][plan["plan_id"]] = plan
    elif kind in ("CANCEL", "MARK_EXECUTED"):
        required = {"plan_id", "reason"} | ({"no_execution_confirmed", "no_open_orders_confirmed"} if kind == "CANCEL" else set())
        fields(data, required)
        plan_id = identifier(data["plan_id"], "plan_id")
        text(data["reason"], "reason")
        plan = result["plans"].get(plan_id)
        if plan is None or plan["status"] != "RESERVED":
            raise PlanningError("PLAN_STATE_CONFLICT", "当前状态不能执行该操作", status=409)
        if kind == "CANCEL":
            require(data["no_execution_confirmed"] is True and data["no_open_orders_confirmed"] is True,
                    "MANUAL_CONFIRMATION_REQUIRED", "需确认完全未执行且无外部未成交委托")
        plan.update(status="CANCELLED" if kind == "CANCEL" else "RECONCILIATION_REQUIRED", status_reason=data["reason"], updated_at=now.isoformat())
    elif kind == "RECONCILE":
        fields(data, {"currency", "plan_ids", "no_open_orders_confirmed", "accounting_checked", "reason"})
        currency = data["currency"]
        require(type(currency) is str and currency in CURRENCIES.values(), "INVALID_CURRENCY", "币种不合法")
        require(data["no_open_orders_confirmed"] is True and data["accounting_checked"] is True,
                "MANUAL_CONFIRMATION_REQUIRED", "对账需核对实际持仓现金并清理外部委托")
        text(data["reason"], "reason")
        pending = active_plans(result, currency=currency)
        ids = data["plan_ids"]
        require(type(ids) is list and bool(ids) and all(type(x) is str for x in ids)
                and len(set(ids)) == len(ids) and set(ids) == {p["plan_id"] for p in pending},
                "INCOMPLETE_RECONCILIATION", "必须一次处理该币种全部未关闭计划", 409)
        for p in pending:
            p.update(status="CLOSED_MANUAL_RECONCILIATION", status_reason=data["reason"], updated_at=now.isoformat())
        # Invalidate ALL old confirmations in this currency, not only traded symbols.
        result["cash"].pop(currency, None)
        result["inventory"] = {k: v for k, v in result["inventory"].items() if v["currency"] != currency}
    else:
        raise PlanningError("UNKNOWN_COMMAND", "不支持的计划命令")
    result["revision"] += 1
    return result


def public_book(book: dict[str, Any], parents: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Read-time stale/reconciliation flags do not release reserved resources."""
    now = clock(now)
    result = copy.deepcopy(book)
    for name in ("allocations", "inventory"):
        for pid, value in result[name].items():
            value["parent_matches"] = parents.get(pid) == value["parent"]
            if name == "inventory":
                value["fresh"] = when(value["observed_at"], "observed_at") <= now < when(value["expires_at"], "expires_at")
    for value in result["cash"].values():
        value["fresh"] = when(value["observed_at"], "observed_at") <= now < when(value["expires_at"], "expires_at")
    for plan in result["plans"].values():
        plan["expired"] = now >= when(plan["valid_until"], "valid_until")
        plan["parent_matches"] = parents.get(plan["position_id"]) == plan["parent"]
    result.update(as_of=now.isoformat(), assurance="MANUAL_UNVERIFIED", auto_trade=False,
                  execution_authorized=False, investment_performance_claim=False)
    return result


def attribution_scenario(data: dict[str, Any]) -> dict[str, Any]:
    """Mark-to-market difference versus identical buy-and-hold; manual scenario only."""
    fields(data, {"starting_quantity", "sellable_old_quantity", "buy_quantity", "sell_quantity", "average_buy", "average_sell", "fees", "mark_price", "currency"})
    start = integer(data["starting_quantity"], "starting_quantity")
    old = integer(data["sellable_old_quantity"], "sellable_old_quantity")
    b, s = integer(data["buy_quantity"], "buy_quantity"), integer(data["sell_quantity"], "sell_quantity")
    require(s <= old <= start, "SELLABLE_EXCEEDED", "卖出不得超过可卖旧仓，不把新买量算作旧仓")
    require(data["currency"] in CURRENCIES.values() and type(data["currency"]) is str, "INVALID_CURRENCY", "币种不合法")
    prices = []
    for qty, key in ((b, "average_buy"), (s, "average_sell")):
        if qty == 0:
            require(data[key] is None, "UNUSED_PRICE", "零数量腿的价格必须为空")
            prices.append(Decimal(0))
        else:
            prices.append(amount(data[key], key, positive=True))
    fees, mark = amount(data["fees"], "fees"), amount(data["mark_price"], "mark_price", positive=True)
    with localcontext(MONEY_CONTEXT):
        cash_delta = s * prices[1] - b * prices[0] - fees
        relative = cash_delta + (b - s) * mark
    return {"schema": "t-relative-hold-scenario-v1", "currency": data["currency"],
            "cash_delta": money(cash_delta), "quantity_delta": b - s,
            "ending_quantity": start + b - s, "unpaired_quantity": abs(b - s),
            "relative_hold_delta": money(relative), "fees": money(fees),
            "assurance": "MANUAL_SCENARIO", "investment_performance_claim": False,
            "auto_trade": False, "assumptions": "同币种/同估值时点/无外部资金流及公司行为；不是实际成交验证"}

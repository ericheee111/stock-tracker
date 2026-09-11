"""Synthetic manual planning contract, persistence and concurrency regressions."""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import getcontext
from pathlib import Path

from stock_tracker.portfolio_planning.domain import (
    PlanningError,
    attribution_scenario,
    digest,
    empty_book,
    public_book,
    reduce_command,
)
from stock_tracker.portfolio_planning.store import PlanningStore

NOW = datetime(2026, 9, 11, 2, 0, tzinfo=timezone.utc)
PARENT = {"position_id": "p1", "symbol": "600519.SH", "market": "A", "shares": 1000,
          "cost": "10", "added_at": "2026-01-01T00:00:00+00:00"}


def envelope(book, kind, data, cid=None):
    return {"command_id": cid or uuid.uuid4().hex, "expected_revision": book["revision"], "kind": kind, "data": data}


def allocation(parent=PARENT, quantity=1000, core=400):
    return {"position_id": parent["position_id"], "parent_hash": digest(parent), "sleeves": [
        {"purpose": "SWING", "quantity": quantity, "core_quantity": core, "thesis": "数周至数月趋势",
         "invalidation": "日线结构失效复核，不自动报单", "review_at": "2026-12-01T00:00:00+00:00"}]}


def inventory(parent=PARENT, **over):
    return {"position_id": parent["position_id"], "parent_hash": digest(parent), "sellable_gross": parent["shares"],
            "external_reserved_sell": 100, "lot_size": 100, "maximum_position_quantity": parent["shares"]+500,
            "currency": {"A": "CNY", "HK": "HKD", "US": "USD"}[parent["market"]],
            "observed_at": NOW.isoformat(), "expires_at": (NOW+timedelta(minutes=10)).isoformat(),
            "rule_note": "synthetic fixture of manually checked stock rules", **over}


def cash(**over):
    return {"currency": "CNY", "available_cash": "10000", "observed_at": NOW.isoformat(),
            "expires_at": (NOW+timedelta(minutes=10)).isoformat(), **over}


def reserve(parent=PARENT, **over):
    return {"plan_id": uuid.uuid4().hex, "position_id": parent["position_id"], "parent_hash": digest(parent),
            "purpose": "SWING", "direction": "BUY_THEN_SELL", "quantity": 200,
            "buy_limit": "10", "sell_limit": "11", "fee_buffer": "10",
            "valid_until": (NOW+timedelta(minutes=5)).isoformat(), "manual_conditions_acknowledged": True, **over}


def ready():
    book = empty_book()
    for k, d in (("SET_ALLOCATION", allocation()), ("CONFIRM_INVENTORY", inventory()), ("CONFIRM_CASH", cash())):
        book = reduce_command(book, envelope(book,k,d), {"p1": PARENT}, NOW)
    return book


class TestPlanningDomain(unittest.TestCase):
    def setUp(self):
        self.book = ready()
        self.parents = {"p1": copy.deepcopy(PARENT)}

    def apply(self, kind, data, now=NOW):
        self.book = reduce_command(self.book, envelope(self.book, kind, data), self.parents, now)
        return self.book

    def assertCode(self, code, fn):
        with self.assertRaises(PlanningError) as caught:
            fn()
        self.assertEqual(caught.exception.code, code)

    def test_unclassified_is_remainder_not_new_position(self):
        self.apply("SET_ALLOCATION", allocation(quantity=600,core=200))
        self.assertEqual(self.book["allocations"]["p1"]["parent"]["shares"], 1000)
        self.assertEqual(sum(s["quantity"] for s in self.book["allocations"]["p1"]["sleeves"]),600)

    def test_month_horizon_and_review_date_do_not_expire_holding(self):
        view = public_book(self.book,self.parents,NOW+timedelta(days=180))
        self.assertEqual(view["allocations"]["p1"]["sleeves"][0]["quantity"],1000)
        self.assertFalse(view["inventory"]["p1"]["fresh"])

    def test_duplicate_purpose_rejected(self):
        value=allocation();value["sleeves"]*=2
        self.assertCode("INVALID_PURPOSE",lambda:self.apply("SET_ALLOCATION",value))

    def test_allocation_and_core_upper_bounds(self):
        self.assertCode("ALLOCATION_EXCEEDED",lambda:self.apply("SET_ALLOCATION",allocation(quantity=1001)))
        self.assertCode("CORE_EXCEEDS_QUANTITY",lambda:self.apply("SET_ALLOCATION",allocation(core=1001)))

    def test_two_purposes_share_old_inventory(self):
        value=allocation(quantity=600,core=0)
        value["sleeves"].append({**value["sleeves"][0],"purpose":"SHORT_TERM","quantity":400})
        self.apply("SET_ALLOCATION",value)
        self.apply("CONFIRM_INVENTORY",inventory(sellable_gross=500,external_reserved_sell=0))
        self.apply("RESERVE",reserve(quantity=400,direction="SELL_THEN_BUY"))
        self.assertCode("SELLABLE_EXCEEDED",lambda:self.apply("RESERVE",reserve(purpose="SHORT_TERM",quantity=200)))

    def test_tactical_and_lot_limits(self):
        self.assertCode("TACTICAL_CAPACITY_EXCEEDED",lambda:self.apply("RESERVE",reserve(quantity=700)))
        self.assertCode("LOT_SIZE_MISMATCH",lambda:self.apply("RESERVE",reserve(quantity=150)))

    def test_new_purchase_cannot_supply_sellable(self):
        self.apply("CONFIRM_INVENTORY",inventory(sellable_gross=0,external_reserved_sell=0))
        self.assertCode("SELLABLE_EXCEEDED",lambda:self.apply("RESERVE",reserve()))

    def test_peak_position_and_cash_limit(self):
        self.assertCode("PEAK_EXPOSURE_EXCEEDED",lambda:self.apply("RESERVE",reserve(quantity=600)))
        self.apply("CONFIRM_CASH",cash(available_cash="2000"))
        self.assertCode("CASH_EXCEEDED",lambda:self.apply("RESERVE",reserve()))
        self.assertCode("CASH_EXCEEDED",lambda:self.apply("RESERVE",reserve(direction="SELL_THEN_BUY")))

    def test_two_symbols_share_currency_cash(self):
        p2={**PARENT,"position_id":"p2","symbol":"000001.SZ"}
        self.parents["p2"]=p2
        self.apply("SET_ALLOCATION",allocation(p2));self.apply("CONFIRM_INVENTORY",inventory(p2))
        self.apply("CONFIRM_CASH",cash(available_cash="3000"))
        self.apply("RESERVE",reserve())
        self.assertCode("CASH_EXCEEDED",lambda:self.apply("RESERVE",reserve(p2)))

    def test_currency_cannot_be_substituted(self):
        self.assertCode("CURRENCY_MISMATCH",lambda:self.apply("CONFIRM_INVENTORY",inventory(currency="USD")))

    def test_expiry_does_not_release(self):
        p=reserve();self.apply("RESERVE",p)
        view=public_book(self.book,self.parents,NOW+timedelta(minutes=6))
        self.assertTrue(view["plans"][p["plan_id"]]["expired"])
        self.assertEqual(view["plans"][p["plan_id"]]["status"],"RESERVED")
        self.assertCode("ACTIVE_RESERVATIONS",lambda:self.apply("CONFIRM_CASH",cash()))

    def test_parent_snapshot_change_rejected(self):
        self.parents["p1"]["shares"]=900
        self.assertCode("POSITION_RECONCILIATION_REQUIRED",lambda:self.apply("RESERVE",reserve()))
        self.assertCode("POSITION_RECONCILIATION_REQUIRED",lambda:self.apply("SET_ALLOCATION",allocation(quantity=800)))

    def test_recreated_position_cannot_bypass_old_symbol_reservation(self):
        self.apply("RESERVE",reserve())
        new={**PARENT,"position_id":"p-new"}
        self.parents={"p-new":new}
        self.apply("SET_ALLOCATION",allocation(new))
        self.apply("CONFIRM_INVENTORY",inventory(new))
        self.assertCode("POSITION_RECONCILIATION_REQUIRED",lambda:self.apply("RESERVE",reserve(new)))

    def test_parent_deleted_does_not_erase_plan(self):
        p=reserve();self.apply("RESERVE",p);self.parents.clear()
        self.assertFalse(public_book(self.book,self.parents,NOW)["plans"][p["plan_id"]]["parent_matches"])

    def test_stale_future_and_naive_snapshots_rejected(self):
        for changes in ({"observed_at":(NOW+timedelta(seconds=1)).isoformat()},
                        {"expires_at":NOW.isoformat()}, {"expires_at":(NOW+timedelta(hours=1)).isoformat()},
                        {"observed_at":"2026-09-11T02:00:00"}):
            with self.subTest(changes=changes),self.assertRaises(PlanningError):
                self.apply("CONFIRM_INVENTORY",inventory(**changes))

    def test_bad_primitive_inputs(self):
        for x in (True,False,"200",2.5,None,-1):
            with self.subTest(quantity=x),self.assertRaises(PlanningError):self.apply("RESERVE",reserve(quantity=x))
        for x in (True,10.0,"NaN","Infinity","-1","1e5",None,"1.1234567"):
            with self.subTest(amount=x),self.assertRaises(PlanningError):self.apply("RESERVE",reserve(buy_limit=x))
        self.assertCode("MANUAL_CONFIRMATION_REQUIRED",lambda:self.apply("RESERVE",reserve(manual_conditions_acknowledged=1)))

    def test_valid_cancel_needs_both_confirmations(self):
        p=reserve();self.apply("RESERVE",p)
        data={"plan_id":p["plan_id"],"reason":"未执行","no_execution_confirmed":True,"no_open_orders_confirmed":False}
        self.assertCode("MANUAL_CONFIRMATION_REQUIRED",lambda:self.apply("CANCEL",data))
        self.apply("CANCEL",{**data,"no_open_orders_confirmed":True})
        self.assertEqual(self.book["plans"][p["plan_id"]]["status"],"CANCELLED")

    def test_started_cannot_cancel_or_double_spend_until_reconciled(self):
        p=reserve();self.apply("RESERVE",p)
        self.apply("MARK_EXECUTED",{"plan_id":p["plan_id"],"reason":"转手工实际操作，尚未录入成交"})
        self.assertCode("PLAN_STATE_CONFLICT",lambda:self.apply("CANCEL",{"plan_id":p["plan_id"],"reason":"错误取消","no_execution_confirmed":True,"no_open_orders_confirmed":True}))
        self.assertCode("RECONCILIATION_REQUIRED",lambda:self.apply("RESERVE",reserve()))
        self.apply("RECONCILE",{"currency":"CNY","plan_ids":[p["plan_id"]],"no_open_orders_confirmed":True,"accounting_checked":True,"reason":"已在原Portfolio核对并清空外部委托"})
        self.assertNotIn("CNY",self.book["cash"]);self.assertFalse(self.book["inventory"])
        self.assertCode("PLAN_INPUT_MISSING",lambda:self.apply("RESERVE",reserve()))

    def test_reconciliation_must_cover_all_currency_plans(self):
        a,b=reserve(),reserve();self.apply("RESERVE",a);self.apply("RESERVE",b)
        self.assertCode("INCOMPLETE_RECONCILIATION",lambda:self.apply("RECONCILE",{"currency":"CNY","plan_ids":[a["plan_id"]],"no_open_orders_confirmed":True,"accounting_checked":True,"reason":"incomplete"}))

    def test_global_decimal_context_cannot_change_reservation(self):
        before=getcontext().copy()
        try:
            getcontext().prec=2
            p=reserve(buy_limit="10.123456",sell_limit="11.123456",fee_buffer="1.234567")
            self.apply("RESERVE",p)
            self.assertEqual(self.book["plans"][p["plan_id"]]["reserved_cash"],"2025.925767")
        finally:
            getcontext().prec=before.prec

    def test_capacity_reserves_room_for_currency_reconciliation(self):
        from unittest.mock import patch
        p=reserve();self.apply("RESERVE",p)
        with patch("stock_tracker.portfolio_planning.domain.MAX_COMMANDS",self.book["revision"]+3):
            self.assertCode("CAPACITY_RECONCILIATION_ONLY",lambda:self.apply("RESERVE",reserve()))
            self.apply("RECONCILE",{"currency":"CNY","plan_ids":[p["plan_id"]],
                        "no_open_orders_confirmed":True,"accounting_checked":True,"reason":"容量前对账"})
        self.assertEqual(self.book["plans"][p["plan_id"]]["status"],"CLOSED_MANUAL_RECONCILIATION")

    def test_revision_conflict(self):
        with self.assertRaises(PlanningError) as error:
            reduce_command(self.book,{"command_id":"old","expected_revision":0,"kind":"CONFIRM_CASH","data":cash()},self.parents,NOW)
        self.assertEqual(error.exception.code,"REVISION_CONFLICT")


class TestPlanningStore(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/"planning.sqlite"
        self.store=PlanningStore.create(self.path)

    def cmd(self,kind="SET_ALLOCATION",data=None):
        return envelope(self.store.read(),kind,data if data is not None else allocation())

    def test_missing_open_never_creates(self):
        missing=self.path.with_name("missing.sqlite")
        with self.assertRaises(PlanningError):PlanningStore(missing,"fake")
        self.assertFalse(missing.exists())

    def test_explicit_init_no_overwrite_or_primary(self):
        before=self.path.read_bytes()
        with self.assertRaises(PlanningError):PlanningStore.create(self.path)
        self.assertEqual(self.path.read_bytes(),before)
        with self.assertRaises(PlanningError):PlanningStore.create(self.path.with_name("stock_tracker.db"))

    def test_wrong_store_id_and_other_schema(self):
        with self.assertRaises(PlanningError):PlanningStore(self.path,"wrong")
        other=self.path.with_name("other.sqlite")
        con=sqlite3.connect(other);con.execute("CREATE TABLE x(a)");con.close()
        with self.assertRaises(PlanningError):PlanningStore(other,"fake")

    def test_restart_and_idempotency(self):
        cmd=self.cmd();first=self.store.apply(cmd,{"p1":PARENT},NOW)
        reopened=PlanningStore(self.path,self.store.store_id)
        second=reopened.apply(cmd,{},NOW+timedelta(days=1))
        self.assertEqual(first["command_revision"],second["command_revision"])
        self.assertTrue(second["idempotent"])
        drift=copy.deepcopy(cmd);drift["data"]["sleeves"][0]["thesis"]="different"
        with self.assertRaises(PlanningError):reopened.apply(drift,{"p1":PARENT},NOW)

    def test_rollback_before_commit(self):
        def fail(stage):
            if stage=="before_commit":raise RuntimeError("synthetic crash")
        store=PlanningStore(self.path,self.store.store_id,fault=fail)
        with self.assertRaises(RuntimeError):store.apply(self.cmd(),{"p1":PARENT},NOW)
        self.assertEqual(self.store.read()["revision"],0)

    def test_commit_succeeded_response_lost(self):
        def fail(stage):
            if stage=="after_commit":raise RuntimeError("synthetic lost response")
        store=PlanningStore(self.path,self.store.store_id,fault=fail);cmd=self.cmd()
        with self.assertRaises(RuntimeError):store.apply(cmd,{"p1":PARENT},NOW)
        self.assertTrue(self.store.apply(cmd,{"p1":PARENT},NOW)["idempotent"])
        self.assertEqual(self.store.read()["revision"],1)

    def test_concurrent_same_revision_only_one_wins(self):
        cmds=[self.cmd(),self.cmd()]
        def apply(cmd):
            try:return self.store.apply(cmd,{"p1":PARENT},NOW)["book"]["revision"]
            except PlanningError as exc:return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(apply,cmds))
        self.assertCountEqual(results,[1,"REVISION_CONFLICT"])

    def test_concurrent_exact_retry_once(self):
        cmd=self.cmd()
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:self.store.apply(cmd,{"p1":PARENT},NOW),range(2)))
        self.assertEqual(sum(r["idempotent"] for r in results),1)
        self.assertEqual(self.store.read()["revision"],1)

    def test_schema_injection_and_record_tamper(self):
        self.store.apply(self.cmd(),{"p1":PARENT},NOW)
        con=sqlite3.connect(self.path)
        with self.assertRaises(sqlite3.IntegrityError):con.execute("DELETE FROM commands")
        con.execute("CREATE VIEW injected AS SELECT 1");con.commit();con.close()
        with self.assertRaises(PlanningError):self.store.read()

    def test_record_hash_tamper_rejected(self):
        self.store.apply(self.cmd(),{"p1":PARENT},NOW)
        con=sqlite3.connect(self.path);con.execute("DROP TRIGGER commands_no_update")
        con.execute("UPDATE commands SET record_hash=?",("f"*64,))
        con.execute("CREATE TRIGGER commands_no_update BEFORE UPDATE ON commands BEGIN SELECT RAISE(ABORT,'immutable command'); END")
        con.commit();con.close()
        with self.assertRaises(PlanningError):self.store.read()

    def test_file_replacement_rejected(self):
        other=self.path.with_name("other.sqlite");PlanningStore.create(other)
        os.replace(other,self.path)
        with self.assertRaises(PlanningError):self.store.read()

    def test_hardlink_rejected(self):
        other=self.path.with_name("linked.sqlite");os.link(self.path,other)
        with self.assertRaises(PlanningError):self.store.read()
        with self.assertRaises(PlanningError):PlanningStore(other,self.store.store_id)

    def test_symlink_rejected_when_supported(self):
        other=self.path.with_name("linked.sqlite")
        try:other.symlink_to(self.path)
        except OSError:self.skipTest("symlink capability unavailable")
        with self.assertRaises(PlanningError):PlanningStore(other,self.store.store_id)

    def test_clock_rollback_rejected(self):
        self.store.apply(self.cmd(),{"p1":PARENT},NOW)
        with self.assertRaises(PlanningError) as exc:self.store.apply(self.cmd(),{"p1":PARENT},NOW-timedelta(seconds=1))
        self.assertEqual(exc.exception.code,"CLOCK_ROLLBACK")

    def test_full_ledger_replays_without_real_trades(self):
        for kind,data in (("SET_ALLOCATION",allocation()),("CONFIRM_INVENTORY",inventory()),("CONFIRM_CASH",cash()),("RESERVE",reserve(plan_id="t1")),("MARK_EXECUTED",{"plan_id":"t1","reason":"manual"}),("RECONCILE",{"currency":"CNY","plan_ids":["t1"],"no_open_orders_confirmed":True,"accounting_checked":True,"reason":"manual reconcile"})):
            self.store.apply(self.cmd(kind,data),{"p1":PARENT},NOW)
        self.assertEqual(self.store.read()["plans"]["t1"]["status"],"CLOSED_MANUAL_RECONCILIATION")
        con=sqlite3.connect(self.path);rows=con.execute("SELECT record_json FROM commands").fetchall();con.close()
        self.assertEqual(len(rows),6)
        self.assertFalse(any("broker" in json.loads(r[0])["command"] for r in rows))


class TestAttributionScenario(unittest.TestCase):
    def data(self,**over):
        return {"starting_quantity":1000,"sellable_old_quantity":1000,"buy_quantity":100,"sell_quantity":100,
                "average_buy":"10","average_sell":"11","fees":"10","mark_price":"12","currency":"CNY",**over}

    def test_complete_pair_and_no_performance_claim(self):
        value=attribution_scenario(self.data());self.assertEqual(value["relative_hold_delta"],"90")
        self.assertEqual(value["quantity_delta"],0);self.assertFalse(value["investment_performance_claim"])

    def test_sell_without_rebuy_includes_missed_upside(self):
        value=attribution_scenario(self.data(buy_quantity=0,average_buy=None))
        self.assertEqual(value["cash_delta"],"1090");self.assertEqual(value["relative_hold_delta"],"-110")
        self.assertEqual(value["unpaired_quantity"],100)

    def test_buy_without_sell_includes_open_position(self):
        value=attribution_scenario(self.data(sell_quantity=0,average_sell=None))
        self.assertEqual(value["relative_hold_delta"],"190")
        self.assertEqual(value["ending_quantity"],1100)

    def test_new_buy_not_available_for_sale(self):
        with self.assertRaises(PlanningError):attribution_scenario(self.data(sellable_old_quantity=0))

    def test_empty_leg_price_and_unknown_not_coerced(self):
        with self.assertRaises(PlanningError):attribution_scenario(self.data(buy_quantity=0))
        for bad in (None,True,12.0,"NaN"):
            with self.subTest(mark=bad),self.assertRaises(PlanningError):attribution_scenario(self.data(mark_price=bad))

    def test_positive_spread_can_lose_after_fees(self):
        self.assertEqual(attribution_scenario(self.data(fees="150"))["relative_hold_delta"],"-50")

    def test_hold_baseline_unchanged_when_no_t(self):
        value=attribution_scenario(self.data(buy_quantity=0,sell_quantity=0,average_buy=None,average_sell=None,fees="0"))
        self.assertEqual(value["relative_hold_delta"],"0")


class TestPlanningCLI(unittest.TestCase):
    def call(self, args):
        import io
        from contextlib import redirect_stdout

        from stock_tracker.portfolio_planning.__main__ import main
        output=io.StringIO()
        with redirect_stdout(output):code=main(args)
        return code,json.loads(output.getvalue())

    def test_explicit_init_then_read_only_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"plans.sqlite"
            code,result=self.call(["init","--database",str(path)])
            self.assertEqual(code,0);before=path.read_bytes()
            code,audit=self.call(["audit","--database",str(path),"--store-id",result["store_id"]])
            self.assertEqual(code,0);self.assertEqual(audit["revision"],0)
            self.assertEqual(path.read_bytes(),before)
            self.assertEqual(self.call(["init","--database",str(path)])[0],2)

    def test_missing_audit_never_creates(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"missing.sqlite"
            self.assertEqual(self.call(["audit","--database",str(path),"--store-id","fake"])[0],2)
            self.assertFalse(path.exists())

    def test_primary_database_name_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"stock_tracker.db"
            self.assertEqual(self.call(["init","--database",str(path)])[0],2)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()

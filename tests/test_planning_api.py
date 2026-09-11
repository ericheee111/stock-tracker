"""Private planning HTTP endpoints against temporary SQLite, never a Provider."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from stock_tracker.api.handlers import AppContext
from stock_tracker.api.planning_handlers import configure
from stock_tracker.api.server import APIServer, _private_api_access_allowed
from stock_tracker.core.store import MarketStore
from stock_tracker.portfolio_planning.store import PlanningStore
from stock_tracker.storage.db import close_all
from stock_tracker.storage.repository import Repository


class TestPlanningAPI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db = root / "portfolio.sqlite"
        self.repo = Repository(str(self.db))
        self.position = self.repo.create_position(symbol="600519.SH", market=__import__(
            "stock_tracker.core.types", fromlist=["Market"]).Market.A,
            shares=1000, average_cost=10, added_at=datetime(2026,1,1,tzinfo=timezone.utc))
        class NoProvider:
            def __getattr__(self, name):
                raise AssertionError("unexpected Provider access: " + name)
        self.ctx = AppContext(bundle=SimpleNamespace(), store=MarketStore(), repo=self.repo,
                              router=NoProvider(), signal_manager=None, sse_hub=SimpleNamespace(),
                              web_root=str(root))
        self.ctx.planning_store = PlanningStore.create(root / "planner.sqlite")
        self.ctx.planning_status = "READY"
        self.server = APIServer("127.0.0.1",0,self.ctx,None)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown_wait()
        self.thread.join(5)
        close_all()

    def request(self,method,path,payload=None,raw=None,headers=None):
        data = raw if raw is not None else json.dumps(payload,allow_nan=False).encode() if payload is not None else None
        req=urllib.request.Request(f"http://127.0.0.1:{self.server.server_address[1]}{path}",
                                   method=method,data=data,headers={"Content-Type":"application/json",**(headers or {})})
        try:
            response=urllib.request.urlopen(req,timeout=5)
        except urllib.error.HTTPError as error:
            with error:return error.code,json.loads(error.read())
        with response:return response.status,json.loads(response.read())

    def command(self,kind,data,cid=None,revision=None):
        import uuid
        if revision is None:
            status,view=self.request("GET","/api/planning/book")
            self.assertEqual(status,200)
            revision=view["book"]["revision"]
        payload={"store_id":self.ctx.planning_store.store_id,"command_id":cid or uuid.uuid4().hex,"expected_revision":revision,"kind":kind,"data":data}
        return self.request("POST","/api/planning/commands",payload)

    def ready(self):
        _,view=self.request("GET","/api/planning/book")
        self.parent=view["positions"][0]
        base={"position_id":self.parent["position_id"],"parent_hash":self.parent["parent_hash"]}
        start=datetime.now(timezone.utc)-timedelta(seconds=1)
        end=(start+timedelta(minutes=10)).isoformat()
        self.end=end
        data={**base,"sleeves":[{"purpose":"SWING","quantity":1000,"core_quantity":400,
                                 "thesis":"数月主线 <script>fixture</script>","invalidation":"日线结构复核","review_at":None}]}
        self.assertEqual(self.command("SET_ALLOCATION",data)[0],200)
        inv={**base,"sellable_gross":800,"external_reserved_sell":100,"lot_size":100,
             "maximum_position_quantity":1400,"currency":"CNY","observed_at":start.isoformat(),
             "expires_at":end,"rule_note":"manual fixture rule, not verified"}
        self.assertEqual(self.command("CONFIRM_INVENTORY",inv)[0],200)
        self.assertEqual(self.command("CONFIRM_CASH",{"currency":"CNY","available_cash":"10000",
                         "observed_at":start.isoformat(),"expires_at":end})[0],200)
        return {**base,"plan_id":"plan1","purpose":"SWING","direction":"SELL_THEN_BUY",
                "quantity":200,"buy_limit":"10","sell_limit":"11","fee_buffer":"10",
                "valid_until":end,"manual_conditions_acknowledged":True}

    def test_get_no_write_no_provider_and_unclassified(self):
        before=self.ctx.planning_store.path.read_bytes()
        status,body=self.request("GET","/api/planning/book")
        self.assertEqual(status,200);self.assertTrue(body["enabled"])
        self.assertEqual(len(body["positions"]),1)
        self.assertEqual(body["book"]["allocations"],{})
        self.assertEqual(self.ctx.planning_store.path.read_bytes(),before)
        self.assertFalse(body["auto_trade"])

    def test_preview_does_not_reserve_then_command_idempotent(self):
        plan=self.ready()
        revision=self.ctx.planning_store.read()["revision"]
        cmd={"store_id":self.ctx.planning_store.store_id,"command_id":"reserve-1","expected_revision":revision,"kind":"RESERVE","data":plan}
        code,preview=self.request("POST","/api/planning/preview",cmd)
        self.assertEqual(code,200);self.assertFalse(preview["reservation_created"])
        self.assertEqual(self.ctx.planning_store.read()["revision"],revision)
        code,response=self.request("POST","/api/planning/commands",cmd)
        self.assertEqual(code,200);self.assertFalse(response["idempotent"])
        code,response=self.request("POST","/api/planning/commands",cmd)
        self.assertEqual(code,200);self.assertTrue(response["idempotent"])
        self.assertFalse(response["book"]["plans"]["plan1"]["execution_authorized"])
        self.assertEqual(response["book"]["plans"]["plan1"]["reserved_cash"],"2010")

    def test_reservation_never_updates_portfolio(self):
        plan=self.ready()
        before=hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.assertEqual(self.command("RESERVE",plan)[0],200)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(),before)
        self.assertEqual(self.repo.get_position(self.position.id).shares,1000)

    def test_changed_parent_requires_refresh(self):
        plan=self.ready()
        self.repo.update_position(self.position.id,shares=900)
        status,body=self.command("RESERVE",plan)
        self.assertEqual((status,body["error"]["code"]),(409,"POSITION_RECONCILIATION_REQUIRED"))

    def test_client_store_binding_rejects_cross_store_command(self):
        plan=self.ready()
        cmd={"store_id":"different-store","command_id":"cross-store","expected_revision":3,"kind":"RESERVE","data":plan}
        for route in ("commands","preview"):
            code,result=self.request("POST","/api/planning/"+route,cmd)
            self.assertEqual((code,result["error"]["code"]),(409,"STORE_IDENTITY_MISMATCH"))
        self.assertEqual(self.ctx.planning_store.read()["revision"],3)

    def test_old_page_revision_conflicts(self):
        plan=self.ready()
        status,body=self.command("RESERVE",plan,revision=0)
        self.assertEqual((status,body["error"]["code"]),(409,"REVISION_CONFLICT"))

    def test_invalid_payloads_fail_closed(self):
        for raw in (b'[]',b'{',b'{"command_id":"x","command_id":"y"}',b'{"bad":NaN}',b'null'):
            with self.subTest(raw=raw):
                status,_=self.request("POST","/api/planning/commands",raw=raw)
                self.assertEqual(status,400)
        plan=self.ready();plan["quantity"]=True
        self.assertEqual(self.command("RESERVE",plan)[0],400)
        self.assertEqual(self.ctx.planning_store.read()["plans"],{})

    def test_request_limit(self):
        code,_=self.request("POST","/api/planning/commands",raw=b'{"padding":"'+b'x'*70000+b'"}')
        self.assertEqual(code,413)

    def test_unknown_route_and_read_method_dont_write(self):
        self.assertEqual(self.request("POST","/api/planning/unknown",{})[0],404)
        self.assertEqual(self.ctx.planning_store.read()["revision"],0)

    def test_remote_paths_are_private(self):
        for path in ("/api/planning/book","/api/planning/commands","/api/planning/preview","/api/planning/attribution"):
            allowed=_private_api_access_allowed(path=path,client_host="203.0.113.1",request_host="example.test",
                    has_forwarding_headers=True,request_origin="",sec_fetch_site="same-origin",
                    authorization="",configured_access="")
            self.assertFalse(allowed,path)
        from stock_tracker.core.security import PRIVATE_ACCESS_ENV
        with patch.dict(os.environ, {PRIVATE_ACCESS_ENV: ""}):
            code, body = self.request("GET", "/api/planning/book", headers={"X-Forwarded-For": "203.0.113.1"})
            self.assertEqual((code, body["error"]["code"]), (503, "PRIVATE_API_DISABLED"))
        with patch.dict(os.environ, {PRIVATE_ACCESS_ENV: "synthetic-private-access-" + "x" * 32}):
            code, body = self.request("GET", "/api/planning/book", headers={"X-Forwarded-For": "203.0.113.1"})
            self.assertEqual((code, body["error"]["code"]), (401, "PRIVATE_API_AUTH_REQUIRED"))

    def test_cross_origin_is_rejected(self):
        code,_=self.request("GET","/api/planning/book",headers={"Origin":"https://hostile.invalid","Sec-Fetch-Site":"cross-site"})
        self.assertIn(code,(401,403))

    def test_disabled_mode_is_honest_and_writes_503(self):
        self.ctx.planning_store=None;self.ctx.planning_status="NOT_CONFIGURED"
        code,result=self.request("GET","/api/planning/book")
        self.assertEqual(code,200);self.assertFalse(result["enabled"]);self.assertIsNone(result["book"])
        code,result=self.request("POST","/api/planning/commands",{})
        self.assertEqual((code,result["error"]["code"]),(503,"PLANNER_NOT_INITIALIZED"))

    def test_configuration_never_initializes_and_primary_rejected(self):
        missing=Path(self.tmp.name)/"missing.sqlite"
        for path in (str(missing),str(self.db)):
            with self.subTest(path=path),patch.dict(os.environ,{"STOCK_TRACKER_PLANNING_DB":path,"STOCK_TRACKER_PLANNING_STORE_ID":"fake"}):
                configure(self.ctx,str(self.db),logging.getLogger("test"))
                self.assertIsNone(self.ctx.planning_store);self.assertEqual(self.ctx.planning_status,"UNAVAILABLE")
        self.assertFalse(missing.exists())

    def test_reconcile_does_not_create_real_fills(self):
        plan=self.ready();self.command("RESERVE",plan)
        self.assertEqual(self.command("MARK_EXECUTED",{"plan_id":"plan1","reason":"有实际操作待核对"})[0],200)
        code,result=self.command("RECONCILE",{"currency":"CNY","plan_ids":["plan1"],"reason":"已核对原持仓与现金，外部委托清空",
                                "no_open_orders_confirmed":True,"accounting_checked":True})
        self.assertEqual(code,200);self.assertEqual(result["book"]["cash"],{})
        self.assertEqual(result["book"]["plans"]["plan1"]["status"],"CLOSED_MANUAL_RECONCILIATION")
        self.assertFalse(result["book"]["investment_performance_claim"])

    def test_manual_attribution_works_without_store(self):
        self.ctx.planning_store=None
        data={"starting_quantity":1000,"sellable_old_quantity":1000,"buy_quantity":0,"sell_quantity":100,
              "average_buy":None,"average_sell":"11","fees":"10","mark_price":"12","currency":"CNY"}
        status,value=self.request("POST","/api/planning/attribution",data)
        self.assertEqual(status,200);self.assertEqual(value["relative_hold_delta"],"-110")
        self.assertEqual(value["assurance"],"MANUAL_SCENARIO")

    def test_internal_error_does_not_leak_account_or_paths(self):
        with patch.object(self.ctx.repo,"load_positions",side_effect=RuntimeError("secret account at D:/private")):
            status,result=self.request("GET","/api/planning/book")
        self.assertEqual(status,500);self.assertNotIn("private",json.dumps(result))


if __name__ == "__main__":
    unittest.main()

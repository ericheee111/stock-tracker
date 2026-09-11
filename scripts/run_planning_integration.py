"""Real local API/browser integration using ONLY temporary DBs and synthetic quotes."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_stage1_today_integration import (
    _LocalBus,
    _quote,
    _RouterStub,
    _signal,
    _SignalManagerStub,
)
from stock_tracker.api.handlers import AppContext
from stock_tracker.api.server import APIServer
from stock_tracker.api.sse import SSEHub
from stock_tracker.core import types as T
from stock_tracker.core.config import load_configs
from stock_tracker.core.store import MarketStore
from stock_tracker.portfolio_planning.store import PlanningStore
from stock_tracker.storage.db import close_all
from stock_tracker.storage.repository import Repository


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="manual-plan-browser-") as temp:
        servers = []
        threads = []
        try:
            repo = Repository(str(Path(temp) / "portfolio.sqlite"))
            position = repo.create_position(symbol="600519.SH", market=T.Market.A, shares=1000,
                                            average_cost=10, added_at=datetime(2026,1,1,tzinfo=timezone.utc))
            store = MarketStore()
            store.set_positions([position])
            store.update_quote(_quote("600519.SH", name="持仓合成样例", last=10.5))
            store.update_quote(_quote("000001.SZ", name="机会合成样例", last=10.5))
            store.upsert_signal(_signal("000001.SZ", T.SignalState.ARMED_BREAKOUT, strategy_id="S1",
                entry_low=10,entry_high=10.4,trigger=10.6,invalidation=9.5,target_1=11.5,target_2=12.2,opportunity=84))
            planning = PlanningStore.create(Path(temp) / "planning.sqlite")
            for enabled in (True, False):
                context = AppContext(bundle=load_configs(str(ROOT/"config")), store=store, repo=repo,
                    router=_RouterStub(), signal_manager=_SignalManagerStub(), sse_hub=SSEHub(_LocalBus()),
                    web_root=str(ROOT/"web"), planning_store=planning if enabled else None,
                    planning_status="READY" if enabled else "NOT_CONFIGURED")
                server=APIServer("127.0.0.1",0,context,None)
                thread=threading.Thread(target=server.serve_forever,daemon=True)
                servers.append(server);threads.append(thread);thread.start()
            env=dict(os.environ)
            env.update(PLANNING_QA_BASE_URL=f"http://127.0.0.1:{servers[0].server_address[1]}",
                       PLANNING_QA_DISABLED_URL=f"http://127.0.0.1:{servers[1].server_address[1]}")
            env.setdefault("PLANNING_QA_REPORT_DIR", str(Path(temp)/"reports"))
            result=subprocess.run(["node",str(ROOT/"qa/ui/planning_qa.cjs")],cwd=ROOT,env=env,
                                  timeout=180,check=False)
            after=repo.get_position(position.id)
            unchanged=after is not None and after.shares==1000 and after.cost==10
            print(json.dumps({"fixture_portfolio_unchanged":unchanged,"auto_trade":False,
                              "planning_revision":planning.read()["revision"],
                              "assurance":"SYNTHETIC_TEMPORARY_INTEGRATION"}))
            return result.returncode if result.returncode else (0 if unchanged else 1)
        finally:
            for server in servers:server.shutdown_wait()
            for thread in threads:thread.join(5)
            close_all()


if __name__ == "__main__":
    raise SystemExit(main())

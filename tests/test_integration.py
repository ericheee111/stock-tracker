"""集成测试（T10）。

两部分：
1) 端到端管线（in-process，无网络）：归一化 Quote → FeatureEngine → 策略 → 评分 → 风险闸门
   → 状态机 → 持久化 SQLite，验证「注入 Quote → 特征 → 信号 → SQLite」闭环真实可用。
2) API 契约（in-process HTTP）：在本进程启动真实 APIServer（同一套 handlers/serializers），
   命中 /api/overview、/api/sectors、/api/radar、/api/provider_health、/api/markets，
   校验 JSON 契约与「新鲜度不伪造」（陈旧数据不伪装 LIVE、observed_age_ms>0）。
3) 生产服务探针：尝试连接 :8080；沙箱未启动则跳过（不阻塞套件）。

不修改任何业务代码；仅启动应用并命中真实端点。
"""

import json
import os
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from types import SimpleNamespace

import unittest

from stock_tracker.core import types as T
from stock_tracker.core.config import load_configs, ProviderConfig
from stock_tracker.core.store import MarketStore
from stock_tracker.storage.repository import Repository
from stock_tracker.collector.router import ProviderRouter
from stock_tracker.collector.tencent import TencentProvider
from stock_tracker.collector.sina import SinaProvider
from stock_tracker.collector.eastmoney import EastmoneyProvider
from stock_tracker.data_quality.gate import DataQualityGate
from stock_tracker.features.engine import FeatureEngine
from stock_tracker.signals.manager import SignalManager
from stock_tracker.api.handlers import AppContext
from stock_tracker.api.server import APIServer
from stock_tracker.storage.db import close_all

from tests._common import _ROOT, make_bars, make_regime, make_sector, make_quote, now_ts

CONFIG_DIR = os.path.join(_ROOT, "config")
WEB_ROOT = os.path.join(_ROOT, "web")

_REGISTRY = {
    "TencentProvider": TencentProvider,
    "SinaProvider": SinaProvider,
    "EastmoneyProvider": EastmoneyProvider,
}


def _tencent_body_a(last="105.00", prev="100.00", open_="101.00", vol_hand="10000",
                    high="106.00", low="95.00", amount_yuan="500000000", turnover="2.0"):
    parts = ["x"] * 39
    parts[0] = "v_sh600519"
    parts[1] = "贵州茅台"
    parts[3] = last
    parts[4] = prev
    parts[5] = open_
    parts[6] = vol_hand
    parts[30] = "20260812150000"
    parts[33] = high
    parts[34] = low
    parts[35] = f"{last}/{vol_hand}/{amount_yuan}"
    parts[37] = str(float(amount_yuan) / 10000.0)
    parts[38] = turnover
    return "~".join(parts)


def _build_test_context(db_path: str) -> AppContext:
    """装配与 __main__.build_context 等价的上下文（清除故障注入 host，指向临时 DB，不启动调度）。"""
    bundle = load_configs(CONFIG_DIR)
    for pc in bundle.providers:
        pc.host = ""  # 清除故障注入，避免任何真实网络
    bundle.app.store.sqlite_path = db_path
    bundle.app.root_dir = os.path.dirname(CONFIG_DIR)
    store = MarketStore()
    repo = Repository(db_path)
    repo.recover_provider_states()
    providers = [cls(pc) for pc in bundle.providers
                 if (cls := _REGISTRY.get(pc.cls)) is not None]
    router = ProviderRouter(bundle, providers)
    gate = DataQualityGate(bundle)
    fe = FeatureEngine(bundle)
    sm = SignalManager(bundle, store, repo, router, fe, gate)
    sm.recover()
    ctx = AppContext(bundle=bundle, store=store, repo=repo, router=router,
                     signal_manager=sm, sse_hub=SimpleNamespace(), web_root=WEB_ROOT)
    return ctx


class TestE2EPipeline(unittest.TestCase):
    def tearDown(self):
        close_all()

    def test_quote_to_signal_to_sqlite(self):
        tmp = tempfile.mkdtemp()
        db_path = os.path.join(tmp, "e2e.db")
        ctx = _build_test_context(db_path)

        # 放宽最小 R，使突破候选能通过风险闸门（测试配置，未改业务代码）
        ctx.bundle.risk.min_r_multiple = 0.1
        ctx.bundle.risk.regime_blocked_states = []

        # 1) 归一化真实腾讯响应 → Quote（贴近近期高位以触发 S1 突破，与 bars 末值一致）
        tencent = TencentProvider(ProviderConfig(name="tencent", cls="TencentProvider",
                                                 markets=["a", "hk", "us"]))
        q = tencent.normalize(("sh600519", _tencent_body_a(
            last="148.00", prev="145.00", open_="147.00", high="149.00", low="140.00")),
            T.Market.A)
        q.timestamp = now_ts(-10)
        q.observed_age_ms = 10_000  # 新鲜 → DQ VALID

        bars = make_bars(n=25, start=100, step=2, symbol="600519.SH")
        regime = make_regime(T.RegimeState.RISK_ON_TREND, 70)
        sector = make_sector(T.SectorStage.LEADING, 65, rs=70, sector="白酒")

        # 2) 全管线扫描
        produced = ctx.signal_manager.scan_symbol("600519.SH", q, bars, regime, sector)
        self.assertTrue(len(produced) >= 1, "应至少产出 1 个信号")
        sig = produced[0]
        self.assertIn(sig.state, (T.SignalState.ARMED_BREAKOUT, T.SignalState.TRIGGERED))
        self.assertEqual(sig.symbol, "600519.SH")

        # 3) 持久化到 SQLite 并可回读
        repo = Repository(db_path)
        loaded = repo.load_signals()
        self.assertIn(sig.signal_id, loaded)
        self.assertEqual(loaded[sig.signal_id].state, sig.state)
        # Quote 也已落库
        quotes = repo.load_quotes()
        self.assertIn("600519.SH", quotes)

        # 4) 分数与证据族存在
        self.assertIsNotNone(sig.scores)
        self.assertGreaterEqual(sig.scores.opportunity, 0)


class TestApiContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.db = os.path.join(cls.tmp, "api.db")
        cls.ctx = _build_test_context(cls.db)

        # 注入一个陈旧（3 小时前）的 A 股 Quote，用于验证新鲜度不伪造
        old_q = make_quote(
            symbol="600519.SH", open=101.0, high=110.0, low=95.0, close=105.0, last=105.0,
            prev_close=100.0, turnover=2.0, amount=1e9,
            timestamp=datetime.now() - timedelta(hours=3),
            observed_age_ms=3 * 3600 * 1000,
        )
        cls.ctx.store.update_quote(old_q)
        cls.ctx.repo.save_quote(old_q)
        cls.ctx.store.set_regime(make_regime(T.RegimeState.ROTATION, 55))
        cls.ctx.store.update_sector(make_sector(T.SectorStage.LEADING, 60, rs=65, sector="白酒"))
        cls.ctx.store.upsert_instrument("600519.SH", {"sector": "白酒", "name": "贵州茅台"})

        cls.port = 18080
        cls.server = APIServer("127.0.0.1", cls.port, cls.ctx, None)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        # 等待服务可达
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{cls.port}/api/provider_health", timeout=1)
                break
            except Exception:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.shutdown_wait()
        except Exception:
            pass
        close_all()

    def _get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))

    def test_provider_health_contract(self):
        data = self._get("/api/provider_health")
        self.assertIn("providers", data)
        self.assertIn("data_status", data)
        self.assertIsInstance(data["providers"], list)
        for p in data["providers"]:
            self.assertIn("circuit_state", p)
            self.assertIn(p["circuit_state"], ("CLOSED", "OPEN", "HALF_OPEN"))

    def test_overview_contract(self):
        data = self._get("/api/overview")
        self.assertIn("meta", data)
        self.assertIn("data_mode", data["meta"])
        self.assertIn("providers", data["meta"])
        self.assertIn("observed_age_ms", data)
        self.assertGreater(data["observed_age_ms"], 0)  # 注入的 A 股 quote 年龄 >0
        self.assertIn("top_opportunities", data)
        self.assertIn("breadth", data)
        self.assertIn("risk_events", data)

    def test_sectors_contract(self):
        data = self._get("/api/sectors")
        self.assertIn("sectors", data)
        self.assertIn("count", data)
        self.assertIn("data_status", data)
        self.assertGreaterEqual(data["count"], 1)  # 已注入「白酒」板块

    def test_radar_contract(self):
        data = self._get("/api/radar")
        self.assertIn("signals", data)
        self.assertIn("candidates", data)
        self.assertIn("data_status", data)
        self.assertIn("observed_age_ms", data)

    def test_freshness_not_faked(self):
        # 注入的 A 股 quote 已陈旧（3 小时前）：不应伪装成 LIVE，且 observed_age_ms>0
        data = self._get("/api/markets")
        self.assertIn("observed_age_ms", data)
        self.assertGreater(data["observed_age_ms"], 0)
        a = data.get("a")
        if a is not None and a.get("count", 0) > 0:
            self.assertIn(a["data_status"], ("DELAYED", "STALE", "UNKNOWN"))
            self.assertNotEqual(a["data_status"], "LIVE")
            self.assertGreater(a["observed_age_ms"], 0)


class TestLiveServiceProbe(unittest.TestCase):
    def test_live_8080_probe(self):
        """尝试连接生产 :8080；沙箱未启动则跳过。"""
        try:
            with urllib.request.urlopen("http://127.0.0.1:8080/api/provider_health", timeout=2) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            self.skipTest(f"生产服务 :8080 不可达，跳过实时集成用例：{e}")
        self.assertIn("providers", data)


if __name__ == "__main__":
    unittest.main()

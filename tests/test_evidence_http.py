"""Actual HTTP/detail/asset paths with temporary SQLite and synthetic market inputs."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.request import ProxyHandler, build_opener

from scripts.run_stage1_today_integration import (
    _LocalBus,
    _RouterStub,
    _SignalManagerStub,
)
from stock_tracker.api.handlers import AppContext
from stock_tracker.api.server import APIServer
from stock_tracker.api.sse import SSEHub
from stock_tracker.core.config import load_configs
from stock_tracker.core.store import MarketStore
from stock_tracker.storage.db import close_all
from stock_tracker.storage.repository import Repository
from tests.test_evidence_comparison import context

ROOT = Path(__file__).resolve().parents[1]


class TestEvidenceHTTP(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='n4-http-')
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(close_all)
        repo = Repository(str(Path(self.temp.name)/'temporary.sqlite'))
        sample = context()
        now = datetime.now(timezone.utc)
        sample.quote.timestamp = now - timedelta(seconds=2)
        sample.quote.received_at = now - timedelta(seconds=1)
        sample.quote.computed_at = now
        sample.quote.quality = sample.dq
        # Preserve relative ages, never inject future fixture timestamps.
        for i, bar in enumerate(sample.recent_bars):
            bar.timestamp = now - timedelta(days=len(sample.recent_bars)-i)
        repo.save_bars_batch(sample.recent_bars)
        store = MarketStore()
        store.update_quote(sample.quote)
        self.ctx = AppContext(bundle=load_configs(str(ROOT/'config')),store=store,repo=repo,
                              router=_RouterStub(),signal_manager=_SignalManagerStub(),sse_hub=SSEHub(_LocalBus()),
                              web_root=str(ROOT/'web'))
        self.server = APIServer('127.0.0.1', 0, self.ctx, None)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.origin = f'http://127.0.0.1:{self.server.server_address[1]}'
        self.opener = build_opener(ProxyHandler({}))

    def stop(self):
        self.server.shutdown_wait()
        self.thread.join(5)

    def fetch(self, path):
        with self.opener.open(self.origin+path, timeout=5) as response:
            return response.read()

    def test_real_detail_contains_bounded_comparison_without_signal_writes(self):
        before = self.ctx.store.get_signals()
        with patch.object(self.ctx.repo, 'load_recent_bars', wraps=self.ctx.repo.load_recent_bars) as reads:
            d = json.loads(self.fetch('/api/quote/600000.SH'))
            reads.assert_called_once_with('600000.SH','1d',n=260)
        report = d['evidence_comparison']
        self.assertEqual(report['status'], 'PARTIAL')
        self.assertEqual(report['computed_at'], d['indicator_diagnostics']['computed_at'])
        self.assertEqual(report['sample_info']['selected_count'], 80)
        self.assertFalse(report['affects_live_scores'])
        self.assertEqual(d['bar_count'],80)
        self.assertEqual(len(d['recent_bars']),30)
        self.assertEqual(self.ctx.store.get_signals(), before)
        self.assertEqual(self.ctx.repo.load_positions(), [])

    def test_actual_asset_routes_and_detail_bootstrap_include_optional_renderer(self):
        html = self.fetch('/').decode('utf-8')
        script = self.fetch('/js/evidence_comparison.js').decode('utf-8')
        css = self.fetch('/css/evidence_comparison.css').decode('utf-8')
        app = self.fetch('/js/app.js').decode('utf-8')
        self.assertIn('js/evidence_comparison.js',html)
        self.assertIn('Object.freeze({render, valid})',script)
        self.assertIn('.ec-panel',css)
        self.assertIn('window.EvidenceComparison.render(d.evidence_comparison, d.symbol, d.market)',app)


if __name__ == '__main__':
    unittest.main()

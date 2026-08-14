import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace

from stock_tracker.api.handlers import AppContext
from stock_tracker.api.server import APIServer
from stock_tracker.core.store import MarketStore
from stock_tracker.storage.db import close_all
from stock_tracker.storage.repository import Repository


class TestPortfolioAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmp.name, "portfolio-api.db")
        cls.store = MarketStore()
        cls.repo = Repository(cls.db_path)
        cls.ctx = AppContext(
            bundle=SimpleNamespace(),
            store=cls.store,
            repo=cls.repo,
            router=SimpleNamespace(),
            signal_manager=None,
            sse_hub=SimpleNamespace(),
            web_root=cls.tmp.name,
        )
        cls.server = APIServer("127.0.0.1", 0, cls.ctx, None)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown_wait()
        cls.thread.join(timeout=5)
        close_all()
        db_path = cls.db_path
        cls.tmp.cleanup()
        cls.db_removed = not os.path.exists(db_path)

    def request(self, method, path, payload=None, raw=None, content_type="application/json"):
        body = raw if raw is not None else (
            json.dumps(payload, allow_nan=False).encode("utf-8") if payload is not None else None
        )
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=body,
            method=method,
            headers={"Content-Type": content_type},
        )
        try:
            response = urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))
        with response:
            return response.status, json.loads(response.read().decode("utf-8"))

    @staticmethod
    def profile(**overrides):
        payload = {
            "account_equity": 500000.0,
            "available_cash": 120000.0,
            "risk_mode": "BALANCED",
            "per_trade_risk_pct": 0.005,
            "max_position_pct": 0.20,
            "max_portfolio_heat_pct": 0.06,
            "max_sector_pct": 0.35,
            "max_theme_pct": 0.35,
        }
        payload.update(overrides)
        return payload

    @staticmethod
    def position(**overrides):
        payload = {
            "symbol": "000001.SZ",
            "market": "A",
            "shares": 1000,
            "average_cost": 11.2,
            "added_at": "2026-08-01T10:00:00+08:00",
        }
        payload.update(overrides)
        return payload

    def test_profile_and_position_crud_contract(self):
        status, profile = self.request("PUT", "/api/portfolio/profile", self.profile())
        self.assertEqual(status, 200)
        self.assertEqual(profile["risk_mode"], "BALANCED")
        status, position = self.request("POST", "/api/portfolio/positions", self.position())
        self.assertEqual(status, 201)
        self.assertEqual(position["average_cost"], 11.2)
        position_id = position["id"]
        status, portfolio = self.request("GET", "/api/portfolio")
        self.assertEqual((status, portfolio["schema_version"]), (200, "stage1-v1"))
        self.assertEqual(portfolio["positions"][0]["id"], position_id)
        status, updated = self.request(
            "PATCH", f"/api/portfolio/positions/{position_id}", {"shares": 1200}
        )
        self.assertEqual((status, updated["shares"]), (200, 1200))
        status, deleted = self.request("DELETE", f"/api/portfolio/positions/{position_id}")
        self.assertEqual((status, deleted["ok"]), (200, True))

    def test_strict_json_and_number_validation(self):
        cases = [
            (b"{", "INVALID_JSON"),
            (b"\xff", "INVALID_JSON"),
            (b"[]", "INVALID_JSON_OBJECT"),
            (json.dumps(self.profile(account_equity=True)).encode(), "INVALID_NUMBER"),
            (b'{"account_equity":NaN}', "INVALID_JSON"),
            (b'{"account_equity":Infinity}', "INVALID_JSON"),
        ]
        for raw, code in cases:
            with self.subTest(code=code, raw=raw):
                status, body = self.request("PUT", "/api/portfolio/profile", raw=raw)
                self.assertEqual((status, body["error"]["code"]), (400, code))

    def test_oversized_json_is_rejected_before_parsing(self):
        raw = b'{"padding":"' + (b"x" * (70 * 1024)) + b'"}'
        status, body = self.request(
            "PUT",
            "/api/portfolio/profile",
            raw=raw,
        )
        self.assertEqual(
            (status, body["error"]["code"]),
            (413, "REQUEST_TOO_LARGE"),
        )

    def test_profile_range_validation(self):
        invalid = [
            (self.profile(account_equity=-1), "INVALID_NUMBER"),
            (self.profile(available_cash=600000), "INVALID_PROFILE"),
            (self.profile(max_position_pct=1.1), "INVALID_PROFILE"),
            (self.profile(per_trade_risk_pct=0.07), "INVALID_PROFILE"),
            (self.profile(risk_mode="UNKNOWN"), "INVALID_RISK_MODE"),
            ({**self.profile(), "extra": 1}, "UNKNOWN_FIELD"),
        ]
        for payload, code in invalid:
            with self.subTest(code=code):
                status, body = self.request("PUT", "/api/portfolio/profile", payload)
                self.assertEqual((status, body["error"]["code"]), (400, code))

    def test_position_validation_conflict_and_not_found(self):
        invalid = [
            self.position(shares=True),
            self.position(shares=0),
            self.position(shares=-100),
            self.position(average_cost=-1),
            self.position(symbol="AAPL.US", market="A"),
            self.position(added_at="2026-08-01T10:00:00"),
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                status, _ = self.request("POST", "/api/portfolio/positions", payload)
                self.assertEqual(status, 400)
        status, created = self.request(
            "POST", "/api/portfolio/positions", self.position(symbol="600000.SH")
        )
        self.assertEqual(status, 201)
        status, body = self.request(
            "POST", "/api/portfolio/positions", self.position(symbol="600000.SH")
        )
        self.assertEqual((status, body["error"]["code"]), (409, "POSITION_CONFLICT"))
        position_id = created["id"]
        status, body = self.request(
            "PATCH", f"/api/portfolio/positions/{position_id}", {"symbol": "000001.SZ"}
        )
        self.assertEqual((status, body["error"]["code"]), (400, "UNKNOWN_FIELD"))
        status, updated = self.request(
            "PATCH", f"/api/portfolio/positions/{position_id}", {"shares": 150}
        )
        self.assertEqual((status, updated["shares"]), (200, 150))
        self.request("DELETE", f"/api/portfolio/positions/{position_id}")
        status, body = self.request("DELETE", "/api/portfolio/positions/missing")
        self.assertEqual((status, body["error"]["code"]), (404, "POSITION_NOT_FOUND"))

    def test_api_never_calls_provider(self):
        class ExplodingRouter:
            def __getattr__(self, name):
                raise AssertionError(f"provider access: {name}")

        original = self.ctx.router
        self.ctx.router = ExplodingRouter()
        try:
            status, _ = self.request("GET", "/api/portfolio")
            self.assertEqual(status, 200)
        finally:
            self.ctx.router = original

    def test_internal_error_does_not_leak_details(self):
        original = self.ctx.repo.load_positions

        def fail():
            raise RuntimeError(r"secret database path C:\private\portfolio.db")

        self.ctx.repo.load_positions = fail
        try:
            status, body = self.request("GET", "/api/portfolio")
            self.assertEqual((status, body["error"]["code"]), (500, "INTERNAL_ERROR"))
            self.assertNotIn("private", json.dumps(body))
        finally:
            self.ctx.repo.load_positions = original


if __name__ == "__main__":
    unittest.main()

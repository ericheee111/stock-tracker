import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone

from stock_tracker.core import types as T
from stock_tracker.decision.types import RiskMode, UserPortfolioProfile
from stock_tracker.storage.db import close_all
from stock_tracker.storage.repository import (
    Repository,
    RepositoryConflictError,
    RepositoryValidationError,
)


class TestPortfolioRepository(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "portfolio.db")

    def tearDown(self):
        close_all()
        self.tmp.cleanup()

    def test_profile_round_trip_and_schema_idempotency(self):
        repo = Repository(self.db_path)
        Repository(self.db_path)
        profile = UserPortfolioProfile(
            account_equity=500000.0,
            available_cash=120000.0,
            risk_mode=RiskMode.BALANCED,
            updated_at=datetime.now(timezone.utc),
        )
        repo.save_portfolio_profile(profile)
        loaded = repo.load_portfolio_profile()
        self.assertEqual(loaded, profile)
        conn = sqlite3.connect(self.db_path)
        tables = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='portfolio_profile'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(tables, 1)

    def test_existing_database_upgrade_preserves_positions(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE positions (id TEXT PRIMARY KEY, symbol TEXT NOT NULL, market TEXT,"
            " shares REAL, cost REAL, added_at TEXT, closed_at TEXT)"
        )
        conn.execute(
            "INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
            ("old", "000001.SZ", "A", 1000, 11.2, datetime.now(timezone.utc).isoformat(), None),
        )
        conn.commit()
        conn.close()
        repo = Repository(self.db_path)
        self.assertEqual(repo.get_position("old").cost, 11.2)
        self.assertIsNone(repo.load_portfolio_profile())

    def test_position_create_update_delete_and_duplicate_conflict(self):
        repo = Repository(self.db_path)
        added_at = datetime.now(timezone.utc)
        position = repo.create_position(
            symbol="000001.SZ",
            market=T.Market.A,
            shares=1000,
            average_cost=11.2,
            added_at=added_at,
        )
        self.assertEqual(repo.get_position(position.id).cost, 11.2)
        with self.assertRaises(RepositoryConflictError):
            repo.create_position(
                symbol="000001.SZ",
                market=T.Market.A,
                shares=100,
                average_cost=10.0,
                added_at=added_at,
            )
        updated = repo.update_position(position.id, shares=1200, average_cost=11.35)
        self.assertEqual((updated.shares, updated.cost), (1200, 11.35))
        self.assertTrue(repo.delete_position(position.id))
        self.assertIsNone(repo.get_position(position.id))
        self.assertFalse(repo.delete_position(position.id))

    def test_repository_rejects_invalid_direct_position_inputs(self):
        repo = Repository(self.db_path)
        added_at = datetime.now(timezone.utc)
        invalid_cases = (
            {"symbol": "aapl.us", "market": T.Market.US, "shares": 1, "average_cost": 100.0, "added_at": added_at},
            {"symbol": "AAPL.US", "market": T.Market.A, "shares": 1, "average_cost": 100.0, "added_at": added_at},
            {"symbol": "AAPL.US", "market": T.Market.US, "shares": True, "average_cost": 100.0, "added_at": added_at},
            {"symbol": "AAPL.US", "market": T.Market.US, "shares": 1, "average_cost": float("nan"), "added_at": added_at},
            {"symbol": "AAPL.US", "market": T.Market.US, "shares": 1, "average_cost": 100.0, "added_at": datetime.now()},
        )
        for values in invalid_cases:
            with self.subTest(values=values):
                with self.assertRaises(RepositoryValidationError):
                    repo.create_position(**values)

    def test_repository_accepts_odd_lot_position_facts(self):
        repo = Repository(self.db_path)
        position = repo.create_position(
            symbol="600000.SH",
            market=T.Market.A,
            shares=37,
            average_cost=9.8,
            added_at=datetime.now(timezone.utc),
        )
        self.assertEqual(position.shares, 37)
        updated = repo.update_position(position.id, shares=13)
        self.assertEqual(updated.shares, 13)

    def test_update_rejects_invalid_values(self):
        repo = Repository(self.db_path)
        position = repo.create_position(
            symbol="AAPL.US",
            market=T.Market.US,
            shares=1,
            average_cost=100.0,
            added_at=datetime.now(timezone.utc),
        )
        for values in ({"shares": True}, {"shares": 0}, {"average_cost": float("inf")}):
            with self.subTest(values=values):
                with self.assertRaises(RepositoryValidationError):
                    repo.update_position(position.id, **values)

    def test_save_positions_remains_compatible(self):
        repo = Repository(self.db_path)
        item = T.Position(
            id="legacy",
            symbol="600000.SH",
            market=T.Market.A,
            shares=100,
            cost=9.8,
            added_at=datetime.now(timezone.utc),
        )
        repo.save_positions([item])
        self.assertEqual(repo.load_positions()[0].cost, 9.8)

    def test_temporary_database_is_removable(self):
        repo = Repository(self.db_path)
        repo.load_positions()
        close_all()
        os.remove(self.db_path)
        self.assertFalse(os.path.exists(self.db_path))


if __name__ == "__main__":
    unittest.main()

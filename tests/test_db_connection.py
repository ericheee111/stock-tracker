from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from stock_tracker.storage.db import close_all, get_connection


class TestThreadLocalConnectionLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        close_all()

    def tearDown(self) -> None:
        close_all()

    def test_switching_database_closes_previous_connection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first_path = Path(directory) / "first.db"
            second_path = Path(directory) / "second.db"
            first = get_connection(str(first_path))
            first.execute("SELECT 1").fetchone()

            second = get_connection(str(second_path))

            self.assertIsNot(first, second)
            with self.assertRaises(sqlite3.ProgrammingError):
                first.execute("SELECT 1")
            self.assertEqual(second.execute("SELECT 1").fetchone()[0], 1)
            close_all()

    def test_relative_and_absolute_paths_share_one_connection(self) -> None:
        with tempfile.TemporaryDirectory(dir=".") as directory:
            relative = Path(directory) / "same.db"
            absolute = relative.resolve()
            first = get_connection(str(relative))
            second = get_connection(str(absolute))
            self.assertIs(first, second)
            close_all()


if __name__ == "__main__":
    unittest.main()

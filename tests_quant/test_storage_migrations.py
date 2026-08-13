from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from stock_tracker.quant.storage import (
    Migration,
    MigrationContractError,
    MigrationState,
    apply_connection,
    apply_database,
    iter_sql_statements,
    load_migrations,
    plan_connection,
    plan_database,
)


class TestMigrationDiscovery(unittest.TestCase):
    def test_two_ordered_checksum_verified_migrations_exist(self) -> None:
        migrations = load_migrations()
        self.assertEqual(tuple(item.version for item in migrations), (1, 2))
        self.assertTrue(all(len(item.checksum) == 64 for item in migrations))
        self.assertTrue(all(iter_sql_statements(item.sql) for item in migrations))

    def test_missing_database_dry_plan_does_not_create_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.db"
            plan = plan_database(path)
            self.assertFalse(path.exists())
            self.assertEqual(len(plan.pending), 2)

    def test_existing_database_dry_plan_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "existing.db"
            with sqlite3.connect(path):
                pass
            before_stat = path.stat()
            before_entries = tuple(sorted(item.name for item in path.parent.iterdir()))
            plan = plan_database(path)
            after_stat = path.stat()
            after_entries = tuple(sorted(item.name for item in path.parent.iterdir()))
            self.assertEqual(len(plan.pending), 2)
            self.assertEqual(before_stat.st_size, after_stat.st_size)
            self.assertEqual(before_stat.st_mtime_ns, after_stat.st_mtime_ns)
            self.assertEqual(before_entries, after_entries)


class TestMigrationApply(unittest.TestCase):
    def test_apply_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quant.db"
            first = apply_database(path)
            second = apply_database(path)
            self.assertEqual(len(first.pending), 0)
            self.assertEqual(len(second.pending), 0)
            self.assertEqual(len(second.applied), 2)

    def test_expected_tables_and_foreign_keys_exist(self) -> None:
        with sqlite3.connect(":memory:") as connection:
            plan = apply_connection(connection)
            self.assertEqual(len(plan.applied), 2)
            table_names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            expected = {
                "quant_schema_migration",
                "pit_fact",
                "quant_label",
                "quant_model_registry_event",
                "quant_experiment_event",
                "quant_holdout",
                "quant_data_artifact",
                "quant_data_snapshot",
                "quant_data_snapshot_artifact",
                "quant_calendar_coverage",
                "quant_calendar_day",
                "quant_instrument_session_status",
            }
            self.assertTrue(expected.issubset(table_names))
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO quant_data_snapshot_artifact(
                        snapshot_id, artifact_id, ordinal
                    ) VALUES (?, ?, 0)
                    """,
                    ("a" * 64, "b" * 64),
                )

    def test_append_only_trigger_blocks_update_and_delete(self) -> None:
        with sqlite3.connect(":memory:") as connection:
            apply_connection(connection)
            connection.execute(
                """
                INSERT INTO pit_fact(
                    fact_id, namespace, entity_id, field_name,
                    event_time, known_at, usable_from,
                    revision_kind, revision_value, payload_json,
                    source, verified, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "a" * 64,
                    "fixture",
                    "600000.SH",
                    "field",
                    "2025-01-01T00:00:00+00:00",
                    "2025-01-02T00:00:00+00:00",
                    "2025-01-02T00:00:00+00:00",
                    "INTEGER",
                    "1",
                    "{}",
                    "fixture",
                    1,
                    "2025-01-02T00:00:00+00:00",
                ),
            )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                connection.execute(
                    "UPDATE pit_fact SET payload_json = '{}' WHERE fact_id = ?",
                    ("a" * 64,),
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                connection.execute(
                    "DELETE FROM pit_fact WHERE fact_id = ?",
                    ("a" * 64,),
                )

    def test_noncanonical_integer_revision_rejected_by_sql(self) -> None:
        with sqlite3.connect(":memory:") as connection:
            apply_connection(connection)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO pit_fact(
                        fact_id, namespace, entity_id, field_name,
                        event_time, known_at, usable_from,
                        revision_kind, revision_value, payload_json,
                        source, verified, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "b" * 64,
                        "fixture",
                        "600000.SH",
                        "field",
                        "2025-01-01T00:00:00+00:00",
                        "2025-01-02T00:00:00+00:00",
                        "2025-01-02T00:00:00+00:00",
                        "INTEGER",
                        "01",
                        "{}",
                        "fixture",
                        1,
                        "2025-01-02T00:00:00+00:00",
                    ),
                )

    def test_failed_migration_rolls_back_all_its_statements(self) -> None:
        real = load_migrations()
        sql = "CREATE TABLE quant_should_rollback(id INTEGER);\nTHIS IS INVALID;\n"
        bad = Migration(
            version=3,
            name="intentional_failure",
            path=Path("0003_intentional_failure.sql"),
            sql=sql,
            checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
        )
        with sqlite3.connect(":memory:") as connection:
            apply_connection(connection, migrations=real)
            with self.assertRaises(sqlite3.Error):
                apply_connection(connection, migrations=(*real, bad))
            row = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'quant_should_rollback'"
            ).fetchone()
            self.assertIsNone(row)
            self.assertEqual(len(plan_connection(connection, migrations=real).applied), 2)

    def test_history_checksum_tamper_is_detected(self) -> None:
        with sqlite3.connect(":memory:") as connection:
            apply_connection(connection)
            connection.execute("DROP TRIGGER quant_schema_migration_no_update")
            connection.execute(
                "UPDATE quant_schema_migration SET checksum = ? WHERE version = 1",
                ("f" * 64,),
            )
            connection.commit()
            with self.assertRaisesRegex(MigrationContractError, "does not match"):
                plan_connection(connection)


class TestMigrationCli(unittest.TestCase):
    @staticmethod
    def run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "scripts/quant_migrate.py", *arguments],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_cli_is_dry_run_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "quant.db"
            result = self.run_cli("--database", str(database))
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["mode"], "DRY_RUN")
            self.assertFalse(payload["database_modified"])
            self.assertFalse(database.exists())

    def test_cli_requires_explicit_apply_to_create_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "quant.db"
            result = self.run_cli("--database", str(database), "--apply")
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["mode"], "APPLY")
            self.assertTrue(payload["database_modified"])
            self.assertTrue(database.exists())
            self.assertTrue(
                all(
                    item["state"] == MigrationState.APPLIED.value
                    for item in payload["migrations"]
                )
            )


if __name__ == "__main__":
    unittest.main()

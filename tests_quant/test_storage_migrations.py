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
    def test_three_ordered_checksum_verified_migrations_exist(self) -> None:
        migrations = load_migrations()
        self.assertEqual(tuple(item.version for item in migrations), (1, 2, 3))
        self.assertTrue(all(len(item.checksum) == 64 for item in migrations))
        self.assertTrue(all(iter_sql_statements(item.sql) for item in migrations))

    def test_missing_database_dry_plan_does_not_create_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.db"
            plan = plan_database(path)
            self.assertFalse(path.exists())
            self.assertEqual(len(plan.pending), 3)

    def test_existing_database_dry_plan_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "existing.db"
            sqlite3.connect(path).close()
            before_stat = path.stat()
            before_entries = tuple(sorted(item.name for item in path.parent.iterdir()))
            plan = plan_database(path)
            after_stat = path.stat()
            after_entries = tuple(sorted(item.name for item in path.parent.iterdir()))
            self.assertEqual(len(plan.pending), 3)
            self.assertEqual(before_stat.st_size, after_stat.st_size)
            self.assertEqual(before_stat.st_mtime_ns, after_stat.st_mtime_ns)
            self.assertEqual(before_entries, after_entries)

    def test_lf_and_crlf_have_the_same_canonical_checksum(self) -> None:
        sql = "CREATE TABLE fixture(id INTEGER);\nINSERT INTO fixture VALUES (1);\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lf = root / "lf"
            crlf = root / "crlf"
            lf.mkdir()
            crlf.mkdir()
            (lf / "0001_fixture.sql").write_bytes(sql.encode("utf-8"))
            (crlf / "0001_fixture.sql").write_bytes(
                sql.replace("\n", "\r\n").encode("utf-8")
            )
            lf_migration = load_migrations(lf)[0]
            crlf_migration = load_migrations(crlf)[0]
            self.assertEqual(lf_migration.checksum, crlf_migration.checksum)
            self.assertEqual(lf_migration.sql, crlf_migration.sql)
            self.assertIn(
                hashlib.sha256(sql.encode("utf-8")).hexdigest(),
                lf_migration.accepted_checksums,
            )
            self.assertIn(
                hashlib.sha256(
                    sql.replace("\n", "\r\n").encode("utf-8")
                ).hexdigest(),
                lf_migration.accepted_checksums,
            )


class TestMigrationApply(unittest.TestCase):
    def test_apply_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quant.db"
            first = apply_database(path)
            second = apply_database(path)
            self.assertEqual(len(first.pending), 0)
            self.assertEqual(len(second.pending), 0)
            self.assertEqual(len(second.applied), 3)

    def test_expected_tables_and_foreign_keys_exist(self) -> None:
        with sqlite3.connect(":memory:") as connection:
            plan = apply_connection(connection)
            self.assertEqual(len(plan.applied), 3)
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
                "quant_universe_coverage",
                "quant_instrument_identity",
                "quant_security_status",
                "quant_universe_membership",
            }
            self.assertTrue(expected.issubset(table_names))
            for table in (
                "quant_calendar_coverage",
                "quant_calendar_day",
                "quant_instrument_session_status",
            ):
                columns = {
                    row[1]
                    for row in connection.execute(f"PRAGMA table_info({table})")
                }
                self.assertIn("usable_from", columns)
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

    def test_stage2_universe_tables_are_append_only_and_status_safe(self) -> None:
        with sqlite3.connect(":memory:") as connection:
            apply_connection(connection)
            connection.execute(
                """
                INSERT INTO quant_universe_coverage(
                    coverage_id, universe_id, market, start_date, end_date,
                    source, universe_version, known_at, usable_from,
                    revision_kind, revision_value, verified, complete,
                    source_note, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "c" * 64,
                    "A_ALL_COMMON_EQUITY",
                    "A",
                    "2025-01-01",
                    "2025-12-31",
                    "fixture",
                    "fixture-v1",
                    "2025-01-01T00:00:00+00:00",
                    "2025-01-01T00:00:00+00:00",
                    "INTEGER",
                    "1",
                    1,
                    1,
                    "synthetic fixture only",
                    "{}",
                ),
            )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                connection.execute(
                    "UPDATE quant_universe_coverage SET complete = 0 WHERE coverage_id = ?",
                    ("c" * 64,),
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                connection.execute(
                    "DELETE FROM quant_universe_coverage WHERE coverage_id = ?",
                    ("c" * 64,),
                )
            calendar_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(quant_calendar_day)")
            }
            self.assertIn("supersedes_revision_kind", calendar_columns)
            self.assertIn("supersedes_revision_value", calendar_columns)
            connection.execute(
                """
                INSERT INTO quant_calendar_coverage(
                    coverage_id, market, start_date, end_date, source,
                    calendar_version, known_at, revision_kind, revision_value,
                    verified, source_note, payload_json, usable_from
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "e" * 64,
                    "A",
                    "2025-01-02",
                    "2025-01-02",
                    "fixture",
                    "fixture-calendar-v1",
                    "2025-01-01T00:00:00+00:00",
                    "STRING",
                    "annual-r1",
                    0,
                    "synthetic fixture only",
                    "{}",
                    "2025-01-01T00:00:00+00:00",
                ),
            )
            connection.execute(
                """
                INSERT INTO quant_calendar_day(
                    day_id, coverage_id, market, session_date, status,
                    open_time, close_time, session_kind, known_at, source,
                    calendar_version, revision_kind, revision_value,
                    verified, source_note, payload_json, usable_from,
                    supersedes_revision_kind, supersedes_revision_value
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "f" * 64,
                    "e" * 64,
                    "A",
                    "2025-01-02",
                    "CLOSED",
                    None,
                    None,
                    "REGULAR",
                    "2025-01-01T00:00:00+00:00",
                    "fixture/SSE_OFFICIAL_NOTICE_DETAIL",
                    "fixture-calendar-v1",
                    "STRING",
                    "r2",
                    0,
                    "synthetic fixture only",
                    "{}",
                    "2025-01-01T00:00:00+00:00",
                    "STRING",
                    "annual-r1",
                ),
            )
            persisted_predecessor = connection.execute(
                """
                SELECT supersedes_revision_kind, supersedes_revision_value
                FROM quant_calendar_day WHERE day_id = ?
                """,
                ("f" * 64,),
            ).fetchone()
            self.assertEqual(persisted_predecessor, ("STRING", "annual-r1"))
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                connection.execute(
                    """
                    UPDATE quant_calendar_day
                    SET supersedes_revision_value = 'different-r1'
                    WHERE day_id = ?
                    """,
                    ("f" * 64,),
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                connection.execute(
                    "DELETE FROM quant_calendar_day WHERE day_id = ?",
                    ("f" * 64,),
                )

            with self.assertRaisesRegex(sqlite3.IntegrityError, "supersedes revision"):
                connection.execute(
                    """
                    INSERT INTO quant_calendar_day(
                        day_id, coverage_id, market, session_date, status,
                        open_time, close_time, session_kind, known_at, source,
                        calendar_version, revision_kind, revision_value,
                        verified, source_note, payload_json, usable_from,
                        supersedes_revision_kind, supersedes_revision_value
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "g" * 64,
                        "e" * 64,
                        "A",
                        "2025-01-02",
                        "CLOSED",
                        None,
                        None,
                        "REGULAR",
                        "2025-01-01T00:00:00+00:00",
                        "fixture",
                        "fixture-calendar-v1",
                        "STRING",
                        "r2",
                        0,
                        "synthetic fixture only",
                        "{}",
                        "2025-01-01T00:00:00+00:00",
                        "STRING",
                        None,
                    ),
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "supersedes revision"):
                connection.execute(
                    """
                    INSERT INTO quant_calendar_day(
                        day_id, coverage_id, market, session_date, status,
                        open_time, close_time, session_kind, known_at, source,
                        calendar_version, revision_kind, revision_value,
                        verified, source_note, payload_json, usable_from,
                        supersedes_revision_kind, supersedes_revision_value
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "h" * 64,
                        "e" * 64,
                        "A",
                        "2025-01-02",
                        "CLOSED",
                        None,
                        None,
                        "REGULAR",
                        "2025-01-01T00:00:00+00:00",
                        "fixture",
                        "fixture-calendar-v1",
                        "STRING",
                        "r3",
                        0,
                        "synthetic fixture only",
                        "{}",
                        "2025-01-01T00:00:00+00:00",
                        "INTEGER",
                        "01",
                    ),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO quant_security_status(
                        status_id, instrument_id, symbol, market, session_date,
                        listing_state, trading_state, risk_designation,
                        known_at, usable_from, source,
                        revision_kind, revision_value, verified,
                        source_note, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "d" * 64,
                        "SSE:fixture:600000",
                        "600000.SH",
                        "A",
                        "2025-01-02",
                        "DELISTED",
                        "TRADABLE",
                        "NORMAL",
                        "2025-01-02T00:00:00+00:00",
                        "2025-01-02T00:00:00+00:00",
                        "fixture",
                        "INTEGER",
                        "1",
                        1,
                        "synthetic fixture only",
                        "{}",
                    ),
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
            version=4,
            name="intentional_failure",
            path=Path("0004_intentional_failure.sql"),
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
            self.assertEqual(
                len(plan_connection(connection, migrations=real).applied),
                len(real),
            )

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

    def test_legacy_crlf_checksum_history_is_accepted(self) -> None:
        migrations = load_migrations()
        first = migrations[0]
        crlf_checksum = hashlib.sha256(
            first.sql.replace("\n", "\r\n").encode("utf-8")
        ).hexdigest()
        self.assertIn(crlf_checksum, first.accepted_checksums)
        with sqlite3.connect(":memory:") as connection:
            apply_connection(connection, migrations=migrations)
            connection.execute("DROP TRIGGER quant_schema_migration_no_update")
            connection.execute(
                "UPDATE quant_schema_migration SET checksum = ? WHERE version = ?",
                (crlf_checksum, first.version),
            )
            connection.commit()
            plan = plan_connection(connection, migrations=migrations)
            self.assertEqual(len(plan.applied), len(migrations))

    def test_non_line_ending_change_is_not_accepted(self) -> None:
        migrations = load_migrations()
        first = migrations[0]
        changed_checksum = hashlib.sha256(
            (first.sql + "-- semantic identity changed\n").encode("utf-8")
        ).hexdigest()
        self.assertNotIn(changed_checksum, first.accepted_checksums)


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

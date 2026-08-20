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
    def test_four_ordered_checksum_verified_migrations_exist(self) -> None:
        migrations = load_migrations()
        self.assertEqual(tuple(item.version for item in migrations), (1, 2, 3, 4))
        self.assertTrue(all(len(item.checksum) == 64 for item in migrations))
        self.assertTrue(all(iter_sql_statements(item.sql) for item in migrations))

    def test_missing_database_dry_plan_does_not_create_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.db"
            plan = plan_database(path)
            self.assertFalse(path.exists())
            self.assertEqual(len(plan.pending), 4)

    def test_existing_database_dry_plan_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "existing.db"
            sqlite3.connect(path).close()
            before_stat = path.stat()
            before_entries = tuple(sorted(item.name for item in path.parent.iterdir()))
            plan = plan_database(path)
            after_stat = path.stat()
            after_entries = tuple(sorted(item.name for item in path.parent.iterdir()))
            self.assertEqual(len(plan.pending), 4)
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
            self.assertEqual(len(second.applied), 4)

    def test_expected_tables_and_foreign_keys_exist(self) -> None:
        with sqlite3.connect(":memory:") as connection:
            plan = apply_connection(connection)
            self.assertEqual(len(plan.applied), 4)
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
                "quant_corporate_action_coverage",
                "quant_corporate_action_fact",
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

    def test_stage2b_corporate_action_tables_are_append_only_and_term_safe(self) -> None:
        with sqlite3.connect(":memory:") as connection:
            apply_connection(connection)
            connection.execute(
                """
                INSERT INTO quant_instrument_identity(
                    identity_id, instrument_id, symbol, market, exchange,
                    security_type, effective_from, effective_to,
                    known_at, usable_from, source,
                    revision_kind, revision_value, verified,
                    source_note, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "a" * 64,
                    "CN:SSE:fixture-security-1",
                    "600000.SH",
                    "A",
                    "SSE",
                    "COMMON_EQUITY",
                    "2020-01-01",
                    None,
                    "2024-12-01T00:00:00+00:00",
                    "2024-12-01T00:00:00+00:00",
                    "fixture-identity",
                    "STRING",
                    "identity-r1",
                    1,
                    "synthetic fixture identity",
                    "{}",
                ),
            )
            connection.execute(
                """
                INSERT INTO quant_corporate_action_coverage(
                    coverage_id, instrument_id, market, start_date, end_date,
                    source, action_version, known_at, usable_from,
                    revision_kind, revision_value,
                    supersedes_revision_kind, supersedes_revision_value,
                    verified, complete, source_note, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "b" * 64,
                    "CN:SSE:fixture-security-1",
                    "A",
                    "2025-01-01",
                    "2025-01-31",
                    "fixture-corporate-actions",
                    "fixture-corporate-actions-v1",
                    "2025-01-31T00:00:00+00:00",
                    "2025-01-31T00:00:00+00:00",
                    "STRING",
                    "coverage-r2",
                    "STRING",
                    "coverage-r1",
                    1,
                    1,
                    "synthetic complete action coverage",
                    "{}",
                ),
            )
            connection.execute(
                """
                INSERT INTO quant_corporate_action_fact(
                    fact_id, action_id, instrument_id, identity_fact_id,
                    symbol, market, ex_date, record_date, payment_date,
                    share_listing_date, lifecycle,
                    automatic_share_ratio, cash_dividend_per_share,
                    rights_entitlement_ratio, rights_subscription_price,
                    currency, reference_price, reference_price_snapshot_id,
                    known_at, usable_from,
                    source, action_version, revision_kind, revision_value,
                    supersedes_revision_kind, supersedes_revision_value,
                    verified, source_note, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "c" * 64,
                    "action-plan-1",
                    "CN:SSE:fixture-security-1",
                    "a" * 64,
                    "600000.SH",
                    "A",
                    "2025-01-15",
                    "2025-01-14",
                    "2025-01-20",
                    "2025-01-15",
                    "EFFECTIVE",
                    "2",
                    "0",
                    "0",
                    None,
                    None,
                    None,
                    None,
                    "2025-01-14T00:00:00+00:00",
                    "2025-01-15T00:00:00+00:00",
                    "fixture-corporate-actions",
                    "fixture-corporate-actions-v1",
                    "STRING",
                    "action-r2",
                    "STRING",
                    "action-r1",
                    1,
                    "synthetic corporate action",
                    "{}",
                ),
            )
            persisted = connection.execute(
                """
                SELECT lifecycle, automatic_share_ratio,
                       supersedes_revision_kind, supersedes_revision_value
                FROM quant_corporate_action_fact WHERE fact_id = ?
                """,
                ("c" * 64,),
            ).fetchone()
            self.assertEqual(persisted, ("EFFECTIVE", "2", "STRING", "action-r1"))
            for statement in (
                "UPDATE quant_corporate_action_fact SET revision_value = 'r3' WHERE fact_id = ?",
                "DELETE FROM quant_corporate_action_fact WHERE fact_id = ?",
                "UPDATE quant_corporate_action_coverage SET complete = 0 WHERE coverage_id = ?",
                "DELETE FROM quant_corporate_action_coverage WHERE coverage_id = ?",
            ):
                target = "c" * 64 if "fact" in statement else "b" * 64
                with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                    connection.execute(statement, (target,))

            invalid_cancelled = [
                "d" * 64,
                "cancelled-plan",
                "CN:SSE:fixture-security-1",
                "a" * 64,
                "600000.SH",
                "A",
                "2025-01-16",
                None,
                None,
                None,
                "CANCELLED",
                "1",
                None,
                None,
                None,
                None,
                None,
                None,
                "2025-01-16T00:00:00+00:00",
                "2025-01-16T00:00:00+00:00",
                "fixture-corporate-actions",
                "fixture-corporate-actions-v1",
                "STRING",
                "cancel-r1",
                None,
                None,
                0,
                "synthetic cancellation",
                "{}",
            ]
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO quant_corporate_action_fact(
                        fact_id, action_id, instrument_id, identity_fact_id,
                        symbol, market, ex_date, record_date, payment_date,
                        share_listing_date, lifecycle,
                        automatic_share_ratio, cash_dividend_per_share,
                        rights_entitlement_ratio, rights_subscription_price,
                        currency, reference_price, reference_price_snapshot_id,
                        known_at, usable_from,
                        source, action_version, revision_kind, revision_value,
                        supersedes_revision_kind, supersedes_revision_value,
                        verified, source_note, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    invalid_cancelled,
                )

            invalid_rights = invalid_cancelled.copy()
            invalid_rights[0] = "e" * 64
            invalid_rights[1] = "rights-plan"
            invalid_rights[10] = "EFFECTIVE"
            invalid_rights[11] = "1"
            invalid_rights[12] = "0"
            invalid_rights[13] = "0.1"
            invalid_rights[14] = None
            invalid_rights[15] = "CNY"
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO quant_corporate_action_fact(
                        fact_id, action_id, instrument_id, identity_fact_id,
                        symbol, market, ex_date, record_date, payment_date,
                        share_listing_date, lifecycle,
                        automatic_share_ratio, cash_dividend_per_share,
                        rights_entitlement_ratio, rights_subscription_price,
                        currency, reference_price, reference_price_snapshot_id,
                        known_at, usable_from,
                        source, action_version, revision_kind, revision_value,
                        supersedes_revision_kind, supersedes_revision_value,
                        verified, source_note, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    invalid_rights,
                )

    def test_stage2b_sql_rejects_identity_time_term_and_provenance_bypass(self) -> None:
        action_sql = """
            INSERT INTO quant_corporate_action_fact(
                fact_id, action_id, instrument_id, identity_fact_id,
                symbol, market, ex_date, record_date, payment_date,
                share_listing_date, lifecycle,
                automatic_share_ratio, cash_dividend_per_share,
                rights_entitlement_ratio, rights_subscription_price,
                currency, reference_price, reference_price_snapshot_id,
                known_at, usable_from,
                source, action_version, revision_kind, revision_value,
                supersedes_revision_kind, supersedes_revision_value,
                verified, source_note, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        with sqlite3.connect(":memory:") as connection:
            apply_connection(connection)
            connection.execute(
                """
                INSERT INTO quant_instrument_identity(
                    identity_id, instrument_id, symbol, market, exchange,
                    security_type, effective_from, effective_to,
                    known_at, usable_from, source,
                    revision_kind, revision_value, verified,
                    source_note, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "a" * 64,
                    "CN:SSE:fixture-security-1",
                    "600000.SH",
                    "A",
                    "SSE",
                    "COMMON_EQUITY",
                    "2020-01-01",
                    None,
                    "2024-12-01T00:00:00+00:00",
                    "2024-12-01T00:00:00+00:00",
                    "fixture-identity",
                    "STRING",
                    "identity-r1",
                    1,
                    "synthetic fixture identity",
                    "{}",
                ),
            )
            base = [
                "f" * 64,
                "action-plan-sql-guard",
                "CN:SSE:fixture-security-1",
                "a" * 64,
                "600000.SH",
                "A",
                "2025-01-15",
                "2025-01-14",
                "2025-01-20",
                "2025-01-15",
                "EFFECTIVE",
                "2",
                "0",
                "0",
                None,
                None,
                None,
                None,
                "2025-01-14T00:00:00+00:00",
                "2025-01-15T00:00:00+00:00",
                "fixture-corporate-actions",
                "fixture-corporate-actions-v1",
                "STRING",
                "action-r1",
                None,
                None,
                0,
                "",
                "{}",
            ]
            cases: list[tuple[str, int, object]] = [
                ("noncanonical decimal", 11, "2.0"),
                ("identity mismatch", 4, "600001.SH"),
                ("usable before known", 19, "2025-01-13T00:00:00+00:00"),
                ("uppercase SHA", 0, "F" * 64),
                ("zero rights price", 14, "0"),
            ]
            for name, index, value in cases:
                row = base.copy()
                row[index] = value
                if name == "zero rights price":
                    row[13] = "0.1"
                    row[15] = "CNY"
                with self.subTest(name=name), self.assertRaises(
                    sqlite3.IntegrityError
                ):
                    connection.execute(action_sql, row)

            no_op = base.copy()
            no_op[11] = "1"
            no_op[9] = None
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(action_sql, no_op)

            self_superseding = base.copy()
            self_superseding[24] = "STRING"
            self_superseding[25] = "action-r1"
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(action_sql, self_superseding)

            verified_without_note = base.copy()
            verified_without_note[26] = 1
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(action_sql, verified_without_note)

            reference_without_canonical_snapshot = base.copy()
            reference_without_canonical_snapshot[15] = "CNY"
            reference_without_canonical_snapshot[16] = "10"
            reference_without_canonical_snapshot[17] = "A" * 64
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(action_sql, reference_without_canonical_snapshot)

            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO quant_corporate_action_coverage(
                        coverage_id, instrument_id, market, start_date, end_date,
                        source, action_version, known_at, usable_from,
                        revision_kind, revision_value,
                        supersedes_revision_kind, supersedes_revision_value,
                        verified, complete, source_note, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "b" * 64,
                        "CN:SSE:fixture-security-1",
                        "A",
                        "2025-01-01",
                        "2025-01-31",
                        "fixture-corporate-actions",
                        "fixture-corporate-actions-v1",
                        "2025-01-31T00:00:00+00:00",
                        "2025-01-30T00:00:00+00:00",
                        "STRING",
                        "coverage-r1",
                        None,
                        None,
                        1,
                        1,
                        "",
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
            version=5,
            name="intentional_failure",
            path=Path("0005_intentional_failure.sql"),
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

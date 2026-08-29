from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from scripts import ingest_outcome_ledger as ingest_cli
from scripts import report_outcome_ledger as report_cli
from stock_tracker.quant.storage.outcome_ledger import (
    OutcomeLedgerError,
    signal_outcome_to_json_bytes,
)
from tests_quant.test_outcomes import _BASE, _complete_outcome

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TestOutcomeLedgerCli(unittest.TestCase):
    def test_ingest_cli_has_no_caller_selected_ingestion_timestamp(self) -> None:
        with self.assertRaises(SystemExit):
            ingest_cli.build_parser().parse_args(
                [
                    "--input",
                    "outcome.json",
                    "--recorded-by",
                    "reviewed-import",
                    "--ingested-at",
                    "2026-08-01T00:00:00Z",
                ]
            )

    def test_report_cli_has_no_caller_selected_generation_timestamp(self) -> None:
        with self.assertRaises(SystemExit):
            report_cli.build_parser().parse_args(
                [
                    "--output-dir",
                    "reports",
                    "--strategy-id",
                    "S1_BREAKOUT",
                    "--strategy-version",
                    "v1",
                    "--market",
                    "A",
                    "--horizon-sessions",
                    "20",
                    "--evidence-tier",
                    "OPERATIONAL_VERIFIED",
                    "--window-start",
                    "2026-01-01T00:00:00Z",
                    "--window-end",
                    "2026-01-31T00:00:00Z",
                    "--as-of",
                    "2026-02-01T00:00:00Z",
                    "--generated-at",
                    "2026-02-02T00:00:00Z",
                ]
            )

    @staticmethod
    def _guarded_environment(root: Path) -> dict[str, str]:
        guard = root / "sqlite-guard"
        guard.mkdir()
        (guard / "sitecustomize.py").write_text(
            "import pathlib, sqlite3\n"
            "_real_connect = sqlite3.connect\n"
            "def _guard(database, *args, **kwargs):\n"
            "    text = str(database).replace('\\\\', '/').lower()\n"
            "    if text.endswith('/data/stock_tracker.db'):\n"
            "        raise RuntimeError('STAGE4F_PRODUCTION_DB_FORBIDDEN')\n"
            "    return _real_connect(database, *args, **kwargs)\n"
            "sqlite3.connect = _guard\n"
            "sqlite3.dbapi2.connect = _guard\n",
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join((str(guard), str(_PROJECT_ROOT)))
        return environment

    def test_ingest_and_report_subprocess_are_idempotent_and_production_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).absolute()
            production = _PROJECT_ROOT / "data" / "stock_tracker.db"
            before = production.stat().st_size if production.exists() else None
            input_one = root / "outcome-one.json"
            input_two = root / "outcome-two.json"
            input_one.write_bytes(
                signal_outcome_to_json_bytes(
                    _complete_outcome(signal_suffix="cli-one")
                )
            )
            input_two.write_bytes(
                signal_outcome_to_json_bytes(
                    _complete_outcome(
                        signal_suffix="cli-two",
                        recorded_offset_days=1,
                    )
                )
            )
            record_root = root / "records"
            catalog = root / "ledger.db"
            environment = self._guarded_environment(root)

            base_ingest = [
                sys.executable,
                str(_PROJECT_ROOT / "scripts" / "ingest_outcome_ledger.py"),
                "--record-root",
                str(record_root),
                "--catalog",
                str(catalog),
                "--recorded-by",
                "cli-reviewed-import",
            ]
            first = subprocess.run(
                [
                    *base_ingest,
                    "--input",
                    str(input_one),
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(first.returncode, 0, msg=first.stderr)
            first_payload = json.loads(first.stdout)
            self.assertEqual(first_payload["disposition"], "APPENDED")
            self.assertEqual(first_payload["lane"], "LIVE_CANDIDATE")
            self.assertTrue(first_payload["outcome_contract_eligible"])
            self.assertFalse(first_payload["trusted_ledger_admitted"])
            self.assertFalse(first_payload["trusted_outcome_authority_configured"])
            self.assertFalse(first_payload["investment_performance_claim"])
            self.assertFalse(first_payload["production_database_modified"])

            retried = subprocess.run(
                [
                    *base_ingest,
                    "--input",
                    str(input_one),
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(retried.returncode, 0, msg=retried.stderr)
            retry_payload = json.loads(retried.stdout)
            self.assertEqual(retry_payload["disposition"], "IDEMPOTENT")
            self.assertEqual(retry_payload["ledger_record_count"], 1)

            second = subprocess.run(
                [
                    *base_ingest,
                    "--input",
                    str(input_two),
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(second.returncode, 0, msg=second.stderr)
            self.assertEqual(json.loads(second.stdout)["ledger_record_count"], 2)

            report_root = root / "reports"
            report_as_of = datetime.now(timezone.utc)
            report_window_end = report_as_of - timedelta(hours=1)
            reported = subprocess.run(
                [
                    sys.executable,
                    str(_PROJECT_ROOT / "scripts" / "report_outcome_ledger.py"),
                    "--record-root",
                    str(record_root),
                    "--catalog",
                    str(catalog),
                    "--output-dir",
                    str(report_root),
                    "--strategy-id",
                    "S1_BREAKOUT",
                    "--strategy-version",
                    "v1",
                    "--market",
                    "A",
                    "--horizon-sessions",
                    "20",
                    "--model-id",
                    "model-v1",
                    "--evidence-tier",
                    "OPERATIONAL_VERIFIED",
                    "--window-start",
                    (_BASE - timedelta(days=1)).isoformat(),
                    "--window-end",
                    report_window_end.isoformat(),
                    "--as-of",
                    report_as_of.isoformat(),
                    "--minimum-real-samples",
                    "2",
                    "--minimum-bucket-samples",
                    "1",
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(reported.returncode, 0, msg=reported.stderr)
            report_payload = json.loads(reported.stdout)
            self.assertEqual(report_payload["ledger_record_count"], 2)
            self.assertEqual(report_payload["candidate_record_count"], 2)
            self.assertEqual(report_payload["outcome_contract_eligible_count"], 2)
            self.assertEqual(report_payload["trusted_admitted_record_count"], 0)
            self.assertEqual(report_payload["eligible_real_sample_count"], 0)
            self.assertEqual(
                report_payload["scoreboard_state"],
                "INSUFFICIENT_REAL_EVIDENCE",
            )
            self.assertFalse(report_payload["metrics_available"])
            self.assertIn(
                "TRUSTED_OUTCOME_ADMISSION_NOT_CONFIGURED",
                report_payload["admission_blockers"],
            )
            self.assertFalse(report_payload["trusted_outcome_authority_configured"])
            self.assertFalse(report_payload["investment_performance_claim"])
            self.assertFalse(report_payload["auto_promote_model"])
            self.assertFalse(report_payload["auto_change_strategy_weight"])
            self.assertFalse(report_payload["auto_trade"])
            report_json = Path(report_payload["json_output"])
            report_markdown = Path(report_payload["markdown_output"])
            self.assertTrue(report_json.is_file())
            self.assertTrue(report_markdown.is_file())
            self.assertFalse(
                json.loads(report_json.read_text(encoding="utf-8"))[
                    "investment_performance_claim"
                ]
            )
            self.assertIn(
                "TRUSTED_OUTCOME_ADMISSION_NOT_CONFIGURED",
                report_markdown.read_text(encoding="utf-8"),
            )
            after = production.stat().st_size if production.exists() else None
            self.assertEqual(after, before)

    def test_cli_path_guards_reject_production_overlap_and_linked_input(self) -> None:
        production = _PROJECT_ROOT / "data" / "stock_tracker.db"
        with self.assertRaisesRegex(OutcomeLedgerError, "production database"):
            ingest_cli._validate_paths(
                production,
                Path("data/outcome-ledger-records"),
                Path("data/outcome-ledger.db"),
            )

        with tempfile.TemporaryDirectory() as directory:
            source_root = Path(directory).absolute()
            absent_production = source_root / "data" / "stock_tracker.db"
            record_root = source_root / "records"
            record_root.mkdir()
            catalog = source_root / "catalog.db"
            catalog.write_bytes(b"catalog-fixture")
            with patch.object(
                ingest_cli,
                "PROJECT_ROOT",
                source_root,
            ), self.assertRaisesRegex(
                OutcomeLedgerError,
                "production database",
            ):
                ingest_cli._validate_paths(
                    absent_production,
                    record_root,
                    catalog,
                )
            with patch.object(report_cli, "PROJECT_ROOT", source_root):
                for guarded_root, guarded_catalog in (
                    (absent_production, catalog),
                    (record_root, absent_production),
                ):
                    with self.subTest(
                        guarded_root=guarded_root,
                        guarded_catalog=guarded_catalog,
                    ), self.assertRaisesRegex(
                        OutcomeLedgerError,
                        "production database",
                    ):
                        report_cli._validate_paths(
                            guarded_root,
                            guarded_catalog,
                            source_root / "reports",
                        )

        with tempfile.TemporaryDirectory() as alias_directory:
            alias = Path(alias_directory) / "production-alias.db"
            try:
                os.link(production, alias)
            except OSError:
                pass
            else:
                with self.assertRaisesRegex(OutcomeLedgerError, "production database"):
                    ingest_cli._validate_paths(
                        alias,
                        Path(alias_directory) / "records",
                        Path(alias_directory) / "catalog.db",
                    )
                record_root = Path(alias_directory) / "report-records"
                record_root.mkdir()
                with self.assertRaisesRegex(OutcomeLedgerError, "production database"):
                    report_cli._validate_paths(
                        record_root,
                        alias,
                        Path(alias_directory) / "reports",
                    )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).absolute()
            source = root / "outcome.json"
            source.write_text("{}", encoding="utf-8")
            with patch.object(
                ingest_cli,
                "_is_link",
                side_effect=lambda path: path == source,
            ), self.assertRaisesRegex(OutcomeLedgerError, "symlink or junction"):
                ingest_cli._validate_paths(
                    source,
                    root / "records",
                    root / "catalog.db",
                )

            record_root = root / "records"
            record_root.mkdir()
            catalog = root / "catalog.db"
            catalog.write_bytes(b"not-used")
            with self.assertRaisesRegex(OutcomeLedgerError, "separate"):
                report_cli._validate_paths(
                    record_root,
                    catalog,
                    record_root / "reports",
                )


if __name__ == "__main__":
    unittest.main()

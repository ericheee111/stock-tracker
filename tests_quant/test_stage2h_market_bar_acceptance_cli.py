from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from scripts import build_stage2h_market_bar_acceptance as builder
from scripts import report_stage2h_market_bar_acceptance as reporter
from stock_tracker.quant.data import (
    MarketBarAcceptanceError,
    materialize_golden_case,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_GOLDEN_MANIFEST = (
    _PROJECT_ROOT
    / "tests_quant"
    / "fixtures"
    / "market_bar_golden"
    / "v2"
    / "manifest.json"
)


class TestStage2HMarketBarAcceptanceCli(unittest.TestCase):
    def test_offline_registries_include_hithink_without_environment_key(self) -> None:
        environment = os.environ.copy()
        environment.pop("HITHINK_FINANCE_API_KEY", None)
        with patch.dict(os.environ, environment, clear=True):
            builder_registry = builder._registry()
            reporter_registry = reporter._registry()
        for registry in (builder_registry, reporter_registry):
            self.assertEqual(
                set(registry),
                {"eastmoney", "hithink_finance", "tencent"},
            )
            binding = registry["hithink_finance"]
            self.assertEqual(binding.source, "hithink_finance")
            self.assertEqual(
                binding.schema_version,
                "hithink-a-share-prices-historical-v1",
            )
            self.assertEqual(binding.parser_version, "hithink-bars-v1")

    @staticmethod
    def _descriptor_keys(artifact_root: Path) -> dict[str, str]:
        result: dict[str, str] = {}
        for path in artifact_root.rglob("*.json"):
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("schema") != "captured-market-bars-v1":
                continue
            source = value["artifact"]["source"]
            result[source] = path.relative_to(artifact_root).as_posix()
        return result

    def _materialize_golden_artifacts(self, root: Path):
        artifact_root = root / "artifacts"
        pack, case, _ = materialize_golden_case(
            manifest_path=_GOLDEN_MANIFEST,
            case_name="A_600519",
            artifact_root=artifact_root,
            parser_registry=builder._registry(),
        )
        descriptors = self._descriptor_keys(artifact_root)
        self.assertEqual(set(descriptors), {"eastmoney", "tencent"})
        return artifact_root, pack, case, descriptors

    def test_builder_and_reporter_run_with_sqlite_forbidden(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_root, pack, case, descriptors = (
                self._materialize_golden_artifacts(root)
            )
            manifest_path = root / "acceptance" / "manifest.json"
            report_root = root / "reports"
            guard_root = root / "sqlite-guard"
            guard_root.mkdir()
            (guard_root / "sitecustomize.py").write_text(
                "import sqlite3\n"
                "def _forbidden(*args, **kwargs):\n"
                "    raise RuntimeError('STAGE2H_SQLITE_FORBIDDEN')\n"
                "sqlite3.connect = _forbidden\n"
                "sqlite3.dbapi2.connect = _forbidden\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = os.pathsep.join(
                (str(guard_root), str(_PROJECT_ROOT))
            )
            build_command = [
                sys.executable,
                str(Path(builder.__file__).resolve()),
                "--artifact-root",
                str(artifact_root),
                "--output",
                str(manifest_path),
                "--case-name",
                case.case_name,
                "--symbol",
                case.symbol,
                "--market",
                case.market.value,
                "--interval",
                case.interval,
                "--adjustment",
                case.adjustment,
                "--as-of",
                pack.retrieved_at.isoformat(),
                "--created-at",
                (pack.retrieved_at + timedelta(days=1)).isoformat(),
                "--calendar-snapshot-id",
                case.calendar_snapshot_id,
            ]
            for session in case.expected_open_sessions:
                build_command.extend(("--open-session", session.isoformat()))
            for source, descriptor_key in sorted(descriptors.items()):
                build_command.extend(
                    ("--capture", f"{source}={descriptor_key}")
                )
            built = subprocess.run(
                build_command,
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(built.returncode, 0, msg=built.stderr)
            build_payload = json.loads(built.stdout)
            self.assertEqual(
                build_payload["schema"],
                "stage2h-market-bar-manifest-builder-v1",
            )
            self.assertEqual(build_payload["capture_count"], 2)
            self.assertFalse(build_payload["research_grade"])
            self.assertFalse(build_payload["production_database_modified"])
            self.assertTrue(manifest_path.is_file())

            reported = subprocess.run(
                [
                    sys.executable,
                    str(Path(reporter.__file__).resolve()),
                    "--manifest",
                    str(manifest_path),
                    "--artifact-root",
                    str(artifact_root),
                    "--output-dir",
                    str(report_root),
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(reported.returncode, 0, msg=reported.stderr)
            report_payload = json.loads(reported.stdout)
            self.assertEqual(
                report_payload["schema"],
                "stage2h-market-bar-acceptance-cli-v1",
            )
            self.assertEqual(
                report_payload["acceptance_state"],
                "SYNTHETIC_CONTRACT_ONLY",
            )
            self.assertEqual(
                report_payload["t3_preflight_state"],
                "EVIDENCE_PACKAGE_INCOMPLETE",
            )
            self.assertFalse(report_payload["operational_acceptance_complete"])
            self.assertFalse(report_payload["research_grade"])
            self.assertFalse(report_payload["t3_reached"])
            self.assertFalse(report_payload["production_database_modified"])
            self.assertTrue(Path(report_payload["json_output"]).is_file())
            self.assertTrue(Path(report_payload["markdown_output"]).is_file())

    def test_builder_rejects_duplicate_inputs_before_registry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_root = root / "artifacts"
            artifact_root.mkdir()
            base = [
                "--artifact-root",
                str(artifact_root),
                "--output",
                str(root / "manifest.json"),
                "--case-name",
                "A_600519",
                "--symbol",
                "600519.SH",
                "--market",
                "A",
                "--as-of",
                "2024-01-04T00:00:00Z",
                "--calendar-snapshot-id",
                "0" * 64,
                "--capture",
                "eastmoney=manifests/market-bars/fixture.json",
                "--capture",
                "tencent=manifests/market-bars/fixture-2.json",
            ]
            for extra in (
                [
                    "--open-session",
                    "2024-01-02",
                    "--open-session",
                    "2024-01-02",
                ],
                [
                    "--open-session",
                    "2024-01-02",
                    "--comparable-field",
                    "OPEN",
                    "--comparable-field",
                    "OPEN",
                ],
            ):
                with self.subTest(extra=extra), patch.object(
                    builder,
                    "_registry",
                ) as registry:
                    self.assertEqual(builder.main([*base, *extra]), 2)
                registry.assert_not_called()

    def test_builder_rejects_manifest_inside_artifact_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_root, pack, case, descriptors = (
                self._materialize_golden_artifacts(root)
            )
            command = [
                "--artifact-root",
                str(artifact_root),
                "--output",
                str(artifact_root / "manifest.json"),
                "--case-name",
                case.case_name,
                "--symbol",
                case.symbol,
                "--market",
                case.market.value,
                "--as-of",
                pack.retrieved_at.isoformat(),
                "--calendar-snapshot-id",
                case.calendar_snapshot_id,
                "--open-session",
                case.expected_open_sessions[0].isoformat(),
            ]
            for source, descriptor_key in sorted(descriptors.items()):
                command.extend(("--capture", f"{source}={descriptor_key}"))
            result = builder.main(command)
            self.assertEqual(result, 2)
            self.assertFalse((artifact_root / "manifest.json").exists())

    def test_reporter_rejects_output_overlap_with_artifact_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_root, pack, case, descriptors = (
                self._materialize_golden_artifacts(root)
            )
            manifest_path = root / "manifest.json"
            command = [
                "--artifact-root",
                str(artifact_root),
                "--output",
                str(manifest_path),
                "--case-name",
                case.case_name,
                "--symbol",
                case.symbol,
                "--market",
                case.market.value,
                "--as-of",
                pack.retrieved_at.isoformat(),
                "--created-at",
                (pack.retrieved_at + timedelta(days=1)).isoformat(),
                "--calendar-snapshot-id",
                case.calendar_snapshot_id,
            ]
            for session in case.expected_open_sessions:
                command.extend(("--open-session", session.isoformat()))
            for source, descriptor_key in sorted(descriptors.items()):
                command.extend(("--capture", f"{source}={descriptor_key}"))
            self.assertEqual(builder.main(command), 0)
            result = reporter.main(
                [
                    "--manifest",
                    str(manifest_path),
                    "--artifact-root",
                    str(artifact_root),
                    "--output-dir",
                    str(artifact_root / "reports"),
                ]
            )
            self.assertEqual(result, 2)

    def test_path_validation_rejects_linked_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).absolute()
            artifact_root = root / "artifacts"
            artifact_root.mkdir()
            manifest_path = root / "manifest.json"
            output_dir = root / "reports"
            with patch.object(
                builder,
                "_is_link",
                side_effect=lambda path: path == root,
            ), self.assertRaisesRegex(
                MarketBarAcceptanceError,
                "traverse symlinks or junctions",
            ):
                builder._validate_paths(artifact_root, manifest_path)
            manifest_path.write_text("{}", encoding="utf-8")
            with patch.object(
                reporter,
                "_is_link",
                side_effect=lambda path: path == root,
            ), self.assertRaisesRegex(
                MarketBarAcceptanceError,
                "traverse symlinks or junctions",
            ):
                reporter._validate_paths(manifest_path, artifact_root, output_dir)

    def test_assurance_declaration_path_must_be_regular_and_unlinked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).absolute()
            declaration_directory = root / "declaration-dir"
            declaration_directory.mkdir()
            with self.assertRaisesRegex(
                MarketBarAcceptanceError,
                "regular file",
            ):
                builder._load_assurance_declaration(declaration_directory)

            declaration_path = root / "declaration.json"
            declaration_path.write_text("{}", encoding="utf-8")
            with patch.object(
                builder,
                "_is_link",
                side_effect=lambda path: path == root,
            ), self.assertRaisesRegex(
                MarketBarAcceptanceError,
                "traverse symlinks or junctions",
            ):
                builder._load_assurance_declaration(declaration_path)


if __name__ == "__main__":
    unittest.main()

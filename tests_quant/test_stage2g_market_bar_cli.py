from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _PROJECT_ROOT / "scripts" / "report_stage2g_market_bars.py"
_MANIFEST = (
    _PROJECT_ROOT
    / "tests_quant"
    / "fixtures"
    / "market_bar_golden"
    / "v1"
    / "manifest.json"
)
_PRODUCTION_DB = _PROJECT_ROOT / "data" / "stock_tracker.db"


class TestStage2GMarketBarCli(unittest.TestCase):
    @staticmethod
    def _database_hash() -> str | None:
        if not _PRODUCTION_DB.is_file():
            return None
        return hashlib.sha256(_PRODUCTION_DB.read_bytes()).hexdigest()

    def test_cli_materializes_all_cases_without_touching_production_database(self) -> None:
        before = self._database_hash()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_root = root / "artifacts"
            output_dir = root / "reports"
            result = subprocess.run(
                [
                    sys.executable,
                    str(_SCRIPT),
                    "--manifest",
                    str(_MANIFEST),
                    "--artifact-root",
                    str(artifact_root),
                    "--output-dir",
                    str(output_dir),
                ],
                cwd=_PROJECT_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["schema"], "stage2g-market-bar-cli-v1")
            self.assertTrue(payload["synthetic_fixture_only"])
            self.assertFalse(payload["source_verification_complete"])
            self.assertFalse(payload["license_clearance_complete"])
            self.assertFalse(payload["t3_reached"])
            self.assertFalse(payload["research_grade"])
            self.assertFalse(payload["production_database_modified"])
            self.assertEqual(
                {item["case_name"] for item in payload["cases"]},
                {"A_600519", "HK_00700", "US_AAPL"},
            )
            for item in payload["cases"]:
                self.assertEqual(item["candidate_state"], "STRUCTURALLY_CONSTRUCTIBLE")
                self.assertEqual(item["finding_counts"]["HARD_BLOCK"], 0)
                self.assertIn("T3_NOT_REACHED", item["open_blockers"])
                self.assertTrue(Path(item["json_output"]).is_file())
                self.assertTrue(Path(item["markdown_output"]).is_file())
                self.assertEqual(
                    Path(item["json_output"]).stem,
                    item["report_id"],
                )
                self.assertEqual(
                    Path(item["markdown_output"]).stem,
                    item["report_id"],
                )
        self.assertEqual(self._database_hash(), before)

    def test_cli_rejects_output_overlap_with_fixture_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    sys.executable,
                    str(_SCRIPT),
                    "--manifest",
                    str(_MANIFEST),
                    "--artifact-root",
                    str(_MANIFEST.parent / "generated"),
                    "--output-dir",
                    str(Path(directory) / "reports"),
                    "--case",
                    "A_600519",
                ],
                cwd=_PROJECT_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            payload = json.loads(result.stderr)
            self.assertEqual(payload["schema"], "stage2g-market-bar-cli-error-v1")
            self.assertIn("cannot overlap", payload["message"])

    def test_cli_rejects_duplicate_or_unknown_cases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = (
                ("A_600519", "A_600519", "non-empty and unique"),
                ("UNKNOWN_CASE", None, "unknown golden case"),
            )
            for first, second, expected in cases:
                command = [
                    sys.executable,
                    str(_SCRIPT),
                    "--manifest",
                    str(_MANIFEST),
                    "--artifact-root",
                    str(root / f"artifacts-{first}"),
                    "--output-dir",
                    str(root / f"reports-{first}"),
                    "--case",
                    first,
                ]
                if second is not None:
                    command.extend(("--case", second))
                with self.subTest(case=first, second=second):
                    result = subprocess.run(
                        command,
                        cwd=_PROJECT_ROOT,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 2)
                    payload = json.loads(result.stderr)
                    self.assertIn(expected, payload["message"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import capture_quant_bars as cli
from stock_tracker.collector.eastmoney import EastmoneyProvider
from stock_tracker.collector.tencent import TencentProvider
from stock_tracker.core.config import ProviderConfig


class TestCaptureQuantBarsCli(unittest.TestCase):
    def test_script_entrypoint_bootstraps_project_root(self) -> None:
        script = Path(cli.__file__).resolve()
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, str(script), "--help"],
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("Capture exact public K-line bytes", result.stdout)

    def test_cli_captures_best_effort_artifact_without_database(self) -> None:
        raw = json.dumps(
            {
                "rc": 0,
                "data": {
                    "klines": [
                        "2024-01-02,100,105,110,95,12000,1500000000,2.3"
                    ]
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")
        provider = EastmoneyProvider(
            ProviderConfig(
                name="eastmoney",
                cls="EastmoneyProvider",
                markets=["a", "hk", "us"],
                max_rps=100,
            )
        )
        provider.fetch_bars_raw = lambda *args, **kwargs: raw  # type: ignore[method-assign]

        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with patch.object(cli, "_provider", return_value=provider), redirect_stdout(output):
                result = cli.main(
                    [
                        "--symbol",
                        "600519.SH",
                        "--market",
                        "A",
                        "--start",
                        "2024-01-01",
                        "--end",
                        "2024-12-31",
                        "--adjust",
                        "raw",
                        "--output-root",
                        directory,
                    ]
                )
            value = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(value["trust_tier"], "BEST_EFFORT")
            self.assertFalse(value["research_grade"])
            self.assertFalse(value["production_database_modified"])
            self.assertEqual(value["request_parameters"]["adjustment"], "raw")
            self.assertEqual(
                value["request_parameters"]["requested_start"],
                "2024-01-01",
            )
            self.assertEqual(
                value["request_parameters"]["requested_end"],
                "2024-12-31",
            )
            self.assertTrue((Path(directory) / value["storage_key"]).is_file())
            descriptor_path = Path(directory) / value["descriptor_key"]
            self.assertTrue(descriptor_path.is_file())
            descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
            self.assertEqual(
                descriptor["request_parameters"],
                value["request_parameters"],
            )
            self.assertFalse((Path(directory) / "stock_tracker.db").exists())

    def test_cli_captures_tencent_qfq_exact_raw_bytes(self) -> None:
        raw = json.dumps(
            {
                "code": 0,
                "msg": "",
                "data": {
                    "sh600519": {
                        "qfqday": [
                            [
                                "2024-01-02",
                                "100",
                                "105",
                                "110",
                                "95",
                                "12000",
                                "1500000000",
                            ]
                        ]
                    }
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")
        provider = TencentProvider(
            ProviderConfig(
                name="tencent",
                cls="TencentProvider",
                markets=["a", "hk", "us"],
                max_rps=100,
            )
        )
        provider.fetch_bars_raw = lambda *args, **kwargs: raw  # type: ignore[method-assign]

        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with patch.object(cli, "_provider", return_value=provider), redirect_stdout(output):
                result = cli.main(
                    [
                        "--provider",
                        "tencent",
                        "--symbol",
                        "600519.SH",
                        "--market",
                        "A",
                        "--start",
                        "2024-01-01",
                        "--end",
                        "2024-12-31",
                        "--adjust",
                        "qfq",
                        "--output-root",
                        directory,
                    ]
                )
            value = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(value["schema"], "capture-quant-bars-cli-v2")
            self.assertEqual(value["provider"], "tencent")
            self.assertEqual(value["trust_tier"], "BEST_EFFORT")
            self.assertFalse(value["research_grade"])
            self.assertFalse(value["production_database_modified"])
            self.assertEqual(value["request_parameters"]["adjustment"], "qfq")
            self.assertFalse(value["request_parameters"]["synthetic_fixture"])
            self.assertTrue((Path(directory) / value["storage_key"]).is_file())

    def test_cli_rejects_tencent_hk_qfq_before_network(self) -> None:
        provider = TencentProvider(
            ProviderConfig(
                name="tencent",
                cls="TencentProvider",
                markets=["a", "hk", "us"],
                max_rps=100,
            )
        )
        with tempfile.TemporaryDirectory() as directory, patch.object(
            cli,
            "_provider",
            return_value=provider,
        ), patch.object(provider, "fetch_bars_raw") as fetch:
            with self.assertRaisesRegex(RuntimeError, "market=HK"):
                cli.main(
                    [
                        "--provider",
                        "tencent",
                        "--symbol",
                        "00700.HK",
                        "--market",
                        "HK",
                        "--adjust",
                        "qfq",
                        "--output-root",
                        directory,
                    ]
                )
            fetch.assert_not_called()

    def test_cli_rejects_invalid_range_and_production_database_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(cli, "_provider") as provider:
                with self.assertRaisesRegex(RuntimeError, "cannot precede"):
                    cli.main(
                        [
                            "--provider",
                            "eastmoney",
                            "--symbol",
                            "600519.SH",
                            "--market",
                            "A",
                            "--start",
                            "2024-01-03",
                            "--end",
                            "2024-01-02",
                            "--output-root",
                            str(root / "artifacts"),
                        ]
                    )
                provider.assert_not_called()

            project_root = root / "project"
            production_database = project_root / "data" / "stock_tracker.db"
            production_database.parent.mkdir(parents=True)
            production_database.write_bytes(b"not-a-real-database")
            with patch.object(cli, "PROJECT_ROOT", project_root), patch.object(
                cli,
                "_provider",
            ) as provider:
                with self.assertRaisesRegex(RuntimeError, "production database"):
                    cli.main(
                        [
                            "--provider",
                            "eastmoney",
                            "--symbol",
                            "600519.SH",
                            "--market",
                            "A",
                            "--output-root",
                            str(production_database),
                        ]
                    )
                provider.assert_not_called()

    def test_cli_rejects_existing_file_as_artifact_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "artifact-root"
            output.write_text("not a directory", encoding="utf-8")
            with patch.object(cli, "_provider") as provider:
                with self.assertRaisesRegex(RuntimeError, "must be a directory"):
                    cli.main(
                        [
                            "--provider",
                            "eastmoney",
                            "--symbol",
                            "600519.SH",
                            "--market",
                            "A",
                            "--output-root",
                            str(output),
                        ]
                    )
                provider.assert_not_called()

    def test_cli_rejects_tencent_adjustment_it_cannot_supply(self) -> None:
        provider = TencentProvider(
            ProviderConfig(
                name="tencent",
                cls="TencentProvider",
                markets=["a", "hk", "us"],
                max_rps=100,
            )
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(cli, "_provider", return_value=provider),
            self.assertRaisesRegex(RuntimeError, "cannot honestly provide"),
        ):
            cli.main(
                [
                    "--provider",
                    "tencent",
                    "--symbol",
                    "600519.SH",
                    "--market",
                    "A",
                    "--adjust",
                    "raw",
                    "--output-root",
                    directory,
                ]
            )


if __name__ == "__main__":
    unittest.main()

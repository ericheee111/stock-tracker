from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import capture_quant_bars as cli
from stock_tracker.collector.eastmoney import EastmoneyProvider
from stock_tracker.core.config import ProviderConfig


class TestCaptureQuantBarsCli(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

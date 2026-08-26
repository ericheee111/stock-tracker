from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from scripts import capture_hithink_bars as cli
from stock_tracker.collector.hithink_finance import HithinkFinanceProvider
from stock_tracker.core import types as T
from stock_tracker.core.config import ProviderConfig

ROOT = Path(__file__).resolve().parents[1]
_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _date_ms(value: str) -> int:
    current = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=_SHANGHAI)
    return int(current.timestamp() * 1000)


class TestCaptureHithinkBarsCli(unittest.TestCase):
    def test_loader_activates_only_the_explicit_capture_process(self) -> None:
        sentinel = object()
        with patch.object(
            cli,
            "HithinkFinanceProvider",
            return_value=sentinel,
        ) as constructor:
            provider = cli._provider(ROOT / "config" / "providers.toml")
        self.assertIs(provider, sentinel)
        config = constructor.call_args.args[0]
        self.assertTrue(config.enabled)
        self.assertFalse(config.allow_live_decision)
        self.assertFalse(config.allow_model_training)
        self.assertFalse(config.allow_public_redistribution)

    def test_cli_captures_exact_best_effort_artifact_without_database(self) -> None:
        raw = json.dumps(
            {
                "code": 0,
                "message": "success",
                "request_id": "capture-fixture",
                "data": {
                    "timestamp": _date_ms("2024-01-02"),
                    "item": [
                        {
                            "date_ms": _date_ms("2024-01-02"),
                            "open_price": 100.0,
                            "high_price": 110.0,
                            "low_price": 95.0,
                            "close_price": 105.0,
                            "volume": 12000.0,
                            "turnover": 1500000000.0,
                        }
                    ],
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")
        provider = HithinkFinanceProvider(
            ProviderConfig(
                name="hithink_finance",
                cls="HithinkFinanceProvider",
                markets=["a"],
                enabled=True,
                max_rps=1000,
                read_only=True,
                trust_tier="T1_BEST_EFFORT",
                allow_live_decision=False,
                allow_model_training=False,
                allow_public_redistribution=False,
            ),
            credential_provider=lambda: "fixture-credential-value",
            opener=object(),
        )
        provider.fetch_bars_raw = lambda *args, **kwargs: raw  # type: ignore[method-assign]

        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with patch.object(cli, "_provider", return_value=provider), redirect_stdout(output):
                result = cli.main(
                    [
                        "--symbol",
                        "600519.SH",
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
            rendered = output.getvalue()
            value = json.loads(rendered)
            self.assertEqual(result, 0)
            self.assertNotIn("fixture-credential-value", rendered)
            self.assertEqual(value["trust_tier"], "BEST_EFFORT")
            self.assertFalse(value["research_grade"])
            self.assertFalse(value["production_database_modified"])
            self.assertFalse(value["credential_in_output"])
            self.assertEqual(value["request_parameters"]["adjustment"], "raw")
            self.assertEqual(
                value["request_parameters"]["upstream_adjustment"],
                "none",
            )
            storage_path = Path(directory) / value["storage_key"]
            self.assertTrue(storage_path.is_file())
            self.assertEqual(storage_path.read_bytes(), raw)
            descriptor_path = Path(directory) / value["descriptor_key"]
            self.assertTrue(descriptor_path.is_file())
            descriptor_text = descriptor_path.read_text(encoding="utf-8")
            self.assertNotIn("fixture-credential-value", descriptor_text)
            descriptor = json.loads(descriptor_text)
            self.assertEqual(descriptor["artifact"]["source"], "hithink_finance")
            self.assertEqual(descriptor["artifact"]["market"], T.Market.A.value)
            self.assertFalse((Path(directory) / "stock_tracker.db").exists())


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from stock_tracker.core.config import (
    ConfigError,
    load_app,
    load_providers,
    load_risk,
    load_strategies,
)


class TestStrictBooleanConfig(unittest.TestCase):
    @staticmethod
    def write(directory: str, name: str, content: str) -> Path:
        path = Path(directory) / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_app_safety_booleans_reject_strings_and_integers(self) -> None:
        invalid_documents = (
            '[collector]\nbars_enabled = "false"\n',
            '[markets]\na = "false"\n',
            '[markets]\nhk = 0\n',
            '[markets]\nus = 1\n',
        )
        with tempfile.TemporaryDirectory() as directory:
            for index, document in enumerate(invalid_documents):
                with self.subTest(document=document):
                    path = self.write(directory, f"app-{index}.toml", document)
                    with self.assertRaisesRegex(ConfigError, "TOML boolean"):
                        load_app(str(path), root_dir=directory)

    def test_strategy_enabled_requires_toml_boolean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                directory,
                "strategies.toml",
                '[s1]\nenabled = "false"\n',
            )
            with self.assertRaisesRegex(ConfigError, "s1.enabled"):
                load_strategies(str(path))

    def test_provider_safety_booleans_require_toml_boolean(self) -> None:
        fields = (
            "enabled",
            "primary",
            "supports_snapshot",
            "bars_fallback",
            "read_only",
            "allow_live_decision",
            "allow_model_training",
            "allow_public_redistribution",
        )
        with tempfile.TemporaryDirectory() as directory:
            for field in fields:
                with self.subTest(field=field):
                    path = self.write(
                        directory,
                        f"providers-{field}.toml",
                        (
                            "[[providers]]\n"
                            'name = "fixture"\n'
                            'cls = "FixtureProvider"\n'
                            'markets = ["a"]\n'
                            f'{field} = "false"\n'
                        ),
                    )
                    with self.assertRaisesRegex(ConfigError, field):
                        load_providers(str(path))

    def test_provider_bars_priority_rejects_bool_string_and_out_of_range(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for index, value in enumerate(("true", '"30"', "-1001", "1001")):
                with self.subTest(value=value):
                    path = self.write(
                        directory,
                        f"providers-priority-{index}.toml",
                        (
                            "[[providers]]\n"
                            'name = "fixture"\n'
                            'cls = "FixtureProvider"\n'
                            'markets = ["a"]\n'
                            f"bars_priority = {value}\n"
                        ),
                    )
                    with self.assertRaisesRegex(ConfigError, "bars_priority"):
                        load_providers(str(path))

    def test_disabled_sidecar_policy_fields_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                directory,
                "providers-sidecar.toml",
                (
                    "[[providers]]\n"
                    'name = "free_stockdb"\n'
                    'cls = "FreeStockDbProvider"\n'
                    'markets = ["a"]\n'
                    "enabled = false\n"
                    "read_only = true\n"
                    'trust_tier = "T1_BEST_EFFORT"\n'
                    "allow_live_decision = false\n"
                    "allow_model_training = false\n"
                    "allow_public_redistribution = false\n"
                    "bars_priority = 30\n"
                ),
            )
            provider = load_providers(str(path))[0]
            self.assertFalse(provider.enabled)
            self.assertTrue(provider.read_only)
            self.assertEqual(provider.trust_tier, "T1_BEST_EFFORT")
            self.assertFalse(provider.allow_live_decision)
            self.assertFalse(provider.allow_model_training)
            self.assertFalse(provider.allow_public_redistribution)
            self.assertEqual(provider.bars_priority, 30)

    def test_risk_gate_boolean_rejects_integer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                directory,
                "risk.toml",
                "[data_quality]\nblock_if_stale = 0\n",
            )
            with self.assertRaisesRegex(ConfigError, "block_if_stale"):
                load_risk(str(path))

    def test_valid_toml_booleans_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app_path = self.write(
                directory,
                "app.toml",
                (
                    "[collector]\n"
                    "bars_enabled = false\n"
                    "[markets]\n"
                    "a = true\n"
                    "hk = false\n"
                    "us = true\n"
                ),
            )
            app = load_app(str(app_path), root_dir=directory)
            self.assertFalse(app.collector.bars_enabled)
            self.assertEqual(
                app.markets_enabled,
                {"a": True, "hk": False, "us": True},
            )


if __name__ == "__main__":
    unittest.main()

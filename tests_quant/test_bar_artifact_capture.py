from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from stock_tracker.collector.eastmoney import EastmoneyProvider
from stock_tracker.core.config import ProviderConfig
from stock_tracker.core.types import Market
from stock_tracker.quant.core.fingerprint import fingerprint
from stock_tracker.quant.data import (
    DataTrustTier,
    ManifestContractError,
    capture_market_bars,
    load_captured_market_bars,
)


class TestBarArtifactCapture(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = EastmoneyProvider(
            ProviderConfig(
                name="eastmoney",
                cls="EastmoneyProvider",
                markets=["a", "hk", "us"],
                max_rps=100,
            )
        )
        self.raw = json.dumps(
            {
                "rc": 0,
                "data": {
                    "klines": [
                        "2024-01-02,100,105,110,95,12000,1500000000,2.3",
                        "2024-01-03,105,106,111,101,13000,1600000000,2.4",
                    ]
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")

    @staticmethod
    def request_parameters(**overrides):
        value = {
            "adjustment": "qfq",
            "requested_start": "2024-01-01",
            "requested_end": "2024-12-31",
            "endpoint": "push2his-kline",
            "interval": "1d",
        }
        value.update(overrides)
        return value

    def capture(
        self,
        root: str | Path,
        parser_version: str | None = None,
        request_parameters: dict | None = None,
    ):
        return capture_market_bars(
            root,
            raw_bytes=self.raw,
            parser=self.provider.parse_bars_strict,
            symbol="600519.SH",
            market=Market.A,
            interval="1d",
            retrieved_at=datetime(2025, 1, 5, tzinfo=timezone.utc),
            source="eastmoney",
            source_dataset="push2his-kline",
            provider_version="push2his-observed-2026-08",
            schema_version=self.provider.KLINE_SCHEMA_VERSION,
            parser_version=(parser_version or self.provider.KLINE_ADAPTER_VERSION),
            request_parameters=(request_parameters or self.request_parameters()),
            source_note="synthetic golden payload",
        )

    def test_exact_bytes_and_descriptor_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            captured = self.capture(directory)
            raw_path = Path(directory) / Path(captured.artifact.storage_key)
            descriptor_path = Path(directory) / Path(captured.descriptor_key)
            self.assertEqual(raw_path.read_bytes(), self.raw)
            self.assertEqual(raw_path.stem, captured.artifact.sha256)
            self.assertTrue(descriptor_path.is_file())
            self.assertEqual(captured.artifact.row_count, 2)
            self.assertEqual(captured.trust_tier, DataTrustTier.BEST_EFFORT)
            self.assertEqual(captured.bars[0].volume, 1_200_000)

            loaded = load_captured_market_bars(
                directory,
                descriptor_key=captured.descriptor_key,
                parser=self.provider.parse_bars_strict,
            )
            self.assertEqual(loaded.capture_id, captured.capture_id)
            self.assertEqual(loaded.normalized_dataset_id, captured.normalized_dataset_id)
            self.assertEqual(loaded.request_parameters, captured.request_parameters)
            self.assertEqual(
                loaded.request_parameters["adjustment"],
                "qfq",
            )
            self.assertEqual(loaded.bars, captured.bars)

    def test_capture_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = self.capture(directory)
            second = self.capture(directory)
            self.assertEqual(first.capture_id, second.capture_id)
            self.assertEqual(first.descriptor_key, second.descriptor_key)

    def test_parser_versions_share_raw_but_not_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = self.capture(directory, parser_version="parser-v1")
            second = self.capture(directory, parser_version="parser-v2")
            self.assertEqual(first.artifact.storage_key, second.artifact.storage_key)
            self.assertNotEqual(first.descriptor_key, second.descriptor_key)
            self.assertNotEqual(first.normalized_dataset_id, second.normalized_dataset_id)

    def test_request_identity_changes_descriptor_but_reuses_raw_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            qfq = self.capture(
                directory,
                request_parameters=self.request_parameters(adjustment="qfq"),
            )
            raw = self.capture(
                directory,
                request_parameters=self.request_parameters(adjustment="raw"),
            )
            narrower = self.capture(
                directory,
                request_parameters=self.request_parameters(
                    requested_start="2024-06-01",
                ),
            )
            self.assertEqual(qfq.artifact.storage_key, raw.artifact.storage_key)
            self.assertEqual(qfq.artifact.storage_key, narrower.artifact.storage_key)
            self.assertNotEqual(qfq.descriptor_key, raw.descriptor_key)
            self.assertNotEqual(qfq.capture_id, raw.capture_id)
            self.assertNotEqual(qfq.descriptor_key, narrower.descriptor_key)

    def test_request_parameters_require_explicit_provenance_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                ManifestContractError,
                "missing fields: requested_end",
            ):
                self.capture(
                    directory,
                    request_parameters={
                        "adjustment": "qfq",
                        "requested_start": "2024-01-01",
                        "endpoint": "push2his-kline",
                    },
                )

    def test_request_date_range_fails_closed(self) -> None:
        invalid = (
            (
                self.request_parameters(requested_start="not-a-date"),
                "requested_start must be an ISO date",
            ),
            (
                self.request_parameters(
                    requested_start="2024-12-31",
                    requested_end="2024-01-01",
                ),
                "requested_end cannot precede",
            ),
        )
        for parameters, message in invalid:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(ManifestContractError, message):
                    self.capture(directory, request_parameters=parameters)

    def test_recomputed_request_tamper_cannot_reuse_descriptor_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            captured = self.capture(directory)
            descriptor_path = Path(directory) / Path(captured.descriptor_key)
            value = json.loads(descriptor_path.read_text(encoding="utf-8"))
            value["request_parameters"]["adjustment"] = "raw"
            identity = dict(value)
            identity.pop("capture_id")
            value["capture_id"] = fingerprint(identity)
            descriptor_path.write_text(
                json.dumps(value, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ManifestContractError,
                "descriptor path does not match",
            ):
                load_captured_market_bars(
                    directory,
                    descriptor_key=captured.descriptor_key,
                    parser=self.provider.parse_bars_strict,
                )

    def test_unknown_descriptor_fields_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            captured = self.capture(directory)
            descriptor_path = Path(directory) / Path(captured.descriptor_key)
            value = json.loads(descriptor_path.read_text(encoding="utf-8"))
            value["unexpected"] = "payload"
            identity = dict(value)
            identity.pop("capture_id")
            value["capture_id"] = fingerprint(identity)
            descriptor_path.write_text(
                json.dumps(value, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ManifestContractError, "unknown fields"):
                load_captured_market_bars(
                    directory,
                    descriptor_key=captured.descriptor_key,
                    parser=self.provider.parse_bars_strict,
                )

    def test_modified_raw_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            captured = self.capture(directory)
            raw_path = Path(directory) / Path(captured.artifact.storage_key)
            raw_path.write_bytes(raw_path.read_bytes() + b" ")
            with self.assertRaisesRegex(ManifestContractError, "size changed"):
                load_captured_market_bars(
                    directory,
                    descriptor_key=captured.descriptor_key,
                    parser=self.provider.parse_bars_strict,
                )

    def test_capture_cannot_claim_research_grade(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ManifestContractError, "cannot self-promote"):
                capture_market_bars(
                    directory,
                    raw_bytes=self.raw,
                    parser=self.provider.parse_bars_strict,
                    symbol="600519.SH",
                    market=Market.A,
                    interval="1d",
                    retrieved_at=datetime(2025, 1, 5, tzinfo=timezone.utc),
                    source="eastmoney",
                    source_dataset="push2his-kline",
                    provider_version="fixture",
                    schema_version="fixture-v1",
                    parser_version="fixture-parser-v1",
                    request_parameters=self.request_parameters(),
                    trust_tier=DataTrustTier.RESEARCH_GRADE,
                )

    def test_recomputed_descriptor_id_cannot_raise_trust_grade(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            captured = self.capture(directory)
            descriptor_path = Path(directory) / Path(captured.descriptor_key)
            value = json.loads(descriptor_path.read_text(encoding="utf-8"))
            value["trust_tier"] = DataTrustTier.RESEARCH_GRADE.value
            identity = dict(value)
            identity.pop("capture_id")
            value["capture_id"] = fingerprint(identity)
            descriptor_path.write_text(
                json.dumps(value, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ManifestContractError, "cannot self-promote"):
                load_captured_market_bars(
                    directory,
                    descriptor_key=captured.descriptor_key,
                    parser=self.provider.parse_bars_strict,
                )

    def test_raw_capture_cannot_claim_operational_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                ManifestContractError,
                "cannot self-promote above BEST_EFFORT",
            ):
                capture_market_bars(
                    directory,
                    raw_bytes=self.raw,
                    parser=self.provider.parse_bars_strict,
                    symbol="600519.SH",
                    market=Market.A,
                    interval="1d",
                    retrieved_at=datetime(2025, 1, 5, tzinfo=timezone.utc),
                    source="eastmoney",
                    source_dataset="push2his-kline",
                    provider_version="fixture",
                    schema_version="fixture-v1",
                    parser_version="fixture-parser-v1",
                    request_parameters=self.request_parameters(),
                    trust_tier=DataTrustTier.OPERATIONAL_VERIFIED,
                )

    def test_normalized_rows_reject_source_and_ohlc_corruption(self) -> None:
        good = tuple(
            self.provider.parse_bars_strict(
                self.raw,
                "600519.SH",
                Market.A,
                "1d",
            )
        )

        def wrong_source(raw, symbol, market, interval):
            return tuple(replace(bar, source="other") for bar in good)

        def invalid_ohlc(raw, symbol, market, interval):
            return (replace(good[0], high=104.0), *good[1:])

        cases = (
            (wrong_source, "source differs"),
            (invalid_ohlc, "high is inconsistent"),
        )
        for parser, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(ManifestContractError, message):
                    capture_market_bars(
                        directory,
                        raw_bytes=self.raw,
                        parser=parser,
                        symbol="600519.SH",
                        market=Market.A,
                        interval="1d",
                        retrieved_at=datetime(2025, 1, 5, tzinfo=timezone.utc),
                        source="eastmoney",
                        source_dataset="push2his-kline",
                        provider_version="fixture",
                        schema_version="fixture-v1",
                        parser_version="fixture-parser-v1",
                        request_parameters=self.request_parameters(),
                    )


if __name__ == "__main__":
    unittest.main()

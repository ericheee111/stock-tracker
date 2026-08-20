from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import patch
from urllib import request as urllib_request
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from stock_tracker.__main__ import _build_providers
from stock_tracker.collector.free_stockdb import (
    FreeStockDbContractError,
    FreeStockDbProvider,
)
from stock_tracker.collector.router import ProviderRouter
from stock_tracker.core import types as T
from stock_tracker.core.config import (
    AppConfig,
    ConfigBundle,
    MarketsConfig,
    ProviderConfig,
    RiskConfig,
    StrategiesConfig,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_free_stockdb_sidecar.py"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_START = datetime(2026, 6, 22, 0, 0, tzinfo=_SHANGHAI)
_END = datetime(2026, 6, 26, 23, 59, tzinfo=_SHANGHAI)


def _cfg(**overrides) -> ProviderConfig:
    values = {
        "name": "free_stockdb",
        "cls": "FreeStockDbProvider",
        "markets": ["a"],
        "enabled": True,
        "primary": False,
        "supports_snapshot": False,
        "timeout_ms": 1000,
        "max_rps": 20,
        "host": "127.0.0.1:7899",
        "bars_fallback": False,
        "bars_priority": 30,
        "read_only": True,
        "trust_tier": "T1_BEST_EFFORT",
        "allow_live_decision": False,
        "allow_model_training": False,
        "allow_public_redistribution": False,
        "release_version": "synthetic-release-v1",
        "binary_inventory_sha256": "a" * 64,
        "data_snapshot_manifest_sha256": "b" * 64,
        "sync_manifest_sha256": "c" * 64,
    }
    values.update(overrides)
    return ProviderConfig(**values)


def _daily_row(date_value: int = 20260625, code: str = "600633") -> dict:
    return {
        "amount": 189010000,
        "amplitude": 2.38,
        "close": 10.45,
        "code": code,
        "date": date_value,
        "high": 10.62,
        "is_st": False,
        "low": 10.37,
        "name": "synthetic",
        "open": 10.45,
        "pre_close": 10.52,
        "turnover": 1.42,
        "volume": 18031500,
    }


def _minute_row(date_value: int = 20260625145200, code: str = "600422") -> dict:
    return {
        "amount": 428554,
        "close": 7.95,
        "code": code,
        "date": date_value,
        "high": 7.96,
        "low": 7.94,
        "open": 7.95,
        "volume": 53900,
    }


def _audit_files(root: Path) -> tuple[Path, Path, Path]:
    binary = root / "stockdb.exe"
    data_manifest = root / "data-snapshot-manifest.json"
    sync_manifest = root / "sync-manifest.json"
    binary.write_bytes(b"synthetic stockdb executable")
    data_manifest.write_text(
        '{"schema":"synthetic-data-manifest-v1","files":[]}\n',
        encoding="utf-8",
    )
    sync_manifest.write_text(
        '{"schema":"synthetic-sync-manifest-v1","files":[]}\n',
        encoding="utf-8",
    )
    return binary, data_manifest, sync_manifest


class TestFreeStockDbPolicy(unittest.TestCase):
    def test_only_pinned_loopback_read_only_t1_configuration_is_accepted(self) -> None:
        provider = FreeStockDbProvider(_cfg())
        self.assertTrue(provider.supports_bars())
        self.assertTrue(provider.supports_raw_bars())
        self.assertTrue(provider.supports_adjustment("raw"))
        self.assertFalse(provider.supports_adjustment("qfq"))
        self.assertFalse(provider.supports_snapshot())

    def test_non_loopback_dns_https_path_and_credentials_fail_closed(self) -> None:
        hosts = (
            "192.168.1.10:7899",
            "localhost:7899",
            "https://127.0.0.1:7899",
            "http://127.0.0.1:7899/api",
            "http://user:pass@127.0.0.1:7899",
        )
        for host in hosts:
            with self.subTest(host=host), self.assertRaises(FreeStockDbContractError):
                FreeStockDbProvider(_cfg(host=host))

    def test_trust_write_live_training_and_redistribution_bypasses_fail(self) -> None:
        cases = (
            {"primary": True},
            {"supports_snapshot": True},
            {"read_only": False},
            {"trust_tier": "T3_RESEARCH_GRADE"},
            {"allow_live_decision": True},
            {"allow_model_training": True},
            {"allow_public_redistribution": True},
            {"bars_priority": True},
            {"bars_priority": 1001},
            {"markets": ["a", "hk"]},
        )
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(
                FreeStockDbContractError
            ):
                FreeStockDbProvider(_cfg(**changes))

    def test_enabled_provider_requires_release_and_all_provenance_hashes(self) -> None:
        cases = (
            {"release_version": ""},
            {"binary_inventory_sha256": ""},
            {"data_snapshot_manifest_sha256": "not-a-hash"},
            {"sync_manifest_sha256": "D" * 64},
        )
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(
                FreeStockDbContractError
            ):
                FreeStockDbProvider(_cfg(**changes))

    def test_direct_disabled_construction_cannot_bypass_enabled_gate(self) -> None:
        with self.assertRaisesRegex(FreeStockDbContractError, "enabled=false"):
            FreeStockDbProvider(_cfg(enabled=False))

    def test_disabled_provider_is_not_instantiated_by_application_registry(self) -> None:
        disabled = _cfg(
            enabled=False,
            release_version="",
            binary_inventory_sha256="",
            data_snapshot_manifest_sha256="",
            sync_manifest_sha256="",
        )
        tencent = ProviderConfig(
            name="tencent",
            cls="TencentProvider",
            markets=["a"],
            enabled=True,
        )
        bundle = SimpleNamespace(providers=[disabled, tencent])
        logger = SimpleNamespace(info=lambda *args: None, warning=lambda *args: None)
        providers = _build_providers(bundle, logger)
        self.assertEqual([provider.name for provider in providers], ["tencent"])

    def test_hot_quote_surface_is_not_available(self) -> None:
        provider = FreeStockDbProvider(_cfg())
        with self.assertRaises(NotImplementedError):
            provider.fetch_quotes(["600633.SH"])


class TestFreeStockDbQueryAndParsing(unittest.TestCase):
    def test_daily_query_is_bounded_read_only_and_records_evidence(self) -> None:
        provider = FreeStockDbProvider(_cfg())
        raw = json.dumps(_daily_row(), ensure_ascii=False).encode("utf-8")
        observed_urls: list[str] = []

        def fake_request(url: str) -> bytes:
            observed_urls.append(url)
            return raw

        provider._request_local = fake_request  # type: ignore[method-assign]
        bars, evidence = provider.fetch_bars_with_evidence(
            "600633.SH",
            T.Market.A,
            interval="1d",
            start=_START,
            end=_END,
            adjust="raw",
        )
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].timestamp.tzinfo, _SHANGHAI)
        self.assertEqual(bars[0].quality_status, T.DataStatus.UNKNOWN)
        self.assertEqual(bars[0].adjustment_factor, 1.0)
        self.assertEqual(bars[0].source, "free_stockdb")
        query = parse_qs(urlparse(observed_urls[0]).query)
        self.assertEqual(query["cmd"], ["get"])
        self.assertEqual(query["t"], ["日k:600633:20260622<20260626"])
        self.assertNotIn("set", observed_urls[0].lower())
        self.assertEqual(evidence.response_sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(provider.last_read_evidence, evidence)
        self.assertEqual(evidence.as_dict()["trust_tier"], "T1_BEST_EFFORT")
        self.assertFalse(evidence.as_dict()["allow_model_training"])
        self.assertFalse(evidence.as_dict()["allow_public_redistribution"])

    def test_range_mapping_is_sorted_and_single_row_is_supported(self) -> None:
        provider = FreeStockDbProvider(_cfg())
        payload = {
            "20260625": _daily_row(20260625),
            "20260623": _daily_row(20260623),
        }
        bars = provider.parse_bars(
            json.dumps(payload).encode("utf-8"),
            "600633.SH",
            T.Market.A,
        )
        self.assertEqual(
            [bar.timestamp.date().isoformat() for bar in bars],
            ["2026-06-23", "2026-06-25"],
        )
        single = provider.parse_bars(
            json.dumps(_daily_row()).encode("utf-8"),
            "600633.SH",
            T.Market.A,
        )
        self.assertEqual(len(single), 1)

    def test_minute_row_is_supported_but_missing_turnover_remains_unknown_zero(self) -> None:
        provider = FreeStockDbProvider(_cfg())
        bars = provider.parse_bars(
            json.dumps(_minute_row()).encode("utf-8"),
            "600422.SH",
            T.Market.A,
            interval="1m",
        )
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].timestamp.hour, 14)
        self.assertEqual(bars[0].timestamp.minute, 52)
        self.assertEqual(bars[0].turnover, 0.0)
        self.assertEqual(bars[0].quality_status, T.DataStatus.UNKNOWN)

    def test_qfq_unbounded_naive_wrong_market_and_oversized_queries_fail(self) -> None:
        provider = FreeStockDbProvider(_cfg())
        cases = (
            {
                "symbol": "600633.SH",
                "market": T.Market.A,
                "start": _START,
                "end": _END,
                "adjust": "qfq",
            },
            {
                "symbol": "600633.SH",
                "market": T.Market.A,
                "start": None,
                "end": _END,
                "adjust": "raw",
            },
            {
                "symbol": "600633.SH",
                "market": T.Market.A,
                "start": _START.replace(tzinfo=None),
                "end": _END,
                "adjust": "raw",
            },
            {
                "symbol": "00700.HK",
                "market": T.Market.HK,
                "start": _START,
                "end": _END,
                "adjust": "raw",
            },
            {
                "symbol": "600633.SH",
                "market": T.Market.A,
                "interval": "1m",
                "start": datetime(2026, 1, 1, tzinfo=_SHANGHAI),
                "end": datetime(2026, 3, 1, tzinfo=_SHANGHAI),
                "adjust": "raw",
            },
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(FreeStockDbContractError):
                provider.fetch_bars_raw(**kwargs)

    def test_duplicate_json_nonfinite_bool_bad_ohlc_and_code_mismatch_fail(self) -> None:
        provider = FreeStockDbProvider(_cfg())
        malformed = (
            b'{"code":"600633","code":"600633","date":20260625}',
            json.dumps({**_daily_row(), "open": float("nan")}).encode("utf-8"),
            json.dumps({**_daily_row(), "volume": True}).encode("utf-8"),
            json.dumps({**_daily_row(), "low": 20}).encode("utf-8"),
            json.dumps(_daily_row(code="600634")).encode("utf-8"),
            json.dumps({"data": [_daily_row()]}).encode("utf-8"),
        )
        for raw in malformed:
            with self.subTest(raw=raw[:80]), self.assertRaises(
                FreeStockDbContractError
            ):
                provider.parse_bars(raw, "600633.SH", T.Market.A)

    def test_local_http_requests_use_provider_rate_limiter(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureSidecarHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            provider = FreeStockDbProvider(
                _cfg(host=f"127.0.0.1:{server.server_port}")
            )
            calls: list[str] = []
            provider._rl = SimpleNamespace(  # type: ignore[assignment]
                acquire=lambda: calls.append("acquire") or 0.0,
                hits=0,
            )
            bars = provider.fetch_bars(
                "600633.SH",
                T.Market.A,
                start=_START,
                end=_END,
                adjust="raw",
            )
            self.assertEqual(len(bars), 1)
            self.assertEqual(calls, ["acquire"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_local_http_explicitly_disables_environment_proxies(self) -> None:
        provider = FreeStockDbProvider(_cfg())
        url, _, _ = provider._query_url(
            "600633.SH",
            T.Market.A,
            "1d",
            _START,
            _END,
            "raw",
        )
        fake_opener = SimpleNamespace(
            open=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("stop"))
        )
        with (
            patch(
                "stock_tracker.collector.free_stockdb.urllib_request.build_opener",
                return_value=fake_opener,
            ) as build_opener,
            self.assertRaisesRegex(RuntimeError, "stop"),
        ):
            provider._request_local(url)
        handlers = build_opener.call_args.args
        proxy_handlers = [
            handler
            for handler in handlers
            if isinstance(handler, urllib_request.ProxyHandler)
        ]
        self.assertEqual(len(proxy_handlers), 1)
        self.assertEqual(proxy_handlers[0].proxies, {})

    def test_duplicate_timestamp_and_response_outside_query_range_fail(self) -> None:
        provider = FreeStockDbProvider(_cfg())
        duplicate = json.dumps([_daily_row(), _daily_row()]).encode("utf-8")
        with self.assertRaisesRegex(FreeStockDbContractError, "duplicate"):
            provider.parse_bars(duplicate, "600633.SH", T.Market.A)

        provider._request_local = lambda _url: json.dumps(  # type: ignore[method-assign]
            _daily_row(20260621)
        ).encode("utf-8")
        with self.assertRaisesRegex(FreeStockDbContractError, "precedes"):
            provider.fetch_bars(
                "600633.SH",
                T.Market.A,
                start=_START,
                end=_END,
                adjust="raw",
            )


class _BarsProvider:
    def __init__(self, cfg: ProviderConfig, source: str) -> None:
        self.cfg = cfg
        self.name = cfg.name
        self.timeout = 1.0
        self._rl = SimpleNamespace(hits=0)
        self.source = source
        self.calls = 0

    def applies_to(self, market: T.Market) -> bool:
        return market is T.Market.A

    def supports_bars(self) -> bool:
        return True

    def supports_adjustment(self, adjust: str) -> bool:
        return True

    def fetch_bars(self, symbol, market, interval="1d", start=None, end=None, adjust="qfq"):
        self.calls += 1
        return [
            T.Bar(
                symbol=symbol,
                market=market,
                timestamp=_START,
                close=10.0,
                source=self.source,
            )
        ]


class TestFreeStockDbRouting(unittest.TestCase):
    @staticmethod
    def _router(providers) -> ProviderRouter:
        bundle = ConfigBundle(
            app=AppConfig(),
            markets=MarketsConfig(),
            strategies=StrategiesConfig(),
            providers=[provider.cfg for provider in providers],
            risk=RiskConfig(),
        )
        return ProviderRouter(bundle, providers)

    def test_raw_query_prefers_pinned_sidecar_but_qfq_filters_it(self) -> None:
        sidecar = FreeStockDbProvider(_cfg(bars_priority=30))
        sidecar._request_local = lambda _url: json.dumps(_daily_row()).encode(  # type: ignore[method-assign]
            "utf-8"
        )
        remote = _BarsProvider(
            ProviderConfig(
                name="eastmoney",
                cls="EastmoneyProvider",
                markets=["a"],
                bars_priority=0,
            ),
            "eastmoney",
        )
        router = self._router([remote, sidecar])
        raw = router.fetch_bars(
            "600633.SH",
            T.Market.A,
            start=_START,
            end=_END,
            adjust="raw",
        )
        self.assertEqual(raw[0].source, "free_stockdb")
        self.assertEqual(remote.calls, 0)

        qfq = router.fetch_bars(
            "600633.SH",
            T.Market.A,
            start=_START,
            end=_END,
            adjust="qfq",
        )
        self.assertEqual(qfq[0].source, "eastmoney")
        self.assertEqual(remote.calls, 1)

    def test_config_identity_change_changes_read_evidence_identity(self) -> None:
        provider = FreeStockDbProvider(_cfg())
        raw = json.dumps(_daily_row()).encode("utf-8")
        provider._request_local = lambda _url: raw  # type: ignore[method-assign]
        _, first = provider.fetch_bars_with_evidence(
            "600633.SH",
            T.Market.A,
            start=_START,
            end=_END,
        )
        changed = FreeStockDbProvider(
            replace(_cfg(), data_snapshot_manifest_sha256="d" * 64)
        )
        changed._request_local = lambda _url: raw  # type: ignore[method-assign]
        _, second = changed.fetch_bars_with_evidence(
            "600633.SH",
            T.Market.A,
            start=_START,
            end=_END,
        )
        self.assertNotEqual(first.evidence_id, second.evidence_id)


class _FixtureSidecarHandler(BaseHTTPRequestHandler):
    seen_queries: ClassVar[list[dict[str, list[str]]]] = []

    def do_GET(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        type(self).seen_queries.append(query)
        payload = json.dumps(_daily_row(), ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:
        return


class TestFreeStockDbVerificationCli(unittest.TestCase):
    def test_help_has_no_write_database_training_or_promotion_surface(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lowered = result.stdout.lower()
        for forbidden in (
            "--database",
            "--apply",
            "--set",
            "--write",
            "--train",
            "--backtest",
            "--verified",
            "--trust-tier",
            "--promote",
            "--binary-sha256",
            "--data-snapshot-sha256",
            "--sync-manifest-sha256",
        ):
            self.assertNotIn(forbidden, lowered)

    def test_cli_rejects_symlinked_audit_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary, data_manifest, sync_manifest = _audit_files(root)
            linked_binary = root / "linked-stockdb.exe"
            try:
                linked_binary.symlink_to(binary)
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--release-version",
                    "synthetic-release-v1",
                    "--binary-path",
                    str(linked_binary),
                    "--data-snapshot-manifest-path",
                    str(data_manifest),
                    "--sync-manifest-path",
                    str(sync_manifest),
                    "--symbol",
                    "600633.SH",
                    "--start",
                    "2026-06-22",
                    "--end",
                    "2026-06-26",
                    "--output",
                    str(root / "verification.json"),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("symlink", result.stderr)

    def test_cli_rejects_more_than_one_hundred_symbols_before_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary, data_manifest, sync_manifest = _audit_files(root)
            command = [
                sys.executable,
                str(SCRIPT),
                "--release-version",
                "synthetic-release-v1",
                "--binary-path",
                str(binary),
                "--data-snapshot-manifest-path",
                str(data_manifest),
                "--sync-manifest-path",
                str(sync_manifest),
                "--start",
                "2026-06-22",
                "--end",
                "2026-06-26",
                "--output",
                str(Path(directory) / "verification.json"),
            ]
            for code in range(600000, 600101):
                command.extend(("--symbol", f"{code:06d}.SH"))
            result = subprocess.run(
                command,
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("at most 100", result.stderr)

    def test_cli_queries_localhost_and_writes_t1_report(self) -> None:
        _FixtureSidecarHandler.seen_queries = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureSidecarHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                binary, data_manifest, sync_manifest = _audit_files(root)
                output = root / "verification.json"
                result = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPT),
                        "--host",
                        f"127.0.0.1:{server.server_port}",
                        "--release-version",
                        "synthetic-release-v1",
                        "--binary-path",
                        str(binary),
                        "--data-snapshot-manifest-path",
                        str(data_manifest),
                        "--sync-manifest-path",
                        str(sync_manifest),
                        "--symbol",
                        "600633.SH",
                        "--start",
                        "2026-06-22",
                        "--end",
                        "2026-06-26",
                        "--output",
                        str(output),
                    ],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(output.read_text(encoding="utf-8"))
                self.assertTrue(report["all_queries_contract_valid"])
                self.assertTrue(report["all_queries_nonempty"])
                self.assertFalse(report["running_process_identity_attested"])
                self.assertFalse(report["data_manifest_entries_verified"])
                self.assertEqual(
                    report["identity_source"],
                    "COMPUTED_FROM_LOCAL_FILES",
                )
                self.assertEqual(
                    report["binary_inventory"][0]["sha256"],
                    hashlib.sha256(binary.read_bytes()).hexdigest(),
                )
                self.assertEqual(
                    report["data_snapshot_manifest"]["sha256"],
                    hashlib.sha256(data_manifest.read_bytes()).hexdigest(),
                )
                self.assertEqual(
                    report["sync_manifest"]["sha256"],
                    hashlib.sha256(sync_manifest.read_bytes()).hexdigest(),
                )
                self.assertEqual(report["trust_tier"], "T1_BEST_EFFORT")
                self.assertEqual(report["license_status"], "LICENSE_PENDING")
                self.assertEqual(report["evidence_tier_status"], "T3_NOT_REACHED")
                self.assertFalse(report["production_database_modified"])
                self.assertFalse(report["allow_model_training"])
                self.assertEqual(report["results"][0]["bar_count"], 1)
                query = _FixtureSidecarHandler.seen_queries[0]
                self.assertEqual(query["cmd"], ["get"])
                self.assertNotIn("set", query)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()

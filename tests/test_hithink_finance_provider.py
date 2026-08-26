from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from stock_tracker import __main__ as entrypoint
from stock_tracker.collector.hithink_finance import (
    HITHINK_FINANCE_API_KEY_ENV,
    HithinkFinanceContractError,
    HithinkFinanceProvider,
)
from stock_tracker.collector.router import ProviderRouter
from stock_tracker.core import types as T
from stock_tracker.core.config import ProviderConfig, load_configs, load_providers

ROOT = Path(__file__).resolve().parents[1]
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_CREDENTIAL = "fixture-hithink-credential-" + ("x" * 24)


def _config(**overrides) -> ProviderConfig:
    values = {
        "name": "hithink_finance",
        "cls": "HithinkFinanceProvider",
        "markets": ["a"],
        "enabled": True,
        "primary": False,
        "supports_snapshot": False,
        "timeout_ms": 1000,
        "host": "",
        "max_rps": 1000,
        "bars_fallback": False,
        "bars_priority": 40,
        "read_only": True,
        "trust_tier": "T1_BEST_EFFORT",
        "allow_live_decision": False,
        "allow_model_training": False,
        "allow_public_redistribution": False,
    }
    values.update(overrides)
    return ProviderConfig(**values)


def _date_ms(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=_SHANGHAI).timestamp() * 1000)


def _payload(*, code: int = 0, request_id: str = "request-fixture") -> bytes:
    data = None
    if code == 0:
        data = {
            "timestamp": _date_ms(2024, 1, 3),
            "item": [
                {
                    "date_ms": _date_ms(2024, 1, 3),
                    "open_price": 11.0,
                    "high_price": 12.0,
                    "low_price": 10.5,
                    "close_price": 11.5,
                    "volume": 1200.0,
                    "turnover": 345600.0,
                },
                {
                    "date_ms": _date_ms(2024, 1, 2),
                    "open_price": 10.0,
                    "high_price": 11.0,
                    "low_price": 9.8,
                    "close_price": 10.8,
                    "volume": 1000.0,
                    "turnover": 234500.0,
                },
            ],
        }
    return json.dumps(
        {
            "code": code,
            "message": "fixture",
            "request_id": request_id,
            "data": data,
        },
        separators=(",", ":"),
    ).encode("utf-8")


class _Response:
    def __init__(self, raw: bytes, url: str) -> None:
        self.status = 200
        self.headers = {"Content-Type": "application/json; charset=utf-8"}
        self._raw = raw
        self._url = url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback

    def geturl(self) -> str:
        return self._url

    def read(self, limit: int) -> bytes:
        return self._raw[:limit]


class _Opener:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.requests = []
        self.timeouts: list[float] = []

    def open(self, request, timeout: float):
        self.requests.append(request)
        self.timeouts.append(timeout)
        return _Response(self.raw, request.full_url)


class TestHithinkFinanceProvider(unittest.TestCase):
    def _provider(self, raw: bytes | None = None, **config_overrides):
        opener = _Opener(raw or _payload())
        provider = HithinkFinanceProvider(
            _config(**config_overrides),
            credential_provider=lambda: _CREDENTIAL,
            opener=opener,
        )
        return provider, opener

    def test_committed_config_is_disabled_and_research_only(self) -> None:
        configs = load_providers(str(ROOT / "config" / "providers.toml"))
        matches = [config for config in configs if config.name == "hithink_finance"]
        self.assertEqual(len(matches), 1)
        config = matches[0]
        self.assertFalse(config.enabled)
        self.assertFalse(config.primary)
        self.assertFalse(config.supports_snapshot)
        self.assertTrue(config.read_only)
        self.assertEqual(config.trust_tier, "T1_BEST_EFFORT")
        self.assertFalse(config.allow_live_decision)
        self.assertFalse(config.allow_model_training)
        self.assertFalse(config.allow_public_redistribution)

    def test_normal_engine_startup_skips_disabled_provider_without_credential(self) -> None:
        bundle = load_configs(str(ROOT / "config"))
        providers = entrypoint._build_providers(bundle, mock.Mock())
        self.assertNotIn("hithink_finance", {provider.name for provider in providers})

    def test_missing_credential_fails_without_echoing_a_value(self) -> None:
        with self.assertRaisesRegex(
            HithinkFinanceContractError,
            HITHINK_FINANCE_API_KEY_ENV,
        ) as raised:
            HithinkFinanceProvider(
                _config(),
                credential_provider=lambda: None,
                opener=_Opener(_payload()),
            )
        self.assertNotIn(_CREDENTIAL, str(raised.exception))

    def test_policy_cannot_be_upgraded_by_config(self) -> None:
        base = _config()
        invalid = (
            replace(base, primary=True),
            replace(base, supports_snapshot=True),
            replace(base, read_only=False),
            replace(base, trust_tier="T2_OPERATIONAL_VERIFIED"),
            replace(base, allow_live_decision=True),
            replace(base, allow_model_training=True),
            replace(base, allow_public_redistribution=True),
            replace(base, markets=["a", "hk"]),
            replace(base, host="https://attacker.example"),
        )
        for config in invalid:
            with self.subTest(config=config), self.assertRaises(
                HithinkFinanceContractError
            ):
                HithinkFinanceProvider(
                    config,
                    credential_provider=lambda: _CREDENTIAL,
                    opener=_Opener(_payload()),
                )

    def test_request_is_exact_authenticated_and_parses_daily_bars(self) -> None:
        provider, opener = self._provider()
        raw = provider.fetch_bars_raw(
            "600519.SH",
            T.Market.A,
            interval="1d",
            start=datetime(2024, 1, 1, tzinfo=_SHANGHAI),
            end=datetime(2024, 1, 31, tzinfo=_SHANGHAI),
            adjust="qfq",
        )
        bars = provider.parse_bars_strict(raw, "600519.SH", T.Market.A, "1d")
        self.assertEqual([bar.timestamp.date().isoformat() for bar in bars], [
            "2024-01-02",
            "2024-01-03",
        ])
        self.assertEqual(bars[0].volume, 1000)
        self.assertEqual(bars[0].amount, 234500.0)
        self.assertEqual(bars[0].source, "hithink_finance")
        self.assertEqual(bars[0].quality_status, T.DataStatus.UNKNOWN)

        self.assertEqual(len(opener.requests), 1)
        request = opener.requests[0]
        parsed = urlparse(request.full_url)
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.hostname, "fuyao.aicubes.cn")
        self.assertEqual(parsed.path, provider.HISTORICAL_ENDPOINT)
        self.assertEqual(query["thscode"], ["600519.SH"])
        self.assertEqual(query["interval"], ["1d"])
        self.assertEqual(query["adjust"], ["forward"])
        self.assertEqual(query["offset"], ["0"])
        headers = {name.lower(): value for name, value in request.header_items()}
        auth_header = "-".join(("x", "api", "key"))  # noqa: FLY002
        self.assertEqual(headers[auth_header], _CREDENTIAL)
        self.assertNotIn(_CREDENTIAL, repr(provider.__dict__))

    def test_business_error_is_bounded_and_preserves_request_id(self) -> None:
        provider, _ = self._provider(_payload(code=2003, request_id="req-2003"))
        with self.assertRaisesRegex(
            HithinkFinanceContractError,
            "code=2003; request_id=req-2003",
        ) as raised:
            provider.fetch_bars(
                "600519.SH",
                T.Market.A,
                start=datetime(2024, 1, 1, tzinfo=_SHANGHAI),
                end=datetime(2024, 1, 31, tzinfo=_SHANGHAI),
                adjust="raw",
            )
        self.assertNotIn(_CREDENTIAL, str(raised.exception))

    def test_time_window_requires_aware_datetimes_and_calendar_year_limit(self) -> None:
        provider, _ = self._provider()
        with self.assertRaisesRegex(HithinkFinanceContractError, "timezone-aware"):
            provider.historical_request_parameters(
                "600519.SH",
                T.Market.A,
                "1d",
                datetime(2024, 1, 1),  # noqa: DTZ001 - deliberate naive negative case
                datetime(2024, 1, 31, tzinfo=_SHANGHAI),
                "raw",
            )
        with self.assertRaisesRegex(HithinkFinanceContractError, "10 years"):
            provider.historical_request_parameters(
                "600519.SH",
                T.Market.A,
                "1d",
                datetime(2016, 2, 29, tzinfo=_SHANGHAI),
                datetime(2026, 3, 1, tzinfo=_SHANGHAI),
                "raw",
            )

    def test_malformed_rows_fail_closed(self) -> None:
        malformed = json.loads(_payload())
        malformed["data"]["item"][0]["low_price"] = 99.0
        provider, _ = self._provider(
            json.dumps(malformed, separators=(",", ":")).encode("utf-8")
        )
        with self.assertRaisesRegex(HithinkFinanceContractError, "OHLC"):
            provider.parse_bars_strict(
                provider._opener.raw,
                "600519.SH",
                T.Market.A,
                "1d",
            )

    def test_research_only_provider_is_excluded_from_runtime_routes(self) -> None:
        provider, _ = self._provider()
        router = ProviderRouter(load_configs(str(ROOT / "config")), [provider])
        self.assertIsNone(router.select(T.Market.A, "quote"))
        self.assertEqual(
            router._select_bars(T.Market.A, "raw"),
            [],
        )
        self.assertFalse(provider.supports_quotes())
        self.assertFalse(provider.supports_bars())
        self.assertTrue(provider.supports_raw_bars())


if __name__ == "__main__":
    unittest.main()

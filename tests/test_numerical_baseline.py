"""Runtime numeric-domain and TLS invariants; no provider or production DB access."""
from __future__ import annotations

import math
import ssl
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from stock_tracker.api.serializers import serialize_indicators
from stock_tracker.collector.provider import MarketDataProvider, _ssl_ctx
from stock_tracker.features import indicators as I


class TestRuntimeTLSBaseline(unittest.TestCase):
    def test_chain_and_hostname_are_both_verified(self):
        context = _ssl_ctx()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_certificate_rejection_is_not_retried_insecurely(self):
        instance = SimpleNamespace(timeout=1, _with_host=lambda url: url)
        with (
            patch('stock_tracker.collector.provider.urllib_request.urlopen',
                  side_effect=ssl.SSLCertVerificationError('synthetic rejection')) as request,
            self.assertRaises(ssl.SSLCertVerificationError),
        ):
            MarketDataProvider._request(instance, 'https://fixture.invalid/quotes')
        self.assertEqual(request.call_count, 1)
        self.assertTrue(request.call_args.kwargs['context'].check_hostname)
        self.assertEqual(request.call_args.kwargs['context'].verify_mode, ssl.CERT_REQUIRED)


class TestIndicatorDomains(unittest.TestCase):
    def test_invalid_periods_do_not_escape_or_compute(self):
        values = [float(i + 1) for i in range(70)]
        for period in (True, False, 0, -1, 2.5, '14', None, 10**400):
            for name in ('sma', 'ema', 'rsi', 'roc'):
                with self.subTest(name=name, period=repr(period)):
                    self.assertIsNone(getattr(I, name)(values, period))
            with self.subTest(name='atr', period=repr(period)):
                self.assertIsNone(I.atr(values, values, values, period))

    def test_non_numeric_and_nonfinite_series_are_unknown(self):
        for invalid in (True, '2', None, math.nan, math.inf, 10**400):
            values = [1.0] * 70
            values[30] = invalid
            for name in ('sma', 'ema', 'rsi', 'roc'):
                with self.subTest(name=name, invalid=repr(invalid)):
                    self.assertIsNone(getattr(I, name)(values, 14))
            self.assertEqual(I.macd(values), (None, None, None))
            self.assertIsNone(I.stdev(values))
            self.assertIsNone(I.stdev_pop(values))

    def test_wrong_container_is_rejected(self):
        for values in ('12345', 4, None, {1, 2, 3}, {1: 2}):
            with self.subTest(values=values):
                self.assertIsNone(I.sma(values, 2))
                self.assertIsNone(I.ema(values, 2))
                self.assertIsNone(I.stdev(values))
                self.assertEqual(I.macd(values), (None, None, None))

    def test_misaligned_or_invalid_ohlc_not_silently_truncated(self):
        self.assertIsNone(I.atr([12.0]*20, [10.0]*19, [11.0]*20))
        self.assertIsNone(I.atr([9.0]*20, [10.0]*20, [9.5]*20))
        self.assertIsNone(I.atr([12.0]*20, [10.0]*20, [13.0]*20))

    def test_macd_parameters_and_output_overflow(self):
        for params in ((0, 26, 9), (12, 26, True), (30, 26, 9), (12, 12, 9), (12, 26, -1)):
            with self.subTest(params=params):
                self.assertEqual(I.macd([float(x) for x in range(70)], *params), (None, None, None))
        self.assertEqual(I.macd([1e308]*70), (None, None, None))
        self.assertIsNone(I.sma([1e308]*20, 20))
        self.assertIsNone(I.stdev([1e308, -1e308]))

    def test_percentile_controls(self):
        for window, pct in ((0, 50), (True, 50), (5, True), (5, -1), (5, 101), (5, math.nan)):
            with self.subTest(window=window, pct=pct):
                self.assertIsNone(I.rolling_percentile([1., 2., 3.], window, pct))

    def test_legacy_formula_conventions_are_retained(self):
        self.assertEqual(I.ema([2., 4.], 5), 3.0)
        self.assertEqual(I.rsi([10.]*20), 100.0)
        self.assertEqual(I.rsi([20.-i for i in range(20)]), 0.0)
        self.assertEqual(I.atr([12.]*20, [10.]*20, [11.]*20), 2.0)
        self.assertEqual(I.roc([100., 100., 110.], 2), 10.0)
        dif, dea, hist = I.macd([float(i+1) for i in range(70)])
        self.assertAlmostEqual(hist, dif-dea)

    def test_serializer_cannot_reintroduce_nonfinite_or_bool(self):
        result = serialize_indicators({'rsi14': True, 'ma20': math.inf, 'roc20': '1', 'atr14': 0.0,
                                       'ma60': None, 'bar_count': 60, 'too_big': 10**400})
        for name in ('rsi14', 'ma20', 'roc20', 'ma60', 'too_big'):
            self.assertIsNone(result[name])
        self.assertEqual(result['atr14'], 0.0)
        self.assertEqual(result['bar_count'], 60)


if __name__ == '__main__':
    unittest.main()

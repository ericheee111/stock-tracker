"""Offline synthetic numeric compatibility check; never opens databases or providers."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tracker.features import indicators as I
from stock_tracker.quant.evaluation import metrics as M

FIXTURE_SHA256 = '9cdd460d4619da5a91bd9fa470504bbed61d1932e33ef107fb9f373006fa662f'
BASE_COMMIT = 'cdf8c1b4114252ead3bf5eb19d2f624be8929bd8'
SOURCE_IDENTITIES = {'indicator': {'path': 'stock_tracker/features/indicators.py', 'sha256': '88b4d90cab5cad7615cebfd7ba2d24067ff65b702caa171216c812a08d88444a', 'git_blob': 'dac338c9250716a275a3177f2ffea12a64b3e37e'}, 'metric': {'path': 'stock_tracker/quant/evaluation/metrics.py', 'sha256': '1decfa11f67e8dad0f29b4119b62b52d306858561e2be5208789825647f956b8', 'git_blob': '843ba01b42dc4ee295fae050c3e939ef47e46eae'}}


def validate_provenance(fixture: dict[str, Any]) -> dict[str, Any]:
    if fixture.get('base_commit') != BASE_COMMIT or fixture.get('source_identity_basis') != 'EXACT_GIT_BLOB_BYTES':
        raise ValueError('Frozen baseline provenance mismatch')
    for key, identity in SOURCE_IDENTITIES.items():
        if (fixture.get(key+'_source_sha256') != identity['sha256']
                or fixture.get(key+'_source_git_blob') != identity['git_blob']):
            raise ValueError('Frozen source provenance mismatch: '+key)
    if not (ROOT / '.git').exists():
        return {'mode':'PINNED_MANIFEST_ONLY', 'actual_git_sources_verified':False}
    try:
        for identity in SOURCE_IDENTITIES.values():
            raw = subprocess.run(['git','show',BASE_COMMIT+':'+identity['path']], cwd=ROOT,
                                 capture_output=True, check=True, timeout=20).stdout
            if hashlib.sha256(raw).hexdigest() != identity['sha256']:
                raise ValueError('Actual Git source provenance mismatch')
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError('Baseline provenance cannot be reverified in this Git checkout') from error
    return {'mode':'EXACT_BASE_GIT_BLOBS_VERIFIED', 'actual_git_sources_verified':True}


def verify() -> dict[str, Any]:
    data = (ROOT / 'tests/fixtures/numerical_legacy_v1.json').read_bytes()
    if hashlib.sha256(data).hexdigest() != FIXTURE_SHA256:
        raise ValueError('Frozen baseline fixture changed; do not regenerate to hide regressions')
    fixture = json.loads(data)
    provenance = validate_provenance(fixture)
    mismatches: list[dict[str, Any]] = []
    comparisons = 0
    for item in fixture['cases']:
        values = item['values']
        actual = {'sma20': I.sma(values, 20), 'ema12': I.ema(values, 12),
                  'rsi14': I.rsi(values, 14), 'atr14': I.atr(item['highs'], item['lows'], values, 14),
                  'roc20': I.roc(values, 20), 'macd': list(I.macd(values)),
                  'p50': I.rolling_percentile(values, 60, 50), 'stdev': I.stdev(values),
                  'stdev_pop': I.stdev_pop(values)}
        for field, expected in item['expected'].items():
            comparisons += 1
            if actual[field] != expected:
                mismatches.append({'case': item['name'], 'field': field, 'expected': expected, 'actual': actual[field]})
    for index, item in enumerate(fixture['metrics']):
        labels, probs = item['labels'], item['probabilities']
        actual = {'brier': M.brier_score(labels, probs), 'logloss': M.log_loss(labels, probs),
                  'ece': M.expected_calibration_error(labels, probs),
                  'precision': M.precision_at_k(labels, probs, item['k']),
                  'net': M.top_k_net_expectancy(item['returns'], probs, item['k'], costs_r=item['costs']),
                  'drawdown': M.max_drawdown(item['returns'])}
        for field, expected in item['expected'].items():
            comparisons += 1
            if actual[field] != expected:
                mismatches.append({'case': f'metric-{index}', 'field': field, 'expected': expected, 'actual': actual[field]})
    return {'schema': 'numerical-baseline-report-v1', 'base_commit': fixture['base_commit'],
            'fixture_sha256': FIXTURE_SHA256, 'source_provenance': provenance, 'indicator_policy': I.INDICATOR_INPUT_POLICY_ID,
            'metric_policy': M.METRIC_INPUT_POLICY_ID, 'indicator_vectors': len(fixture['cases']),
            'metric_vectors': len(fixture['metrics']), 'comparisons': comparisons,
            'mismatches': mismatches, 'passed': comparisons > 0 and not mismatches,
            'scope': 'FROZEN_NORMAL_DOMAIN_COMPATIBILITY_ONLY', 'synthetic_fixture_only': True,
            'investment_performance_claim': False, 'auto_trade': False,
            'strategy_weights_changed': False}


def main() -> int:
    if len(sys.argv) != 1:
        print('This read-only command accepts no arguments.', file=sys.stderr)
        return 2
    try:
        report = verify()
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({'passed': False, 'error': str(error), 'synthetic_fixture_only': True}, allow_nan=False))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

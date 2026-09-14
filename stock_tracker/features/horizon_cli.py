"""Bounded offline N4b input adapter. No Store, network, training or portfolio writes."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import fields
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..core.types import Market
from .horizon_research import (
    DeclaredCalendarDay,
    DeclaredDailyObservation,
    ExperimentSample,
    HoldingPurpose,
    HorizonExperiment,
    HorizonResearchError,
    closed_week_facts,
    purged_experiment_manifest,
)

MAX_INPUT_BYTES = 2 * 1024 * 1024
WEEK_INPUT_SCHEMA = 'n4-declared-week-input-v2'
EXPERIMENT_INPUT_SCHEMA = 'n4-experiment-input-v2'


def _object(value: Any, names: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != names:
        raise HorizonResearchError('EXACT_DOCUMENT_FIELDS_REQUIRED')
    return dict(value)


def _timestamp(value: Any) -> datetime:
    if type(value) is not str or len(value) > 40:
        raise HorizonResearchError('AWARE_ISO_TIME_REQUIRED')
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HorizonResearchError('AWARE_ISO_TIME_REQUIRED') from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise HorizonResearchError('AWARE_ISO_TIME_REQUIRED')
    return result


def _date(value: Any) -> date:
    if type(value) is not str:
        raise HorizonResearchError('ISO_DATE_REQUIRED')
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        raise HorizonResearchError('ISO_DATE_REQUIRED')
    return result


def _market(value: Any) -> Market:
    if type(value) is not str:
        raise HorizonResearchError('MARKET_TEXT_REQUIRED')
    return Market(value)


def _record(value: Any, cls: Any) -> Any:
    d = _object(value, {f.name for f in fields(cls)})
    if 'market' in d:
        d['market'] = _market(d['market'])
    if 'purpose' in d:
        if type(d['purpose']) is not str:
            raise HorizonResearchError('PURPOSE_TEXT_REQUIRED')
        d['purpose'] = HoldingPurpose(d['purpose'])
    if 'day' in d:
        d['day'] = _date(d['day'])
    for key in ('known_at', 'usable_from', 'decision_at', 'feature_known_at', 'feature_usable_from',
                'label_end_at', 'label_known_at', 'label_usable_from'):
        if key in d:
            d[key] = _timestamp(d[key])
    if 'close_at' in d and d['close_at'] is not None:
        d['close_at'] = _timestamp(d['close_at'])
    if cls is DeclaredDailyObservation:
        for key in ('open', 'high', 'low', 'close'):
            if type(d[key]) is not str or re.fullmatch(r'(?:0|[1-9][0-9]{0,27})(?:\.[0-9]{1,28})?', d[key]) is None:
                raise HorizonResearchError('CANONICAL_DECIMAL_TEXT_REQUIRED')
            d[key] = Decimal(d[key])
    return cls(**d)


def _array(value: Any, limit: int) -> list[Any]:
    if type(value) is not list or len(value) > limit:
        raise HorizonResearchError('BOUNDED_ARRAY_REQUIRED')
    return value


def evaluate_document(value: Any) -> dict[str, Any]:
    """Exact versioned document in; declared-only reproducible report out."""
    if type(value) is not dict:
        raise HorizonResearchError('DOCUMENT_REQUIRED')
    if value.get('schema') == WEEK_INPUT_SCHEMA:
        d = _object(value, {'schema', 'symbol', 'market', 'as_of', 'calendar', 'observations'})
        return closed_week_facts(
            tuple(_record(row, DeclaredCalendarDay) for row in _array(d['calendar'], 371)),
            tuple(_record(row, DeclaredDailyObservation) for row in _array(d['observations'], 371)),
            symbol=d['symbol'], market=_market(d['market']), as_of=_timestamp(d['as_of']),
        )
    if value.get('schema') == EXPERIMENT_INPUT_SCHEMA:
        d = _object(value, {'schema', 'spec', 'samples', 'train_start', 'calibration_start',
                            'validation_start', 'validation_end', 'embargo_microseconds', 'as_of'})
        embargo = d['embargo_microseconds']
        if type(embargo) is not int or not 0 <= embargo <= 10**15:
            raise HorizonResearchError('EXPLICIT_BOUNDED_EMBARGO_REQUIRED')
        return purged_experiment_manifest(
            _record(d['spec'], HorizonExperiment),
            tuple(_record(row, ExperimentSample) for row in _array(d['samples'], 10000)),
            train_start=_timestamp(d['train_start']), calibration_start=_timestamp(d['calibration_start']),
            validation_start=_timestamp(d['validation_start']), validation_end=_timestamp(d['validation_end']),
            as_of=_timestamp(d['as_of']), embargo=timedelta(microseconds=embargo),
        )
    raise HorizonResearchError('UNKNOWN_INPUT_SCHEMA')


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise HorizonResearchError('DUPLICATE_JSON_KEY')
        out[key] = value
    return out


def _constant(_: str) -> Any:
    raise HorizonResearchError('NONFINITE_JSON_TOKEN')


def parse_document(raw: bytes) -> dict[str, Any]:
    if type(raw) is not bytes or not raw or len(raw) > MAX_INPUT_BYTES:
        raise HorizonResearchError('INPUT_BYTE_LIMIT')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, HorizonResearchError):
            raise
        raise HorizonResearchError('INVALID_JSON_DOCUMENT') from exc
    if type(value) is not dict:
        raise HorizonResearchError('DOCUMENT_REQUIRED')
    return value


def fixture_document(kind: str) -> dict[str, Any]:
    """Illustrative declared schedules/labels, not an exchange calendar or trading defaults."""
    h = lambda text: hashlib.sha256(text.encode()).hexdigest()
    at = datetime(2026, 9, 14, 8, tzinfo=UTC)
    if kind == 'week':
        calendar, observations = [], []
        for i in range(7):
            day = date(2026, 9, 7) + timedelta(days=i)
            close = datetime(day.year, day.month, day.day, 7, tzinfo=UTC)
            known = at - timedelta(days=10)
            calendar.append({'day': day.isoformat(), 'market': 'A', 'is_open': i < 5,
                             'close_at': close.isoformat() if i < 5 else None,
                             'known_at': known.isoformat(), 'usable_from': known.isoformat(), 'evidence_id': h('fixture-calendar')})
            if i < 5:
                available = (close+timedelta(seconds=1)).isoformat()
                observations.append({'symbol': '600519.SH', 'market': 'A', 'day': day.isoformat(),
                                     'close_at': close.isoformat(), 'known_at': available, 'usable_from': available,
                                     'open': str(10+i), 'high': str(12+i), 'low': str(9+i), 'close': str(11+i),
                                     'volume': 100+i, 'price_basis_id': h('fixture-raw'), 'source_id': h('fixture-source')})
        return {'schema': WEEK_INPUT_SCHEMA, 'symbol': '600519.SH', 'market': 'A', 'as_of': at.isoformat(),
                'calendar': calendar, 'observations': observations}
    if kind != 'experiment':
        raise HorizonResearchError('UNKNOWN_FIXTURE')
    start = at - timedelta(days=90)
    spec = {'purpose': 'SWING', 'market': 'A', 'lookback_sessions': 60, 'horizon_sessions': 20,
            'review_every_sessions': 5, 'label_policy_id': h('fixture-label'), 'exit_policy_id': h('fixture-exit'),
            'cost_model_id': h('fixture-cost'), 'price_basis_id': h('fixture-raw')}
    definition = _record(spec, HorizonExperiment).definition_id
    samples = []
    for i, days in enumerate((1, 28, 31, 58, 61, 88)):
        decision = (start+timedelta(days=days)).isoformat()
        label = (start+timedelta(days=days+2)).isoformat()
        samples.append({'sample_id': h('sample'+str(i)), 'episode_id': h('episode'+str(i)), 'purpose': 'SWING',
                        'market': 'A', 'decision_at': decision, 'feature_known_at': decision, 'feature_usable_from': decision,
                        'label_end_at': label, 'label_known_at': label, 'label_usable_from': label,
                        'source_snapshot_id': h('fixture-source'), 'definition_id': definition})
    return {'schema': EXPERIMENT_INPUT_SCHEMA, 'spec': spec, 'samples': samples, 'train_start': start.isoformat(),
            'calibration_start': (start+timedelta(days=30)).isoformat(), 'validation_start': (start+timedelta(days=60)).isoformat(),
            'validation_end': at.isoformat(), 'as_of': at.isoformat(), 'embargo_microseconds': 86400000000}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='N4b offline declared-input diagnostics; no model fitting or data capture.')
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--input', type=Path, help='Exact v2 JSON document; never a database')
    source.add_argument('--fixture', choices=('week', 'experiment'), help='Explicit synthetic fixture only')
    parser.add_argument('--output', type=Path, help='New .json file; existing paths are never overwritten')
    args = parser.parse_args(argv)
    try:
        if args.input is not None:
            with args.input.open('rb') as stream:
                document = parse_document(stream.read(MAX_INPUT_BYTES + 1))
        else:
            document = fixture_document(args.fixture)
        result = evaluate_document(document)
        report = {'schema': 'n4-offline-horizon-report-v1', 'input_mode': 'SYNTHETIC_FIXTURE' if args.fixture else 'DECLARED_JSON',
                  'result': result, 'source_authority_verified': False, 'model_fitted': False, 'auto_trade': False}
        encoded = json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + '\n'
        if args.output is not None:
            if args.output.suffix != '.json':
                raise HorizonResearchError('NEW_JSON_OUTPUT_REQUIRED')
            with args.output.open('x', encoding='utf-8', newline='\n') as stream:
                stream.write(encoded)
        print(encoded, end='')
        return 0
    except (OSError, ValueError, TypeError, OverflowError, RecursionError) as exc:
        print(json.dumps({'schema': 'n4-offline-horizon-error-v1', 'error_type': type(exc).__name__,
                          'code': str(exc) if isinstance(exc, HorizonResearchError) else 'INVALID_INPUT_OR_OUTPUT',
                          'model_fitted': False, 'auto_trade': False}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

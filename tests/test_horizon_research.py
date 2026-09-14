"""Calendar-aware declared weekly facts and leakage-safe experiment manifests."""
from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from stock_tracker.core.types import Market
from stock_tracker.features.horizon_research import (
    DeclaredCalendarDay,
    DeclaredDailyObservation,
    ExperimentSample,
    HoldingPurpose,
    HorizonExperiment,
    HorizonResearchError,
    closed_week_facts,
    purged_experiment_manifest,
)


def h(text): return hashlib.sha256(text.encode()).hexdigest()
NOW = datetime(2026, 9, 14, 8, tzinfo=UTC)


def week(market=Market.A, monday=date(2026, 9, 7)):
    zone = ZoneInfo({Market.A:'Asia/Shanghai', Market.HK:'Asia/Hong_Kong', Market.US:'America/New_York'}[market])
    symbol = {Market.A:'600519.SH', Market.HK:'00700.HK', Market.US:'AAPL.US'}[market]
    days, bars = [], []
    for i in range(7):
        day = monday + timedelta(days=i)
        close = datetime.combine(day, datetime.min.time(), zone) + timedelta(hours=16)
        days.append(DeclaredCalendarDay(day, i < 5, close if i < 5 else None,
                                        NOW-timedelta(days=10), h('calendar'), market=market, usable_from=NOW-timedelta(days=10)))
        if i < 5:
            bars.append(DeclaredDailyObservation(symbol, market, day, close, close+timedelta(seconds=1),
                        Decimal(10+i), Decimal(12+i), Decimal(9+i), Decimal(11+i), 100+i,
                        h('raw'), h('fixture'), usable_from=close+timedelta(seconds=1)))
    return tuple(days), tuple(bars), symbol


class TestClosedWeeks(unittest.TestCase):
    def test_complete_week_exact_ohlcv_and_no_trust_promotion(self):
        days, bars, symbol = week()
        report = closed_week_facts(days, bars, symbol=symbol, market=Market.A, as_of=NOW)
        w = report['weeks'][0]
        self.assertEqual((w['open'],w['high'],w['low'],w['close'],w['volume']), ('10','16','9','15',510))
        self.assertFalse(report['research_grade'])
        self.assertEqual(len(w['session_dates']),5)

    def test_holiday_is_explicit_not_weekday_guess(self):
        days, bars, symbol = week()
        days = days[:2]+(replace(days[2],is_open=False,close_at=None),)+days[3:]
        bars = bars[:2]+bars[3:]
        result = closed_week_facts(days,bars,symbol=symbol,market=Market.A,as_of=NOW)
        self.assertEqual(len(result['weeks'][0]['session_dates']),4)

    def test_incomplete_calendar_and_missing_bar_fail(self):
        days,bars,symbol=week()
        for ds,bs in ((days[:-1],bars),(days[:3]+days[4:]+days[-1:],bars),(days,bars[:-1]),(days,bars+bars[:1])):
            with self.subTest(n=len(ds)),self.assertRaises(HorizonResearchError):
                closed_week_facts(ds,bs,symbol=symbol,market=Market.A,as_of=NOW)

    def test_closed_day_extra_observation_is_not_discarded(self):
        days,bars,symbol=week()
        extra=replace(bars[-1],day=days[5].day,close_at=bars[-1].close_at+timedelta(days=1),known_at=bars[-1].known_at+timedelta(days=1),usable_from=bars[-1].usable_from+timedelta(days=1))
        with self.assertRaises(HorizonResearchError):
            closed_week_facts(days,bars+(extra,),symbol=symbol,market=Market.A,as_of=NOW)

    def test_future_known_and_incomplete_week_fail(self):
        days,bars,symbol=week()
        future=bars[:-1]+(replace(bars[-1],known_at=NOW+timedelta(days=1),usable_from=NOW+timedelta(days=1)),)
        with self.assertRaises(HorizonResearchError):
            closed_week_facts(days,future,symbol=symbol,market=Market.A,as_of=NOW)
        with self.assertRaises(HorizonResearchError):
            closed_week_facts(days,bars,symbol=symbol,market=Market.A,as_of=NOW-timedelta(days=1))
        altered=(replace(days[0],known_at=NOW+timedelta(seconds=1),usable_from=NOW+timedelta(seconds=1)),)+days[1:]
        with self.assertRaises(HorizonResearchError):
            closed_week_facts(altered,bars,symbol=symbol,market=Market.A,as_of=NOW)

    def test_basis_source_close_and_symbol_mismatches_fail(self):
        days,bars,symbol=week()
        changes=({'price_basis_id':h('different')},{'source_id':h('different')},{'symbol':'000001.SZ'},
                 {'close_at':bars[-1].close_at-timedelta(minutes=1)})
        for change in changes:
            with self.subTest(change=change),self.assertRaises(HorizonResearchError):
                closed_week_facts(days,bars[:-1]+(replace(bars[-1],**change),),symbol=symbol,market=Market.A,as_of=NOW)

    def test_all_closed_week_is_not_zero_price(self):
        days,_,symbol=week()
        days=tuple(replace(d,is_open=False,close_at=None) for d in days)
        result=closed_week_facts(days,(),symbol=symbol,market=Market.A,as_of=NOW)
        self.assertEqual(result['weeks'][0]['state'],'NO_OPEN_SESSIONS')
        self.assertNotIn('close',result['weeks'][0])

    def test_three_markets_and_us_dst_week_use_local_boundaries(self):
        for market,monday in ((Market.A,date(2026,9,7)),(Market.HK,date(2026,9,7)),(Market.US,date(2026,3,2))):
            days,bars,symbol=week(market,monday)
            # Calendar fixture's supplied known-at is still before evaluation.
            result=closed_week_facts(days,bars,symbol=symbol,market=market,as_of=NOW)
            self.assertEqual(result['weeks'][0]['week_start'],monday.isoformat())
            self.assertEqual(result['market'],market.value)

    def test_bool_calendar_price_and_naive_time_are_refused(self):
        days,bars,_=week()
        with self.assertRaises(HorizonResearchError):replace(days[0],is_open=1)
        with self.assertRaises(HorizonResearchError):replace(bars[0],open=True)
        with self.assertRaises(HorizonResearchError):replace(bars[0],known_at=NOW.replace(tzinfo=None))


class TestPurgedExperiments(unittest.TestCase):
    def setup_values(self):
        start=NOW-timedelta(days=90)
        spec=HorizonExperiment(HoldingPurpose.SWING,60,20,5,h('label'),h('exit'),h('cost'),h('raw'),market=Market.A)
        samples=tuple(ExperimentSample(h('s'+str(i)),h('e'+str(i)),HoldingPurpose.SWING,
                     start+timedelta(days=d),start+timedelta(days=d),start+timedelta(days=d+2),
                     start+timedelta(days=d+2),h('source'),spec.definition_id, market=Market.A,feature_usable_from=start+timedelta(days=d), label_usable_from=start+timedelta(days=d+2)) for i,d in enumerate((1,28,31,58,61,88)))
        kwargs={'train_start': start,'calibration_start': start+timedelta(days=30),
                    'validation_start': start+timedelta(days=60),'validation_end': NOW,'embargo': timedelta(days=1),'as_of': NOW}
        return spec,samples,kwargs

    def test_every_sample_assigned_or_purged_and_no_training(self):
        spec,samples,kwargs=self.setup_values()
        result=purged_experiment_manifest(spec,samples,**kwargs)
        self.assertEqual([len(v) for v in result['partitions'].values()],[1,1,1])
        self.assertEqual(len(result['purged']),3)
        self.assertFalse(result['model_fitted'])
        self.assertTrue(result['ready_for_fixture_comparison'])

    def test_overlapping_t_operations_cannot_inflate_episodes(self):
        spec,samples,kwargs=self.setup_values()
        with self.assertRaises(HorizonResearchError):
            purged_experiment_manifest(spec,(samples[0],replace(samples[1],episode_id=samples[0].episode_id)),**kwargs)

    def test_mixed_purpose_unordered_and_future_features_fail(self):
        spec,samples,kwargs=self.setup_values()
        for rows in ((replace(samples[0],purpose=HoldingPurpose.SHORT_TERM),)+samples[1:],tuple(reversed(samples))):
            with self.assertRaises(HorizonResearchError):purged_experiment_manifest(spec,rows,**kwargs)
        with self.assertRaises(HorizonResearchError):replace(samples[0],feature_known_at=NOW)
        with self.assertRaises(HorizonResearchError):replace(spec,horizon_sessions=True)

    def test_configuration_and_cost_identity_change_experiment(self):
        spec,samples,kwargs=self.setup_values()
        a=purged_experiment_manifest(spec,samples,**kwargs)
        changed=replace(spec,cost_model_id=h('other'))
        with self.assertRaises(HorizonResearchError):
            purged_experiment_manifest(changed,samples,**kwargs)
        b=purged_experiment_manifest(changed,tuple(replace(s,definition_id=changed.definition_id) for s in samples),**kwargs)
        self.assertNotEqual(a['experiment_id'],b['experiment_id'])

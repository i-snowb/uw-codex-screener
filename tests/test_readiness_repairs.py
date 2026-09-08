from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

from morning_edge.benchmarks import BENCHMARKS, relative_context
from morning_edge.clock import is_nyse_session
from morning_edge.config import Settings
from morning_edge.data_health import assert_publishable, chain_quality, run_health
from morning_edge.evidence_join import attach_enhanced, field_sources
from morning_edge.evaluation import _Observation, _option_outcome
from morning_edge.freshness import dataset_freshness, latest_complete_session
from morning_edge.greek_flow import summarize
from morning_edge.providers.unusual_whales import UnusualWhalesClient
from morning_edge.providers.base import ProviderSchemaError
from test_unusual_whales import ScriptedTransport

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import build_dashboard_bundle as bundle
import build_research_control_plane as control
import run_daily_capture as gateway

NOW = datetime(2026, 9, 8, 10, 45, tzinfo=UTC)


def minute(offset, delta=1, **changes):
    return {'timestamp': (datetime(2026, 9, 4, 13, 30, tzinfo=UTC) + timedelta(minutes=offset)).isoformat(),
            'dir_delta_flow': delta, 'dir_vega_flow': -2, 'otm_dir_delta_flow': 2,
            'otm_dir_vega_flow': -1, 'volume': 3, 'transactions': 2, **changes}


def ready_run():
    return {'run_id': 'test', 'cutoff_at': NOW.isoformat(), 'recommendations_enabled': False,
            'watchlist': [{'ticker': 'QCOM', 'action': 'NO_RECOMMENDATION', 'agent_enrichment_validated': True,
                'price': {'as_of': '2026-09-04'}, 'edge': {'option_surface': {'market_date': '2026-09-04'}, 'gex_topology': {'date': '2026-09-04'}},
                'benchmark_context': {name: {'status': 'ALIGNED_RESEARCH_CONTEXT', 'latest_session': '2026-09-04'} for name in BENCHMARKS}}]}


class GreekFlowRepairs(unittest.TestCase):
    def test_sums_distinct_minutes_not_last_row(self):
        result = summarize([minute(i, -1 if i == 389 else 1) for i in range(390)], cutoff_at=NOW)
        self.assertEqual(388, result['directional_delta_flow'])
        self.assertEqual(-1, result['latest_minute']['dir_delta_flow'])
        self.assertEqual(1170, result['volume'])
        self.assertEqual('COMPLETE_SESSION', result['coverage_status'])
        self.assertTrue(result['confirmation_eligible'])
        self.assertEqual(28, result['recent_30m_totals']['dir_delta_flow'])

    def test_deduplicates_identical_buckets_and_rejects_conflicts(self):
        self.assertEqual(1, summarize([minute(0), minute(0)])['directional_delta_flow'])
        self.assertEqual(1, summarize([minute(0), minute(0)])['duplicate_rows_removed'])
        with self.assertRaisesRegex(ValueError, 'conflicting duplicate'):
            summarize([minute(0), minute(0, 2)])

    def test_partial_is_not_zero_filled_or_confirmation_eligible(self):
        result = summarize([minute(0), minute(389)], cutoff_at=NOW)
        self.assertEqual(388, result['missing_minutes'])
        self.assertEqual(2, result['directional_delta_flow'])
        self.assertFalse(result['confirmation_eligible'])

    def test_cutoff_excludes_unfinished_minute(self):
        cutoff = datetime(2026, 9, 4, 13, 31, 30, tzinfo=UTC)
        result = summarize([minute(0), minute(1)], cutoff_at=cutoff)
        self.assertEqual(1, result['row_count'])
        self.assertEqual('COMPLETE_TO_CUTOFF', result['coverage_status'])
        self.assertTrue(result['confirmation_eligible'])
        self.assertEqual(1, result['unfinished_or_future_minutes_excluded'])

    def test_invalid_fields_do_not_become_zero(self):
        for bad in (None, True, 'nan', 'inf'):
            result = summarize([minute(0, bad)])
            self.assertIsNone(result['directional_delta_flow'])
            self.assertFalse(result['confirmation_eligible'])
        self.assertEqual(0, summarize([minute(0, 0)])['directional_delta_flow'])

    def test_only_latest_session_is_summed(self):
        old = minute(0, 100, timestamp='2026-09-03T13:30:00Z')
        self.assertEqual(1, summarize([old, minute(0)])['directional_delta_flow'])


class HealthRepairs(unittest.TestCase):
    def test_exceptional_closure_and_labor_day(self):
        self.assertFalse(is_nyse_session(date(2025, 1, 9)))
        self.assertFalse(is_nyse_session(date(2026, 9, 7)))
        self.assertEqual(date(2026, 9, 4), latest_complete_session(NOW))

    def test_future_date_and_holiday_cannot_look_fresh(self):
        for day in ('2026-09-09', '2026-09-07'):
            value = dataset_freshness(cutoff_at=NOW, price_session=day, dataset_dates={})
            self.assertEqual('INVALID_SESSION', value['datasets']['price']['status'])

    def test_publication_requires_current_session_benchmarks_and_enrichment(self):
        run = ready_run()
        self.assertEqual([], assert_publishable(run, observed_at=NOW)['failures'])
        for field, value in (('agent_enrichment_validated', False), ('action', 'BUY')):
            invalid = deepcopy(run); invalid['watchlist'][0][field] = value
            with self.assertRaises(ValueError):
                assert_publishable(invalid, observed_at=NOW)
        run['watchlist'][0]['benchmark_context']['SPY']['latest_session'] = '2026-08-25'
        with self.assertRaisesRegex(ValueError, 'health'):
            assert_publishable(run, observed_at=NOW)

    def test_new_retrieval_does_not_make_old_prices_current(self):
        run = ready_run(); run['watchlist'][0]['price']['as_of'] = '2026-09-01'
        self.assertEqual('BLOCKED', run_health(run)['status'])

    def test_chain_strata_measure_usable_subset_and_quote_timestamp(self):
        good = {'expiry': '2026-10-16', 'strike': 100, 'nbbo_bid': 4, 'nbbo_ask': 4.2, 'open_interest': 200, 'delta': .5, 'gamma': .02, 'implied_volatility': .4, 'last_tape_time': NOW.isoformat()}
        result = chain_quality([good, {'strike': 200}], spot=100, as_of=NOW.date())['strata']
        self.assertEqual(1, result['liquid']['usable_greeks'])
        self.assertEqual(1, result['all']['missing_greeks'])
        self.assertEqual(0, result['liquid']['quote_timestamp_present'])

    def test_benchmark_exact_session_alignment_no_sliding_over_gap(self):
        dates = []; day = date(2026, 9, 4)
        while len(dates) < 64:
            if is_nyse_session(day): dates.append(day.isoformat())
            day -= timedelta(days=1)
        bars = [{'date': day, 'close': 100 + i} for i, day in enumerate(reversed(dates))]
        self.assertEqual(0, relative_context(bars, bars)['returns']['20'])
        self.assertIsNone(relative_context(bars, bars[:-2] + bars[-1:])['returns']['20'])
        self.assertEqual('UNAVAILABLE', relative_context(bars, bars[:-1])['status'])

    def test_expiry_requires_exact_session_price(self):
        option = {'contract': 'TEST', 'ask': 2, 'strike': 100, 'type': 'CALL', 'expiry': '2026-09-04'}
        later = _Observation('QCOM', '2026-09-08', 150, NOW, 'later', {})
        self.assertIsNone(_option_outcome(option, later)[0])
        exact = _Observation('QCOM', '2026-09-04', 102, NOW, 'exact', {})
        self.assertEqual(0, _option_outcome(option, exact)[0])
        self.assertFalse(_option_outcome(option, exact)[1]['execution_validated'])


class SourceJoinRepairs(unittest.TestCase):
    def fixture(self):
        return {'aggregation_version': 'enhanced-evidence-v2', 'cutoff_at': NOW.isoformat(),
                'source_snapshots': [{'id': 9, 'symbol': 'QCOM', 'retrieved_at': NOW.isoformat(), 'as_of': NOW.isoformat()}],
                'symbols': {'QCOM': {'sources': {'greek_flow': 9}, 'greek_flow': {'directional_delta_flow': 20, 'directional_vega_flow': -3}}}}

    def test_join_populates_missing_fields_and_source_membership(self):
        run = attach_enhanced(ready_run(), self.fixture()); entry = run['watchlist'][0]
        self.assertEqual({9}, field_sources(entry, 'whale_evidence.greek_flow.directional_delta_flow'))
        self.assertEqual(20, control._feature_values(entry)['greek.dir_delta_flow'])
        self.assertIn(9, entry['provenance']['analysis_snapshot_ids'])

    def test_future_wrong_ticker_missing_and_legacy_sources_rejected(self):
        for change in ('future', 'ticker', 'missing', 'legacy'):
            enhanced = self.fixture()
            if change == 'future': enhanced['source_snapshots'][0]['retrieved_at'] = (NOW + timedelta(seconds=1)).isoformat()
            if change == 'ticker': enhanced['source_snapshots'][0]['symbol'] = 'AMD'
            if change == 'missing': enhanced['source_snapshots'] = []
            if change == 'legacy': enhanced.pop('aggregation_version')
            with self.assertRaises(ValueError): attach_enhanced(ready_run(), enhanced)

    def test_correct_feature_names_and_units(self):
        entry = {'return_1d_pct': 2, 'edge': {'gex_topology': {'distance_to_flip_pct': -3}, 'dimensions': {'long_volatility_attractiveness': 60}}}
        values = control._feature_values(entry)
        self.assertEqual(.02, values['price.return_1d'])
        self.assertEqual(-3, values['gex.flip_distance'])
        self.assertEqual(60, values['model.long_volatility_attractiveness'])


class OperationalRepairs(unittest.TestCase):
    def test_failed_benchmark_stops_daily_capture_and_preserves_latest(self):
        settings = Settings.from_env({'MORNING_EDGE_PROVIDER': 'unusual_whales', 'UNUSUAL_WHALES_API_KEY': 'local-test-secret'})
        budget = Mock(); budget.__enter__ = Mock(return_value=budget); budget.__exit__ = Mock(return_value=False)
        budget.usage.return_value.remaining_before_reserve = 33000
        report = Mock(preflight_passed=True, results=[Mock(status=Mock(value='unavailable'))])
        report.to_dict.return_value = {'status': 'unavailable'}
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp)/'app'; (app/'data').mkdir(parents=True)
            (app/'data/latest.json').write_text('unchanged')
            with patch.object(gateway, 'WeeklyRequestBudget', return_value=budget), patch.object(gateway, 'SnapshotStore'), patch.object(gateway, 'UnusualWhalesClient'), patch.object(gateway, 'collect_current', return_value=report), patch.object(gateway, 'live_morning_run') as daily:
                result = gateway.capture(settings, now=NOW, output=Path(tmp)/'run.json', app_root=app)
            self.assertEqual('FAILED', result['status']); daily.assert_not_called()
            self.assertEqual('unchanged', (app/'data/latest.json').read_text())
            self.assertFalse((Path(tmp)/'run.json').exists())

    def test_insufficient_budget_prevents_provider_construction(self):
        settings = Settings.from_env({'MORNING_EDGE_PROVIDER': 'unusual_whales', 'UNUSUAL_WHALES_API_KEY': 'local-test-secret'})
        budget = Mock(); budget.__enter__ = Mock(return_value=budget); budget.__exit__ = Mock(return_value=False)
        budget.usage.return_value.remaining_before_reserve = 959
        with tempfile.TemporaryDirectory() as tmp, patch.object(gateway, 'WeeklyRequestBudget', return_value=budget), patch.object(gateway, 'UnusualWhalesClient') as provider:
            result = gateway.capture(settings, now=NOW, output=Path(tmp)/'run.json', app_root=Path(tmp)/'app')
            self.assertEqual('BLOCKED', result['status']); provider.assert_not_called()

    def test_corporate_reference_shapes_and_ticker_boundary(self):
        transport = ScriptedTransport([(200, {}, {'data': {'ticker': 'QCOM', 'splits': []}}),
            (200, {}, {'data': {'ticker': 'QCOM', 'dividends': [{'ex_date': '2026-09-01'}]}}),
            (200, {}, {'data': {'status': 'active', 'listings': []}})])
        client = UnusualWhalesClient('local-test-secret', transport=transport)
        self.assertEqual([], client.company_reference('QCOM', kind='splits').data['splits'])
        self.assertEqual(1, len(client.company_reference('QCOM', kind='dividends').data['dividends']))
        self.assertEqual('active', client.security_listings().data['status'])
        with self.assertRaises(ValueError): client.company_reference('QCOM', kind='../orders')
        for payload in ({'ticker': 'AMD', 'splits': []}, {'ticker': 'QCOM', 'splits': [None]}):
            client = UnusualWhalesClient('local-test-secret', transport=ScriptedTransport([(200, {}, {'data': payload})]))
            with self.assertRaises(ProviderSchemaError): client.company_reference('QCOM', kind='splits')

    def test_closed_day_stops_before_any_provider_call(self):
        settings = Settings.from_env({})
        with tempfile.TemporaryDirectory() as tmp, patch.object(gateway, 'UnusualWhalesClient') as provider:
            result = gateway.capture(settings, now=NOW - timedelta(days=1), output=Path(tmp)/'run.json', app_root=Path(tmp)/'app')
            self.assertEqual('MARKET_CLOSED', result['status']); provider.assert_not_called()
            self.assertFalse((Path(tmp)/'run.json').exists())
        self.assertEqual(320, gateway.plan(settings, NOW)['logical_requests'])
        self.assertEqual(960, gateway.plan(settings, NOW)['maximum_transport_attempts'])

    def test_local_publication_has_immutable_hashed_detail_and_replay(self):
        run = {'run_id': '2026-09-08-test', 'cutoff_at': NOW.isoformat(), 'watchlist': [{'ticker': 'QCOM', 'technical': {'bars': [{'date': '2026-09-04', 'close': 100}]}}]}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = bundle.archive_daily_data(run=run, app_root=root)
            bundle.publish_latest_data(run=run, app_root=root)
            latest = json.loads((root/'data/latest.json').read_text()); entry = latest['entries'][0]
            detail = root / entry['detail']['url'][2:]
            self.assertEqual(hashlib.sha256(detail.read_bytes()).hexdigest(), entry['detail']['sha256'])
            self.assertEqual([], entry['options'])
            self.assertEqual('QCOM', json.loads(detail.read_text())['ticker'])
            self.assertEqual(first, bundle.archive_daily_data(run=run, app_root=root))
            run['run_id'] += '-revision'
            second = bundle.archive_daily_data(run=run, app_root=root)
            self.assertNotEqual(first['files']['daily_data']['path'], second['files']['daily_data']['path'])
            self.assertEqual(2, len(json.loads((root/'data/publications.json').read_text())['entries']))


if __name__ == '__main__':
    unittest.main()

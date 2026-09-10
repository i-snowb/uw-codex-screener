from datetime import UTC, date, datetime, timedelta
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from morning_edge.data_health import chain_quality, execution_readiness
from morning_edge.edge import EdgeAnalyzer
from morning_edge.greek_flow import summarize
from morning_edge.models import Dataset, SnapshotEnvelope
from morning_edge.store import SnapshotStore
from morning_edge.enhanced_features import build_enhanced_summary
from morning_edge.evidence_join import attach_enhanced
from morning_edge.providers.base import ProviderAuthenticationError
from morning_edge.providers.unusual_whales import UnusualWhalesClient
from test_unusual_whales import ScriptedTransport

NOW = datetime(2026, 9, 8, 3, tzinfo=UTC)


@unittest.skipUnless(shutil.which('node'), 'Node.js required for dashboard behavioral tests')
class CalendarDisplayTests(unittest.TestCase):
    def test_empty_past_and_unsorted_calendar_do_not_claim_no_event_risk(self):
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root / 'scripts'))
        import build_dashboard_bundle as bundle
        source = bundle._externalize_data(bundle._split_fragment(bundle.dashboard.build_fragment({'watchlist': []}))[2])
        helper = re.search(r'^function macroEventRead.*$', source, re.M).group()
        installed = (root / 'dashboard-app/assets/app.js').read_text()
        self.assertEqual(helper, re.search(r'^function macroEventRead.*$', installed, re.M).group())
        harness = "const assert=require('node:assert/strict');\n" + helper + r'''
const cutoff='2026-09-08T03:00:00Z';
assert.match(macroEventRead({},cutoff).label,/coverage unavailable/);
assert.match(macroEventRead({events:[]},cutoff).label,/event risk unknown/);
const past={time:'2026-09-04T12:30:00Z',event:'Past event'};
assert.equal(macroEventRead({events:[past]},cutoff).upcoming,undefined);
assert.match(macroEventRead({events:[past]},cutoff).label,/coverage unverified/);
const first={time:'2026-09-09T14:00:00Z',event:'First'},second={time:'2026-09-10T12:30:00Z',event:'Second'};
assert.equal(macroEventRead({events:[second,past,first,{time:'bad',event:'Invalid'}]},cutoff).label,'First');
'''
        result = subprocess.run(['node', '-e', harness], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)


class GreekRecoveryTests(unittest.TestCase):
    def test_recovered_enhanced_sources_require_explicit_ids_and_cutoff(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'test.sqlite'
            with SnapshotStore(path) as store:
                snapshot = store.insert(SnapshotEnvelope(provider='test', dataset=Dataset.GREEK_FLOW,
                    symbol='QCOM', as_of=NOW, retrieved_at=NOW, payload={'data': self.rows()},
                    metadata={'capture_mode': 'identified_gap_recovery', 'enhanced_dataset': 'greek_flow'}))
                self.assertNotIn('QCOM', build_enhanced_summary(path, cutoff_at=NOW)['symbols'])
                self.assertNotIn('QCOM', build_enhanced_summary(path, snapshot_ids=[snapshot.id], cutoff_at=NOW-timedelta(seconds=1))['symbols'])
                explicit = build_enhanced_summary(path, snapshot_ids=[snapshot.id], cutoff_at=NOW)
                self.assertEqual(snapshot.id, explicit['symbols']['QCOM']['sources']['greek_flow'])
                run = {'cutoff_at': NOW.isoformat(), 'watchlist': [{'ticker': 'QCOM',
                    'provenance': {'snapshot_ids': [100], 'analysis_snapshot_ids': [100, 101]}}]}
                joined = attach_enhanced(run, explicit)
                self.assertEqual([snapshot.id, 100, 101], joined['watchlist'][0]['provenance']['analysis_snapshot_ids'])

    def test_calendar_cannot_claim_unsupported_historical_scope(self):
        transport = ScriptedTransport([])
        client = UnusualWhalesClient('local-test-secret', transport=transport)
        with self.assertRaisesRegex(ValueError, 'historical date'):
            client.economic_calendar(as_of='2026-09-04')

    def test_auth_diagnostic_retains_status_without_credentials_or_retry(self):
        for status in (401, 403):
            transport = ScriptedTransport([(status, {}, {'error': 'denied'})])
            client = UnusualWhalesClient('local-test-secret', transport=transport)
            with self.assertRaises(ProviderAuthenticationError) as caught:
                client.company_reference('QCOM', kind='splits')
            self.assertEqual(status, caught.exception.status_code)
            self.assertNotIn('local-test-secret', str(caught.exception))

    def test_research_health_or_input_booleans_cannot_enable_execution(self):
        result = execution_readiness({'cutoff_at': NOW.isoformat(), 'watchlist': [],
            'execution_ready': True, 'recommendations_enabled': True, 'calibrated': True})
        self.assertFalse(result['execution_ready'])
        self.assertFalse(result['recommendations_enabled'])
        self.assertEqual('BLOCKED', result['status'])

    def rows(self):
        return [{'timestamp': (datetime(2026, 9, 4, 13, 30, tzinfo=UTC) + timedelta(minutes=i)).isoformat(),
                 'dir_delta_flow': 1, 'dir_vega_flow': 2} for i in range(390)]

    def test_postclose_is_excluded_without_invalidating_complete_regular_session(self):
        rows = self.rows()
        rows += [dict(rows[0], timestamp='2026-09-04T20:00:00Z', dir_delta_flow=999),
                 dict(rows[0], timestamp='2026-09-04T20:26:00Z', dir_delta_flow=999)]
        result = summarize(rows, cutoff_at=NOW)
        self.assertTrue(result['confirmation_eligible'])
        self.assertEqual(390, result['directional_delta_flow'])
        self.assertEqual(2, result['out_of_session_rows_excluded'])
        self.assertEqual(0, result['rejected_rows'])

    def test_bad_timestamps_holidays_and_missing_minutes_still_block(self):
        for extra in ('bad', '2026-09-07T14:00:00Z', '2026-09-04T13:30:01Z'):
            result = summarize(self.rows() + [{'timestamp': extra}], cutoff_at=NOW)
            self.assertFalse(result['confirmation_eligible'])
        self.assertFalse(summarize(self.rows()[1:], cutoff_at=NOW)['confirmation_eligible'])

    def test_numeric_presence_is_not_validity(self):
        good = {'delta': .5, 'gamma': .01, 'implied_volatility': .4, 'type': 'call'}
        rows = [good] + [dict(good, **change) for change in (
            {'delta': 2}, {'delta': -.5}, {'gamma': -.1}, {'implied_volatility': 0}, {'delta': 'nan'})]
        counts = chain_quality(rows, spot=100, as_of=NOW.date())['strata']['all']
        self.assertEqual(1, counts['usable_greeks'])
        self.assertEqual(4, counts['invalid_greeks'])
        self.assertEqual(1, counts['missing_greeks'])

    def test_surface_does_not_impute_missing_liquidity_to_zero(self):
        rows = [{'type': side, 'expiry': '2026-11-20', 'strike': 100,
                 'implied_volatility': .4, 'delta': 2, 'bid': 2, 'ask': 2.1}
                for side in ('call', 'put')]
        result = EdgeAnalyzer._surface_for_rows(rows, spot=100, market_date=date(2026, 9, 4))
        self.assertIsNone(result['median_open_interest'])
        self.assertIsNone(result['median_volume'])
        self.assertIsNone(result['put_call_skew_25d'])


class DarkPoolRecoveryTests(unittest.TestCase):
    def test_all_pages_one_cohort_with_date_boundary_and_cutoff_gate(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'test.sqlite'
            with SnapshotStore(path) as store:
                store.connection.execute('CREATE TABLE backfill_events (id INTEGER PRIMARY KEY, plan_id TEXT, item_key TEXT, state TEXT, details_json TEXT, recorded_at TEXT)')
                def save(rows, page, plan='p', seconds=0):
                    captured = NOW - timedelta(minutes=1) + timedelta(seconds=seconds)
                    return store.insert(SnapshotEnvelope(provider='test', dataset=Dataset.DARK_POOL,
                        symbol='QCOM', as_of=captured, retrieved_at=captured, payload={'data': rows},
                        metadata={'requested_market_date': '2026-09-04', 'pagination_family': 'dark_pool_cursor',
                                  'backfill_plan_id': plan, 'backfill_item_key': 'k', 'pagination_page': page})).id
                a = {'tracking_id': 'a', 'executed_at': '2026-09-04T19:00:00Z', 'price': 100, 'premium': 100}
                b = dict(a, tracking_id='b', executed_at='2026-09-04T18:00:00Z', premium=200)
                older = dict(a, tracking_id='old', executed_at='2026-09-03T19:00:00Z', premium=9000)
                save([dict(a, premium=8888)], 0, plan='old-plan', seconds=-10)
                first = save([a], 0)
                second = save([a, b, older], 1, seconds=1)
                store.connection.execute('INSERT INTO backfill_events VALUES (1,?,?,?,?,?)',
                    ('p', 'k', 'collected', json.dumps({'pages_captured': 2}), NOW.isoformat()))
                store.connection.commit()
                with EdgeAnalyzer(path) as analyzer:
                    result = analyzer.dark_pool_structure('QCOM', NOW, spot=100, average_daily_dollar_volume=1000)
                    earlier = analyzer.dark_pool_structure('QCOM', NOW - timedelta(seconds=1), spot=100, average_daily_dollar_volume=1000)
                self.assertEqual('COMPLETE_SESSION', result['coverage_status'])
                self.assertEqual(300, result['aggregate_premium'])
                self.assertEqual([first, second], result['source_snapshot_ids'])
                self.assertEqual(.3, result['premium_to_adv_ratio'])
                self.assertEqual(1, result['excluded_rows'])
                self.assertIsNone(result['dominant_level_change'])
                self.assertEqual('PARTIAL_OR_UNVERIFIED', earlier['coverage_status'])
                self.assertIsNone(earlier['premium_to_adv_ratio'])


if __name__ == '__main__':
    unittest.main()

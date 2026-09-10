from copy import deepcopy
from datetime import date, datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from morning_edge.experiments import sessions
from morning_edge.price_trial import attach_trial, freeze_trial, paired_trial_report, validate_trial
from morning_edge.evaluation import register_run, build_report, evaluate_registered
from morning_edge.models import Dataset, SnapshotEnvelope
from morning_edge.store import SnapshotStore
from test_evaluation import run as fixture_run


def trial_fixture():
    model = {'features': ['return_5', 'return_20', 'realized_vol_20'], 'means': [0, 0, 0],
             'scales': [1, 1, 1], 'coefficients': [.01, .1, .1, .01], 'penalty': 1.0}
    experiment = {'promotion_eligible': False, 'experiment_id': 'fixture',
        'holdout': {'status': 'SEALED_NOT_SCORED', 'start': '2026-06-09'},
        'folds': [{'horizon': h, 'test_start': '2026-05-01', 'latest_training_target': '2026-04-30',
                   'models': {'absolute_return': {'ridge_price': model}}} for h in (1, 5, 20)]}
    return freeze_trial(experiment, source_sha256='fixture', starts_at='2026-09-11T04:00:00Z',
                        frozen_at='2026-09-10T17:00:00Z', tickers=['QCOM'])


def source_fixture(source_id=1):
    run = fixture_run(run_id='new', cutoff='2026-09-11T12:00:00Z', price_date='2026-09-10',
                      price=100, source_id=source_id, option_bid=1)
    dates = sessions(date(2026, 7, 1), date(2026, 9, 10))
    run['watchlist'][0]['technical']['bars'] = [{'date': day, 'close': 100} for day in dates]
    active = run['watchlist'][0]['edge']['forecast']
    active['path'] = [{'session': 1, 'date': '2026-09-11', 'center_return': .02}]
    return run


class PriceTrialTests(unittest.TestCase):
    def attach(self, run, trial=None):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'trial.json'
            path.write_text(json.dumps(trial or trial_fixture()))
            return attach_trial(run, path)

    def test_fixed_coefficients_exact_targets_no_input_mutation(self):
        source = source_fixture()
        before = deepcopy(source)
        result = self.attach(source)
        model = result['watchlist'][0]['edge']['price_only_trial']
        self.assertEqual(source, before)
        self.assertEqual('SHADOW_UNCALIBRATED', model['status'])
        self.assertEqual([1, 5, 20], [r['session'] for r in model['path']])
        self.assertEqual(['2026-09-11', '2026-09-17', '2026-10-08'], [r['date'] for r in model['path']])
        self.assertEqual([.01]*3, [r['center_return'] for r in model['path']])
        self.assertEqual(result, self.attach(source))

    def test_activation_history_cutoff_and_origin_guards(self):
        for change in ('old', 'reprocessed', 'gap', 'partial', 'price', 'scope', 'stale'):
            source = source_fixture()
            entry = source['watchlist'][0]
            if change == 'old': source['cutoff_at'] = '2026-09-10T12:00:00Z'
            if change == 'reprocessed': source['reprocessing'] = True
            if change == 'gap': entry['technical']['bars'].pop(-3)
            if change == 'partial': entry['technical']['bars'].append({'date': '2026-09-11', 'close': 100})
            if change == 'price': entry['price']['value'] = 101
            if change == 'scope': entry['ticker'] = 'OUTSIDE'
            if change == 'stale': entry['technical']['bars'].pop()
            with self.subTest(change=change):
                self.assertEqual([], self.attach(source)['watchlist'][0]['edge']['price_only_trial']['path'])

    def test_tampered_trial_rejected_and_missing_trial_noop(self):
        trial = trial_fixture()
        trial['models']['1']['coefficients'][0] = 1
        with self.assertRaises(ValueError): validate_trial(trial)
        with tempfile.TemporaryDirectory() as temporary:
            source = source_fixture()
            self.assertEqual(source, attach_trial(source, Path(temporary)/'missing'))

    def test_ledger_integration_is_idempotent_and_scores_same_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary)/'test.sqlite'
            with SnapshotStore(database) as store:
                snapshot = store.insert(SnapshotEnvelope(provider='test', dataset=Dataset.OHLC, symbol='QCOM',
                    as_of=datetime(2026, 9, 10, 20, tzinfo=timezone.utc),
                    retrieved_at=datetime(2026, 9, 11, 11, tzinfo=timezone.utc), payload={'data': []}))
            source = self.attach(source_fixture(snapshot.id))
            self.assertEqual(register_run(database, source), register_run(database, source))
            report = build_report(database)
            self.assertEqual(4, report['tracked_model_forecasts'])
            self.assertEqual(1, sum(r['pending'] for r in report['price_trial_comparison']))
            self.assertEqual(2, sum(r['excluded'] for r in report['price_trial_comparison']))
            future = deepcopy(source)
            future.update(run_id='later', cutoff_at='2026-09-14T12:00:00Z', generated_at='2026-09-14T12:00:00Z')
            future['watchlist'][0]['technical']['bars'].append({'date': '2026-09-11', 'close': 103})
            future['watchlist'][0]['price'].update(as_of='2026-09-11', value=103)
            evaluate_registered(database, [source, future])
            report = build_report(database)
            row = next(r for r in report['price_trial_comparison'] if r['horizon'] == 1)
            self.assertEqual((1, 1), (row['matched'], row['origin_dates']))
            self.assertAlmostEqual(2, row['trial_mae'])
            self.assertAlmostEqual(1, row['active_mae'])
            self.assertFalse(row['promotion_eligible'])

    def test_pairing_excludes_seeds_duplicates_different_cutoffs_and_outcomes(self):
        base = {'ticker': 'QCOM', 'origin_session': '2026-09-10', 'origin_close': 100,
                'horizon_sessions': 1, 'published_at': '2026-09-11T12:00:00Z',
                'registration_mode': 'PROSPECTIVE', 'daily_tracking_eligible': True,
                'status': 'EVALUATED', 'target_session': '2026-09-11', 'underlying_return_pct': 3,
                'target_center_return_pct': 1, 'baseline_directions': {'twenty_session_momentum': 'BULLISH'}}
        trial = dict(base, forecast_id=1, model_role='SHADOW_PRICE_TRIAL', model_version='trial')
        active = dict(base, forecast_id=2, model_role='ACTIVE_THESIS_V3', model_version='active', target_center_return_pct=2)
        self.assertEqual(1, paired_trial_report([trial, active])[0]['matched'])
        for changes in ({'published_at': 'different'}, {'underlying_return_pct': 4},
                        {'target_session': '2026-09-14'}, {'registration_mode': 'RETROSPECTIVE_ARTIFACT_SEED'},
                        {'daily_tracking_eligible': False}):
            self.assertEqual(0, paired_trial_report([trial, dict(active, **changes)])[0]['matched'])
        self.assertEqual(0, paired_trial_report([trial, active, dict(active, forecast_id=3)])[0]['matched'])

    def test_panel_empty_state_and_escaping(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
        from price_trial_panel import PRICE_TRIAL_JS
        helper = PRICE_TRIAL_JS.split('const originalPlatformDetails')[0]
        harness = "const n=v=>typeof v==='number'&&Number.isFinite(v);const esc=v=>String(v).replaceAll('<','&lt;');\n"+helper
        harness += "\nif(!priceTrialMarkup([]).includes('Awaiting prospective'))throw Error('empty');if(priceTrialMarkup([{trial_version:'<img>',horizon:1}]).includes('<img>'))throw Error('escaping');"
        process = subprocess.run(['node', '-e', harness], capture_output=True, text=True)
        self.assertEqual(0, process.returncode, process.stderr)

    def test_origin_dates_have_equal_weight_and_maturity_never_promotes(self):
        rows = []
        for day, ticker, actual in [('2026-09-10', 'A', 0), ('2026-09-10', 'B', 0), ('2026-09-11', 'A', 6)]:
            common = {'ticker': ticker, 'origin_session': day, 'origin_close': 100, 'horizon_sessions': 1,
                      'published_at': day, 'target_session': day, 'status': 'EVALUATED',
                      'daily_tracking_eligible': True, 'registration_mode': 'PROSPECTIVE',
                      'underlying_return_pct': actual, 'target_center_return_pct': 0}
            for role, model in [('ACTIVE_THESIS_V3', 'active'), ('SHADOW_PRICE_TRIAL', 'trial')]:
                rows.append(dict(common, forecast_id=len(rows)+1, model_role=role, model_version=model))
        report = paired_trial_report(rows)[0]
        self.assertEqual((3, 2), (report['matched'], report['origin_dates']))
        self.assertEqual(3, report['trial_mae'])
        self.assertEqual(.5, report['trial_accuracy'])
        self.assertFalse(report['promotion_eligible'])

    def test_model_pins_and_registration_activation_fail_closed(self):
        from morning_edge.experiments import digest
        trial = trial_fixture()
        trial['implementation_sha256'] = 'changed'
        trial['trial_id'] = digest({k: v for k, v in trial.items() if k != 'trial_id'})
        with self.assertRaises(ValueError): validate_trial(trial)
        source = self.attach(source_fixture())
        source['watchlist'][0]['edge']['price_only_trial']['starts_at'] = '2027-01-01T00:00:00Z'
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, 'cannot be backfilled'):
                register_run(Path(temporary)/'test.sqlite', source)

    def test_trial_predictions_are_not_available_to_active_analyst(self):
        from morning_edge.unattended import evidence_packet
        source = self.attach(source_fixture())
        packet = evidence_packet(source, ['QCOM'], 'fixed')
        self.assertNotIn('price_only_trial', packet['watchlist'][0]['edge'])
        self.assertIn('price_only_trial', packet['projection']['omitted_keys'])
        self.assertEqual(source['watchlist'][0]['edge']['forecast'], packet['watchlist'][0]['edge']['forecast'])


if __name__ == '__main__':
    unittest.main()

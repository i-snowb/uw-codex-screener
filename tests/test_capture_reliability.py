from dataclasses import replace
from datetime import datetime, UTC, timedelta
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from morning_edge import capture_checkpoint as checkpoint
from morning_edge.config import Settings
from morning_edge.current_collection import CurrentCaptureReport, CurrentCaptureItem, CurrentCaptureStatus, CurrentDataset
from morning_edge.daily import build_morning_run
from morning_edge.edge import EdgeAnalyzer, RawSnapshot
from morning_edge.enhanced_collection import EnhancedCaptureReport, EnhancedCaptureItem, EnhancedCaptureStatus, EnhancedDataset
from morning_edge.models import SnapshotEnvelope, Dataset
from morning_edge.snapshot_archive import ArchivePayloads
from morning_edge.store import SnapshotStore
from morning_edge.unattended import immutable_json
from morning_edge.unattended import bounded_process, analyst_environment

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import run_unattended_morning as runner

NOW = datetime(2026, 9, 8, 11, tzinfo=UTC)


class CaptureReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / 'raw.sqlite'
        self.store = SnapshotStore(self.database)
        self.base_id = self.insert(Dataset.OPTION_CHAIN, {'data': [{
            'option_type': 'call', 'strike': 100, 'expiry': '2026-12-18',
            'last_tape_time': '2026-09-04T19:59:00Z', 'nbbo_bid': 3, 'nbbo_ask': 3.1,
            'delta': .5, 'gamma': .01, 'theta': -.1, 'vega': .2, 'open_interest': 100,
            'implied_volatility': .4}]}, 'current', '/chain')
        self.extra_id = self.insert(Dataset.VOLATILITY_STATS, {'data': [{'iv': .4, 'date': '2026-09-04'}]},
            'enhanced_current', '/volatility')
        self.base = CurrentCaptureReport(NOW.isoformat(), ('QCOM',), (CurrentDataset.OPTION_CHAIN,), True, 3, 33000,
            (CurrentCaptureItem('QCOM', CurrentDataset.OPTION_CHAIN, CurrentCaptureStatus.CAPTURED,
                '/chain', self.base_id, NOW.isoformat(), 1),))
        self.extra = EnhancedCaptureReport(NOW.isoformat(), ('QCOM',), (EnhancedDataset.VOLATILITY_STATS,), True,
            1, 3, 33000, (EnhancedCaptureItem(EnhancedDataset.VOLATILITY_STATS, 'QCOM', EnhancedCaptureStatus.CAPTURED,
                '/volatility', self.extra_id, NOW.isoformat(), 1),))
        self.path = self.root / 'morning-run-collection.json'

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def insert(self, dataset, payload, mode, endpoint, *, observed=NOW):
        return self.store.insert(SnapshotEnvelope(provider='unusual_whales', dataset=dataset, symbol='QCOM',
            as_of=observed, retrieved_at=observed, payload=payload,
            metadata={'capture_mode': mode, 'provider_endpoint': endpoint,
                'response_status': 'captured', 'response_row_count': 1})).id

    def save(self, **options):
        checkpoint.save_checkpoint(self.path, root=ROOT, database=self.database,
            base=self.base, extra=self.extra, **options)

    def load(self, **options):
        return checkpoint.load_checkpoint(self.path, root=ROOT, database=self.database,
            tickers=('QCOM',), datasets=(CurrentDataset.OPTION_CHAIN,), now=options.pop('now', NOW), **options)

    def test_round_trip_is_immutable_private_and_preserves_cutoff(self):
        self.save(); self.save()
        base, extra, context = self.load()
        self.assertEqual(self.base, base)
        self.assertEqual(self.extra, extra)
        self.assertEqual({}, context)
        self.assertEqual(0o600, self.path.stat().st_mode & 0o777)
        with self.assertRaises(ValueError):
            checkpoint.save_checkpoint(self.path, root=ROOT, database=self.database,
                base=replace(self.base, remaining_transport_attempt_capacity_before_run=32000), extra=self.extra)

    def test_checkpoint_rejects_other_session_future_clock_code_and_universe(self):
        self.save()
        for moment in (NOW - timedelta(seconds=1), NOW + timedelta(days=1)):
            with self.subTest(moment=moment), self.assertRaises(ValueError):
                self.load(now=moment)
        with patch.object(checkpoint, 'source_code_digest', return_value='changed'), self.assertRaises(ValueError):
            self.load()
        with self.assertRaises(ValueError):
            checkpoint.load_checkpoint(self.path, root=ROOT, database=self.database, tickers=('NVDA',),
                datasets=(CurrentDataset.OPTION_CHAIN,), now=NOW)

    def test_checkpoint_rejects_failed_missing_duplicate_and_wrong_snapshot(self):
        bad = replace(self.base.results[0], status=CurrentCaptureStatus.UNAVAILABLE)
        for report in (replace(self.base, preflight_passed=False), replace(self.base, results=()),
                       replace(self.base, results=(bad,)), replace(self.base, results=self.base.results * 2),
                       replace(self.base, results=(replace(self.base.results[0], snapshot_id=self.extra_id),))):
            with self.subTest(report=report), self.assertRaises(ValueError):
                checkpoint.save_checkpoint(self.path, root=ROOT, database=self.database, base=report, extra=self.extra)
        self.assertFalse(self.path.exists())

    def test_checkpoint_rejects_changed_raw_payload(self):
        self.save()
        self.store.connection.execute('DROP TRIGGER raw_payloads_no_update')
        self.store.connection.execute("UPDATE raw_payloads SET content_json='{}'")
        with self.assertRaisesRegex(ValueError, 'payload hash'):
            self.load()

    def test_checkpoint_rejects_changed_context_and_links(self):
        import hashlib
        context = {}
        for name in checkpoint.CONTEXT_FILES:
            immutable_json(self.root / name, {})
            context[name] = hashlib.sha256((self.root / name).read_bytes()).hexdigest()
        self.save(context=context)
        self.load()
        (self.root / checkpoint.CONTEXT_FILES[0]).write_text('{"changed": true}')
        with self.assertRaisesRegex(ValueError, 'context hash'):
            self.load()
        link = self.root / 'link.json'; link.symlink_to(self.path)
        with self.assertRaisesRegex(ValueError, 'linked'):
            checkpoint.load_checkpoint(link, root=ROOT, database=self.database, tickers=('QCOM',),
                datasets=(CurrentDataset.OPTION_CHAIN,), now=NOW)

    def test_custom_output_stem_retains_companion_hash_checks(self):
        import hashlib
        self.path = self.root / 'custom-collection.json'
        names = checkpoint.context_files(self.path)
        self.assertEqual(('custom-benchmarks.json', 'custom-company-context.json'), names)
        context = {}
        for name in names:
            immutable_json(self.root / name, {})
            context[name] = hashlib.sha256((self.root / name).read_bytes()).hexdigest()
        self.save(context=context)
        self.assertEqual(context, self.load()[2])

    def test_wrong_collector_database_fails_before_collection(self):
        import io
        import run_daily_capture as gateway
        settings = Settings.from_env({'CODEX_SCREENER_DATABASE': str(self.database)})
        with patch.object(gateway, '_load_env'), patch.object(gateway.Settings, 'from_env', return_value=settings), \
             patch.object(gateway, 'capture') as capture, patch.object(gateway.faulthandler, 'register'), \
             patch('sys.stderr', new=io.StringIO()), self.assertRaises(SystemExit):
            gateway.main(['--live','--audit-accepted','--expected-database',str(self.root/'different.sqlite')])
        capture.assert_not_called()

    def test_resume_never_constructs_provider_or_writable_store(self):
        from morning_edge.cli import live_morning_run
        self.save()
        settings = Settings.from_env({'MORNING_EDGE_PROVIDER': 'unusual_whales',
            'UNUSUAL_WHALES_API_KEY': 'local-test-secret'})
        with patch.object(checkpoint, 'datetime') as clock, \
             patch('morning_edge.cli.UnusualWhalesClient', side_effect=AssertionError('network forbidden')), \
             patch('morning_edge.cli.WeeklyRequestBudget', side_effect=AssertionError('budget mutation forbidden')), \
             patch('morning_edge.cli.SnapshotStore', side_effect=AssertionError('write forbidden')):
            clock.now.return_value = NOW
            result = live_morning_run(settings, tickers=('QCOM',), datasets=('option_chain',), audit_accepted=True,
                database_path=self.database, output_path=self.root / 'retry.json', resume_checkpoint=self.path)
        self.assertFalse(result['network_called'])
        self.assertTrue(result['resumed_collection'])
        self.assertEqual(NOW, datetime.fromisoformat(result['cutoff_at'].replace('Z', '+00:00')))
        artifact = json.loads((self.root / 'retry.json').read_text())
        self.assertEqual('NO_RECOMMENDATION', artifact['watchlist'][0]['action'])
        self.assertEqual('SAME_SESSION_CHECKPOINT_NO_NEW_PROVIDER_REQUESTS', artifact['collection_recovery']['mode'])

    def test_per_ticker_checkpoint_skips_completed_work_and_rejects_corruption(self):
        directory = self.root / 'calculation-records'
        first = build_morning_run(database=self.database, capture_report=self.base, checkpoint_directory=directory)
        with patch('morning_edge.daily._ticker_record', side_effect=AssertionError('duplicate calculation')):
            second = build_morning_run(database=self.database, capture_report=self.base, checkpoint_directory=directory)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        saved = next(directory.glob('*/QCOM.json'))
        value = json.loads(saved.read_text()); value['record']['price'] = {'changed': True}
        saved.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'checkpoint verification'):
            build_morning_run(database=self.database, capture_report=self.base, checkpoint_directory=directory)
        with self.assertRaisesRegex(ValueError, 'ticker'):
            build_morning_run(database=self.database, capture_report=replace(self.base, tickers=('../bad',)),
                checkpoint_directory=directory)

    def test_metadata_scan_is_lazy_and_matches_eager_payloads(self):
        with EdgeAnalyzer(self.database) as analyzer:
            snapshots = analyzer.snapshots('QCOM', 'option_chain', NOW)
            self.assertEqual(0, analyzer._payloads.cached_bytes)
            self.assertEqual('2026-09-04', snapshots[0].market_date.isoformat())
            technical = {'latest_regular_close': 100, 'bars': [], 'realized_vol_20': .3}
            actual = analyzer.analyze('QCOM', NOW, technical=technical)
        with EdgeAnalyzer(self.database) as eager:
            original = eager.snapshots
            def decoded(*args):
                return [RawSnapshot(s.snapshot_id, s.dataset, s.market_date, s.as_of, s.retrieved_at,
                    s.metadata, s.payload) for s in original(*args)]
            with patch.object(eager, 'snapshots', side_effect=decoded):
                expected = eager.analyze('QCOM', NOW, technical=technical)
        self.assertEqual(expected, actual)

    def test_payload_cache_is_bounded_including_oversized_inputs(self):
        cache = ArchivePayloads(self.store.connection, max_bytes=30)
        hashes = self.store.connection.execute('SELECT content_hash FROM raw_payloads').fetchall()
        for row in hashes:
            self.assertIsInstance(cache.load(row[0]), dict)
            self.assertLessEqual(cache.cached_bytes, 30)
        self.assertEqual(0, cache.cached_bytes)
        with self.assertRaisesRegex(ValueError, 'missing'):
            cache.load('missing')

    def test_runner_passes_prior_checkpoint_without_resetting_attempts(self):
        config = {'python': sys.executable, 'max_attempts': 2, 'deadline_et': '08:00'}
        with patch.object(runner, 'ROOT', self.root), patch('builtins.print'):
            instance = runner.Runner(config, self.root / 'session', validation=True)
            instance.state['attempts']['capture'] = 1
            prior = instance.directory / 'attempts/capture/1/morning-run-collection.json'
            immutable_json(prior, {'receipt': 'synthetic'})
            with patch.object(runner, 'bounded_process', return_value={'exit_code': 1}) as execute:
                with self.assertRaisesRegex(ValueError, 'capture process failed'):
                    instance.capture()
            command = execute.call_args.args[0]
            self.assertEqual(str(prior), command[command.index('--resume-capture') + 1])
            self.assertEqual(2, instance.state['attempts']['capture'])

    def test_timeout_preserves_registered_capture_stack(self):
        result = bounded_process([sys.executable, '-c',
            'import faulthandler,signal,time; faulthandler.register(signal.SIGTERM,chain=True); time.sleep(30)'],
            directory=self.root / 'timeout', cwd=self.root, timeout=1.0, environment=analyst_environment())
        self.assertTrue(result['timed_out'])
        self.assertIn('most recent call first', (self.root / 'timeout/stderr.log').read_text())


if __name__ == '__main__':
    unittest.main()

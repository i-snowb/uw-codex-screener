from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from morning_edge.execution_controls import contract_identity, policy_diagnostic, quote_diagnostic
from morning_edge.evaluation import register_run, update_evaluations
from morning_edge.models import Dataset, SnapshotEnvelope, timestamp_text
from morning_edge.operational_context import attach_operational_context, collect_company_context, reviewed_calendar_context
from morning_edge.providers.base import ProviderSchemaError
from morning_edge.providers.unusual_whales import UnusualWhalesClient
from morning_edge.store import SnapshotStore
from test_unusual_whales import ScriptedTransport

NOW = datetime(2026, 9, 8, 14, tzinfo=UTC)
POLICY = {'policy_id': 'synthetic-test', 'broker_or_quote_feed': 'synthetic', 'allowed_symbols': ['QCOM'],
    'max_trade_loss_usd': 1, 'max_total_options_risk_usd': 3, 'max_correlated_risk_usd': 2,
    'max_quote_age_seconds': 5, 'max_spread_fraction': .1, 'min_open_interest': 1, 'min_volume': 1,
    'min_dte': 1, 'max_dte': 100, 'earnings_holding_rule': 'prohibited'}
CONTRACT = {'option_symbol': 'QCOM261120C00175000', 'expires': '2026-11-20', 'strike': 175, 'option_type': 'call'}
QUOTE = CONTRACT | {'bid': 10, 'ask': 10.5, 'bid_size': 1, 'ask_size': 2, 'open_interest': 5, 'volume': 5,
    'quote_timestamp': '2026-09-08T13:59:59Z', 'received_at': '2026-09-08T14:00:00Z',
    'timestamp_semantics': 'quote_update', 'source_kind': 'executable_nbbo', 'source': 'synthetic'}


class ExecutionControlTests(unittest.TestCase):
    def test_batch_evaluation_skips_context_only_copies(self):
        with patch('morning_edge.evaluation.load_run', return_value={'forecast_registration_allowed': False}), \
             patch('morning_edge.evaluation.register_run') as register, \
             patch('morning_edge.evaluation.evaluate_registered', return_value={}) as evaluate, \
             patch('morning_edge.evaluation.build_report', return_value={}):
            report = update_evaluations('unused.sqlite', ['context.json'])
            register.assert_not_called()
            evaluate.assert_called_once_with('unused.sqlite', [])
            self.assertEqual(1, report['update']['context_only_runs_skipped'])

    def test_context_only_revision_cannot_inflate_forecast_ledger(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / 'evaluation.sqlite'
            for marker in ({'forecast_registration_allowed': False}, {'revision_scope': 'CONTEXT_ONLY_NO_NEW_FORECAST'}):
                with self.assertRaisesRegex(ValueError, 'context-only'):
                    register_run(database, marker)
            self.assertFalse(database.exists())

    def test_valid_draft_cannot_approve_or_enable_execution(self):
        result = policy_diagnostic(POLICY | {'approved': True, 'execution_ready': True})
        self.assertEqual([], result['errors'])
        self.assertFalse(result['approved'])
        self.assertFalse(result['execution_ready'])

    def test_missing_and_invalid_limits_fail_closed(self):
        self.assertTrue(policy_diagnostic({})['errors'])
        for field, value in [('max_trade_loss_usd', True), ('max_trade_loss_usd', float('nan')),
                             ('max_correlated_risk_usd', 4), ('min_dte', .5), ('max_spread_fraction', 2),
                             ('allowed_symbols', []), ('earnings_holding_rule', 'unrestricted')]:
            self.assertTrue(policy_diagnostic(POLICY | {field: value})['errors'])

    def test_identity_consistency_is_not_deliverable_reconciliation(self):
        result = contract_identity(CONTRACT, 'QCOM')
        self.assertEqual('IDENTITY_FIELDS_MATCH', result['status'])
        self.assertFalse(result['deliverables_verified'])
        for field, value in [('strike', 174), ('expires', '2026-11-19'), ('option_type', 'put'),
                             ('option_symbol', 'QCOM1261120C00175000'), ('option_symbol', 'bad')]:
            self.assertEqual('INVALID', contract_identity(CONTRACT | {field: value}, 'QCOM')['status'])

    def check_quote(self, row, policy=POLICY):
        return quote_diagnostic(row, expected_contract=CONTRACT['option_symbol'], observed_at=NOW, policy=policy)

    def test_quote_valid_inputs_still_cannot_authorize(self):
        result = self.check_quote(QUOTE)
        self.assertEqual([], result['errors'])
        self.assertFalse(result['execution_ready'])
        self.assertEqual('INPUT_CHECKS_PASS_NOT_AUTHORIZED', result['status'])

    def test_quote_trade_timestamp_is_not_quote_timestamp(self):
        row = deepcopy(QUOTE)
        row['last_tape_time'] = row.pop('quote_timestamp')
        self.assertIn('verified_quote_and_receipt_timestamps_required', self.check_quote(row)['errors'])

    def test_quote_abuse_and_staleness_cases(self):
        for field, value in [('quote_timestamp', '2026-09-08T13:59:00Z'), ('quote_timestamp', '2026-09-08T14:00:01Z'),
                             ('received_at', '2026-09-08T13:59:58Z'), ('quote_timestamp', '2026-09-08T13:59:59'),
                             ('bid', 11), ('ask', float('inf')), ('ask_size', True), ('source', 'other'),
                             ('timestamp_semantics', 'last_trade'), ('source_kind', 'smoothed_nbbo'),
                             ('strike', 1), ('option_symbol', 'AMD261120C00175000'), ('volume', None)]:
            with self.subTest(field=field, value=value):
                self.assertTrue(self.check_quote(QUOTE | {field: value})['errors'])
        self.assertIn('expiry_outside_policy', self.check_quote(QUOTE, POLICY | {'max_dte': 10})['errors'])


class OperationalContextTests(unittest.TestCase):
    def calendar(self):
        return {'reviewed_at': timestamp_text(NOW - timedelta(hours=1)), 'expires_at': timestamp_text(NOW + timedelta(hours=1)),
                'events': [{'event': 'Synthetic calendar test', 'time': timestamp_text(NOW + timedelta(days=1)),
                            'source_url': 'https://www.bls.gov/schedule/2026/09_sched.htm'}]}

    def test_reviewed_calendar_partial_sorted_and_deduplicated(self):
        calendar = self.calendar()
        calendar['events'] *= 2
        result = reviewed_calendar_context(calendar, cutoff=NOW)
        self.assertEqual(1, result['event_count'])
        self.assertEqual('PARTIAL_OFFICIAL_SCHEDULE', result['coverage'])

    def test_company_events_stay_separate_from_macro_and_earnings(self):
        calendar = self.calendar()
        event = {'ticker': 'NBIS', 'event': 'Synthetic conference test',
                 'time': timestamp_text(NOW + timedelta(hours=2)),
                 'source_url': 'https://nebius.com/investor-events/nebius-to-present-at-citi-2026-global-tmt-conference'}
        calendar['company_events'] = [event, event]
        result = reviewed_calendar_context(calendar, cutoff=NOW)
        self.assertEqual(1, result['event_count'])
        self.assertEqual(1, len(result['company_events']))
        self.assertNotIn('ticker', result['events'][0])
        calendar['company_events'] = [event | {'source_url': 'https://nebius.com.evil.invalid/'}]
        self.assertEqual('unavailable', reviewed_calendar_context(calendar, cutoff=NOW)['quality'])

    def test_calendar_stale_future_invalid_and_unbounded_are_unavailable(self):
        for changes in ({'reviewed_at': timestamp_text(NOW + timedelta(seconds=1))},
                        {'expires_at': timestamp_text(NOW)}, {'expires_at': timestamp_text(NOW + timedelta(days=2))},
                        {'reviewed_at': '2026-09-08T13:00:00'}, {'events': [{'event': '<bad>', 'time': timestamp_text(NOW), 'source_url': 'https://unknown.invalid/'}]}):
            self.assertEqual('unavailable', reviewed_calendar_context(self.calendar() | changes, cutoff=NOW)['quality'])

    def test_stock_info_schema_and_symbol_binding(self):
        for data in ({'symbol': 'AMD', 'full_name': 'Test'}, {'symbol': 'QCOM'},
                     {'symbol': 'QCOM', 'full_name': 'Test', 'next_earnings_date': 'bad'}):
            client = UnusualWhalesClient('local-test-secret', transport=ScriptedTransport([(200, {}, {'data': data})]))
            with self.assertRaises(ProviderSchemaError):
                client.stock_info('QCOM')

    def test_stock_info_collection_and_cutoff_safe_join(self):
        data = {'symbol': 'QCOM', 'full_name': 'Synthetic Company', 'next_earnings_date': '2026-11-04', 'issue_type': 'Common Stock'}
        with tempfile.TemporaryDirectory() as temp, SnapshotStore(Path(temp) / 'snapshots.sqlite') as snapshots:
            row = snapshots.insert(SnapshotEnvelope(provider='test', dataset=Dataset.SECURITY_IDENTITY, symbol='QCOM',
                as_of=NOW, retrieved_at=NOW, payload={'data': data}, metadata={'reference_kind': 'stock_info'}))
            chain = snapshots.insert(SnapshotEnvelope(provider='test', dataset=Dataset.OPTION_CHAIN, symbol='QCOM',
                as_of=NOW, retrieved_at=NOW, payload={'data': [CONTRACT]}))
            run = {'cutoff_at': timestamp_text(NOW), 'watchlist': [{'ticker': 'QCOM', 'provenance': {'snapshot_ids': [chain.id]},
                'trade_thesis': {'option_reference': {'contract': CONTRACT['option_symbol']}}}]}
            results = [{'ticker': 'QCOM', 'snapshot_id': row.id, 'status': 'CAPTURED'}]
            joined = attach_operational_context(run, snapshots=snapshots, company_results=results, calendar=self.calendar())
            self.assertEqual('2026-11-04', joined['watchlist'][0]['company_reference']['next_earnings_date'])
            self.assertEqual([row.id], joined['watchlist'][0]['field_source_snapshot_ids']['company_reference'])
            self.assertEqual(1, joined['operational_context']['known_upcoming_earnings'])
            checks = joined['operational_context']['diagnostics']
            self.assertEqual(1, checks['chain_identity']['QCOM']['counts']['identity_fields_match'])
            self.assertEqual(chain.id, checks['reference_quote_checks']['QCOM']['source_snapshot_id'])
            self.assertFalse(checks['reference_quote_checks']['QCOM']['execution_ready'])
            with self.assertRaisesRegex(ValueError, 'unavailable at cutoff'):
                attach_operational_context(run | {'cutoff_at': timestamp_text(NOW-timedelta(seconds=1))}, snapshots=snapshots, company_results=results)
            with self.assertRaisesRegex(ValueError, 'identity mismatch'):
                attach_operational_context(run | {'watchlist': [{'ticker': 'AMD'}]}, snapshots=snapshots,
                    company_results=[{'ticker': 'AMD', 'snapshot_id': row.id}])

    def test_collection_stops_after_auth_failure(self):
        transport = ScriptedTransport([(403, {}, {'error': 'denied'})])
        with tempfile.TemporaryDirectory() as temp, SnapshotStore(Path(temp) / 'snapshots.sqlite') as snapshots:
            result = collect_company_context(client=UnusualWhalesClient('local-test-secret', transport=transport),
                snapshots=snapshots, tickers=['QCOM', 'AMD'])
            self.assertEqual(1, len(transport.requests))
            self.assertEqual(403, result[0]['http_status'])
            self.assertTrue(result[0]['collection_stopped'])
            self.assertEqual('SKIPPED_AFTER_COLLECTION_FAILURE', result[1]['status'])

    def test_absent_option_reference_remains_blocked_without_aborting_research(self):
        with tempfile.TemporaryDirectory() as temp, SnapshotStore(Path(temp) / 'snapshots.sqlite') as snapshots:
            chain = snapshots.insert(SnapshotEnvelope(provider='test', dataset=Dataset.OPTION_CHAIN,
                symbol='QCOM', as_of=NOW, retrieved_at=NOW, payload={'data': [CONTRACT]}))
            for reference in (None, {}, 'invalid'):
                run = {'cutoff_at': timestamp_text(NOW), 'watchlist': [{'ticker': 'QCOM',
                    'provenance': {'snapshot_ids': [chain.id]}, 'trade_thesis': {'option_reference': reference}}]}
                joined = attach_operational_context(run, snapshots=snapshots, company_results=[])
                check = joined['operational_context']['diagnostics']['reference_quote_checks']['QCOM']
                self.assertEqual(['no_selected_option_reference'], check['errors'])
                self.assertFalse(check['execution_ready'])
                self.assertFalse(joined['operational_context']['recommendations_enabled'])


if __name__ == '__main__':
    unittest.main()

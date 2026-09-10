#!/usr/bin/env python3
"""Capture company metadata; optionally retry the specifically identified data gaps."""
import argparse
from datetime import UTC, datetime
import json
from pathlib import Path

from morning_edge.config import Settings
from morning_edge.daily import write_morning_run
from morning_edge.greek_flow import summarize
from morning_edge.models import Dataset, SnapshotEnvelope
from morning_edge.operational_context import collect_company_context
from morning_edge.providers.budget import WeeklyRequestBudget, API_BASIC_ROLLING_WINDOW
from morning_edge.providers.unusual_whales import UnusualWhalesClient
from morning_edge.store import SnapshotStore
from run_intraday_refresh import _load_env


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--audit-accepted', action='store_true')
    parser.add_argument('--retry-identified-gaps', action='store_true')
    parser.add_argument('--env-file', type=Path, default=Path('.env'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.live != args.audit_accepted:
        parser.error('live collection requires both --live and --audit-accepted')
    _load_env(args.env_file)
    settings = Settings.from_env()
    maximum = (len(settings.watchlist) + (4 if args.retry_identified_gaps else 0)) * 3
    report = {'schema_version': 'operational-capture-v1', 'status': 'PLANNED', 'maximum_transport_attempts': maximum,
              'protected_reserve': 7000, 'network_called': False, 'company_results': [], 'gap_retries': []}
    if not args.live:
        print(json.dumps(report)); return 0
    if args.output.exists():
        parser.error('preserve the existing output; choose a new attempt path')
    if settings.provider_name != 'unusual_whales' or not settings.provider_api_key:
        parser.error('configured Unusual Whales provider and key required')
    with WeeklyRequestBudget(settings.provider_usage_path, weekly_cap=40000, protected_reserve=7000,
            rolling_window=API_BASIC_ROLLING_WINDOW) as budget, SnapshotStore(settings.database_path) as snapshots:
        before = budget.usage()
        if before.remaining_before_reserve < maximum:
            raise ValueError('insufficient budget above reserve')
        client = UnusualWhalesClient(settings.provider_api_key, request_budget=budget)
        report['network_called'] = True
        report['company_results'] = collect_company_context(client=client, snapshots=snapshots, tickers=settings.watchlist)
        write_morning_run(args.output, report)
        stopped = any(row.get('collection_stopped') for row in report['company_results'])
        if args.retry_identified_gaps and not stopped:
            for ticker, kind in [('AAOI', 'greek_flow'), ('CSCO', 'greek_flow'), ('NOK', 'greek_flow'), ('ARM', 'splits')]:
                row = {'ticker': ticker, 'kind': kind}
                try:
                    response = client.greek_flow(ticker, as_of='2026-09-02') if kind == 'greek_flow' else client.company_reference(ticker, kind=kind)
                    raw = response.response.raw
                    source = snapshots.insert(SnapshotEnvelope(provider='unusual_whales',
                        dataset=Dataset.GREEK_FLOW if kind == 'greek_flow' else Dataset.CORPORATE_ACTION,
                        symbol=ticker, as_of=raw.fetched_at, retrieved_at=raw.fetched_at, payload=response.response.payload,
                        metadata={'capture_mode': 'identified_gap_recovery', 'enhanced_dataset': kind,
                                  'requested_market_date': '2026-09-02' if kind == 'greek_flow' else None,
                                  'provider_endpoint': response.endpoint, 'retrospective_recovery': True}))
                    row.update(status='CAPTURED', snapshot_id=source.id)
                    if kind == 'greek_flow':
                        row['coverage'] = summarize(response.data, cutoff_at=raw.fetched_at)
                        if row['coverage'].get('session_date') != '2026-09-02':
                            row['status'] = 'SCOPE_UNVERIFIED'
                except Exception as error:
                    row.update(status='FAILED', error_class=type(error).__name__, http_status=getattr(error, 'status_code', None))
                report['gap_retries'].append(row)
                write_morning_run(args.output, report)
                if row.get('error_class') == 'ProviderAuthenticationError':
                    break
        report.update(status='CAPTURED_REVIEW_REQUIRED', completed_at=datetime.now(UTC).isoformat(),
            transport_attempts=budget.usage().transport_attempts - before.transport_attempts,
            budget_after=budget.usage().public_dict())
        write_morning_run(args.output, report)
    print(json.dumps({key: report[key] for key in ('status', 'completed_at', 'transport_attempts')}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

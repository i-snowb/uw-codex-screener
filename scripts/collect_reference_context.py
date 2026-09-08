#!/usr/bin/env python3
"""Stage corporate actions and security identity; never auto-adjust historical prices."""

import argparse
from datetime import datetime, UTC
import json
from pathlib import Path

from morning_edge.clock import eastern, is_nyse_session
from morning_edge.config import Settings
from morning_edge.models import Dataset, SnapshotEnvelope
from morning_edge.providers.budget import WeeklyRequestBudget, API_BASIC_ROLLING_WINDOW
from morning_edge.providers.unusual_whales import UnusualWhalesClient
from morning_edge.store import SnapshotStore
from run_intraday_refresh import _load_env


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--audit-accepted', action='store_true')
    parser.add_argument('--env-file', type=Path, default=Path('.env'))
    args = parser.parse_args(argv)
    if args.live != args.audit_accepted:
        parser.error('both --live and --audit-accepted are required')
    _load_env(args.env_file)
    settings = Settings.from_env()
    logical = len(settings.watchlist) * 2 + 1
    result = {'status': 'PLANNED', 'logical_requests': logical, 'maximum_transport_attempts': logical * 3,
              'network_called': False, 'protected_reserve': 7000,
              'boundary': 'Raw reference context only. Adjustment basis, option deliverables and historical symbol lineage require reconciliation before model use.'}
    if not args.live:
        print(json.dumps(result, sort_keys=True)); return 0
    if not is_nyse_session(eastern(datetime.now(UTC)).date()):
        result['status'] = 'MARKET_CLOSED'; print(json.dumps(result, sort_keys=True)); return 0
    if settings.provider_name != 'unusual_whales' or not settings.provider_api_key:
        parser.error('configured Unusual Whales provider and key are required')
    captured = []
    try:
        with WeeklyRequestBudget(settings.provider_usage_path, weekly_cap=40000, protected_reserve=7000, rolling_window=API_BASIC_ROLLING_WINDOW) as budget, SnapshotStore(settings.database_path) as store:
            if budget.usage().remaining_before_reserve < logical * 3:
                raise ValueError('insufficient protected request capacity')
            client = UnusualWhalesClient(settings.provider_api_key, request_budget=budget)
            jobs = [(symbol, kind) for symbol in settings.watchlist for kind in ('splits', 'dividends')] + [('MARKET:LISTINGS', 'listings')]
            for symbol, kind in jobs:
                result['network_called'] = True
                response = client.security_listings() if kind == 'listings' else client.company_reference(symbol, kind=kind)
                raw = response.response.raw
                row = store.insert(SnapshotEnvelope(provider='unusual_whales', dataset=Dataset.SECURITY_IDENTITY if kind == 'listings' else Dataset.CORPORATE_ACTION,
                    symbol=symbol, as_of=raw.fetched_at, retrieved_at=raw.fetched_at, payload=response.response.payload,
                    metadata={'capture_mode': 'reference_context', 'reference_kind': kind, 'provider_endpoint': response.endpoint, 'adjustment_verified': False, 'timestamp_semantics': 'retrieval; event dates remain in raw rows'}))
                captured.append(row.id)
        result['status'] = 'CAPTURED_CONTEXT_NOT_RECONCILED'
    except Exception as error:
        result['status'] = 'FAILED_CLOSED'
        result['error_class'] = type(error).__name__
    result['source_snapshot_ids'] = captured
    print(json.dumps(result, sort_keys=True))
    return 2 if result['status'] == 'FAILED_CLOSED' else 0


if __name__ == '__main__':
    raise SystemExit(main())

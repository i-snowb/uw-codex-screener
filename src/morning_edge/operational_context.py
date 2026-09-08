"""Cutoff-safe company/event context, separate from frozen predictive features."""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta
import re
from zoneinfo import ZoneInfo

from .execution_controls import contract_identity, policy_diagnostic, quote_diagnostic
from .models import Dataset, SnapshotEnvelope, timestamp_from_text, timestamp_text
from .providers.base import CollectionCircuit

ET = ZoneInfo('America/New_York')
def official_calendar_source(value):
    return isinstance(value, str) and (value == 'https://www.bea.gov/news/schedule' or
        re.fullmatch(r'https://www\.bls\.gov/schedule/\d{4}/(?:0[1-9]|1[0-2])_sched\.htm', value) is not None)


def reviewed_calendar_context(document, *, cutoff):
    """Admit a reviewed public calendar only within its explicit freshness window."""
    unavailable = {'quality': 'unavailable', 'events': [], 'event_count': 0,
                   'coverage': 'UNKNOWN', 'reason': 'No current reviewed calendar cache'}
    if not document:
        return unavailable
    try:
        reviewed = timestamp_from_text(document['reviewed_at'])
        expires = timestamp_from_text(document['expires_at'])
        if not reviewed <= cutoff < expires or expires > reviewed + timedelta(hours=24):
            return unavailable | {'reason': 'Reviewed calendar is stale, future-dated, or has excessive validity'}
        events = []
        seen = set()
        for row in document['events']:
            when = timestamp_from_text(row['time'])
            source = row['source_url']
            name = row['event']
            if not official_calendar_source(source) or not isinstance(name, str) or not 1 <= len(name) <= 200:
                raise ValueError('invalid calendar event source or name')
            if not cutoff <= when <= cutoff + timedelta(days=14):
                continue
            key = (source, name, when)
            if key not in seen:
                seen.add(key)
                events.append({'event': name, 'time': timestamp_text(when), 'source_url': source})
        events.sort(key=lambda row: (row['time'], row['event']))
        company_events = []
        company_seen = set()
        for row in document.get('company_events', []):
            when = timestamp_from_text(row['time'])
            source = row['source_url']
            if row.get('ticker') != 'NBIS' or source not in {
                'https://nebius.com/investor-events/nebius-to-present-at-goldman-sachs-communacopia-technology-conference',
                'https://nebius.com/investor-events/nebius-to-present-at-citi-2026-global-tmt-conference'}:
                raise ValueError('unregistered company event source')
            if not isinstance(row.get('event'), str) or not 1 <= len(row['event']) <= 200:
                raise ValueError('invalid company event name')
            key = (row['ticker'], source, row['event'], when)
            if cutoff <= when <= cutoff + timedelta(days=14) and key not in company_seen:
                company_seen.add(key)
                company_events.append({'event': row['event'], 'ticker': row['ticker'], 'time': timestamp_text(when), 'source_url': source})
        company_events.sort(key=lambda row: (row['time'], row['event']))
    except (KeyError, TypeError, ValueError, AttributeError):
        return unavailable | {'reason': 'Reviewed calendar schema failed validation'}
    return {'quality': 'observed' if events else 'empty', 'events': events, 'event_count': len(events),
            'company_events': company_events,
            'coverage': 'PARTIAL_OFFICIAL_SCHEDULE', 'reviewed_at': timestamp_text(reviewed),
            'expires_at': timestamp_text(expires), 'source_kind': 'reviewed_official_schedule',
            'caveat': 'Reviewed official releases only; not a complete macro calendar. Cache expires after at most 24 hours.'}


def collect_company_context(*, client, snapshots, tickers):
    results = []
    circuit = CollectionCircuit()
    for ticker in tickers:
        if circuit.reason:
            results.append({'ticker': ticker, 'status': 'SKIPPED_AFTER_COLLECTION_FAILURE'})
            continue
        try:
            response = client.stock_info(ticker)
            circuit.success()
            raw = response.response.raw
            row = snapshots.insert(SnapshotEnvelope(provider='unusual_whales', dataset=Dataset.SECURITY_IDENTITY,
                symbol=ticker, as_of=raw.fetched_at, retrieved_at=raw.fetched_at, payload=response.response.payload,
                metadata={'capture_mode': 'operational_context', 'reference_kind': 'stock_info',
                          'provider_endpoint': response.endpoint, 'timestamp_semantics': 'retrieval; next event remains provider-reported'}))
            results.append({'ticker': ticker, 'status': 'CAPTURED', 'snapshot_id': row.id})
        except Exception as error:
            circuit.failure(error)
            status = getattr(error, 'status_code', None)
            results.append({'ticker': ticker, 'status': 'FAILED', 'error_class': type(error).__name__, 'http_status': status,
                            'collection_stopped': bool(circuit.reason)})
    return results


def attach_operational_context(run, *, snapshots, company_results, calendar=None, policy=None, diagnostics=None):
    cutoff = timestamp_from_text(run['cutoff_at'])
    result = deepcopy(run)
    collected = {row['ticker']: row for row in company_results}
    identities, known_earnings = 0, 0
    checks = deepcopy(diagnostics or {})
    checks['chain_identity'], checks['reference_quote_checks'] = {}, {}
    for entry in result.get('watchlist', []):
        ticker = entry['ticker']
        item = collected.get(ticker, {})
        profile = {'status': item.get('status', 'NOT_CAPTURED'), 'next_earnings_date': None}
        snapshot_id = item.get('snapshot_id')
        if snapshot_id is not None:
            stored = snapshots.get(snapshot_id)
            source = stored.envelope if stored else None
            if source is None or source.symbol != ticker or source.dataset != Dataset.SECURITY_IDENTITY or source.metadata.get('reference_kind') != 'stock_info':
                raise ValueError('company context source identity mismatch')
            if source.as_of > cutoff or source.retrieved_at > cutoff:
                raise ValueError('company context source unavailable at cutoff')
            data = source.payload['data']
            if data.get('symbol') != ticker:
                raise ValueError('company context payload identity mismatch')
            event_date = data.get('next_earnings_date')
            if event_date and datetime.fromisoformat(event_date).date() < cutoff.astimezone(ET).date():
                event_date = None
            identities += 1
            known_earnings += int(event_date is not None)
            profile = {'status': 'PROVIDER_IDENTITY_OBSERVED_NOT_RECONCILED', 'full_name': data.get('full_name'),
                'issue_type': data.get('issue_type'), 'next_earnings_date': event_date,
                'announce_time': data.get('announce_time'), 'has_earnings_history': data.get('has_earnings_history'),
                'retrieved_at': timestamp_text(source.retrieved_at), 'source_snapshot_id': snapshot_id,
                'boundary': 'Provider-reported identity and event date; not independent corporate-action or deliverable verification.'}
            provenance = entry.setdefault('provenance', {})
            provenance['analysis_snapshot_ids'] = sorted(set(provenance.get('analysis_snapshot_ids', provenance.get('snapshot_ids', []))) | {snapshot_id})
            entry.setdefault('field_source_snapshot_ids', {})['company_reference'] = [snapshot_id]
        entry['company_reference'] = profile
        chain_sources = []
        for sid in entry.get('provenance', {}).get('snapshot_ids', []):
            stored = snapshots.get(sid)
            if stored and stored.envelope.dataset == Dataset.OPTION_CHAIN:
                source = stored.envelope
                if source.symbol != ticker or source.as_of > cutoff or source.retrieved_at > cutoff:
                    raise ValueError('chain context source identity/cutoff mismatch')
                chain_sources.append(stored)
        if len(chain_sources) == 1:
            stored = chain_sources[0]
            rows = stored.envelope.payload['data']
            checks['chain_identity'][ticker] = chain_identity_diagnostic(rows, ticker) | {'source_snapshot_id': stored.id}
            expected = entry.get('trade_thesis', {}).get('option_reference', {}).get('contract')
            selected = next((row for row in rows if row.get('option_symbol') == expected), {})
            quote = dict(selected, bid=selected.get('nbbo_bid', selected.get('bid')), ask=selected.get('nbbo_ask', selected.get('ask')),
                quote_timestamp=selected.get('quote_time') or selected.get('nbbo_timestamp'),
                received_at=timestamp_text(stored.envelope.retrieved_at), timestamp_semantics='unverified_stored_chain',
                source_kind='stored_chain', source='unusual_whales')
            checks['reference_quote_checks'][ticker] = quote_diagnostic(quote,
                expected_contract=expected, observed_at=cutoff, policy=policy or {}) | {'source_snapshot_id': stored.id}
        else:
            checks['chain_identity'][ticker] = {'status': 'UNAVAILABLE_OR_AMBIGUOUS', 'source_count': len(chain_sources)}
            checks['reference_quote_checks'][ticker] = {'status': 'BLOCKED', 'errors': ['one_current_chain_source_required'], 'execution_ready': False}
    cal = reviewed_calendar_context(calendar, cutoff=cutoff)
    old = result.setdefault('enhanced_contexts', {}).get('economic_calendar', {})
    if not old.get('events') and cal['events']:
        result['enhanced_contexts']['economic_calendar'] = cal
    result['operational_context'] = {'schema_version': 'operational-context-v1', 'checked_at': run['cutoff_at'],
        'company_identity_observed': identities, 'known_upcoming_earnings': known_earnings,
        'unknown_upcoming_earnings': len(result.get('watchlist', [])) - known_earnings,
        'calendar': cal, 'risk_policy': policy_diagnostic(policy or {}),
        'diagnostics': checks, 'recommendations_enabled': False}
    return result


def chain_identity_diagnostic(rows, ticker):
    counts = Counter()
    for row in rows:
        result = contract_identity(row, ticker)
        counts['contracts'] += 1
        counts['identity_fields_match'] += int(result['status'] == 'IDENTITY_FIELDS_MATCH')
        for error in result['errors']:
            counts[error] += 1
    return {'counts': dict(counts), 'deliverables_verified': False, 'adjustment_basis_verified': False}

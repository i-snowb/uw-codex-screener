"""Separate transport success, session freshness, coverage and analytical validity."""

from collections import Counter
from datetime import date, datetime
import math
from typing import Any, Mapping, Sequence

from .freshness import latest_complete_session, dataset_freshness
from .benchmarks import BENCHMARKS


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def chain_quality(rows: Sequence[Mapping[str, Any]], *, spot: float | None, as_of: date) -> dict[str, Any]:
    strata: dict[str, Counter] = {}
    for row in rows:
        try:
            expiry = date.fromisoformat(str(row.get('expires') or row.get('expiry'))[:10])
            dte = (expiry - as_of).days
        except ValueError:
            dte = None
        strike = number(row.get('strike'))
        distance = abs(strike / spot - 1) if strike is not None and spot else None
        bid, ask = number(row.get('nbbo_bid', row.get('bid'))), number(row.get('nbbo_ask', row.get('ask')))
        spread = (ask-bid)/((ask+bid)/2) if bid is not None and ask is not None and bid > 0 and ask >= bid else None
        liquid = spread is not None and spread <= .25 and (number(row.get('open_interest')) or 0) >= 100
        groups = ['all', 'liquid' if liquid else 'illiquid_or_unknown', 'near_money' if distance is not None and distance <= .1 else 'far_or_unknown', 'front_60d' if dte is not None and 0 <= dte <= 60 else 'long_or_unknown']
        delta, gamma, iv = (number(row.get(field)) for field in ('delta', 'gamma', 'implied_volatility'))
        present = all(value is not None for value in (delta, gamma, iv))
        side = str(row.get('option_type', row.get('type', ''))).lower()
        valid_delta = delta is not None and -1 <= delta <= 1 and not (side == 'call' and delta < 0 or side == 'put' and delta > 0)
        valid = present and valid_delta and gamma >= 0 and iv > 0
        for group in groups:
            counts = strata.setdefault(group, Counter())
            counts['contracts'] += 1
            counts['usable_greeks'] += int(valid)
            counts['missing_greeks'] += int(not present)
            counts['invalid_greeks'] += int(present and not valid)
            counts['crossed_quotes'] += int(bid is not None and ask is not None and bid > ask)
            counts['quote_timestamp_present'] += int(bool(row.get('quote_time') or row.get('nbbo_timestamp')))
    return {'status': 'MEASURED_FIELD_AND_RANGE_COVERAGE', 'market_date': as_of.isoformat(), 'strata': {key: dict(value) for key, value in strata.items()}, 'boundary': 'Finite fields and basic delta/gamma/IV ranges are checked, not model accuracy. Quote timestamp presence is not verified executable quote age. Last-trade time is not quote time.'}


def run_health(run: Mapping[str, Any], *, observed_at: datetime | None = None) -> dict[str, Any]:
    cutoff = observed_at or datetime.fromisoformat(str(run['cutoff_at']).replace('Z', '+00:00'))
    expected = latest_complete_session(cutoff).isoformat()
    failures, warnings, rows = [], [], []
    for entry in run.get('watchlist', []):
        ticker = entry['ticker']
        freshness = dataset_freshness(cutoff_at=cutoff, price_session=entry.get('price', {}).get('as_of'), dataset_dates={
            'chain': entry.get('edge', {}).get('option_surface', {}).get('market_date'),
            'gex': entry.get('edge', {}).get('gex_topology', {}).get('date'),
        })
        problems = [name for name, value in freshness['datasets'].items() if value['status'] not in {'LATEST_EXPECTED_SESSION', 'INTRADAY_PARTIAL'}]
        if problems:
            failures.append(f"{ticker}: stale or missing required session fields: {','.join(problems)}")
        for benchmark in BENCHMARKS:
            context = entry.get('benchmark_context', {}).get(benchmark, {})
            if context.get('status') not in {'ALIGNED_RESEARCH_CONTEXT', 'INSUFFICIENT_ALIGNED_HISTORY'} or context.get('latest_session') != expected:
                failures.append(f'{ticker}: {benchmark} benchmark not aligned to expected session')
            elif context.get('status') == 'INSUFFICIENT_ALIGNED_HISTORY':
                warnings.append(f'{ticker}: insufficient history for {benchmark} relative features')
        flow = entry.get('edge', {}).get('flow_conviction', {})
        if flow.get('coverage_status') != 'COMPLETE_SESSION':
            warnings.append(f'{ticker}: bounded flow, complete-session comparisons unavailable')
        greek = entry.get('whale_evidence', {}).get('greek_flow', {})
        if not greek.get('confirmation_eligible'):
            warnings.append(f'{ticker}: Greek-flow confirmation unavailable')
        whale = entry.get('whale_evidence', {})
        slow = whale.get('short_crowding', {})
        enhanced_dates = {'greek_flow': greek.get('session_date'),
            'greek_exposure': whale.get('greek_exposure', {}).get('provider_date'),
            'volatility': whale.get('volatility', {}).get('provider_date'),
            'short_interest': slow.get('short_interest_date'), 'borrow': slow.get('latest_borrow_timestamp'),
            'short_volume': slow.get('short_volume_date')}
        rows.append({'ticker': ticker, 'freshness': freshness, 'flow_window_coverage': flow.get('coverage_status', 'UNAVAILABLE'), 'greek_window_coverage': greek.get('coverage_status', 'UNAVAILABLE'), 'enhanced_observation_dates': enhanced_dates, 'slow_feed_policy': 'Report observation date and retrieval separately. Short-interest reporting lag is context, not a same-day positioning change. Missing timestamps remain unknown; no execution or model eligibility is inferred.', 'field_validity': entry.get('chain_quality', {}), 'model_eligibility': 'RESEARCH_ONLY_UNCALIBRATED'})
    if not rows:
        failures.append('watchlist is empty')
    calendar = run.get('enhanced_contexts', {}).get('economic_calendar', {})
    if calendar.get('quality') != 'observed' or not calendar.get('event_count'):
        warnings.append('Upcoming economic-calendar coverage is unavailable; an empty feed does not mean no event risk')
    warnings.append('Corporate-action adjustment basis, security identity and option deliverables remain unreconciled')
    return {'schema_version': 'data-health-v2', 'status': 'BLOCKED' if failures else 'RESEARCH_READY_WITH_LIMITATIONS' if warnings else 'RESEARCH_READY', 'expected_complete_session': expected, 'checked_at': cutoff.isoformat(), 'failures': failures, 'warnings': warnings, 'tickers': rows, 'recommendations_enabled': False}


def assert_publishable(run: Mapping[str, Any], *, observed_at: datetime) -> dict[str, Any]:
    health = run_health(run, observed_at=observed_at)
    if health['failures']:
        raise ValueError('publication blocked by required session/benchmark health')
    if run.get('recommendations_enabled') is not False:
        raise ValueError('publication must remain research-only')
    if str(run.get('mode', '')).startswith('RETROSPECTIVE'):
        raise ValueError('retrospective reprocessing cannot pass a daily readiness gate')
    cutoff = datetime.fromisoformat(str(run['cutoff_at']).replace('Z', '+00:00'))
    from .clock import eastern
    if cutoff > observed_at or eastern(cutoff).date() != eastern(observed_at).date():
        raise ValueError('daily publication cutoff must be today and not in the future')
    for entry in run['watchlist']:
        if entry.get('agent_enrichment_validated') is not True or entry.get('action') != 'NO_RECOMMENDATION':
            raise ValueError('every ticker needs validated, fail-closed enrichment')
    return health


def execution_readiness(run: Mapping[str, Any]) -> dict[str, Any]:
    """Expose unsatisfied execution prerequisites; this report cannot authorize trades."""
    health = run_health(run)
    gates = [
        {'id': 'research_data', 'status': 'BLOCKED' if health['failures'] else 'PASS',
         'reason': 'Latest required session and benchmark checks. This is not executable quote validation.'},
        {'id': 'calibration', 'status': 'BLOCKED',
         'reason': 'No independently reviewed calibration and promotion artifact is registered. More recovered history cannot create prospective origins.'},
        {'id': 'execution_quotes', 'status': 'BLOCKED',
         'reason': 'No independent executable bid/ask feed with verified quote timestamps, contract identity and friction checks is integrated.'},
        {'id': 'risk_policy', 'status': 'BLOCKED',
         'reason': 'No approved policy registry exists for per-trade loss, aggregate correlated risk, expiry, liquidity and event rules.'},
        {'id': 'reference_reconciliation', 'status': 'BLOCKED',
         'reason': 'Corporate actions, security identity, price adjustment basis and option deliverables are not reconciled.'},
        {'id': 'event_coverage', 'status': 'BLOCKED',
         'reason': 'Historical earnings and a bounded economic calendar do not establish complete upcoming event coverage.'},
        {'id': 'order_routing', 'status': 'DISABLED',
         'reason': 'This application is research-only. No orders are placed or routed.'},
    ]
    ticker_gaps = []
    for entry in run.get('watchlist', []):
        edge = entry.get('edge', {})
        gaps = []
        if edge.get('open_interest', {}).get('status') != 'DERIVED_FROM_CONSECUTIVE_CHAINS':
            gaps.append('consecutive_chain_oi')
        if not entry.get('whale_evidence', {}).get('greek_flow', {}).get('confirmation_eligible'):
            gaps.append('regular_session_greek_coverage')
        for section in ('flow_conviction', 'dark_pool'):
            if edge.get(section, {}).get('coverage_status') != 'COMPLETE_SESSION':
                gaps.append(section + '_window')
        ticker_gaps.append({'ticker': entry['ticker'], 'remaining_context_gaps': gaps})
    return {'schema_version': 'execution-readiness-v1', 'status': 'BLOCKED',
            'execution_ready': False, 'recommendations_enabled': False,
            'gates': gates, 'ticker_context': ticker_gaps,
            'boundary': 'Diagnostic checklist, not a trading permission or calibration certificate.'}

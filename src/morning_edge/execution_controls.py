"""Deterministic preflight diagnostics. These functions never authorize an order."""
from datetime import datetime
import hashlib
import json
import math
import re

from .models import timestamp_from_text
from .clock import eastern


def finite(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def policy_diagnostic(policy):
    """Validate a draft; approval and execution remain separate, unimplemented gates."""
    policy = policy if isinstance(policy, dict) else {}
    errors = []
    for field in ('broker_or_quote_feed', 'policy_id'):
        if not isinstance(policy.get(field), str) or not policy[field].strip():
            errors.append(field + ': required')
    symbols = policy.get('allowed_symbols')
    if not isinstance(symbols, list) or not symbols or any(not isinstance(s, str) or not re.fullmatch(r'[A-Z][A-Z0-9.-]{0,9}', s) for s in symbols):
        errors.append('allowed_symbols: explicit nonempty ticker list required')
    fields = ('max_trade_loss_usd', 'max_total_options_risk_usd', 'max_correlated_risk_usd',
              'max_quote_age_seconds', 'max_spread_fraction', 'min_open_interest', 'min_volume',
              'min_dte', 'max_dte')
    values = {field: finite(policy.get(field)) for field in fields}
    for field, value in values.items():
        if value is None or value <= 0:
            errors.append(field + ': positive finite value required')
    for lower, upper in (('max_trade_loss_usd', 'max_correlated_risk_usd'),
                         ('max_correlated_risk_usd', 'max_total_options_risk_usd'), ('min_dte', 'max_dte')):
        if values[lower] is not None and values[upper] is not None and values[lower] > values[upper]:
            errors.append(lower + ': must not exceed ' + upper)
    if values['max_spread_fraction'] is not None and values['max_spread_fraction'] > 1:
        errors.append('max_spread_fraction: must not exceed 1')
    for field in ('min_dte', 'max_dte', 'min_open_interest', 'min_volume'):
        value = values[field]
        if value is not None and value != int(value):
            errors.append(field + ': integer required')
    if policy.get('earnings_holding_rule') not in {'prohibited', 'manual_review'}:
        errors.append('earnings_holding_rule: prohibited or manual_review required')
    try:
        canonical = json.dumps(policy, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (ValueError, TypeError):
        errors.append('policy: non-JSON or nonfinite values prohibited')
        canonical = '{}'
    return {'status': 'DRAFT_VALIDATED_NOT_APPROVED' if not errors else 'UNCONFIGURED_OR_INVALID',
            'errors': errors, 'policy_digest': hashlib.sha256(canonical.encode()).hexdigest(),
            'execution_ready': False, 'approved': False}


def contract_identity(row, ticker):
    symbol = row.get('option_symbol', row.get('contract'))
    match = re.fullmatch(r'([A-Z][A-Z0-9.]{0,9})(\d{6})([CP])(\d{8})', str(symbol or ''))
    errors = []
    if not match:
        return {'status': 'INVALID', 'errors': ['unparseable_option_symbol'], 'deliverables_verified': False}
    root, expiry_text, side, strike_text = match.groups()
    try:
        expiry = datetime.strptime(expiry_text, '%y%m%d').date().isoformat()
    except ValueError:
        errors.append('invalid_expiry')
        expiry = None
    if root != ticker:
        errors.append('root_mismatch_or_adjusted_contract')
    provided_expiry = row.get('expires', row.get('expiry'))
    if provided_expiry != expiry:
        errors.append('expiry_mismatch')
    if finite(row.get('strike')) != int(strike_text) / 1000:
        errors.append('strike_mismatch')
    if row.get('option_type', row.get('type')) != {'C': 'call', 'P': 'put'}[side]:
        errors.append('option_type_mismatch')
    return {'status': 'IDENTITY_FIELDS_MATCH' if not errors else 'INVALID', 'errors': errors,
            'deliverables_verified': False, 'boundary': 'OSI field consistency does not verify OCC deliverables or price adjustment basis.'}


def quote_diagnostic(row, *, expected_contract, observed_at, policy):
    policy = policy if isinstance(policy, dict) else {}
    errors = []
    if observed_at.tzinfo is None:
        raise ValueError('observed_at must be timezone-aware')
    config = policy_diagnostic(policy)
    if config['errors']:
        errors.append('risk_policy_unconfigured_or_invalid')
    if row.get('option_symbol') != expected_contract:
        errors.append('contract_mismatch')
    parsed = re.fullmatch(r'([A-Z][A-Z0-9.]{0,9})(\d{6})([CP])(\d{8})', str(expected_contract))
    dte = None
    if parsed:
        ticker = parsed.group(1)
        errors.extend(contract_identity(row, ticker)['errors'])
        allowed_symbols = policy.get('allowed_symbols')
        if not isinstance(allowed_symbols, list) or ticker not in allowed_symbols:
            errors.append('ticker_not_allowed')
        try:
            dte = (datetime.strptime(parsed.group(2), '%y%m%d').date() - eastern(observed_at).date()).days
        except ValueError:
            errors.append('invalid_expiry')
    else:
        errors.append('invalid_expected_contract')
    bid, ask = finite(row.get('bid')), finite(row.get('ask'))
    spread = None
    if bid is None or ask is None or bid <= 0 or ask < bid:
        errors.append('invalid_or_crossed_bid_ask')
    else:
        spread = (ask-bid)/((ask+bid)/2)
    for field in ('bid_size', 'ask_size'):
        value = finite(row.get(field))
        if value is None or value < 1 or value != int(value):
            errors.append(field + '_missing_or_invalid')
    age = None
    try:
        quote_at = timestamp_from_text(row['quote_timestamp'])
        received_at = timestamp_from_text(row['received_at'])
        age = (observed_at-quote_at).total_seconds()
        if quote_at > received_at or received_at > observed_at or age < 0:
            errors.append('future_or_inconsistent_quote_time')
    except (KeyError, ValueError, TypeError, AttributeError):
        errors.append('verified_quote_and_receipt_timestamps_required')
    if row.get('timestamp_semantics') != 'quote_update' or row.get('source_kind') != 'executable_nbbo':
        errors.append('not_an_executable_nbbo_quote_update')
    if not row.get('source') or row.get('source') != policy.get('broker_or_quote_feed'):
        errors.append('unconfigured_quote_source')
    if not config['errors']:
        if age is not None and age > finite(policy['max_quote_age_seconds']):
            errors.append('stale_quote')
        if spread is not None and spread > finite(policy['max_spread_fraction']):
            errors.append('spread_exceeds_policy')
        for field, limit in (('open_interest', 'min_open_interest'), ('volume', 'min_volume')):
            value = finite(row.get(field))
            if value is None or value < finite(policy[limit]):
                errors.append(field + '_below_policy_or_unknown')
        if dte is None or not finite(policy['min_dte']) <= dte <= finite(policy['max_dte']):
            errors.append('expiry_outside_policy')
    return {'status': 'INPUT_CHECKS_PASS_NOT_AUTHORIZED' if not errors else 'BLOCKED',
            'errors': errors, 'quote_age_seconds': age, 'spread_fraction': spread,
            'execution_ready': False, 'boundary': 'Input checks do not verify feed entitlement, deliverables, model calibration, or policy approval.'}

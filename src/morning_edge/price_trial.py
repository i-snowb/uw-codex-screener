"""Fixed-coefficient prospective shadow trial. No provider or ledger writes."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import hashlib
import json
from math import log, sqrt
from pathlib import Path
from statistics import fmean, pstdev

from .challengers import _clean_bars
from .clock import next_nyse_session
from .experiments import PRICE_FEATURES, digest, number, ridge_predict
from .freshness import latest_complete_session
from .models import timestamp_from_text

VERSION = "fixed-price-ridge-v1"
HORIZONS = (1, 5, 20)
MIN_ORIGINS = 60


def implementation_digest() -> str:
    root = Path(__file__).parent
    return digest({name: hashlib.sha256((root/name).read_bytes()).hexdigest()
                   for name in ('price_trial.py', 'experiments.py', 'challengers.py', 'clock.py', 'freshness.py')})


def freeze_trial(experiment: dict, *, source_sha256: str, starts_at: str, frozen_at: str,
                 tickers: list[str]) -> dict:
    start, frozen = timestamp_from_text(starts_at), timestamp_from_text(frozen_at)
    if start <= frozen or experiment.get('promotion_eligible') is not False:
        raise ValueError('Trial must start after freezing and remain shadow-only')
    if experiment.get('holdout', {}).get('status') != 'SEALED_NOT_SCORED':
        raise ValueError('An unscored holdout is required')
    models = {}
    for horizon in HORIZONS:
        folds = [fold for fold in experiment['folds'] if fold['horizon'] == horizon]
        fold = max(folds, key=lambda item: item['test_start'])
        if not fold['latest_training_target'] < fold['test_start'] < experiment['holdout']['start']:
            raise ValueError('Training boundary is invalid')
        models[str(horizon)] = deepcopy(fold['models']['absolute_return']['ridge_price'])
        models[str(horizon)]['latest_training_target'] = fold['latest_training_target']
    body = {'schema_version': VERSION, 'starts_at': starts_at, 'frozen_at': frozen_at,
            'implementation_sha256': implementation_digest(),
            'source_sha256': source_sha256, 'experiment_id': experiment['experiment_id'],
            'tickers': sorted(set(tickers)), 'models': models, 'promotion_eligible': False,
            'minimum_matched_origins_per_horizon': MIN_ORIGINS,
            'policy': 'Fixed coefficients and scalers; no retraining, backfill, tuning, or automatic promotion.'}
    trial = dict(body, trial_id=digest(body))
    validate_trial(trial)
    return trial


def validate_trial(trial: dict) -> None:
    body = {key: value for key, value in trial.items() if key != 'trial_id'}
    if (trial.get('schema_version') != VERSION or trial.get('trial_id') != digest(body)
            or trial.get('implementation_sha256') != implementation_digest()
            or trial.get('promotion_eligible') is not False
            or timestamp_from_text(trial['starts_at']) <= timestamp_from_text(trial['frozen_at'])
            or set(trial['models']) != {str(h) for h in HORIZONS}):
        raise ValueError('Invalid frozen trial')
    for model in trial['models'].values():
        if (model['features'] != list(PRICE_FEATURES) or model['penalty'] != 1.0
                or len(model['means']) != 3 or len(model['scales']) != 3 or len(model['coefficients']) != 4
                or any(number(v) is None for key in ('means', 'scales', 'coefficients') for v in model[key])
                or any(v <= 0 for v in model['scales'])):
            raise ValueError('Invalid frozen coefficients or feature contract')


def attach_trial(run: dict, path: Path) -> dict:
    """Called only while creating a new daily source artifact, before registration."""
    if not path.exists():
        return run
    trial = json.loads(path.read_bytes())
    validate_trial(trial)
    result = deepcopy(run)
    cutoff = timestamp_from_text(run['cutoff_at'])
    allowed = (cutoff >= timestamp_from_text(trial['starts_at'])
               and timestamp_from_text(run.get('generated_at', run['cutoff_at'])) >= cutoff
               and not run.get('reprocessing') and run.get('mode') != 'RETROSPECTIVE_REPROCESSING'
               and run.get('forecast_registration_allowed') is not False)
    result['price_trial'] = {'trial_id': trial['trial_id'], 'starts_at': trial['starts_at'],
                             'status': 'SHADOW_ONLY' if allowed else 'NOT_STARTED',
                             'promotion_eligible': False}
    for entry in result['watchlist']:
        forecast = {'status': 'NOT_ELIGIBLE', 'path': [], 'trial_id': trial['trial_id']}
        entry.setdefault('edge', {})['price_only_trial'] = forecast
        try:
            if not allowed or entry['ticker'] not in trial['tickers']:
                raise ValueError('OUTSIDE_TRIAL_SCOPE_OR_START')
            dates, closes = _clean_bars(entry['technical']['bars'], cutoff_at=cutoff)
            if len(closes) < 21 or dates[-1] != latest_complete_session(cutoff):
                raise ValueError('INSUFFICIENT_OR_STALE_HISTORY')
            if (entry['price']['as_of'][:10] != dates[-1].isoformat()
                    or number(entry['price']['value']) != closes[-1]):
                raise ValueError('ORIGIN_PRICE_MISMATCH')
            features = dict(zip(PRICE_FEATURES, (closes[-1]/closes[-6]-1,
                closes[-1]/closes[-21]-1,
                pstdev([log(b/a) for a, b in zip(closes[-21:-1], closes[-20:])])*sqrt(252))))
            targets = []
            for horizon in HORIZONS:
                target = dates[-1]
                for _ in range(horizon):
                    target = next_nyse_session(target, include_current=False)
                predicted = ridge_predict(trial['models'][str(horizon)], features)
                if number(predicted) is None or predicted <= -1:
                    raise ValueError('INVALID_PREDICTED_RETURN')
                targets.append({'session': horizon, 'date': target.isoformat(), 'center_return': predicted})
            forecast.update(status='SHADOW_UNCALIBRATED', model_version=VERSION+'-'+trial['trial_id'],
                            path=targets, features=features, starts_at=trial['starts_at'],
                            direction='NEUTRAL', promotion_eligible=False)
        except (ValueError, KeyError, TypeError) as error:
            forecast['reason'] = str(error)
    return result


def paired_trial_report(rows: list[dict]) -> list[dict]:
    """Pair only canonical prospective publications with the same origin and cutoff."""
    eligible = [r for r in rows if r.get('daily_tracking_eligible')
                and r.get('registration_mode') == 'PROSPECTIVE']
    def key(row):
        return tuple(row.get(k) for k in ('ticker', 'origin_session', 'horizon_sessions', 'published_at', 'origin_close'))
    active = defaultdict(list)
    for row in eligible:
        if row['model_role'] == 'ACTIVE_THESIS_V3':
            active[key(row)].append(row)
    groups = defaultdict(list)
    for trial in eligible:
        if trial['model_role'] != 'SHADOW_PRICE_TRIAL':
            continue
        matches = active.get(key(trial), [])
        if len(matches) != 1:
            groups[(trial['model_version'], 'UNMATCHED', trial['horizon_sessions'])].append((trial, None))
        else:
            groups[(trial['model_version'], matches[0]['model_version'], trial['horizon_sessions'])].append((trial, matches[0]))
    result = []
    for (version, baseline, horizon), pairs in sorted(groups.items()):
        matched, pending, excluded = [], 0, 0
        for trial, active_row in pairs:
            if active_row is None:
                excluded += 1
                continue
            if any(r['status'] != 'EVALUATED' for r in (trial, active_row)):
                pending += 1
                continue
            values = [number(r.get(k)) for r in (trial, active_row)
                      for k in ('underlying_return_pct', 'target_center_return_pct')]
            if (None in values or trial.get('target_session') != active_row.get('target_session')
                    or not trial.get('target_session') or abs(values[0]-values[2]) > 1e-9):
                excluded += 1
                continue
            matched.append((trial, active_row))
        by_date = defaultdict(list)
        for trial, active_row in matched:
            actual = trial['underlying_return_pct']
            by_date[trial['origin_session']].append({
                'trial_mae': abs(trial['target_center_return_pct']-actual),
                'active_mae': abs(active_row['target_center_return_pct']-actual),
                'zero_mae': abs(actual),
                'trial_accuracy': float((trial['target_center_return_pct'] > 0)-(trial['target_center_return_pct'] < 0) == (actual > 0)-(actual < 0)),
                'active_accuracy': float((active_row['target_center_return_pct'] > 0)-(active_row['target_center_return_pct'] < 0) == (actual > 0)-(actual < 0)),
                'always_up_accuracy': float(actual > 0),
                'momentum_accuracy': (float(trial.get('baseline_directions', {}).get('twenty_session_momentum') == ('BULLISH' if actual > 0 else 'BEARISH' if actual < 0 else 'NEUTRAL'))
                                      if trial.get('baseline_directions', {}).get('twenty_session_momentum') in ('BULLISH', 'BEARISH') else None)})
        metrics = {}
        for name in ('trial_mae', 'active_mae', 'zero_mae', 'trial_accuracy', 'active_accuracy', 'always_up_accuracy', 'momentum_accuracy'):
            daily = [fmean(r[name] for r in day) for day in by_date.values() if all(r[name] is not None for r in day)]
            metrics[name] = fmean(daily) if daily and len(daily) == len(by_date) else None
        result.append({'trial_version': version, 'active_version': baseline, 'horizon': horizon,
                       'registered': len(pairs), 'matched': len(matched), 'origin_dates': len(by_date),
                       'pending': pending, 'excluded': excluded, **metrics,
                       'cohort_sha256': digest(sorted((t['forecast_id'], a['forecast_id']) for t, a in matched)),
                       'status': 'REVIEW_REQUIRED' if len(by_date) >= MIN_ORIGINS else 'COLLECTING',
                       'promotion_eligible': False})
    return result

"""Cutoff-safe, exact-session benchmark context; not an active model change."""

from typing import Any, Mapping, Sequence
from .clock import is_nyse_session
from datetime import date, timedelta

BENCHMARKS = ('SPY', 'QQQ', 'SMH', 'SOXX', 'IWM')


def relative_context(stock: Sequence[Mapping[str, Any]], benchmark: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    left, right = ({row['date']: row['close'] for row in rows} for rows in (stock, benchmark))
    latest = max(left, default=None)
    result: dict[str, Any] = {'status': 'UNAVAILABLE', 'latest_session': max(right, default=None), 'units': 'fractional_return', 'method': 'same-session stock return minus benchmark return; not beta-adjusted alpha', 'returns': {}}
    if not latest or latest != max(right, default=None):
        result['reason'] = 'benchmark and stock latest sessions do not match'
        return result
    dates = [latest]
    cursor = date.fromisoformat(latest)
    while len(dates) < 64:
        cursor -= timedelta(days=1)
        if is_nyse_session(cursor):
            dates.append(cursor.isoformat())
    for horizon in (1, 5, 20, 63):
        required = dates[:horizon + 1]
        value = None
        if all(day in left and day in right and left[day] > 0 and right[day] > 0 for day in required):
            value = left[latest] / left[required[-1]] - right[latest] / right[required[-1]]
        result['returns'][str(horizon)] = value
    result['status'] = 'ALIGNED_RESEARCH_CONTEXT' if result['returns']['20'] is not None else 'INSUFFICIENT_ALIGNED_HISTORY'
    return result

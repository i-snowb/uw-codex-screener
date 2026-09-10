"""Join enhanced evidence under one cutoff and retain field/source membership."""

from copy import deepcopy
from typing import Any, Mapping

from .models import timestamp_from_text


GROUPS = {
    "stock_state": ("stock_state",), "option_price_levels": ("option_price_levels", "reference_price"),
    "greek_exposure": ("greek_exposure_strike", "reference_price"), "greek_flow": ("greek_flow",),
    "volatility": ("iv_term_structure", "volatility_stats", "interpolated_iv"),
    "volatility_diagnostics": ("volatility_anomaly", "volatility_character", "variance_risk_premium"),
    "dark_pool_levels": ("dark_pool_levels", "reference_price"),
    "short_crowding": ("short_interest", "short_borrow", "short_volume"),
}


def attach_enhanced(run: Mapping[str, Any], enhanced: Mapping[str, Any]) -> dict[str, Any]:
    cutoff = timestamp_from_text(str(run['cutoff_at']))
    if enhanced.get('aggregation_version') != 'enhanced-evidence-v2':
        raise ValueError('enhanced evidence must be rebuilt with verified minute aggregation')
    if timestamp_from_text(str(enhanced.get('cutoff_at'))) != cutoff:
        raise ValueError('enhanced evidence cutoff differs from the run')
    descriptors = {row['id']: row for row in enhanced.get('source_snapshots', [])}
    for row in descriptors.values():
        if timestamp_from_text(row['retrieved_at']) > cutoff or timestamp_from_text(row['as_of']) > cutoff:
            raise ValueError('enhanced source was unavailable at cutoff')
    result = deepcopy(dict(run))
    for entry in result.get('watchlist', []):
        symbol = entry['ticker']
        evidence = deepcopy(enhanced.get('symbols', {}).get(symbol, {}))
        sources = evidence.get('sources', {})
        ids = sorted({value for value in sources.values() if value is not None})
        if any(value not in descriptors or descriptors[value]['symbol'] != symbol for value in ids):
            raise ValueError('enhanced source is missing or belongs to another ticker')
        evidence['source_snapshot_ids'] = ids
        evidence['source_snapshots'] = [descriptors[value] for value in ids]
        entry['whale_evidence'] = evidence
        provenance = entry.setdefault('provenance', {})
        provenance['analysis_snapshot_ids'] = sorted(set(provenance.get('analysis_snapshot_ids', provenance.get('snapshot_ids', []))) | set(ids))
        mapping = entry.setdefault('field_source_snapshot_ids', {})
        for group, families in GROUPS.items():
            mapping['whale_evidence.' + group] = sorted({sources[name] for name in families if sources.get(name) is not None})
    result['enhanced_contexts'] = deepcopy(enhanced.get('contexts', {}))
    result['enhanced_cutoff_at'] = run['cutoff_at']
    from .data_health import run_health
    result['data_health'] = run_health(result)
    return result


def field_sources(entry: Mapping[str, Any], path: str) -> set[int] | None:
    mappings = entry.get('field_source_snapshot_ids', {})
    matches = [key for key in mappings if path == key or path.startswith(key + '.') or path.startswith(key + '[')]
    return set(mappings[max(matches, key=len)]) if matches else None

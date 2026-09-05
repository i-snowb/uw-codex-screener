#!/usr/bin/env python3
"""Build immutable feature/replay records and research-control manifests."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from morning_edge.config import private_runtime_path  # noqa: E402
from morning_edge.evaluation import build_report  # noqa: E402
from morning_edge.feature_mart import FeatureMart, FeatureRecord  # noqa: E402
from morning_edge.models import timestamp_from_text  # noqa: E402
from morning_edge.provider_contracts import contract_catalog  # noqa: E402
from morning_edge.signal_registry import registry  # noqa: E402
from private_artifacts import write_private_bytes  # noqa: E402


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: object) -> float | None:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("run input must be a JSON object")
    return value


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    content = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    write_private_bytes(path, content)


def _feature_values(entry: Mapping[str, Any]) -> dict[str, Any]:
    price = _mapping(entry.get("price"))
    technical = _mapping(entry.get("technical"))
    edge = _mapping(entry.get("edge"))
    dimensions = _mapping(edge.get("dimensions"))
    surface = _mapping(edge.get("option_surface"))
    flow = _mapping(edge.get("flow_conviction"))
    gex = _mapping(edge.get("gex_topology"))
    thesis = _mapping(entry.get("trade_thesis"))
    whale = _mapping(entry.get("whale_evidence"))
    greek = _mapping(whale.get("greek_flow"))
    values = {
        "price.close": _number(price.get("value")),
        "price.return_1d": _number(entry.get("return_1d_pct")),
        "trend.return_5d": _number(technical.get("return_5d")),
        "trend.return_20d": _number(technical.get("return_20d")),
        "trend.return_63d": _number(technical.get("return_63d")),
        "trend.realized_vol_20": _number(technical.get("realized_vol_20")),
        "trend.ema20": _number(technical.get("ema_20")),
        "trend.ema50": _number(technical.get("ema_50")),
        "trend.ema200": _number(technical.get("ema_200")),
        "surface.front_iv": _number(surface.get("front_iv")),
        "surface.iv_rv_gap": _number(surface.get("iv_rv_gap")),
        "surface.term_slope": _number(surface.get("term_slope")),
        "flow.directional_premium": _number(flow.get("directional_premium")),
        "flow.oi_confirmed": flow.get("oi_confirmed"),
        "gex.gamma_flip": _number(gex.get("gamma_flip")),
        "gex.flip_distance": _number(gex.get("flip_distance")),
        "greek.dir_delta_flow": _number(greek.get("directional_delta_flow")),
        "greek.dir_vega_flow": _number(greek.get("directional_vega_flow")),
        "model.directional_edge": _number(dimensions.get("directional_edge")),
        "model.volatility_edge": _number(dimensions.get("volatility_edge")),
        "thesis.direction": thesis.get("direction"),
        "thesis.evidence_score": _number(thesis.get("conviction_score")),
    }
    return values


def _source_ids(value: Any) -> set[int]:
    result: set[int] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {"source_snapshot_ids", "snapshot_ids"} and isinstance(item, (list, tuple)):
                result.update(x for x in item if isinstance(x, int) and not isinstance(x, bool) and x > 0)
            elif key in {"snapshot_id", "source_snapshot_id"} and isinstance(item, int) and not isinstance(item, bool) and item > 0:
                result.add(item)
            else:
                result.update(_source_ids(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            result.update(_source_ids(item))
    return result


def _source_availability(database: Path, sources: Sequence[int], cutoff: Any) -> Any:
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    rows = []
    try:
        for start in range(0, len(sources), 500):
            chunk = sources[start:start + 500]
            rows.extend(connection.execute(
                f"SELECT id, as_of, retrieved_at FROM snapshots WHERE id IN ({','.join('?' for _ in chunk)})",
                chunk,
            ).fetchall())
    finally:
        connection.close()
    if {row[0] for row in rows} != set(sources):
        raise ValueError("feature record references missing source snapshots")
    timestamps = [(timestamp_from_text(row[1]), timestamp_from_text(row[2])) for row in rows]
    if any(as_of > cutoff or retrieved > cutoff for as_of, retrieved in timestamps):
        raise ValueError("feature source was not available at the declared cutoff")
    return max(retrieved for _, retrieved in timestamps)


def _agent_accountability(run: Mapping[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for raw in run.get("watchlist", ()):
        entry = _mapping(raw)
        enriched = entry.get("agent_enrichment_validated") is True
        analysis = _mapping(entry.get("agent_enrichment" if enriched else "agent_analysis"))
        claims = [row for row in analysis.get("evidence_points" if enriched else "claims", ()) if isinstance(row, Mapping)]
        referenced = sum(bool(row.get("field_refs") and row.get("source_snapshot_ids")) if enriched else bool(row.get("source_refs")) for row in claims)
        summary = str(analysis.get("summary" if enriched else "evidence_summary") or "").strip()
        ticker = str(entry.get("ticker") or "").strip().upper()
        if ticker:
            rows.append({
                "ticker": ticker,
                "status": "VALIDATED_ENRICHMENT_CONTRACT" if enriched else str(analysis.get("status") or "UNAVAILABLE"),
                "measured_object": "agent_enrichment" if enriched else "agent_analysis",
                "claim_count": len(claims),
                "claims_with_source_refs": referenced,
                "claim_reference_coverage": referenced / len(claims) if claims else None,
                "summary_word_count": len(summary.split()),
                "suggested_action": str(analysis.get("action" if enriched else "suggested_action") or "NO_RECOMMENDATION"),
            })
    return {
        "status": "MEASURED_OUTPUT_CONTRACT",
        "ticker_count": len(rows),
        "all_actions_fail_closed": all(row["suggested_action"] == "NO_RECOMMENDATION" for row in rows),
        "rows": rows,
        "boundary": "This measures field completeness and provenance links. It does not measure analytical correctness.",
    }


def build(run: Mapping[str, Any], *, feature_database: Path, evaluation_database: Path) -> dict[str, Any]:
    cutoff = timestamp_from_text(str(run.get("cutoff_at")))
    run_id = str(run.get("run_id") or "").strip()
    if not run_id:
        raise ValueError("run_id is required")
    records: list[int] = []
    model_versions: set[str] = set()
    with FeatureMart(feature_database) as mart:
        for raw in run.get("watchlist", ()):
            entry = _mapping(raw)
            ticker = str(entry.get("ticker") or "").strip().upper()
            price = _mapping(entry.get("price"))
            sources = sorted(_source_ids(entry))
            if not ticker or not sources:
                continue
            session = date.fromisoformat(str(price.get("as_of"))[:10])
            edge = _mapping(entry.get("edge"))
            version = str(edge.get("feature_version") or "edge-research-unknown")
            for key in ("forecast", "forecast_v4"):
                model = _mapping(edge.get(key))
                if model.get("model_version"):
                    model_versions.add(str(model["model_version"]))
            values = _feature_values(entry)
            record = FeatureRecord(
                ticker=ticker, effective_session=session, cutoff_at=cutoff,
                available_at=_source_availability(evaluation_database, sources, cutoff), feature_version=version,
                values={key: value for key, value in values.items() if value is not None}, source_snapshot_ids=sources,
                missing_reasons={key: "not_available_in_source_artifact" for key, value in values.items() if value is None},
                quality_status="SOURCE_IDS_CUTOFF_VERIFIED",
            )
            records.append(mart.insert(record))
        replay_id = mart.register_replay(
            run_id=run_id, cutoff_at=cutoff, input_payload=run,
            feature_record_ids=records, model_versions=sorted(model_versions),
        )
        replay = mart.manifest(replay_id)
    return {
        "schema_version": "codex-screener-research-control-v1",
        "run_id": run_id,
        "cutoff_at": str(run.get("cutoff_at")),
        "feature_database": str(feature_database),
        "feature_record_count": len(records),
        "replay": replay,
        "provider_contracts": contract_catalog(),
        "signal_registry": registry(),
        "evaluation": build_report(evaluation_database),
        "agent_accountability": _agent_accountability(run),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--feature-database", type=Path, default=private_runtime_path("data/research-control.sqlite"))
    parser.add_argument("--evaluation-database", type=Path, default=private_runtime_path("data/morning-edge.sqlite"))
    parser.add_argument("--output", type=Path, default=private_runtime_path("outputs/research-control/latest.json"))
    args = parser.parse_args(argv)
    result = build(_read(args.input), feature_database=args.feature_database, evaluation_database=args.evaluation_database)
    _atomic_json(args.output, result)
    print(json.dumps({
        "status": "BUILT", "output": str(args.output),
        "feature_records": result["feature_record_count"],
        "replay_id": _mapping(result.get("replay")).get("replay_id"),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

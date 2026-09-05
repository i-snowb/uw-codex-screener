#!/usr/bin/env python3
"""Rebuild a derived morning artifact from its stored capture report.

This command performs no provider requests. It is used after a derived-feature
implementation changes while the immutable source snapshots remain valid.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any, Mapping

from morning_edge.current_collection import (
    CurrentCaptureItem,
    CurrentCaptureReport,
    CurrentCaptureStatus,
    CurrentDataset,
)
from morning_edge.daily import build_morning_run, write_morning_run
from morning_edge.models import timestamp_from_text


def _report(value: object) -> CurrentCaptureReport:
    if not isinstance(value, Mapping):
        raise ValueError("capture_report must be an object")
    results = value.get("results")
    if not isinstance(results, list):
        raise ValueError("capture_report.results must be a list")
    items: list[CurrentCaptureItem] = []
    for raw in results:
        if not isinstance(raw, Mapping):
            raise ValueError("capture_report result must be an object")
        items.append(CurrentCaptureItem(
            ticker=str(raw["ticker"]),
            dataset=CurrentDataset(str(raw["dataset"])),
            status=CurrentCaptureStatus(str(raw["status"])),
            endpoint=str(raw["endpoint"]),
            snapshot_id=int(raw["snapshot_id"]) if raw.get("snapshot_id") is not None else None,
            fetched_at=str(raw["fetched_at"]) if raw.get("fetched_at") is not None else None,
            row_count=int(raw["row_count"]) if raw.get("row_count") is not None else None,
            reason=str(raw["reason"]) if raw.get("reason") is not None else None,
        ))
    return CurrentCaptureReport(
        generated_at=str(value["generated_at"]),
        tickers=tuple(str(item) for item in value["tickers"]),
        datasets=tuple(CurrentDataset(str(item)) for item in value["datasets"]),
        preflight_passed=value.get("preflight_passed") is True,
        max_transport_attempts=int(value["max_transport_attempts"]),
        remaining_transport_attempt_capacity_before_run=int(value["remaining_transport_attempt_capacity_before_run"]),
        results=tuple(items),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve() or args.output.exists():
        raise SystemExit("reprocessed output must be a new path; original publications are immutable")

    source: Any = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(source, Mapping):
        raise SystemExit("input artifact must be an object")
    report = _report(source.get("capture_report"))
    if not report.preflight_passed or any(item.status not in {CurrentCaptureStatus.CAPTURED, CurrentCaptureStatus.EMPTY} for item in report.results):
        raise SystemExit("cannot rebuild an incomplete capture as a successful research run")
    artifact = build_morning_run(database=args.database, capture_report=report,
        cutoff_at=timestamp_from_text(str(source["cutoff_at"])))
    artifact["run_id"] += "-reprocessed-" + artifact["edge_feature_version"]
    artifact["mode"] = "RETROSPECTIVE_REPROCESSING"
    artifact["reprocessing"] = {
        "source_run_id": source.get("run_id"),
        "reprocessed_at": datetime.now(UTC).isoformat(),
        "prospective_eligible": False,
        "boundary": "Recomputed from stored cutoff-safe evidence; not a new prospective prediction or live capture.",
    }
    destination = write_morning_run(args.output, artifact)
    print(json.dumps({
        "output": str(destination),
        "run_id": artifact["run_id"],
        "cutoff_at": artifact["cutoff_at"],
        "watchlist_count": len(artifact["watchlist"]),
        "network_called": False,
    }, sort_keys=True))


if __name__ == "__main__":
    main()

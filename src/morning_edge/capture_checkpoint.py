"""Verified same-session collection checkpoints; no provider or model calls."""

from datetime import datetime, UTC
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from .current_collection import (CurrentCaptureReport, CurrentCaptureItem,
    CurrentCaptureStatus, CurrentDataset, SPECS as BASE_SPECS)
from .enhanced_collection import (EnhancedCaptureReport, EnhancedCaptureItem,
    EnhancedCaptureStatus, EnhancedDataset, SPECS as EXTRA_SPECS, _items)
from .models import timestamp_from_text
from .unattended import immutable_json, read_object, source_code_digest

ET = ZoneInfo("America/New_York")
SCHEMA = "collection-checkpoint/v1"
CONTEXT_FILES = ("morning-run-benchmarks.json", "morning-run-company-context.json")


def context_files(path: Path) -> tuple[str, str]:
    if not path.name.endswith('-collection.json'):
        raise ValueError('unexpected collection checkpoint filename')
    stem = path.name[:-len('-collection.json')]
    return (stem + '-benchmarks.json', stem + '-company-context.json')


def _manifest(database: Path, base: CurrentCaptureReport, extra: EnhancedCaptureReport) -> list[dict]:
    expected = [(item, BASE_SPECS[item.dataset], item.ticker, "current") for item in base.results]
    expected += [(item, EXTRA_SPECS[item.dataset], item.symbol, "enhanced_current") for item in extra.results]
    values = []
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        for item, spec, symbol, mode in expected:
            row = connection.execute("""SELECT s.*, p.content_json FROM snapshots s
                JOIN raw_payloads p ON p.content_hash=s.raw_payload_hash WHERE s.id=?""", (item.snapshot_id,)).fetchone()
            if row is None:
                raise ValueError("checkpoint snapshot missing")
            meta = json.loads(row["metadata_json"])
            if (row["provider"] != "unusual_whales" or row["dataset"] != spec.snapshot_dataset.value
                or row["symbol"] != symbol or meta.get("capture_mode") != mode
                or meta.get("response_status") != item.status.value
                or meta.get("response_row_count") != item.row_count
                or meta.get("provider_endpoint") != item.endpoint
                or timestamp_from_text(row["retrieved_at"]) != timestamp_from_text(item.fetched_at)
                or timestamp_from_text(row["as_of"]) > timestamp_from_text(row["retrieved_at"])):
                raise ValueError("checkpoint snapshot provenance mismatch")
            actual_hash = hashlib.sha256(row["content_json"].encode()).hexdigest()
            if actual_hash != row["raw_payload_hash"]:
                raise ValueError("checkpoint raw payload hash mismatch")
            values.append({"id": row["id"], "identity": row["idempotency_key"],
                "payload_sha256": actual_hash,
                "metadata_sha256": hashlib.sha256(row["metadata_json"].encode()).hexdigest()})
    return values


def _complete(base: CurrentCaptureReport, extra: EnhancedCaptureReport) -> None:
    pairs = {(t, d) for t in base.tickers for d in base.datasets}
    if (not base.tickers or not base.datasets or not extra.datasets
        or not base.preflight_passed or not extra.preflight_passed or base.tickers != extra.tickers
        or len(base.results) != len(pairs) or {(i.ticker, i.dataset) for i in base.results} != pairs
        or len(extra.results) != len(_items(extra.tickers, extra.datasets))
        or {(i.dataset, i.symbol) for i in extra.results} != set(_items(extra.tickers, extra.datasets))
        or any(i.status.value not in {"captured", "empty"} or not i.snapshot_id or not i.fetched_at
               for i in (*base.results, *extra.results))):
        raise ValueError("checkpoint requires complete validated collection reports")


def save_checkpoint(path: Path, *, root: Path, database: Path, base: CurrentCaptureReport,
                    extra: EnhancedCaptureReport, context: dict | None = None) -> None:
    _complete(base, extra)
    times = [timestamp_from_text(i.fetched_at) for i in (*base.results, *extra.results)]
    cutoff = max(times + [timestamp_from_text(base.generated_at), timestamp_from_text(extra.generated_at)])
    session = cutoff.astimezone(ET).date()
    if any(t.astimezone(ET).date() != session for t in times):
        raise ValueError("collection checkpoint crosses sessions")
    value = {"schema": SCHEMA, "database": str(database.resolve()), "source_sha256": source_code_digest(root),
        "session_date": cutoff.astimezone(ET).date().isoformat(), "cutoff_at": cutoff.isoformat(),
        "base": base.to_dict(), "enhanced": extra.to_dict(), "context": context or {},
        "snapshots": _manifest(database, base, extra)}
    immutable_json(path, value)


def load_checkpoint(path: Path, *, root: Path, database: Path, tickers: tuple,
                    datasets: tuple, now: datetime | None = None) -> tuple:
    if path.is_symlink():
        raise ValueError("linked collection checkpoint is not allowed")
    value = read_object(path)
    clock = now or datetime.now(UTC)
    if (value.get("schema") != SCHEMA or value.get("database") != str(database.resolve())
        or value.get("source_sha256") != source_code_digest(root)
        or value.get("session_date") != clock.astimezone(ET).date().isoformat()
        or timestamp_from_text(value["cutoff_at"]) > clock):
        raise ValueError("collection checkpoint is stale or belongs to another run")
    b, e = value["base"], value["enhanced"]
    base = CurrentCaptureReport(b["generated_at"], tuple(b["tickers"]), tuple(map(CurrentDataset, b["datasets"])),
        b["preflight_passed"], b["max_transport_attempts"], b["remaining_transport_attempt_capacity_before_run"],
        tuple(CurrentCaptureItem(**(i | {"dataset": CurrentDataset(i["dataset"]),
            "status": CurrentCaptureStatus(i["status"])})) for i in b["results"]))
    extra = EnhancedCaptureReport(e["generated_at"], tuple(e["tickers"]), tuple(map(EnhancedDataset, e["datasets"])),
        e["preflight_passed"], e["logical_items"], e["max_transport_attempts"], e["remaining_transport_attempt_capacity_before_run"],
        tuple(EnhancedCaptureItem(**(i | {"dataset": EnhancedDataset(i["dataset"]),
            "status": EnhancedCaptureStatus(i["status"])})) for i in e["results"]))
    _complete(base, extra)
    cutoff = max(timestamp_from_text(t) for t in [base.generated_at, extra.generated_at,
        *(i.fetched_at for i in (*base.results, *extra.results))])
    if (base.tickers != tickers or base.datasets != datasets
        or cutoff != timestamp_from_text(value["cutoff_at"])
        or value["snapshots"] != _manifest(database, base, extra)):
        raise ValueError("collection checkpoint evidence or universe changed")
    if any(timestamp_from_text(i.fetched_at).astimezone(ET).date() != clock.astimezone(ET).date()
           for i in (*base.results, *extra.results)):
        raise ValueError("collection checkpoint crosses sessions")
    context = value["context"]
    if context and set(context) != set(context_files(path)):
        raise ValueError("unexpected collection context files")
    for name, expected in context.items():
        candidate = path.parent / name
        if candidate.is_symlink() or hashlib.sha256(candidate.read_bytes()).hexdigest() != expected:
            raise ValueError("collection context hash mismatch")
    return base, extra, context

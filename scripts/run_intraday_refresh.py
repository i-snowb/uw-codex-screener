#!/usr/bin/env python3
"""Run selective, fail-closed intraday refreshes for the local dashboard.

The command never changes the published daily forecast or evaluation origin.
Without ``--live --audit-accepted`` it performs a network-free planning run.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, time, timedelta
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time as clock_time
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from morning_edge.config import Settings, private_runtime_path
from morning_edge.current_collection import (
    DEFAULT_CURRENT_DATASETS,
    CurrentCaptureStatus,
    collect_current,
    reconstruct_current_report,
)
from morning_edge.daily import build_morning_run, write_morning_run
from morning_edge.enhanced_collection import EnhancedCaptureStatus, collect_enhanced
from morning_edge.enhanced_features import build_enhanced_summary
from morning_edge.intraday import (
    POLICY,
    IntradayTier,
    due_tiers,
    logical_request_count,
    market_session,
    merge_frozen_daily_model,
)
from morning_edge.intraday_events import IntradayEventLedger, IntradayEventRecord, shadow_intraday_event_model
from morning_edge.providers.budget import WeeklyRequestBudget
from morning_edge.providers.unusual_whales import UnusualWhalesClient
from morning_edge.store import SnapshotStore

import build_dashboard_bundle as bundle


ET = ZoneInfo("America/New_York")
STATE_SCHEMA = "codex_screener/intraday_state/v1"
LEDGER_SCHEMA = "codex_screener/intraday_cycle/v1"


def _read_object(path: Path, label: str) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    return dict(value)


def _load_env(path: Path) -> None:
    """Load an owner-private dotenv file without executing its contents."""

    if not path.exists():
        return
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise PermissionError(f"refusing to load non-private environment file: {path}")
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"invalid environment line {line_number}")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "A").isalnum() or not key[0].isalpha():
            raise ValueError(f"invalid environment key on line {line_number}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    content = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": STATE_SCHEMA, "completed": {}}
    value = _read_object(path, "intraday state")
    if value.get("schema_version") != STATE_SCHEMA or not isinstance(value.get("completed"), Mapping):
        raise ValueError("intraday state has an unsupported schema")
    return value


def _append_cycle(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        with os.fdopen(descriptor, "ab") as handle:
            handle.write(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if path.exists():
            os.chmod(path, 0o600)


def _snapshot_ids(*reports: object) -> list[int]:
    result: list[int] = []
    for report in reports:
        for item in getattr(report, "results", ()):
            snapshot_id = getattr(item, "snapshot_id", None)
            if isinstance(snapshot_id, int) and not isinstance(snapshot_id, bool):
                result.append(snapshot_id)
    return sorted(set(result))


def _failures(current_report: object, enhanced_report: object) -> list[str]:
    failures: list[str] = []
    for item in getattr(current_report, "results", ()):
        if item.status not in {CurrentCaptureStatus.CAPTURED, CurrentCaptureStatus.EMPTY, CurrentCaptureStatus.PARTIAL}:
            failures.append(f"{item.ticker}:{item.dataset.value}:{item.status.value}")
    for item in getattr(enhanced_report, "results", ()):
        if item.status not in {EnhancedCaptureStatus.CAPTURED, EnhancedCaptureStatus.EMPTY}:
            failures.append(f"{item.symbol}:{item.dataset.value}:{item.status.value}")
    return failures


def _tier_datasets(tiers: Sequence[IntradayTier]) -> tuple[tuple[object, ...], tuple[object, ...]]:
    current: list[object] = []
    enhanced: list[object] = []
    for tier in tiers:
        current.extend(POLICY[tier].current)
        enhanced.extend(POLICY[tier].enhanced)
    return tuple(dict.fromkeys(current)), tuple(dict.fromkeys(enhanced))


def _now(value: str | None) -> datetime:
    if value is None:
        return datetime.now(ET)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("--now must include a timezone")
    return parsed.astimezone(ET)


def _baseline_path(explicit: Path | None, now: datetime) -> Path:
    if explicit is not None:
        return explicit
    run_directory = private_runtime_path("outputs/runs") / now.date().isoformat()
    candidates = (
        run_directory / "morning-run-enriched-corrected.json",
        run_directory / "morning-run-enriched.json",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise RuntimeError(
        f"no daily enriched baseline exists for {now.date().isoformat()}; "
        "complete the morning publication or pass --baseline-input"
    )


def plan(now: datetime, completed: Mapping[str, str], ticker_count: int) -> dict[str, Any]:
    session = market_session(now)
    tiers = due_tiers(now, completed) if session.is_open_at(now) else ()
    current, enhanced = _tier_datasets(tiers)
    return {
        "network_called": False,
        "observed_at": now.isoformat(),
        "session_date": session.session_date.isoformat(),
        "market_status": session.status,
        "market_reason": session.reason,
        "market_open_now": session.is_open_at(now),
        "due_tiers": [item.value for item in tiers],
        "current_datasets": [str(item.value) for item in current],
        "enhanced_datasets": [str(item.value) for item in enhanced],
        "logical_requests": logical_request_count(tuple(tiers), ticker_count) if tiers else 0,
        "maximum_transport_attempts": logical_request_count(tuple(tiers), ticker_count) * 3 if tiers else 0,
    }


def run_cycle(args: argparse.Namespace, now: datetime) -> dict[str, Any]:
    state = _state(args.state)
    public_plan = plan(now, state["completed"], len(args.tickers))
    if not args.live:
        return public_plan
    session = market_session(now)
    if not session.is_open_at(now):
        raise RuntimeError(f"market is not open: {session.status} ({session.reason})")
    tiers = due_tiers(now, state["completed"])
    if not tiers:
        return public_plan | {"network_called": False, "reason": "no tier is due"}
    current_datasets, enhanced_datasets = _tier_datasets(tiers)
    baseline = _read_object(_baseline_path(args.baseline_input, now), "baseline input")
    settings = Settings.from_env()
    if settings.provider_api_key is None:
        raise RuntimeError("UNUSUAL_WHALES_API_KEY is not configured")

    with WeeklyRequestBudget(
        args.usage_database,
        weekly_cap=args.daily_cap,
        protected_reserve=args.reserve,
        rolling_window=timedelta(days=1),
    ) as budget:
        before = budget.usage(now=now.astimezone(UTC))
        maximum = logical_request_count(tuple(tiers), len(args.tickers)) * args.max_attempts
        if maximum > before.remaining_before_reserve:
            raise RuntimeError(
                f"budget preflight blocked: {maximum} maximum attempts exceed "
                f"{before.remaining_before_reserve} remaining before reserve"
            )
        client = UnusualWhalesClient(
            settings.provider_api_key,
            max_attempts=args.max_attempts,
            request_budget=budget,
        )
        with SnapshotStore(args.database) as snapshots:
            current_report = collect_current(
                client=client,
                snapshots=snapshots,
                request_budget=budget,
                tickers=args.tickers,
                datasets=current_datasets,
                max_transport_attempts_per_item=args.max_attempts,
                generated_at=now,
            )
            enhanced_report = collect_enhanced(
                client=client,
                snapshots=snapshots,
                request_budget=budget,
                tickers=args.tickers,
                datasets=enhanced_datasets,
                max_transport_attempts_per_item=args.max_attempts,
                generated_at=now,
            )
        after = budget.usage(now=now.astimezone(UTC))

    failures = _failures(current_report, enhanced_report)
    if failures:
        raise RuntimeError("capture failed closed: " + ", ".join(failures[:12]))

    day_start = datetime.combine(now.date(), time(4), ET)
    full_report = reconstruct_current_report(
        args.database,
        day_start,
        now,
        args.tickers,
        datasets=DEFAULT_CURRENT_DATASETS,
        remaining_before_run=before.remaining_before_reserve,
    )
    live_run = build_morning_run(
        database=args.database,
        capture_report=full_report,
        tickers=args.tickers,
        cutoff_at=now,
    )
    enhanced = build_enhanced_summary(args.database, cutoff_at=now)
    merge_frozen_daily_model(live_run, baseline, enhanced, observed_at=now)
    snapshot_ids = _snapshot_ids(current_report, enhanced_report)
    event_type = "+".join(sorted(item.value.upper() for item in tiers))
    for row in live_run.get("watchlist", []):
        if not isinstance(row, dict):
            continue
        condition = row.get("intraday_condition")
        condition = condition if isinstance(condition, Mapping) else {}
        event = IntradayEventRecord(
            ticker=str(row.get("ticker") or ""),
            observed_at=now,
            daily_origin_session=str(row.get("price", {}).get("as_of") or now.date().isoformat())[:10],
            event_type=event_type,
            features={
                "status": condition.get("status"),
                "observed_price": condition.get("observed_price"),
                "change_from_daily_anchor": condition.get("change_from_daily_anchor"),
                "directional_votes": condition.get("directional_votes"),
                "vote_count": condition.get("vote_count"),
            },
            source_snapshot_ids=tuple(snapshot_ids),
        )
        with IntradayEventLedger(args.event_ledger) as event_ledger:
            comparables = event_ledger.comparable(ticker=event.ticker, event_type=event.event_type)
            event_ledger.insert(event)
        row["intraday_event_model"] = shadow_intraday_event_model(
            event=event, comparable_events=comparables,
        )
    cycle_key = {
        "cutoff": now.isoformat(),
        "tiers": [item.value for item in tiers],
        "snapshots": snapshot_ids,
    }
    digest = hashlib.sha256(json.dumps(cycle_key, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    live_run["run_id"] = f"intraday-{now.strftime('%Y%m%dT%H%M%S%z')}-{digest}"
    live_run["mode"] = "INTRADAY_READ_ONLY"
    live_run["enhanced_summary"] = enhanced
    if args.evaluation_input:
        live_run["model_evaluation"] = _read_object(args.evaluation_input, "evaluation input")
    if args.previous_input:
        live_run["previous_run"] = _read_object(args.previous_input, "previous input")
    live_run["intraday_status"] = {
        "mode": "INTRADAY",
        "market_status": session.status,
        "last_cycle_at": now.isoformat(),
        "completed_tiers": [item.value for item in tiers],
        "logical_requests": logical_request_count(tuple(tiers), len(args.tickers)),
        "transport_attempts": after.attempted_requests - before.attempted_requests,
        "remaining_before_reserve": after.remaining_before_reserve,
        "browser_poll_seconds": args.browser_poll_seconds,
        "analysis_mode": "FROZEN_DAILY_FORECAST + DETERMINISTIC_INTRADAY_CONDITION",
        "limitations": [
            "Daily V3/V4 paths and their evaluation origin are frozen.",
            "Intraday condition is deterministic context, not a new forecast or probability.",
            "No order is created, routed, or authorized.",
        ],
    }
    output = args.output_directory / now.date().isoformat() / "latest-run.json"
    write_morning_run(output, live_run)
    publication = bundle.publish_latest_data(run=live_run, app_root=args.app_root)

    completed = dict(state["completed"])
    completed.update({tier.value: now.isoformat() for tier in tiers})
    next_state = {
        "schema_version": STATE_SCHEMA,
        "session_date": now.date().isoformat(),
        "completed": completed,
        "last_run_id": live_run["run_id"],
        "last_published_at": now.isoformat(),
    }
    _atomic_json(args.state, next_state)
    ledger_row = {
        "schema_version": LEDGER_SCHEMA,
        "run_id": live_run["run_id"],
        "cutoff_at": now.isoformat(),
        "tiers": [item.value for item in tiers],
        "snapshot_ids": snapshot_ids,
        "logical_requests": logical_request_count(tuple(tiers), len(args.tickers)),
        "transport_attempts": after.attempted_requests - before.attempted_requests,
        "remaining_before_reserve": after.remaining_before_reserve,
        "publication_sha256": publication["sha256"],
    }
    _append_cycle(args.cycle_ledger, ledger_row)
    return public_plan | {
        "network_called": True,
        "run_id": live_run["run_id"],
        "output": str(output),
        "published": publication,
        "snapshot_count": len(snapshot_ids),
        "transport_attempts": ledger_row["transport_attempts"],
        "remaining_before_reserve": after.remaining_before_reserve,
    }


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--audit-accepted", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--now", help="timezone-aware dry-run timestamp")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--baseline-input", type=Path)
    parser.add_argument("--evaluation-input", type=Path)
    parser.add_argument("--previous-input", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--usage-database", type=Path)
    parser.add_argument("--app-root", type=Path, default=Path("dashboard-app"))
    parser.add_argument("--output-directory", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--cycle-ledger", type=Path)
    parser.add_argument("--event-ledger", type=Path)
    parser.add_argument("--daily-cap", type=int)
    parser.add_argument("--reserve", type=int)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--browser-poll-seconds", type=int, default=30)
    parser.add_argument("--tickers", nargs="+", default=None)
    args = parser.parse_args(argv)
    _load_env(args.env_file)
    settings = Settings.from_env()
    args.tickers = tuple(args.tickers or settings.watchlist)
    args.database = args.database or settings.database_path
    args.usage_database = args.usage_database or settings.provider_usage_path
    args.evaluation_input = args.evaluation_input or private_runtime_path(
        "outputs/model-evaluation-summary-corrected.json"
    )
    args.output_directory = args.output_directory or private_runtime_path("outputs/intraday")
    args.state = args.state or private_runtime_path("outputs/intraday/state.json")
    args.cycle_ledger = args.cycle_ledger or private_runtime_path("outputs/intraday/cycles.jsonl")
    args.event_ledger = args.event_ledger or private_runtime_path("data/intraday-events.sqlite")
    args.daily_cap = args.daily_cap if args.daily_cap is not None else int(os.environ.get("CODEX_SCREENER_INTRADAY_DAILY_CAP", "30000"))
    args.reserve = args.reserve if args.reserve is not None else int(os.environ.get("CODEX_SCREENER_INTRADAY_RESERVE", "20000"))
    if args.live and not args.audit_accepted:
        parser.error("--live requires --audit-accepted")
    if args.loop and not args.live:
        parser.error("--loop requires --live")
    if args.live and args.now:
        parser.error("--now is dry-run only")
    if args.daily_cap < 1 or args.reserve < 0 or args.reserve >= args.daily_cap:
        parser.error("daily cap must be positive and reserve must be smaller")
    if args.max_attempts < 1 or not 15 <= args.browser_poll_seconds <= 120:
        parser.error("invalid retry or browser-poll setting")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = _arguments(argv)
    while True:
        now = _now(args.now)
        result = run_cycle(args, now)
        print(json.dumps(result, sort_keys=True))
        if not args.loop:
            return 0
        session = market_session(now)
        if session.status == "CLOSED" or now >= session.closes_at:
            return 0
        clock_time.sleep(15)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"status": "FAILED_CLOSED", "error": str(error)[:400]}), file=sys.stderr)
        raise SystemExit(2)

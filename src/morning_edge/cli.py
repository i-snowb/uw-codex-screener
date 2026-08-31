"""Command-line operational shell for Codex Screener.

Commands are local and deterministic by default. Network-capable audit,
collection, morning-run, and backfill commands require an explicit ``--live``
flag and the applicable audit acknowledgement. They do not place trades or
enable recommendations by themselves.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Any, Callable, Sequence
from zoneinfo import ZoneInfo

from .audit import AuditStatus, run_trial_audit
from .backfill import BACKFILL_DATASETS, collect, make_plan, plan_preview
from .clock import next_weekday
from .config import ConfigError, Settings
from .current_collection import CurrentDataset, collect_current, reconstruct_current_report
from .daily import build_morning_run, write_morning_run
from .enhanced_collection import EnhancedDataset, GLOBAL_DATASETS, collect_enhanced
from .enhanced_features import build_enhanced_summary
from .models import SnapshotEnvelope
from .providers.base import JsonlRawResponseCapture
from .providers.budget import (
    DEFAULT_WEEKLY_CAP,
    DEFAULT_WEEKLY_RESERVE,
    WeeklyRequestBudget,
)
from .providers.unusual_whales import UnusualWhalesClient
from .store import SnapshotStore


MAX_TRANSPORT_ATTEMPTS_PER_LOGICAL_ITEM = 3


def _json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def initialize_database(path: Path) -> dict[str, object]:
    """Create the immutable local snapshot schema. This has no network effect."""
    with SnapshotStore(path):
        pass
    return {"status": "initialized", "database_path": str(path), "network_called": False}


def provider_usage(settings: Settings) -> dict[str, object]:
    """Read the local request counter without contacting a provider."""
    with WeeklyRequestBudget(settings.provider_usage_path) as request_budget:
        return {
            "status": "local_usage",
            "network_called": False,
            "recommendations_enabled": False,
            **request_budget.usage().public_dict(),
        }


def _utc_timestamp(value: str, *, option: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{option} must use timezone-aware ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{option} must use timezone-aware ISO-8601")
    return parsed.astimezone(ZoneInfo("UTC"))


def add_provider_baseline_adjustment(
    settings: Settings,
    *,
    adjustment_id: str,
    attempted_requests: int,
    evidence_id: str,
    effective_at: str,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    """Add one immutable, evidence-labelled local accounting adjustment."""
    effective = _utc_timestamp(effective_at, option="--effective-at")
    budget_options = {} if clock is None else {"clock": clock}
    with WeeklyRequestBudget(settings.provider_usage_path, **budget_options) as request_budget:
        usage = request_budget.add_baseline_adjustment(
            adjustment_id=adjustment_id,
            attempted_requests=attempted_requests,
            evidence_id=evidence_id,
            effective_at=effective,
        )
    return {
        "status": "baseline_adjustment_recorded",
        "network_called": False,
        "recommendations_enabled": False,
        "adjustment_id": adjustment_id,
        "evidence_id": evidence_id,
        "effective_at_utc": effective.isoformat(),
        **usage.public_dict(),
    }


def _read_fixture(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"fixture does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"fixture is not valid JSON: {path}: {exc.msg}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("snapshots"), list):
        raise ValueError("fixture must be an object containing a snapshots array")
    return payload


def load_fixture(path: Path, database_path: Path) -> dict[str, object]:
    payload = _read_fixture(path)
    snapshots = payload["snapshots"]
    with SnapshotStore(database_path) as store:
        before_count = store.count()
        envelopes = []
        for snapshot in snapshots:
            if not isinstance(snapshot, dict):
                raise ValueError("each fixture snapshot must be an object")
            try:
                envelope = SnapshotEnvelope(
                    provider=str(snapshot["provider"]),
                    dataset=str(snapshot["dataset"]),
                    symbol=snapshot.get("symbol"),
                    as_of=datetime.fromisoformat(str(snapshot["as_of"]).replace("Z", "+00:00")),
                    retrieved_at=datetime.fromisoformat(
                        str(snapshot["retrieved_at"]).replace("Z", "+00:00")
                    ),
                    payload=snapshot["payload"],
                    metadata=snapshot.get("metadata", {}),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    "fixture snapshots require provider, dataset, as_of, retrieved_at, and payload"
                ) from exc
            envelopes.append(envelope)
        store.insert_many(envelopes)
        inserted = store.count() - before_count
    return {
        "status": "loaded",
        "fixture": str(path),
        "inserted_snapshots": inserted,
        "database_path": str(database_path),
        "network_called": False,
    }


def snapshot_datetime(
    settings: Settings,
    as_of: str | None = None,
    *,
    now: datetime | None = None,
) -> datetime:
    timezone = ZoneInfo(settings.timezone)
    if as_of:
        try:
            parsed = datetime.fromisoformat(as_of)
        except ValueError as exc:
            raise ValueError("--as-of must be ISO-8601, for example 2026-08-23T06:55:00-04:00") from exc
        return parsed.astimezone(timezone) if parsed.tzinfo else parsed.replace(tzinfo=timezone)
    if now is not None and (now.tzinfo is None or now.utcoffset() is None):
        raise ValueError("now must be timezone-aware")
    current = datetime.now(timezone) if now is None else now.astimezone(timezone)
    hour, minute = (int(part) for part in settings.snapshot_time.split(":"))
    session_day = next_weekday(current.date())
    return datetime.combine(session_day, time(hour, minute), tzinfo=timezone)


def _audit_date(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError("--as-of must use YYYY-MM-DD") from exc


def audit_provider(
    settings: Settings,
    fixture: Path | None,
    *,
    live: bool = False,
    tickers: Sequence[str] = (),
    as_of: str | None = None,
    raw_capture: Path | None = None,
) -> dict[str, object]:
    """Run the zero-network checklist or an explicit read-only endpoint audit."""

    checks = [
        "authenticate using a local environment variable",
        "record endpoint field names, pagination, and timestamps",
        "measure historical coverage and rate-limit headers",
        "compare a sampled option quote with the execution venue",
        "confirm open-interest timing, trade-condition flags, and GEX methodology",
    ]
    if not live:
        if tickers or as_of or raw_capture is not None:
            raise ValueError("--ticker, --as-of, and --raw-capture require --live")
        response: dict[str, object] = {
            "status": "dry_run",
            "provider": settings.provider_name,
            "provider_configured": settings.provider_api_key is not None,
            "network_called": False,
            "recommendations_enabled": False,
            "checks": checks,
            "next_action": "Use --live only after API entitlement is active and a local key is set.",
        }
        if fixture is not None:
            payload = _read_fixture(fixture)
            response["fixture_snapshot_count"] = len(payload["snapshots"])
        return response

    if fixture is not None:
        raise ValueError("--fixture cannot be combined with --live")
    if settings.provider_name != "unusual_whales":
        raise ConfigError("live audit currently supports CODEX_SCREENER_PROVIDER=unusual_whales only")
    if settings.provider_api_key is None:
        raise ConfigError(
            "live audit requires UNUSUAL_WHALES_API_KEY or CODEX_SCREENER_PROVIDER_API_KEY"
        )
    symbols = tuple(dict.fromkeys(symbol.strip().upper() for symbol in tickers if symbol.strip()))
    if not symbols:
        raise ValueError("--live requires at least one explicit --ticker")

    observed_date = _audit_date(as_of)
    hook = JsonlRawResponseCapture(raw_capture) if raw_capture is not None else None
    with WeeklyRequestBudget(settings.provider_usage_path) as request_budget:
        available_attempts = request_budget.usage().remaining_before_reserve
        maximum_audit_attempts = len(symbols) * 9 * MAX_TRANSPORT_ATTEMPTS_PER_LOGICAL_ITEM
        if maximum_audit_attempts > available_attempts:
            raise ValueError(
                "live audit could exceed remaining protected transport-attempt capacity "
                f"({maximum_audit_attempts} maximum, {available_attempts} available)"
            )
        client = UnusualWhalesClient(
            settings.provider_api_key,
            raw_response_hook=hook,
            request_budget=request_budget,
        )
        reports = tuple(
            run_trial_audit(client, symbol, as_of=observed_date).to_dict() for symbol in symbols
        )
    statuses = [
        result["status"]
        for report in reports
        for result in report["results"]
    ]
    all_available = bool(statuses) and all(status == AuditStatus.AVAILABLE for status in statuses)
    return {
        "status": "audit_complete" if all_available else "audit_incomplete",
        "provider": settings.provider_name,
        "provider_configured": True,
        "network_called": True,
        "recommendations_enabled": False,
        "requested_tickers": list(symbols),
        "requested_date": observed_date,
        "raw_capture": str(raw_capture) if raw_capture is not None else None,
        "reports": reports,
        "checks": checks,
        "next_action": (
            "Cross-check sampled chain quotes and timestamp semantics before normalization."
            if all_available
            else "Resolve unavailable, empty, or schema-mismatched datasets; keep scoring disabled."
        ),
    }


def morning_run(settings: Settings, as_of: str | None, fixture: Path | None) -> dict[str, object]:
    planned_at = snapshot_datetime(settings, as_of)
    response: dict[str, object] = {
        "status": "no_recommendation",
        "reason": "Provider collection is intentionally disabled until the connector audit is complete.",
        "snapshot_at": planned_at.isoformat(),
        "timezone": settings.timezone,
        "watchlist": list(settings.watchlist),
        "provider": settings.provider_name,
        "network_called": False,
        "recommendation": None,
    }
    if fixture is not None:
        payload = _read_fixture(fixture)
        response["fixture_snapshot_count"] = len(payload["snapshots"])
        response["demo_mode"] = True
    return response


def current_capture(
    settings: Settings,
    *,
    tickers: Sequence[str],
    datasets: Sequence[str],
    live: bool = False,
    audit_accepted: bool = False,
    database_path: Path | None = None,
) -> dict[str, object]:
    """Plan or perform one bounded, read-only current-evidence capture."""

    symbols = tuple(
        dict.fromkeys(
            item.strip().upper()
            for item in (tickers or settings.watchlist)
            if item.strip()
        )
    )
    selected = tuple(CurrentDataset(item) for item in datasets) if datasets else tuple(CurrentDataset)
    planned_items = len(symbols) * len(selected)
    maximum_transport_attempts = planned_items * MAX_TRANSPORT_ATTEMPTS_PER_LOGICAL_ITEM
    if not live:
        if audit_accepted:
            raise ValueError("--audit-accepted requires --live")
        return {
            "status": "dry_run",
            "provider": settings.provider_name,
            "provider_configured": settings.provider_api_key is not None,
            "network_called": False,
            "recommendations_enabled": False,
            "tickers": list(symbols),
            "datasets": [item.value for item in selected],
            "logical_items": planned_items,
            "maximum_transport_attempts": maximum_transport_attempts,
        }
    if settings.provider_name != "unusual_whales":
        raise ConfigError("live current capture supports CODEX_SCREENER_PROVIDER=unusual_whales only")
    if settings.provider_api_key is None:
        raise ConfigError(
            "live current capture requires UNUSUAL_WHALES_API_KEY or CODEX_SCREENER_PROVIDER_API_KEY"
        )
    if not audit_accepted:
        raise ValueError(
            "live current capture requires --audit-accepted after reviewing the endpoint audit"
        )
    with (
        SnapshotStore(database_path or settings.database_path) as store,
        WeeklyRequestBudget(settings.provider_usage_path) as request_budget,
    ):
        client = UnusualWhalesClient(settings.provider_api_key, request_budget=request_budget)
        report = collect_current(
            client=client,
            snapshots=store,
            request_budget=request_budget,
            tickers=symbols,
            datasets=selected,
            max_transport_attempts_per_item=MAX_TRANSPORT_ATTEMPTS_PER_LOGICAL_ITEM,
        )
    result = report.to_dict()
    result.update(
        {
            "status": "capture_complete" if report.preflight_passed else "budget_blocked",
            "provider": settings.provider_name,
            "network_called": report.preflight_passed,
            "database_path": str(database_path or settings.database_path),
        }
    )
    return result


def enhanced_capture(
    settings: Settings,
    *,
    tickers: Sequence[str],
    datasets: Sequence[str],
    live: bool = False,
    audit_accepted: bool = False,
    database_path: Path | None = None,
) -> dict[str, object]:
    """Plan or capture compact analytical feeds with one shared quota preflight."""

    symbols = tuple(dict.fromkeys(
        item.strip().upper() for item in (tickers or settings.watchlist) if item.strip()
    ))
    selected = tuple(EnhancedDataset(item) for item in datasets) if datasets else tuple(EnhancedDataset)
    ticker_items = sum(item not in GLOBAL_DATASETS for item in selected) * len(symbols)
    global_items = sum(item in GLOBAL_DATASETS for item in selected)
    planned_items = ticker_items + global_items
    maximum_transport_attempts = planned_items * MAX_TRANSPORT_ATTEMPTS_PER_LOGICAL_ITEM
    if not live:
        if audit_accepted:
            raise ValueError("--audit-accepted requires --live")
        return {
            "status": "dry_run",
            "provider": settings.provider_name,
            "provider_configured": settings.provider_api_key is not None,
            "network_called": False,
            "recommendations_enabled": False,
            "tickers": list(symbols),
            "datasets": [item.value for item in selected],
            "logical_items": planned_items,
            "maximum_transport_attempts": maximum_transport_attempts,
        }
    if settings.provider_name != "unusual_whales":
        raise ConfigError("live enhanced capture supports CODEX_SCREENER_PROVIDER=unusual_whales only")
    if settings.provider_api_key is None:
        raise ConfigError("live enhanced capture requires a configured Unusual Whales API key")
    if not audit_accepted:
        raise ValueError("live enhanced capture requires --audit-accepted after endpoint review")
    with (
        SnapshotStore(database_path or settings.database_path) as store,
        WeeklyRequestBudget(settings.provider_usage_path) as request_budget,
    ):
        client = UnusualWhalesClient(settings.provider_api_key, request_budget=request_budget)
        report = collect_enhanced(
            client=client,
            snapshots=store,
            request_budget=request_budget,
            tickers=symbols,
            datasets=selected,
            max_transport_attempts_per_item=MAX_TRANSPORT_ATTEMPTS_PER_LOGICAL_ITEM,
        )
    result = report.to_dict()
    result.update({
        "status": "capture_complete" if report.preflight_passed else "budget_blocked",
        "provider": settings.provider_name,
        "network_called": report.preflight_passed,
        "database_path": str(database_path or settings.database_path),
    })
    return result


def _render_morning_dashboard(run_path: Path, dashboard_path: Path) -> Path:
    """Render the local derived-data dashboard without exposing raw snapshots."""

    script = Path(__file__).resolve().parents[2] / "scripts" / "build_enriched_morning_dashboard.py"
    if not script.is_file():
        raise ValueError(f"dashboard renderer is unavailable: {script}")
    subprocess.run(
        [sys.executable, str(script), "--input", str(run_path), "--output", str(dashboard_path)],
        check=True,
    )
    os.chmod(dashboard_path, 0o600)
    return dashboard_path


def build_recovered_morning_run(
    settings: Settings,
    *,
    since: str,
    cutoff: str | None,
    tickers: Sequence[str],
    datasets: Sequence[str],
    database_path: Path,
    output_path: Path,
    dashboard_path: Path | None = None,
) -> dict[str, object]:
    """Build a fail-closed run from an already captured immutable time window."""

    start = _utc_timestamp(since, option="--since")
    end = _utc_timestamp(cutoff, option="--cutoff") if cutoff else datetime.now(ZoneInfo("UTC"))
    symbols = tuple(tickers) or settings.watchlist
    report = reconstruct_current_report(
        database_path,
        start,
        end,
        symbols,
        datasets=datasets or None,
    )
    artifact = build_morning_run(
        database=database_path,
        capture_report=report,
        cutoff_at=end,
    )
    destination = write_morning_run(output_path, artifact)
    rendered = _render_morning_dashboard(destination, dashboard_path) if dashboard_path else None
    return {
        "status": "morning_run_built",
        "network_called": False,
        "recommendations_enabled": False,
        "run_id": artifact["run_id"],
        "cutoff_at": artifact["cutoff_at"],
        "watchlist_count": len(artifact["watchlist"]),
        "capture_results": len(report.results),
        "artifact_path": str(destination),
        "dashboard_path": str(rendered) if rendered else None,
    }


def live_morning_run(
    settings: Settings,
    *,
    tickers: Sequence[str],
    datasets: Sequence[str],
    audit_accepted: bool,
    database_path: Path,
    output_path: Path,
    dashboard_path: Path | None = None,
    authorized_reserve_floor: int | None = None,
) -> dict[str, object]:
    """Collect, normalize, critic-check, persist, and render one safe live run."""

    if settings.provider_name != "unusual_whales":
        raise ConfigError("live morning run supports CODEX_SCREENER_PROVIDER=unusual_whales only")
    if settings.provider_api_key is None:
        raise ConfigError("live morning run requires UNUSUAL_WHALES_API_KEY or CODEX_SCREENER_PROVIDER_API_KEY")
    if not audit_accepted:
        raise ValueError("live morning run requires --audit-accepted after reviewing the endpoint audit")
    reserve_floor = DEFAULT_WEEKLY_RESERVE if authorized_reserve_floor is None else authorized_reserve_floor
    if not 0 <= reserve_floor <= DEFAULT_WEEKLY_RESERVE:
        raise ValueError(
            f"--authorized-reserve-floor must be between 0 and {DEFAULT_WEEKLY_RESERVE}"
        )
    symbols = tuple(tickers) or settings.watchlist
    selected = tuple(CurrentDataset(item) for item in datasets) if datasets else tuple(CurrentDataset)
    with (
        SnapshotStore(database_path) as store,
        WeeklyRequestBudget(
            settings.provider_usage_path,
            protected_reserve=reserve_floor,
        ) as request_budget,
    ):
        client = UnusualWhalesClient(settings.provider_api_key, request_budget=request_budget)
        report = collect_current(
            client=client,
            snapshots=store,
            request_budget=request_budget,
            tickers=symbols,
            datasets=selected,
            max_transport_attempts_per_item=MAX_TRANSPORT_ATTEMPTS_PER_LOGICAL_ITEM,
        )
        enhanced_report = collect_enhanced(
            client=client,
            snapshots=store,
            request_budget=request_budget,
            tickers=symbols,
            max_transport_attempts_per_item=MAX_TRANSPORT_ATTEMPTS_PER_LOGICAL_ITEM,
        ) if report.preflight_passed else None
    artifact = build_morning_run(database=database_path, capture_report=report)
    destination = write_morning_run(output_path, artifact)
    enhanced_path = output_path.with_name(f"{output_path.stem}-enhanced.json")
    enhanced_ids = (
        [item.snapshot_id for item in enhanced_report.results if item.snapshot_id is not None]
        if enhanced_report is not None else []
    )
    enhanced_artifact = build_enhanced_summary(database_path, snapshot_ids=enhanced_ids)
    enhanced_artifact["capture_report"] = enhanced_report.to_dict() if enhanced_report is not None else None
    enhanced_destination = write_morning_run(enhanced_path, enhanced_artifact)
    rendered = _render_morning_dashboard(destination, dashboard_path) if dashboard_path else None
    unavailable = sum(item.status.value not in {"captured", "empty"} for item in report.results)
    enhanced_unavailable = (
        sum(item.status.value not in {"captured", "empty"} for item in enhanced_report.results)
        if enhanced_report is not None else 0
    )
    complete = report.preflight_passed and enhanced_report is not None and enhanced_report.preflight_passed
    return {
        "status": "morning_run_complete" if complete else "budget_blocked",
        "network_called": report.preflight_passed,
        "recommendations_enabled": False,
        "run_id": artifact["run_id"],
        "cutoff_at": artifact["cutoff_at"],
        "watchlist_count": len(artifact["watchlist"]),
        "captured_items": sum(item.snapshot_id is not None for item in report.results),
        "unavailable_items": unavailable,
        "enhanced_captured_items": (
            sum(item.snapshot_id is not None for item in enhanced_report.results)
            if enhanced_report is not None else 0
        ),
        "enhanced_unavailable_items": enhanced_unavailable,
        "enhanced_artifact_path": str(enhanced_destination),
        "artifact_path": str(destination),
        "dashboard_path": str(rendered) if rendered else None,
        "remaining_transport_attempt_capacity_before_run": report.remaining_transport_attempt_capacity_before_run,
        "authorized_reserve_floor": reserve_floor,
        "reserve_override_applied": reserve_floor < DEFAULT_WEEKLY_RESERVE,
    }


def _backfill_date(value: str, *, option: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{option} must use YYYY-MM-DD") from exc


def historical_backfill(
    settings: Settings,
    *,
    start_date: str,
    end_date: str,
    tickers: Sequence[str],
    datasets: Sequence[str],
    max_requests: int,
    live: bool = False,
    audit_accepted: bool = False,
    authorized_reserve_floor: int | None = None,
    database_path: Path | None = None,
) -> dict[str, object]:
    """Print a zero-network plan or perform an explicitly bounded raw capture."""
    plan = make_plan(
        # Planning is intentionally usable before a key/provider is configured.
        # The live branch below still requires an explicit configured provider.
        provider=settings.provider_name if live else "unusual_whales",
        start_date=_backfill_date(start_date, option="--start-date"),
        end_date=_backfill_date(end_date, option="--end-date"),
        tickers=tickers or settings.watchlist,
        datasets=datasets or tuple(BACKFILL_DATASETS),
    )
    if not live:
        if audit_accepted:
            raise ValueError("--audit-accepted requires --live")
        if authorized_reserve_floor is not None:
            raise ValueError("--authorized-reserve-floor requires --live")
        return plan_preview(plan, logical_item_cap=max_requests)
    if settings.provider_name != "unusual_whales":
        raise ConfigError("live backfill currently supports CODEX_SCREENER_PROVIDER=unusual_whales only")
    if settings.provider_api_key is None:
        raise ConfigError("live backfill requires UNUSUAL_WHALES_API_KEY or CODEX_SCREENER_PROVIDER_API_KEY")
    if not audit_accepted:
        raise ValueError("live backfill requires --audit-accepted after reviewing the endpoint audit")
    reserve_floor = DEFAULT_WEEKLY_RESERVE if authorized_reserve_floor is None else authorized_reserve_floor
    if not 0 <= reserve_floor <= DEFAULT_WEEKLY_RESERVE:
        raise ValueError(
            f"--authorized-reserve-floor must be between 0 and {DEFAULT_WEEKLY_RESERVE}"
        )
    with (
        SnapshotStore(database_path or settings.database_path) as store,
        WeeklyRequestBudget(
            settings.provider_usage_path,
            weekly_cap=DEFAULT_WEEKLY_CAP,
            protected_reserve=reserve_floor,
        ) as request_budget,
    ):
        available_attempts = request_budget.usage().remaining_before_reserve
        maximum_transport_attempts = max_requests * MAX_TRANSPORT_ATTEMPTS_PER_LOGICAL_ITEM
        if maximum_transport_attempts > available_attempts:
            raise ValueError(
                "--max-items could exceed remaining protected transport-attempt capacity "
                f"({maximum_transport_attempts} maximum, {available_attempts} available)"
            )
        client = UnusualWhalesClient(settings.provider_api_key, request_budget=request_budget)
        result = collect(
            client=client,
            snapshots=store,
            plan=plan,
            max_requests=max_requests,
            audit_accepted=audit_accepted,
        )
        result.update({
            "logical_item_cap": max_requests,
            "maximum_transport_attempts": maximum_transport_attempts,
            "remaining_transport_attempt_capacity_before_run": available_attempts,
            "authorized_reserve_floor": reserve_floor,
            "reserve_override_applied": reserve_floor < DEFAULT_WEEKLY_RESERVE,
        })
        return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-screener", description="Local Codex Screener operations")
    parser.add_argument("--database", type=Path, help="Local SQLite path (overrides CODEX_SCREENER_DATABASE)")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="Initialize the local snapshot database")
    subparsers.add_parser("provider-usage", help="Show local provider request budget usage; no provider call")
    adjustment = subparsers.add_parser(
        "provider-baseline-adjust",
        help="Record a one-time evidenced local baseline adjustment; no provider call",
    )
    adjustment.add_argument("--adjustment-id", required=True, help="Immutable safe identifier for this adjustment")
    adjustment.add_argument("--attempted-requests", required=True, type=int, help="Positive evidenced attempt count")
    adjustment.add_argument("--evidence-id", required=True, help="Safe identifier for the local evidence set")
    adjustment.add_argument("--effective-at", required=True, help="Timezone-aware ISO-8601 evidence timestamp")
    fixture = subparsers.add_parser("load-fixture", help="Load a normalized local fixture into SQLite")
    fixture.add_argument("fixture", type=Path)
    audit = subparsers.add_parser(
        "audit-provider",
        help="Print the audit checklist; add --live for explicit read-only provider probes",
    )
    audit.add_argument("--fixture", type=Path)
    audit.add_argument(
        "--live",
        action="store_true",
        help="Make authenticated GET requests; recommendations remain disabled",
    )
    audit.add_argument(
        "--ticker",
        action="append",
        default=[],
        help="Ticker to audit; repeat for more names (required with --live)",
    )
    audit.add_argument("--as-of", help="Optional provider market date in YYYY-MM-DD format")
    audit.add_argument(
        "--raw-capture",
        type=Path,
        help="Optional private JSONL response capture; may contain licensed market data",
    )
    morning = subparsers.add_parser(
        "morning-run",
        help="Plan the daily snapshot; add --live for bounded collection and a shadow artifact",
    )
    morning.add_argument(
        "--as-of",
        help="ISO-8601 timestamp; defaults to the next weekday at configured snapshot time",
    )
    morning.add_argument("--fixture", type=Path, help="Validate a local demo fixture during the run")
    morning.add_argument("--ticker", action="append", default=[], help="Ticker to collect; defaults to the configured watchlist")
    morning.add_argument(
        "--dataset", action="append", default=[], choices=sorted(item.value for item in CurrentDataset),
        help="Current dataset to collect; repeat as needed (defaults to every bounded family)",
    )
    morning.add_argument("--live", action="store_true", help="Make authenticated read-only provider requests")
    morning.add_argument(
        "--audit-accepted", action="store_true",
        help="Confirm the endpoint audit was reviewed; required with --live",
    )
    morning.add_argument("--output", type=Path, help="Owner-private morning_run/v1 JSON destination; required with --live")
    morning.add_argument("--dashboard-output", type=Path, help="Optional owner-private derived HTML dashboard destination")
    morning.add_argument(
        "--authorized-reserve-floor",
        type=int,
        help=(
            "Explicit live-morning-run-only reserve floor, 0 to 20000. "
            "Omit to preserve the normal 20000-request reserve."
        ),
    )
    current = subparsers.add_parser(
        "current-capture",
        help="Plan or explicitly capture one bounded current evidence page per ticker/dataset",
    )
    current.add_argument(
        "--ticker",
        action="append",
        default=[],
        help="Ticker to collect; defaults to the configured watchlist",
    )
    current.add_argument(
        "--dataset",
        action="append",
        default=[],
        choices=sorted(item.value for item in CurrentDataset),
        help="Current dataset to collect; repeat as needed (defaults to every bounded family)",
    )
    current.add_argument("--live", action="store_true", help="Make authenticated read-only GET requests")
    current.add_argument(
        "--audit-accepted",
        action="store_true",
        help="Confirm the subscription endpoint audit was reviewed; required with --live",
    )
    enhanced = subparsers.add_parser(
        "enhanced-capture",
        help="Plan or capture compact Greeks, volatility, tide, dark-pool-level, and short datasets",
    )
    enhanced.add_argument("--ticker", action="append", default=[], help="Ticker to collect; defaults to the configured watchlist")
    enhanced.add_argument(
        "--dataset", action="append", default=[], choices=sorted(item.value for item in EnhancedDataset),
        help="Enhanced dataset to collect; repeat as needed (defaults to all enhanced families)",
    )
    enhanced.add_argument("--live", action="store_true", help="Make authenticated read-only GET requests")
    enhanced.add_argument(
        "--audit-accepted", action="store_true",
        help="Confirm the subscription endpoint review; required with --live",
    )
    recovered = subparsers.add_parser(
        "morning-build",
        help="Build a shadow morning artifact from an existing current-capture time window; no network",
    )
    recovered.add_argument("--since", required=True, help="Inclusive timezone-aware ISO-8601 retrieval timestamp")
    recovered.add_argument("--cutoff", help="Inclusive timezone-aware ISO-8601 cutoff; defaults to now")
    recovered.add_argument("--ticker", action="append", default=[], help="Ticker to include; defaults to configured watchlist")
    recovered.add_argument(
        "--dataset", action="append", default=[], choices=sorted(item.value for item in CurrentDataset),
        help="Expected current dataset; repeat as needed (defaults to every bounded family)",
    )
    recovered.add_argument("--output", required=True, type=Path, help="Owner-private morning_run/v1 JSON destination")
    recovered.add_argument("--dashboard-output", type=Path, help="Optional owner-private derived HTML dashboard destination")
    backfill = subparsers.add_parser(
        "historical-backfill",
        help="Plan or explicitly collect bounded, raw historical provider responses",
    )
    backfill.add_argument("--start-date", required=True, help="Inclusive YYYY-MM-DD")
    backfill.add_argument("--end-date", required=True, help="Inclusive YYYY-MM-DD")
    backfill.add_argument("--ticker", action="append", default=[], help="Ticker to collect; repeat as needed")
    backfill.add_argument(
        "--dataset", action="append", default=[], choices=sorted(BACKFILL_DATASETS),
        help="Dataset family to collect; repeat as needed (defaults to all raw families)",
    )
    backfill.add_argument(
        "--max-items", "--max-requests", dest="max_items", type=int, default=100,
        help="Maximum logical collection items, 1 to 2000; each may use up to three transport attempts",
    )
    backfill.add_argument("--live", action="store_true", help="Make authenticated GET requests")
    backfill.add_argument(
        "--audit-accepted", action="store_true",
        help="Confirm that the subscription-specific endpoint audit was reviewed; required with --live",
    )
    backfill.add_argument(
        "--authorized-reserve-floor", type=int,
        help=(
            "Explicit live-backfill-only reserve floor, 0 to 20000. "
            "Omit to preserve the normal 20000-request reserve."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = Settings.from_env()
        database_path = args.database or settings.database_path
        if args.command == "init-db":
            result = initialize_database(database_path)
        elif args.command == "provider-usage":
            result = provider_usage(settings)
        elif args.command == "provider-baseline-adjust":
            result = add_provider_baseline_adjustment(
                settings,
                adjustment_id=args.adjustment_id,
                attempted_requests=args.attempted_requests,
                evidence_id=args.evidence_id,
                effective_at=args.effective_at,
            )
        elif args.command == "load-fixture":
            result = load_fixture(args.fixture, database_path)
        elif args.command == "audit-provider":
            result = audit_provider(
                settings,
                args.fixture,
                live=args.live,
                tickers=args.ticker,
                as_of=args.as_of,
                raw_capture=args.raw_capture,
            )
        elif args.command == "morning-run":
            if args.live:
                if args.fixture is not None or args.as_of is not None:
                    raise ValueError("--fixture and --as-of cannot be combined with --live")
                if args.output is None:
                    raise ValueError("live morning run requires --output")
                result = live_morning_run(
                    settings,
                    tickers=args.ticker,
                    datasets=args.dataset,
                    audit_accepted=args.audit_accepted,
                    database_path=database_path,
                    output_path=args.output,
                    dashboard_path=args.dashboard_output,
                    authorized_reserve_floor=args.authorized_reserve_floor,
                )
            else:
                if (
                    args.audit_accepted
                    or args.output is not None
                    or args.dashboard_output is not None
                    or args.ticker
                    or args.dataset
                    or args.authorized_reserve_floor is not None
                ):
                    raise ValueError("live morning-run options require --live")
                result = morning_run(settings, args.as_of, args.fixture)
        elif args.command == "current-capture":
            result = current_capture(
                settings,
                tickers=args.ticker,
                datasets=args.dataset,
                live=args.live,
                audit_accepted=args.audit_accepted,
                database_path=database_path,
            )
        elif args.command == "enhanced-capture":
            result = enhanced_capture(
                settings,
                tickers=args.ticker,
                datasets=args.dataset,
                live=args.live,
                audit_accepted=args.audit_accepted,
                database_path=database_path,
            )
        elif args.command == "morning-build":
            result = build_recovered_morning_run(
                settings,
                since=args.since,
                cutoff=args.cutoff,
                tickers=args.ticker,
                datasets=args.dataset,
                database_path=database_path,
                output_path=args.output,
                dashboard_path=args.dashboard_output,
            )
        elif args.command == "historical-backfill":
            result = historical_backfill(
                settings,
                start_date=args.start_date,
                end_date=args.end_date,
                tickers=args.ticker,
                datasets=args.dataset,
                max_requests=args.max_items,
                live=args.live,
                audit_accepted=args.audit_accepted,
                authorized_reserve_floor=args.authorized_reserve_floor,
                database_path=database_path,
            )
        else:  # argparse makes this unreachable; retain a safe failure mode.
            parser.error("unknown command")
            return 2
    except (ConfigError, ValueError, OSError, sqlite3.Error) as exc:
        _json({"status": "error", "error": str(exc)})
        return 2
    _json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

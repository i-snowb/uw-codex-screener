#!/usr/bin/env python3
"""Calendar- and quota-gated daily capture. Planning is network-free by default."""

from __future__ import annotations

import argparse
from datetime import datetime
import fcntl
import json
from pathlib import Path
import time
from zoneinfo import ZoneInfo

from morning_edge.benchmarks import BENCHMARKS
from morning_edge.cli import live_morning_run
from morning_edge.clock import is_nyse_session
from morning_edge.config import Settings, private_runtime_path
from morning_edge.current_collection import CurrentDataset, collect_current
from morning_edge.daily import write_morning_run
from morning_edge.data_health import run_health
from morning_edge.enhanced_collection import TICKER_DATASETS, GLOBAL_DATASETS
from morning_edge.freshness import latest_complete_session
from morning_edge.providers.budget import WeeklyRequestBudget, API_BASIC_ROLLING_WINDOW
from morning_edge.providers.unusual_whales import UnusualWhalesClient
from morning_edge.store import SnapshotStore
from run_intraday_refresh import _load_env
from private_artifacts import ensure_private_directory

ET = ZoneInfo('America/New_York')


def plan(settings: Settings, now: datetime) -> dict:
    now = now.astimezone(ET)
    logical = len(settings.watchlist) * (len(CurrentDataset) + len(TICKER_DATASETS)) + len(GLOBAL_DATASETS) + len(BENCHMARKS)
    return {'status': 'PLANNED' if is_nyse_session(now.date()) else 'MARKET_CLOSED',
            'observed_at': now.isoformat(), 'expected_complete_session': latest_complete_session(now).isoformat(),
            'watchlist': list(settings.watchlist), 'benchmarks': list(BENCHMARKS),
            'logical_requests': logical, 'maximum_transport_attempts': logical * 3,
            'protected_reserve': 7000, 'credential_configured': bool(settings.provider_api_key),
            'network_called': False, 'recommendations_enabled': False}


def capture(settings: Settings, *, now: datetime, output: Path, app_root: Path) -> dict:
    result = plan(settings, now)
    status_path = app_root / 'data' / 'pipeline-status.json'
    def status(state: str, stage: str, reason: str, **extra: object) -> dict:
        value = result | {'status': state, 'stage': stage, 'reason': reason,
                          'updated_at': datetime.now(ET).isoformat(), **extra}
        write_morning_run(status_path, value)
        return value
    if result['status'] == 'MARKET_CLOSED':
        return status('MARKET_CLOSED', 'calendar', 'No provider requests. Wait for the next NYSE session.')
    if not settings.provider_api_key:
        return status('BLOCKED', 'credentials', 'Provider credential is not configured.')
    if settings.provider_name != 'unusual_whales':
        return status('BLOCKED', 'configuration', 'Live capture requires the configured Unusual Whales provider.')
    ensure_private_directory(output.parent)
    with (output.parent / '.capture.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return result | {'status': 'BLOCKED', 'stage': 'lock', 'reason': 'Daily capture is already running.'}
        if output.exists():
            return status('BLOCKED', 'immutable_output', 'This capture path already exists. Use a new attempt path; do not overwrite evidence.')
        started = time.perf_counter()
        stage = 'budget'
        try:
            with WeeklyRequestBudget(settings.provider_usage_path, weekly_cap=40000,
                    protected_reserve=7000, rolling_window=API_BASIC_ROLLING_WINDOW) as budget:
                capacity = budget.usage().remaining_before_reserve
                if capacity < result['maximum_transport_attempts']:
                    return status('BLOCKED', stage, 'Insufficient local capacity above the protected reserve.', remaining_before_reserve=capacity)
                stage = 'benchmarks'
                status('RUNNING', stage, 'Refreshing five benchmark price series.')
                with SnapshotStore(settings.database_path) as snapshots:
                    client = UnusualWhalesClient(settings.provider_api_key, request_budget=budget)
                    result['network_called'] = True
                    report = collect_current(client=client, snapshots=snapshots, request_budget=budget,
                        tickers=BENCHMARKS, datasets=(CurrentDataset.OHLC,), max_transport_attempts_per_item=3)
                write_morning_run(output.with_name(output.stem + '-benchmarks.json'), report.to_dict())
                if not report.preflight_passed or any(item.status.value != 'captured' for item in report.results):
                    return status('FAILED', stage, 'Benchmark capture incomplete; base capture and publication skipped.')
            benchmark_seconds = time.perf_counter() - started
            stage = 'capture_and_analysis'
            status('RUNNING', stage, 'Capturing base and enhanced evidence under one cutoff.')
            captured = live_morning_run(settings, tickers=settings.watchlist, datasets=(), audit_accepted=True,
                database_path=settings.database_path, output_path=output)
            if captured['status'] != 'morning_run_complete':
                return status('FAILED', stage, 'Base or enhanced collection failed. Inspect the private capture diagnostic.', capture=captured)
            stage = 'health'
            run = json.loads(output.read_text())
            health = run_health(run, observed_at=datetime.now(ET))
            write_morning_run(output.with_name(output.stem + '-health.json'), health)
            timings = {'benchmarks_seconds': round(benchmark_seconds, 3), 'capture_and_analysis_seconds': round(time.perf_counter() - started - benchmark_seconds, 3)}
            if health['failures']:
                return status('BLOCKED', stage, 'Required market evidence failed health checks; publication prohibited.', health=health, timings=timings)
            return status('AWAITING_VALIDATED_ENRICHMENT', 'enrichment', 'Validate this run, register forecasts, then publish with --require-ready.', artifact_path=str(output), health=health, timings=timings)
        except Exception as error:
            # Provider/library exception text may contain sensitive request data.
            return status('FAILED', stage, 'Stage failed; no publication. Error class: ' + type(error).__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--audit-accepted', action='store_true')
    parser.add_argument('--env-file', type=Path, default=Path('.env'))
    parser.add_argument('--output', type=Path)
    parser.add_argument('--app-root', type=Path, default=Path('dashboard-app'))
    args = parser.parse_args(argv)
    if args.live != args.audit_accepted:
        parser.error('live capture requires both --live and --audit-accepted')
    _load_env(args.env_file)
    settings = Settings.from_env()
    now = datetime.now(ET)
    output = args.output or private_runtime_path('outputs/runs') / now.date().isoformat() / 'morning-run.json'
    result = capture(settings, now=now, output=output, app_root=args.app_root) if args.live else plan(settings, now)
    print(json.dumps(result, sort_keys=True))
    return 2 if result['status'] in {'FAILED', 'BLOCKED'} else 0


if __name__ == '__main__':
    raise SystemExit(main())

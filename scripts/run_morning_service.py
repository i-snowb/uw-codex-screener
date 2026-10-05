#!/usr/bin/env python3
"""Keep the premarket worker awake, supervise retries, and report early failures."""

from __future__ import annotations

import argparse
from datetime import datetime, time, timedelta, UTC
import fcntl
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import time as clock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from morning_edge.clock import is_nyse_session
from morning_edge.unattended import atomic_json, immutable_json, read_object
from morning_edge.private_io import ensure_private_directory
from run_unattended_morning import completed_production, validate_config

ET = ZoneInfo("America/New_York")
ISSUES = {"NOT_STARTED", "FAILED", "STALLED", "MISSED_DEADLINE", "INTEGRITY_INVALID"}


def retry_available(config: dict, state: dict, now: datetime) -> bool:
    if state.get("attempts", {}).get(state.get("stage", "capture"), 0) >= config["max_attempts"]:
        return False
    if state.get("status") != "FAILED":
        return True
    try:
        failed_at = datetime.fromisoformat(state["updated_at"].replace("Z", "+00:00"))
        return bool(failed_at.tzinfo) and (now - failed_at).total_seconds() >= 300
    except (KeyError, ValueError, TypeError):
        return False


def assess(config: dict, state: dict | None, pipeline: dict | None, now: datetime) -> dict:
    local = now.astimezone(ET)
    start = datetime.combine(local.date(), time.fromisoformat(config["start_et"]), ET)
    deadline = datetime.combine(local.date(), time.fromisoformat(config["deadline_et"]), ET)
    result = {"schema": "morning-service-status/v1", "session_date": local.date().isoformat(),
              "checked_at": now.isoformat(), "start_at": start.isoformat(), "deadline_at": deadline.isoformat(),
              "recommendations_enabled": False, "external_outage_monitor_configured": False}
    state = state if state and state.get("session_date") == result["session_date"] else {}
    if not is_nyse_session(local.date()):
        return result | {"status": "MARKET_CLOSED", "reason": "No NYSE session today."}
    if state.get("status") == "COMPLETE":
        return result | {"status": "VERIFYING", "reason": "Completion requires artifact and ledger verification."}
    if local >= deadline:
        return result | {"status": "MISSED_DEADLINE", "reason": "No verified completion before the morning deadline.",
                         "worker_status": state.get("status", "ABSENT"), "worker_stage": state.get("stage")}
    if local < start:
        return result | {"status": "WAITING_FOR_START", "reason": "The service is holding the morning window open."}
    if state.get("status") == "FAILED":
        return result | {"status": "FAILED", "reason": state.get("reason", "Worker failed."), "worker_stage": state.get("stage")}
    if not state.get("attempts") and local >= start + timedelta(minutes=10):
        return result | {"status": "NOT_STARTED", "reason": "No capture attempt within ten minutes of scheduled start."}
    progress = []
    for value in (state, pipeline or {}):
        try:
            stamp = datetime.fromisoformat(value["updated_at"].replace("Z", "+00:00"))
            if stamp.tzinfo and stamp.astimezone(ET).date() == local.date() and stamp <= now:
                progress.append(stamp)
        except (KeyError, TypeError, ValueError):
            pass
    if state.get("status") == "RUNNING" and progress and (now - max(progress)).total_seconds() >= 600:
        return result | {"status": "STALLED", "reason": "No saved progress for ten minutes.", "worker_stage": state.get("stage")}
    return result | {"status": "RUNNING", "reason": "Morning work is in progress.", "worker_stage": state.get("stage")}


def inspect(config: dict, now: datetime) -> dict:
    today = now.astimezone(ET).date().isoformat()
    directory = ROOT / "outputs/unattended" / today
    state = None
    try:
        for candidate in (directory, directory.with_name(today + "-recovered")):
            path = candidate / "state.json"
            if path.exists():
                value = read_object(path)
                if state is None:
                    state = value
                if value.get("status") == "COMPLETE":
                    verified = completed_production(candidate)
                    if verified:
                        return assess(config, value, None, now) | {"status": "COMPLETE", "reason": "Saved artifacts, ticker audits and forecasts verified.", **verified}
        path = ROOT / "dashboard-app/data/pipeline-status.json"
        pipeline = read_object(path) if path.exists() else None
        return assess(config, state, pipeline, now)
    except (OSError, ValueError, KeyError) as error:
        return assess(config, None, None, now) | {"status": "INTEGRITY_INVALID", "reason": "Saved verification failed: " + type(error).__name__}


def report(config: dict, *, notify: bool) -> dict:
    result = inspect(config, datetime.now(UTC))
    atomic_json(ROOT / "dashboard-app/data/service-status.json", result)
    if notify and result["status"] in ISSUES:
        path = ROOT / "outputs/unattended-service" / result["session_date"] / "notifications.json"
        previous = read_object(path) if path.exists() else {"requested": []}
        key = result["status"] + ":" + str(result.get("worker_stage", ""))
        if key not in previous["requested"]:
            message = f"{result['session_date']} {result['status']}: {result['reason']}"
            # Fixed local notification, no shell interpolation or provider content.
            try:
                sent = subprocess.run(["/usr/bin/osascript", "-e",
                    "on run argv\ndisplay notification (item 1 of argv) with title \"Codex Screener morning run\"\nend run", message],
                    capture_output=True, timeout=10)
                exit_code = sent.returncode
            except (OSError, subprocess.TimeoutExpired):
                exit_code = -1
            if exit_code == 0:
                previous["requested"].append(key)
            previous["last_request_at"] = result["checked_at"]
            previous["last_request_exit_code"] = exit_code
            atomic_json(path, previous)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--audit-accepted", action="store_true")
    parser.add_argument("--check", action="store_true", help="Read-only assessment; no capture, notification or status writes.")
    args = parser.parse_args(argv)
    config = read_object(args.config)
    validate_config(config)
    if args.check:
        print(json.dumps(inspect(config, datetime.now(UTC)), sort_keys=True))
        return 0
    if not args.live or not args.audit_accepted:
        parser.error("worker execution requires --live --audit-accepted")
    directory = ROOT / "outputs/unattended-service"
    ensure_private_directory(directory)
    with (directory / ".service.lock").open("a") as lock:
        os.fchmod(lock.fileno(), 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        now = datetime.now(ET)
        start = datetime.combine(now.date(), time.fromisoformat(config["start_et"]), ET)
        deadline = datetime.combine(now.date(), time.fromisoformat(config["deadline_et"]), ET)
        hold_until = deadline + timedelta(minutes=15)
        if not is_nyse_session(now.date()) or not start - timedelta(minutes=15) <= now < hold_until:
            print(json.dumps(report(config, notify=is_nyse_session(now.date()) and now >= deadline)))
            return 0
        process = None
        launches = 0
        log_root = directory / now.date().isoformat()
        ensure_private_directory(log_root)
        try:
            # The launchd command wraps this whole service in caffeinate. It
            # stays alive before collection and between attempts, unlike the old job.
            while datetime.now(ET) < hold_until:
                status = report(config, notify=True)
                if datetime.now(ET) >= deadline and process is not None and process.poll() is None:
                    stop_worker(process, log_root)
                    process = None
                if process is not None and process.poll() is not None:
                    process = None
                if start <= datetime.now(ET) < deadline and status["status"] != "COMPLETE" and process is None and launches < 20:
                    state_path = ROOT / "outputs/unattended" / now.date().isoformat() / "state.json"
                    state = read_object(state_path) if state_path.exists() else {}
                    if retry_available(config, state, datetime.now(ET)):
                        launches += 1
                        with (log_root / "worker.stdout.log").open("ab") as output, (log_root / "worker.stderr.log").open("ab") as errors:
                            os.fchmod(output.fileno(), 0o600)
                            os.fchmod(errors.fileno(), 0o600)
                            process = subprocess.Popen([config["python"], str(ROOT / "scripts/run_unattended_morning.py"),
                                "--config", str(args.config.resolve()), "--live", "--audit-accepted"],
                                cwd=ROOT, stdout=output, stderr=errors, start_new_session=True)
                            atomic_json(log_root / "worker.json", {"pid": process.pid, "started_at": datetime.now(UTC).isoformat(), "launch": launches})
                current = datetime.now(ET)
                boundary = start if current < start else deadline if current < deadline else hold_until
                clock.sleep(min(20, max(0.05, (boundary - current).total_seconds())))
        finally:
            if process is not None and process.poll() is None:
                stop_worker(process, log_root)
            print(json.dumps(report(config, notify=True)))
    return 0


def stop_worker(process: subprocess.Popen, log_root: Path) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        # The worker handles TERM and cleans up its separately grouped capture
        # or analyst before recording failure. Allow both cleanup waits.
        process.wait(timeout=12)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
    immutable_json(log_root / f"worker-stop-{process.pid}.json", {"at": datetime.now(UTC).isoformat(), "pid": process.pid})


if __name__ == "__main__":
    def stop_service(_signal, _frame):
        raise InterruptedError("morning service stopped; cleaning up its owned worker")
    signal.signal(signal.SIGTERM, stop_service)
    raise SystemExit(main())

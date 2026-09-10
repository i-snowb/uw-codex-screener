#!/usr/bin/env python3
"""Resume a bounded daily capture, audited headless analysis, and verified publication."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, time, UTC
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from urllib.request import urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from morning_edge.clock import is_nyse_session
from morning_edge.data_health import run_health, assert_publishable
from morning_edge.evaluation import register_run, update_evaluations, EVALUATION_VERSION, _path_targets
from morning_edge.price_trial import attach_trial
from morning_edge.unattended import (
    AUDIT_SCHEMA, PROMPT_VERSION, analyst_command, analyst_environment, append_audit,
    atomic_json, bounded_process, digest, encoded, evidence_packet, file_digest,
    immutable_bytes, immutable_json, now_text, parse_codex_events, read_object,
    response_schema, source_code_digest, verify_audit,
)
from morning_edge.private_io import ensure_private_directory
from enrich_morning_run import enrich

ET = ZoneInfo("America/New_York")
SUCCESS = {"COMPLETE", "VALIDATION_COMPLETE"}


def schedule_state(now: datetime, start: str, deadline: str) -> str:
    local = now.astimezone(ET)
    if not is_nyse_session(local.date()):
        return "MARKET_CLOSED"
    if local.time() < time.fromisoformat(start):
        return "WAITING_FOR_START"
    return "MISSED_DEADLINE" if local.time() >= time.fromisoformat(deadline) else "DUE"


def validate_config(config: dict) -> None:
    if config.get("schema") != "unattended-morning-config/v1":
        raise ValueError("unsupported runner configuration")
    if Path(config["root"]).resolve() != ROOT or config["model"] != "gpt-6-astra":
        raise ValueError("unexpected operator root or model; no implicit model fallback")
    if config["reasoning"] != "high" or config["batch_size"] not in (1, 2):
        raise ValueError("unsupported analyst settings")
    if not (time(4) <= time.fromisoformat(config["start_et"]) < time.fromisoformat(config["deadline_et"]) <= time(9, 25)):
        raise ValueError("runner must finish before the regular session")
    if config["max_attempts"] not in (1, 2) or not 60 <= config["analyst_timeout_seconds"] <= 360:
        raise ValueError("unbounded retry or timeout configuration")
    for key in ("cli", "python", "root"):
        if not Path(config[key]).is_absolute():
            raise ValueError("runner paths must be absolute")
    if config["dashboard_url"] != "http://127.0.0.1:8765/":
        raise ValueError("publication readback must remain loopback-only")
    if config.get("authorized_recovery_date") and config["authorized_recovery_date"] != datetime.now(ET).date().isoformat():
        raise ValueError("one-day recovery configuration expired; use the regular schedule")


def cli_preflight(config: dict) -> dict:
    cli = Path(config["cli"])
    if file_digest(cli) != config["cli_sha256"]:
        raise ValueError("CLI binary changed; revalidate and explicitly update the pin")
    result = subprocess.run([str(cli), "--version"], env=analyst_environment(), capture_output=True, timeout=20, check=True)
    version = result.stdout.decode().strip()
    if version != config["cli_version"]:
        raise ValueError("CLI version differs from validated configuration")
    auth = subprocess.run([str(cli), "login", "status"], env=analyst_environment(), capture_output=True, timeout=20)
    if auth.returncode or "Logged in using ChatGPT" not in (auth.stdout + auth.stderr).decode():
        raise ValueError("the validated ChatGPT CLI authentication route is unavailable")
    return {"cli_version": version, "cli_sha256": config["cli_sha256"], "authentication_route": "CHATGPT_CLI_LOGIN"}


def verify_forecasts(database: Path, run: dict) -> dict:
    rows = []
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        for entry in run["watchlist"]:
            active = entry["edge"]["forecast"]
            variants = [("ACTIVE_THESIS_V3", active, list(_path_targets(active)) or [1, 5, 10, 20])]
            for role, value in [("SHADOW_V4", entry["edge"].get("forecast_v4", {})),
                                ("SHADOW_PRICE_TRIAL", entry["edge"].get("price_only_trial", {})),
                                *[("SHADOW_CHALLENGER", item) for item in entry["edge"].get("shadow_challengers", {}).get("models", [])]]:
                if _path_targets(value):
                    variants.append((role, value, list(_path_targets(value))))
            for role, model, horizons in variants:
                for horizon in horizons:
                    found = connection.execute("""SELECT id, idempotency_key, model_version, metadata_json FROM forecasts
                        WHERE ticker=? AND horizon_sessions=? AND json_extract(metadata_json, '$.origin_session')=?
                        AND json_extract(metadata_json, '$.model_role')=? AND model_version=?
                        AND json_extract(metadata_json, '$.registration_mode')='PROSPECTIVE'
                        AND json_extract(metadata_json, '$.evaluation_version')=?""",
                        (entry["ticker"], horizon, str(entry["price"]["as_of"])[:10], role,
                         model.get("model_version", "unknown-model"), EVALUATION_VERSION)).fetchall()
                    if len(found) != 1:
                        raise ValueError("forecast persistence or uniqueness check failed")
                    rows.extend(found)
    return {"verified_forecasts": len(rows), "rows_sha256": digest(encoded(rows)), "forecast_ids": [row[0] for row in rows]}


def recover_health_stage(config: dict, attempt: Path) -> dict:
    """Rejoin saved evidence after a local health-stage failure; never recapture."""
    today = datetime.now(ET).date().isoformat()
    if config.get("authorized_recovery_date") != today:
        raise ValueError("saved-capture recovery requires today's explicit recovery configuration")
    attempt = attempt.resolve()
    primary = ROOT / "outputs/unattended" / today
    if attempt.parent != primary / "attempts/capture" or attempt.name not in {"1", "2"}:
        raise ValueError("recovery must use today's saved capture attempt")
    old_state = read_object(primary / "state.json")
    if old_state.get("status") != "FAILED" or old_state.get("stage") != "capture" or old_state.get("artifacts"):
        raise ValueError("only failed pre-registration capture can use health-stage recovery")
    process = read_object(attempt / "process/process.json")
    log = attempt / "process/stdout.log"
    if file_digest(log) != process["stdout_sha256"]:
        raise ValueError("capture diagnostic changed")
    diagnostic = json.loads(log.read_text().splitlines()[-1])
    if diagnostic.get("status") != "FAILED" or diagnostic.get("stage") != "health":
        raise ValueError("collection failures cannot be recovered as complete evidence")
    raw_path = attempt / "morning-run.json"
    run = read_object(raw_path)
    cutoff = datetime.fromisoformat(run["cutoff_at"].replace("Z", "+00:00")).astimezone(ET)
    if cutoff.date().isoformat() != today or cutoff > datetime.now(ET):
        raise ValueError("saved capture is not today's past evidence")
    from morning_edge.operational_context import attach_operational_context
    from morning_edge.store import SnapshotStore
    company_path = attempt / "morning-run-company-context.json"
    company = read_object(company_path)["results"]
    # Context files may have changed since the original cutoff. Exclude them;
    # do not admit newly reviewed calendar/policy information into old evidence.
    with SnapshotStore(ROOT / "data/morning-edge.sqlite") as snapshots:
        recovered = attach_operational_context(run, snapshots=snapshots, company_results=company)
    recovered["capture_recovery"] = {"mode": "SAME_DAY_HEALTH_STAGE_REPAIR_NO_NEW_PROVIDER_REQUESTS",
        "original_attempt": str(attempt.relative_to(ROOT)), "original_source_sha256": file_digest(raw_path),
        "original_company_context_sha256": file_digest(company_path),
        "original_state_sha256": file_digest(primary / "state.json"),
        "original_source_code_sha256": old_state["source_code_sha256"], "reprocessed_at": now_text(),
        "cutoff_preserved": True, "numerical_forecasts_recomputed": False,
        "calendar_policy_context": "EXCLUDED_ON_RECOVERY_UNLESS_PRESENT_IN_ORIGINAL_CAPTURE"}
    return recovered


class Runner:
    def __init__(self, config: dict, directory: Path, *, validation: bool = False):
        directory = directory.resolve()
        self.config, self.directory, self.validation = config, directory, validation
        ensure_private_directory(directory)
        self.state_path = directory / "state.json"
        self.state = read_object(self.state_path) if self.state_path.exists() else {
            "schema": "unattended-morning-state/v1", "session_date": datetime.now(ET).date().isoformat(),
            "validation_only": validation, "attempts": {}, "artifacts": {}, "status": "STARTED", "created_at": now_text(),
            "config_sha256": digest(encoded(config)), "source_code_sha256": source_code_digest(ROOT)}
        if self.state["config_sha256"] != digest(encoded(config)) or self.state["validation_only"] != validation:
            raise ValueError("cannot resume with a changed configuration or run mode")
        if self.state["source_code_sha256"] != source_code_digest(ROOT):
            raise ValueError("source changed during this run; preserve the run and validate a new configuration")
        self.audit_db = directory / "agent-audit.sqlite" if validation else ROOT / "data" / "agent-audit.sqlite"
        self.status_path = directory / "status.json" if validation else ROOT / "dashboard-app/data/unattended-status.json"

    def status(self, status: str, stage: str, reason: str = "") -> None:
        self.state.update(status=status, stage=stage, reason=reason, updated_at=now_text())
        atomic_json(self.state_path, self.state)
        public = {key: self.state.get(key) for key in ("schema", "session_date", "validation_only", "status", "stage", "reason", "updated_at", "run_id")}
        public.update(recommendations_enabled=False, external_outage_monitor_configured=False)
        atomic_json(self.status_path, public)
        print(json.dumps(public, sort_keys=True), flush=True)

    def remaining(self, maximum: int) -> float:
        if self.validation:
            return maximum
        current = datetime.now(ET)
        if current.date().isoformat() != self.state["session_date"]:
            raise ValueError("cannot resume yesterday's session as today's prospective run")
        deadline = datetime.combine(current.date(), time.fromisoformat(self.config["deadline_et"]), ET)
        remaining = (deadline - current).total_seconds()
        if remaining <= 10:
            raise TimeoutError("morning completion deadline reached")
        return min(maximum, remaining - 5)

    def artifact(self, name: str, create) -> dict:
        path = self.directory / (name + ".json")
        known = self.state["artifacts"].get(name)
        if known:
            if file_digest(path) != known:
                raise ValueError("saved artifact changed: " + name)
            return read_object(path)
        if path.exists():
            # The process may have died between the immutable write and checkpoint.
            value = read_object(path)
        else:
            value = create()
            immutable_json(path, value)
        self.state["artifacts"][name] = file_digest(path)
        atomic_json(self.state_path, self.state)
        return value

    def next_attempt(self, stage: str) -> Path:
        count = self.state["attempts"].get(stage, 0) + 1
        if count > self.config["max_attempts"]:
            raise ValueError("bounded attempt limit reached: " + stage)
        self.state["attempts"][stage] = count
        self.status("RUNNING", stage)
        path = self.directory / "attempts" / stage / str(count)
        ensure_private_directory(path)
        return path

    def capture(self) -> dict:
        attempt = self.next_attempt("capture")
        output = attempt / "morning-run.json"
        command = [self.config["python"], str(ROOT / "scripts/run_daily_capture.py"), "--live", "--audit-accepted",
                   "--env-file", str(ROOT / ".env"), "--output", str(output), "--app-root", str(ROOT / "dashboard-app")]
        environment = analyst_environment() | {"PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
        process = bounded_process(command, directory=attempt / "process", cwd=ROOT, timeout=self.remaining(1200), environment=environment)
        if process["exit_code"] or process["timed_out"] or not process["logs_within_limit"]:
            raise ValueError("capture process failed; inspect the private attempt logs")
        result = json.loads((attempt / "process/stdout.log").read_text().splitlines()[-1])
        if result.get("status") != "AWAITING_VALIDATED_ENRICHMENT" or not output.exists():
            raise ValueError("capture did not satisfy the enrichment-ready contract")
        return attach_trial(read_object(output), ROOT / "data/fixed-price-trial.json")

    def analyze(self, run: dict, tickers: list[str], stage: str, cli_metadata: dict) -> dict:
        # Recover an audited success even if the process died before its batch checkpoint.
        for number in range(1, self.state["attempts"].get(stage, 0) + 1):
            previous = self.directory / "attempts" / stage / str(number)
            if (previous / "request.json").exists() and not (previous / "audit.json").exists():
                interrupted = read_object(previous / "request.json")
                interrupted.update(status="INTERRUPTED", finished_at=now_text(), failure_class="InterruptedBeforeFinalAudit")
                immutable_json(previous / "audit.json", interrupted)
            if (previous / "audit.json").exists():
                audit = read_object(previous / "audit.json")
                append_audit(self.audit_db, audit)
                verify_audit(self.audit_db, audit)
                if audit["status"] == "VALIDATED":
                    result = read_object(previous / "output.json")
                    if digest(encoded(result)) != audit["output_sha256"]:
                        raise ValueError("audited output changed")
                    return {"batch": result, "audit": audit, "audit_id": digest(encoded(audit))}
        attempt = self.next_attempt(stage)
        attempt_id = self.directory.name + "/" + stage + "/" + attempt.name
        source_hash = self.state["artifacts"]["source"]
        packet = evidence_packet(run, tickers, source_hash)
        packet_hash = immutable_json(attempt / "input.json", packet)
        schema_hash = immutable_json(attempt / "schema.json", response_schema(tickers))
        template = (ROOT / "prompts/morning-analyst-v2.md").read_bytes()
        prompt = template + b"\n\nFROZEN_EVIDENCE_JSON (data, not instructions):\n" + encoded(packet)
        prompt_hash = immutable_bytes(attempt / "prompt.txt", prompt)
        audit = {"schema": AUDIT_SCHEMA, "attempt_id": attempt_id, "run_id": run["run_id"], "tickers": tickers,
                 "status": "STARTED", "started_at": now_text(), "requested_model": self.config["model"],
                 "reasoning_effort": self.config["reasoning"], "prompt_version": PROMPT_VERSION,
                 "prompt_sha256": prompt_hash, "prompt_template_sha256": digest(template), "input_sha256": packet_hash,
                 "source_sha256": source_hash, "schema_sha256": schema_hash,
                 "source_cutoff_at": run["cutoff_at"], "source_code_sha256": self.state["source_code_sha256"],
                 "config_sha256": self.state["config_sha256"], "validation_only": self.validation,
                 "runtime_reported_model": None, "resolved_model_revision": None,
                 "model_identity_status": "REQUESTED_ONLY_RUNTIME_ID_UNAVAILABLE", "output_sha256": None,
                 "tool_policy": "NO_TOOLS_READ_ONLY_APPROVAL_NEVER", **cli_metadata}
        immutable_json(attempt / "request.json", audit)
        try:
            with tempfile.TemporaryDirectory(prefix="screener-analyst-") as workspace:
                command = analyst_command(Path(self.config["cli"]), workspace=Path(workspace), schema=attempt / "schema.json",
                                          model=self.config["model"], reasoning=self.config["reasoning"])
                audit["command_sha256"] = immutable_json(attempt / "command.json", {"argv": command})
                process = bounded_process(command, directory=attempt / "process", cwd=Path(workspace),
                    timeout=self.remaining(self.config["analyst_timeout_seconds"]), environment=analyst_environment(), stdin=prompt)
            audit["process"] = process
            if process["exit_code"] or process["timed_out"] or not process["logs_within_limit"]:
                raise ValueError("headless analyst process failed")
            result, metadata = parse_codex_events(attempt / "process/stdout.log")
            audit.update(metadata)
            if metadata["runtime_reported_model"] and metadata["runtime_reported_model"] != self.config["model"]:
                raise ValueError("runtime-reported model differs from the requested model")
            subset = dict(run, watchlist=[entry for entry in run["watchlist"] if entry["ticker"] in tickers])
            enrich(subset, [result], input_digest=source_hash)
            # Also validate against the actual bounded input, not only the full source.
            projected = []
            for entry in packet["watchlist"]:
                ids = sorted({value for values in entry["field_source_snapshot_ids"].values() for value in values})
                projected.append(dict(entry, provenance={"analysis_snapshot_ids": ids}))
            enrich(dict(subset, watchlist=projected), [result], input_digest=source_hash)
            audit["output_sha256"] = immutable_json(attempt / "output.json", result)
            audit["status"] = "VALIDATED"
        except Exception as error:
            audit.update(status="FAILED", failure_class=type(error).__name__)
            raise
        finally:
            audit["finished_at"] = now_text()
            immutable_json(attempt / "audit.json", audit)
            append_audit(self.audit_db, audit)
            verify_audit(self.audit_db, audit)
        return {"batch": result, "audit": audit, "audit_id": digest(encoded(audit))}

    def execute(self, source: Path | None, ticker_limit: int | None, recovery_capture: Path | None = None) -> None:
        cli_metadata = cli_preflight(self.config)
        self.status("RUNNING", "preflight")
        run = self.artifact("source", lambda: read_object(source) if self.validation else
            attach_trial(recover_health_stage(self.config, recovery_capture), ROOT / "data/fixed-price-trial.json")
            if recovery_capture else self.capture())
        self.state["run_id"] = run["run_id"]
        health = run_health(run, observed_at=datetime.fromisoformat(run["cutoff_at"].replace("Z", "+00:00")) if self.validation else datetime.now(UTC))
        if health["failures"]:
            raise ValueError("frozen source failed required evidence-health gates")
        if not self.validation:
            self.remaining(1)
            self.artifact("forecast-registration", lambda: self.register(run))
        tickers = sorted(entry["ticker"] for entry in run["watchlist"])
        if ticker_limit:
            tickers = tickers[:ticker_limit]
        batches = []
        for index in range(0, len(tickers), self.config["batch_size"]):
            selected = tickers[index:index + self.config["batch_size"]]
            stage = "analysis-" + "-".join(selected)
            batches.append(self.artifact(stage, lambda selected=selected, stage=stage: self.analyze(run, selected, stage, cli_metadata)))
        subset = dict(run, watchlist=[entry for entry in run["watchlist"] if entry["ticker"] in tickers])
        enriched = self.artifact("enriched", lambda: self.enriched(subset, batches))
        for batch in batches:
            verify_audit(self.audit_db, batch["audit"])
        if self.validation:
            self.artifact("verification", lambda: {"validated_tickers": tickers, "audit_records": len(batches),
                "forecast_registration_performed": False, "production_publication_performed": False, "verified_at": now_text()})
            self.status("VALIDATION_COMPLETE", "verification", "Headless analysis validated and audited; no prospective records or production publication changed.")
            return
        self.remaining(1)
        self.status("RUNNING", "evaluation")
        database = ROOT / "data/morning-edge.sqlite"
        evaluation = self.artifact("evaluation", lambda: update_evaluations(database, [self.directory / "enriched.json"]))
        from build_research_control_plane import build
        control = self.artifact("research-control", lambda: build(enriched, feature_database=ROOT / "data/research-control.sqlite", evaluation_database=database))
        if control["feature_record_count"] != len(run["watchlist"]):
            raise ValueError("research-control feature records are incomplete")
        from build_dashboard_bundle import attach_previous_publication
        publication = self.artifact("publication-input", lambda: attach_previous_publication(
            dict(enriched, model_evaluation=evaluation), ROOT / "dashboard-app"))
        self.remaining(1)
        assert_publishable(publication, observed_at=datetime.now(UTC))
        self.status("RUNNING", "publication")
        from build_dashboard_bundle import archive_daily_data, build_shell, publish_latest_data
        self.artifact("publication", lambda: {"archive": archive_daily_data(run=publication, app_root=ROOT / "dashboard-app"),
            "shell": build_shell(run=publication, app_root=ROOT / "dashboard-app"),
            "latest": publish_latest_data(run=publication, app_root=ROOT / "dashboard-app")})
        self.artifact("verification", lambda: self.verify_publication(run, batches))
        self.status("COMPLETE", "verification", "Forecasts, agent audits, archived publication, and loopback readback verified. Research only.")
        atomic_json(ROOT / "dashboard-app/data/pipeline-status.json", read_object(self.status_path))

    def register(self, run: dict) -> dict:
        cutoff = datetime.fromisoformat(run["cutoff_at"].replace("Z", "+00:00")).astimezone(ET)
        if cutoff.date().isoformat() != self.state["session_date"] or cutoff > datetime.now(ET):
            raise ValueError("prospective registration requires today's real capture cutoff")
        database = ROOT / "data/morning-edge.sqlite"
        result = register_run(database, run)
        return result | verify_forecasts(database, run) | {"verified_at": now_text()}

    def enriched(self, run: dict, batches: list[dict]) -> dict:
        result = enrich(run, [item["batch"] for item in batches], input_digest=self.state["artifacts"]["source"])
        result["agentic_analysis"].update(backend="headless_codex_exec", requested_model=self.config["model"],
            reasoning_effort=self.config["reasoning"], prompt_version=PROMPT_VERSION,
            audit_ids=[item["audit_id"] for item in batches], validation_only=self.validation)
        for entry in result["watchlist"]:
            batch = next(item for item in batches if entry["ticker"] in item["audit"]["tickers"])
            entry["agent_provenance"] = batch["audit"] | {"audit_id": batch["audit_id"]}
        if self.validation:
            result.update(mode="UNATTENDED_VALIDATION_ONLY", forecast_registration_allowed=False,
                          revision_scope="CONTEXT_ONLY_NO_NEW_FORECAST")
        return result

    def verify_publication(self, run: dict, batches: list[dict]) -> dict:
        self.remaining(1)
        app = ROOT / "dashboard-app"
        live = read_object(app / "data/live-status.json")
        data = read_object(app / "data/latest.json")
        if live["sha256"] != file_digest(app / "data/latest.json") or data["asOf"] != run["cutoff_at"]:
            raise ValueError("published data or cutoff does not match this run")
        if len(data["entries"]) != len(run["watchlist"]):
            raise ValueError("publication is missing tickers")
        expected_audits = {item["audit_id"] for item in batches}
        for entry in data["entries"]:
            if entry["analysis"]["provenance"]["audit_id"] not in expected_audits:
                raise ValueError("published analysis has no matching audit")
            detail = entry["detail"]
            if file_digest(app / detail["url"].removeprefix("./")) != detail["sha256"]:
                raise ValueError("detail artifact hash mismatch")
        with urlopen(self.config["dashboard_url"] + "data/latest.json", timeout=10) as response:
            content = response.read(128 * 1024 * 1024 + 1)
        if digest(content) != live["sha256"]:
            raise ValueError("loopback dashboard is not serving the verified publication")
        for batch in batches:
            verify_audit(self.audit_db, batch["audit"])
        forecast = verify_forecasts(ROOT / "data/morning-edge.sqlite", run)
        return {"verified_at": now_text(), "latest_sha256": live["sha256"], "audit_records": len(batches), **forecast}


def completed_production(directory: Path) -> dict | None:
    """A verified completed session is final even after a code/config update."""
    path = directory / "state.json"
    if not path.exists():
        return None
    state = read_object(path)
    if state.get("status") != "COMPLETE" or state.get("validation_only") is not False:
        return None
    if state.get("session_date") != datetime.now(ET).date().isoformat():
        raise ValueError("completed production state is not today's session")
    required = {"source", "forecast-registration", "enriched", "publication", "verification"}
    artifacts = state.get("artifacts", {})
    if not required.issubset(artifacts):
        raise ValueError("completed production state lacks required artifacts")
    tickers = []
    for name, expected in artifacts.items():
        artifact = directory / (name + ".json")
        if file_digest(artifact) != expected:
            raise ValueError("completed production artifact changed: " + name)
        if name.startswith("analysis-"):
            batch = read_object(artifact)
            if batch["audit"].get("status") != "VALIDATED":
                raise ValueError("completed production contains an unvalidated batch")
            verify_audit(ROOT / "data/agent-audit.sqlite", batch["audit"])
            tickers.extend(batch["audit"]["tickers"])
    source = read_object(directory / "source.json")
    if sorted(tickers) != sorted(entry["ticker"] for entry in source["watchlist"]) or len(tickers) != len(set(tickers)):
        raise ValueError("completed production has incomplete or duplicate ticker audits")
    if source["run_id"] != state.get("run_id"):
        raise ValueError("completed production run identity differs")
    verify_forecasts(ROOT / "data/morning-edge.sqlite", source)
    return {"status": "COMPLETE", "already_completed": True, "run_id": state["run_id"],
            "directory": str(directory), "completed_at": state["updated_at"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--audit-accepted", action="store_true")
    parser.add_argument("--validate-source", type=Path)
    parser.add_argument("--validation-directory", type=Path)
    parser.add_argument("--ticker-limit", type=int, choices=range(1, 15))
    parser.add_argument("--recover-capture", type=Path)
    args = parser.parse_args(argv)
    validation = args.validate_source is not None
    if not args.live or not args.audit_accepted:
        parser.error("network execution requires --live --audit-accepted")
    if validation != (args.validation_directory is not None) or (args.ticker_limit and not validation):
        parser.error("validation requires its own directory; ticker limits are validation-only")
    config = read_object(args.config)
    validate_config(config)
    if args.recover_capture and (validation or config.get("authorized_recovery_date") != datetime.now(ET).date().isoformat()):
        parser.error("saved-capture recovery requires today's recovery configuration and production mode")
    directory = args.validation_directory if validation else ROOT / "outputs/unattended" / datetime.now(ET).date().isoformat()
    if args.recover_capture:
        directory = directory.with_name(directory.name + "-recovered")
    ensure_private_directory(directory)
    with (directory.parent / ".unattended.lock").open("a") as lock:
        os.fchmod(lock.fileno(), 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('{"status":"ALREADY_RUNNING"}')
            return 0
        if not validation:
            # A separate same-day recovery may have completed after the primary
            # attempt failed. Preserve both histories and do not collect twice.
            candidates = [directory]
            if not args.recover_capture:
                candidates.append(directory.with_name(directory.name + "-recovered"))
            for candidate in candidates:
                completed = completed_production(candidate)
                if completed:
                    print(json.dumps(completed, sort_keys=True))
                    return 0
        runner = Runner(config, directory, validation=validation)
        if runner.state["status"] in SUCCESS:
            for name, expected in runner.state["artifacts"].items():
                if file_digest(directory / (name + ".json")) != expected:
                    runner.status("FAILED", "integrity", "A completed run artifact changed; no new requests were made.")
                    return 2
                if name.startswith("analysis-"):
                    verify_audit(runner.audit_db, read_object(directory / (name + ".json"))["audit"])
            print(json.dumps({"status": runner.state["status"], "already_completed": True}))
            return 0
        due = schedule_state(datetime.now(UTC), config["start_et"], config["deadline_et"])
        if not validation and due != "DUE":
            runner.status(due, "calendar", "No new capture or model call outside the configured morning window.")
            return 2 if due == "MISSED_DEADLINE" else 0
        try:
            runner.execute(args.validate_source, args.ticker_limit, args.recover_capture)
        except Exception as error:
            runner.status("FAILED", runner.state.get("stage", "unknown"), "Workflow incomplete. Error class: " + type(error).__name__)
            # Local exception text is private; provider exception text is sanitized by capture.
            attempt = runner.directory / ("failure-" + datetime.now(UTC).strftime("%H%M%S%f") + ".json")
            immutable_json(attempt, {"error_class": type(error).__name__, "message": str(error)[:1000], "at": now_text()})
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

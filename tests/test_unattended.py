from copy import deepcopy
from contextlib import closing
from datetime import datetime, UTC
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import run_unattended_morning as runner
import install_unattended_agent as installer
import build_enriched_morning_dashboard as dashboard
from morning_edge import unattended as audit

SPEC = importlib.util.spec_from_file_location("enrichment_test_helpers", ROOT / "tests/test_enrich_morning_run.py")
helpers = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helpers)


def config():
    return {"schema": "unattended-morning-config/v1", "root": str(ROOT), "python": sys.executable,
            "cli": "/opt/homebrew/bin/codex", "cli_sha256": "a" * 64, "cli_version": "test-cli",
            "model": "gpt-6-astra", "reasoning": "high", "batch_size": 2,
            "start_et": "06:45", "deadline_et": "08:00", "max_attempts": 2,
            "analyst_timeout_seconds": 360, "dashboard_url": "http://127.0.0.1:8765/"}


def source(count=2):
    value = helpers.source()
    entry = value["watchlist"][0]
    entry["field_source_snapshot_ids"] = {"technical": [10], "action": [11], "evidence.news": [10]}
    value.update(run_id="test-run", cutoff_at="2026-09-08T11:00:00Z", recommendations_enabled=False)
    value["watchlist"] = [dict(deepcopy(entry), ticker=f"S{index:02}") for index in range(count)]
    return value


def record(ticker):
    value = helpers.record()
    value["ticker"] = ticker
    value["evidence_points"][0]["field_refs"] = ["technical.ema20"]
    return value


def events(tickers, *, model=None, tool=False):
    sequence = [{"type": "thread.started", "thread_id": "test-thread"}, {"type": "turn.started"}]
    if model:
        sequence[0]["model"] = model
    if tool:
        sequence.append({"type": "item.completed", "item": {"type": "command_execution", "command": "false"}})
    sequence.extend([{"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(
        {"schema": "codex_agent_enrichment/v1", "records": [record(ticker) for ticker in tickers]})}},
        {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 100}}])
    return b"\n".join(json.dumps(item).encode() for item in sequence) + b"\n"


def fake_process(command, *, directory, cwd, timeout, environment, stdin):
    schema = json.loads(Path(command[command.index("--output-schema") + 1]).read_text())
    tickers = schema["properties"]["records"]["items"]["properties"]["ticker"]["enum"]
    audit.immutable_bytes(directory / "stdout.log", events(tickers))
    audit.immutable_bytes(directory / "stderr.log", b"")
    result = {"exit_code": 0, "timed_out": False, "logs_within_limit": True,
              "started_at": audit.now_text(), "finished_at": audit.now_text()}
    audit.immutable_json(directory / "process.json", result)
    return result


class AuditTests(unittest.TestCase):
    def test_immutable_idempotence_and_conflict(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "immutable.json"
            first = audit.immutable_json(path, {"v": 1})
            self.assertEqual(first, audit.immutable_json(path, {"v": 1}))
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            with self.assertRaisesRegex(ValueError, "conflict"):
                audit.immutable_json(path, {"v": 2})
            target = Path(temp) / "link.json"
            target.symlink_to(path)
            with self.assertRaises(ValueError):
                audit.immutable_json(target, {"v": 1})

    def test_audit_is_idempotent_append_only_and_read_back(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "audit.sqlite"
            row = {"attempt_id": "run/1", "run_id": "run", "requested_model": "gpt-6-astra", "status": "VALIDATED", "finished_at": audit.now_text()}
            key = audit.append_audit(database, row)
            self.assertEqual(key, audit.append_audit(database, row))
            audit.verify_audit(database, row)
            with self.assertRaises(ValueError):
                audit.append_audit(database, row | {"status": "FAILED"})
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM agent_audit").fetchone()[0])
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("DELETE FROM agent_audit")
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("UPDATE agent_audit SET status='FAILED'")

    def test_environment_excludes_credentials_and_overrides(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "not-a-real-key", "UNUSUAL_WHALES_API_KEY": "not-a-real-key",
                                     "OPENAI_BASE_URL": "https://example.invalid", "HTTPS_PROXY": "https://example.invalid"}):
            env = audit.analyst_environment()
        for key in ("OPENAI_API_KEY", "UNUSUAL_WHALES_API_KEY", "OPENAI_BASE_URL", "HTTPS_PROXY"):
            self.assertNotIn(key, env)

    def test_explicit_model_no_tools_no_approval(self):
        command = audit.analyst_command(Path("/bin/codex"), workspace=Path("/tmp/test"), schema=Path("/tmp/schema"), model="gpt-6-astra", reasoning="high")
        self.assertIn("gpt-6-astra", command)
        self.assertIn('approval_policy="never"', command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("read-only", command)
        self.assertIn('model_provider="screener_https"', command)
        self.assertIn('model_providers.screener_https.base_url="https://chatgpt.com/backend-api/codex"', command)
        self.assertIn('model_providers.screener_https.supports_websockets=false', command)
        self.assertIn('model_providers.screener_https.requires_openai_auth=true', command)
        self.assertIn('model_providers.screener_https.request_max_retries=2', command)
        self.assertNotIn("danger", " ".join(command).lower())
        self.assertNotIn("insecure", " ".join(command).lower())
        self.assertNotIn("ssl_verify=false", " ".join(command).lower())
        for item in audit.DISABLED_FEATURES:
            self.assertIn(item, command)

    def test_schema_path_remains_valid_after_changing_working_directory(self):
        command = audit.analyst_command(Path("/bin/codex"), workspace=Path("relative-workspace"),
            schema=Path("relative-schema.json"), model="gpt-6-astra", reasoning="high")
        self.assertTrue(Path(command[command.index("--output-schema") + 1]).is_absolute())
        self.assertTrue(Path(command[command.index("--cd") + 1]).is_absolute())

    def test_parser_keeps_unknown_identity_unknown(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events"
            audit.immutable_bytes(path, events(["QCOM"]))
            _, metadata = audit.parse_codex_events(path)
            self.assertIsNone(metadata["runtime_reported_model"])
            self.assertIsNone(metadata["resolved_model_revision"])
            self.assertEqual("REQUESTED_ONLY_RUNTIME_ID_UNAVAILABLE", metadata["model_identity_status"])

    def test_parser_rejects_tools_missing_completion_and_bad_json(self):
        cases = [events(["QCOM"], tool=True), b'{"type":"turn.failed"}\n', b'not-json\n',
                 b'{"type":"thread.started","thread_id":"test"}\n']
        with tempfile.TemporaryDirectory() as temp:
            for index, content in enumerate(cases):
                path = Path(temp) / str(index)
                audit.immutable_bytes(path, content)
                with self.assertRaises(ValueError):
                    audit.parse_codex_events(path)

    def test_only_exact_pre_turn_disabled_code_notice_is_nonfatal(self):
        sequence = events(["QCOM"]).splitlines()
        notice = json.dumps({"type": "item.completed", "item": {"type": "error", "message": audit.DISABLED_CODE_MODE_NOTICE}}).encode()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "allowed"
            audit.immutable_bytes(path, b"\n".join(sequence[:1] + [notice] + sequence[1:]))
            _, metadata = audit.parse_codex_events(path)
            self.assertEqual([audit.DISABLED_CODE_MODE_NOTICE], metadata["startup_notices"])
            for index, invalid in enumerate([sequence[:2] + [notice] + sequence[2:],
                sequence[:1] + [notice.replace(b"host is disabled", b"host failed")] + sequence[1:]]):
                other = Path(temp) / str(index)
                audit.immutable_bytes(other, b"\n".join(invalid))
                with self.assertRaises(ValueError):
                    audit.parse_codex_events(other)

    def test_projection_retains_paths_and_declares_truncation(self):
        run = source()
        run["watchlist"][0]["technical"]["levels"] = list(range(20))
        packet = audit.evidence_packet(run, ["S00"], "a" * 64)
        self.assertEqual(list(range(8)), packet["watchlist"][0]["technical"]["levels"])
        self.assertTrue(packet["projection"]["truncated_lists"])
        self.assertNotIn("agent_enrichment", packet["watchlist"][0])

    def test_process_timeout_stops_and_preserves_logs(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "process"
            result = audit.bounded_process([sys.executable, "-c", "import time; time.sleep(20)"],
                directory=directory, cwd=Path(temp), timeout=0.05, environment=audit.analyst_environment())
            self.assertTrue(result["timed_out"])
            self.assertNotEqual(0, result["exit_code"])
            self.assertTrue((directory / "process.json").exists())

    def test_sleep_consumes_process_allowance_even_if_monotonic_clock_pauses(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(audit.time, "time", side_effect=[100.0, 160.0, 160.0]), \
             patch.object(audit.time, "monotonic", return_value=10.0):
            result = audit.bounded_process([sys.executable, "-c", "import time; time.sleep(20)"],
                directory=Path(temp) / "process", cwd=Path(temp), timeout=30,
                environment=audit.analyst_environment())
            self.assertTrue(result["timed_out"])
            self.assertEqual(60, result["wall_elapsed_seconds"])
            self.assertNotEqual(0, result["exit_code"])

    def test_process_retains_stdin_and_logs_across_polling(self):
        with tempfile.TemporaryDirectory() as temp:
            result = audit.bounded_process([sys.executable, "-c",
                "import sys,time; value=sys.stdin.read(); time.sleep(1.1); print(value)"],
                directory=Path(temp) / "process", cwd=Path(temp), timeout=5,
                environment=audit.analyst_environment(), stdin=b"synthetic evidence")
            self.assertFalse(result["timed_out"])
            self.assertEqual(0, result["exit_code"])
            self.assertEqual("synthetic evidence\n", (Path(temp) / "process/stdout.log").read_text())


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.quiet = patch("builtins.print")
        self.quiet.start()
        self.addCleanup(self.quiet.stop)

    def test_market_calendar_start_deadline_and_dst(self):
        cases = [("2026-09-07T11:00:00+00:00", "MARKET_CLOSED"),
                 ("2026-09-08T10:44:00+00:00", "WAITING_FOR_START"),
                 ("2026-09-08T10:45:00+00:00", "DUE"),
                 ("2026-09-08T12:00:00+00:00", "MISSED_DEADLINE"),
                 ("2026-11-03T11:45:00+00:00", "DUE")]
        for timestamp, expected in cases:
            self.assertEqual(expected, runner.schedule_state(datetime.fromisoformat(timestamp), "06:45", "08:00"))

    def test_config_rejects_implicit_model_unbounded_retry_and_external_readback(self):
        runner.validate_config(config())
        for changed in ({"model": "default"}, {"max_attempts": 20}, {"deadline_et": "10:00"},
                        {"dashboard_url": "https://example.invalid/"}, {"analyst_timeout_seconds": 9999}):
            with self.assertRaises(ValueError):
                runner.validate_config(config() | changed)

    def test_cli_pin_fails_before_auth_or_model_request(self):
        with patch.object(runner, "file_digest", return_value="b" * 64), patch.object(runner.subprocess, "run") as execute:
            with self.assertRaises(ValueError):
                runner.cli_preflight(config())
            execute.assert_not_called()

    def test_recovery_config_is_date_bound(self):
        runner.validate_config(config() | {"authorized_recovery_date": datetime.now(runner.ET).date().isoformat(), "deadline_et": "09:20"})
        with self.assertRaisesRegex(ValueError, "recovery configuration expired"):
            runner.validate_config(config() | {"authorized_recovery_date": "2000-01-01"})

    def test_saved_capture_recovery_requires_health_failure_and_preserves_forecasts(self):
        today = datetime.now(runner.ET).date().isoformat()
        with tempfile.TemporaryDirectory() as temp, patch.object(runner, "ROOT", Path(temp).resolve()), \
             patch("morning_edge.store.SnapshotStore"), \
             patch("morning_edge.operational_context.attach_operational_context", side_effect=lambda run, **kw: deepcopy(run)) as attach, \
             patch.object(runner.Runner, "capture") as capture:
            primary = Path(temp) / "outputs/unattended" / today
            attempt = primary / "attempts/capture/1"
            state = {"status": "FAILED", "stage": "capture", "artifacts": {}, "source_code_sha256": "a" * 64}
            audit.atomic_json(primary / "state.json", state)
            run = source()
            run["cutoff_at"] = datetime.now(runner.ET).isoformat()
            audit.immutable_json(attempt / "morning-run.json", run)
            audit.immutable_json(attempt / "morning-run-company-context.json", {"results": []})
            log = attempt / "process/stdout.log"
            log_hash = audit.immutable_bytes(log, b'{"status":"FAILED","stage":"health"}\n')
            audit.atomic_json(attempt / "process/process.json", {"stdout_sha256": log_hash})
            recovery_config = config() | {"authorized_recovery_date": today}
            result = runner.recover_health_stage(recovery_config, attempt)
            self.assertEqual(run["watchlist"], result["watchlist"])
            self.assertEqual(run["cutoff_at"], result["cutoff_at"])
            self.assertFalse(result["capture_recovery"]["numerical_forecasts_recomputed"])
            self.assertEqual({"snapshots", "company_results"}, set(attach.call_args.kwargs))
            capture.assert_not_called()
            for invalid_config, invalid_path in [(config(), attempt), (recovery_config, attempt.parent / "3")]:
                with self.assertRaises(ValueError):
                    runner.recover_health_stage(invalid_config, invalid_path)
            audit.atomic_json(primary / "state.json", state | {"artifacts": {"source": "a" * 64}})
            with self.assertRaisesRegex(ValueError, "pre-registration"):
                runner.recover_health_stage(recovery_config, attempt)
            audit.atomic_json(primary / "state.json", state)
            audit.atomic_json(attempt / "process/process.json", {"stdout_sha256": "b" * 64})
            with self.assertRaisesRegex(ValueError, "diagnostic changed"):
                runner.recover_health_stage(recovery_config, attempt)
            with patch.object(runner, "file_digest", return_value="b" * 64), \
                 patch.object(Path, "read_text", return_value='{"status":"FAILED","stage":"collection"}'):
                with self.assertRaisesRegex(ValueError, "collection failures"):
                    runner.recover_health_stage(recovery_config, attempt)

    def test_dashboard_logging_failure_does_not_abort_http_response(self):
        import serve_dashboard
        handler = object.__new__(serve_dashboard.DashboardHandler)
        for error in (BrokenPipeError("closed terminal"), ValueError("closed stream")):
            with patch.object(serve_dashboard.SimpleHTTPRequestHandler, "log_message", side_effect=error):
                handler.log_message("%s", "test")

    def test_full_validation_persists_14_analyses_and_resume_does_not_repeat_model_calls(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(runner, "cli_preflight", return_value={"cli_sha256": "a" * 64}), \
             patch.object(runner, "run_health", return_value={"failures": []}), \
             patch.object(runner, "bounded_process", side_effect=fake_process) as model, \
             patch.object(runner, "register_run") as register:
            root = Path(temp)
            source_path = root / "source.json"
            audit.immutable_json(source_path, source(14))
            instance = runner.Runner(config(), root / "validation", validation=True)
            instance.execute(source_path, None)
            self.assertEqual("VALIDATION_COMPLETE", instance.state["status"])
            self.assertEqual(7, model.call_count)
            register.assert_not_called()
            enriched = audit.read_object(instance.directory / "enriched.json")
            self.assertEqual(14, len(enriched["watchlist"]))
            self.assertFalse(enriched["forecast_registration_allowed"])
            self.assertTrue(all(item["agent_provenance"]["requested_model"] == "gpt-6-astra" for item in enriched["watchlist"]))
            installer.verify_validation(config(), instance.directory)
            resumed = runner.Runner(config(), instance.directory, validation=True)
            resumed.execute(source_path, None)
            self.assertEqual(7, model.call_count)

    def test_production_orchestration_publishes_and_checks_http_without_real_network(self):
        import build_research_control_plane
        real_template = (ROOT / "prompts/morning-analyst-v2.md").read_bytes()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audit.immutable_bytes(root / "prompts/morning-analyst-v2.md", real_template)
            run = source(14)
            run["cutoff_at"] = datetime.now(runner.ET).replace(hour=7, minute=0, second=0, microsecond=0).isoformat()
            run["generated_at"] = run["cutoff_at"]
            with patch.object(runner, "ROOT", root), \
                 patch.object(runner, "cli_preflight", return_value={"cli_sha256": "a" * 64}), \
                 patch.object(runner, "run_health", return_value={"failures": []}), \
                 patch.object(runner, "assert_publishable", return_value={"failures": []}), \
                 patch.object(runner.Runner, "remaining", return_value=360), \
                 patch.object(runner.Runner, "capture", return_value=run), \
                 patch.object(runner, "bounded_process", side_effect=fake_process) as model, \
                 patch.object(runner, "register_run", return_value={"registered": 56}) as register, \
                 patch.object(runner, "verify_forecasts", return_value={"verified_forecasts": 56}), \
                 patch.object(runner, "update_evaluations", return_value={}), \
                 patch.object(build_research_control_plane, "build", return_value={"feature_record_count": 14}), \
                 patch.object(runner, "urlopen", side_effect=lambda *a, **k: io.BytesIO((root / "dashboard-app/data/latest.json").read_bytes())):
                instance = runner.Runner(config(), root / "outputs/run", validation=False)
                instance.execute(None, None)
                self.assertEqual("COMPLETE", instance.state["status"])
                self.assertEqual(1, register.call_count)
                self.assertEqual(7, model.call_count)
                published = audit.read_object(root / "dashboard-app/data/latest.json")
                self.assertEqual(14, len(published["entries"]))
                self.assertTrue(all(entry["analysis"]["provenance"]["requested_model"] == "gpt-6-astra" for entry in published["entries"]))
                self.assertTrue((instance.directory / "verification.json").exists())
                # Completion survives a later configuration/code update without
                # reusing an unfinished state or making another model request.
                completed = runner.completed_production(instance.directory)
                self.assertEqual("COMPLETE", completed["status"])
                self.assertEqual(run["run_id"], completed["run_id"])
                self.assertEqual(7, model.call_count)
                with patch.object(runner, "file_digest", return_value="changed"):
                    with self.assertRaisesRegex(ValueError, "artifact changed"):
                        runner.completed_production(instance.directory)

    def test_main_recognizes_completed_recovery_before_loading_failed_primary(self):
        today = datetime.now(runner.ET).date().isoformat()
        with tempfile.TemporaryDirectory() as temp, patch.object(runner, "ROOT", Path(temp).resolve()), \
             patch.object(runner, "validate_config"), \
             patch.object(runner, "completed_production", side_effect=[None, {"status": "COMPLETE"}]) as completed, \
             patch.object(runner, "Runner") as construct:
            config_path = Path(temp) / "config.json"
            audit.immutable_json(config_path, config())
            self.assertEqual(0, runner.main(["--config", str(config_path), "--live", "--audit-accepted"]))
            self.assertEqual(today, completed.call_args_list[0].args[0].name)
            self.assertEqual(today + "-recovered", completed.call_args_list[1].args[0].name)
            construct.assert_not_called()

    def test_incomplete_or_forged_completion_cannot_suppress_a_run(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self.assertIsNone(runner.completed_production(directory))
            audit.atomic_json(directory / "state.json", {"status": "FAILED"})
            self.assertIsNone(runner.completed_production(directory))
            audit.atomic_json(directory / "state.json", {"status": "COMPLETE", "validation_only": False,
                "session_date": datetime.now(runner.ET).date().isoformat(), "artifacts": {}})
            with self.assertRaisesRegex(ValueError, "required artifacts"):
                runner.completed_production(directory)

    def test_invalid_evidence_fails_before_publication(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(runner, "cli_preflight", return_value={}), \
             patch.object(runner, "run_health", return_value={"failures": ["stale"]}), \
             patch.object(runner, "bounded_process") as model, patch.object(runner, "register_run") as register:
            root = Path(temp)
            path = root / "source.json"
            audit.immutable_json(path, source())
            instance = runner.Runner(config(), root / "run", validation=True)
            with self.assertRaisesRegex(ValueError, "evidence-health"):
                instance.execute(path, None)
            model.assert_not_called()
            register.assert_not_called()
            self.assertFalse((instance.directory / "verification.json").exists())

    def test_audited_output_recovers_without_duplicate_model_request(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(runner, "bounded_process", side_effect=fake_process) as model:
            instance = runner.Runner(config(), Path(temp) / "run", validation=True)
            run = instance.artifact("source", lambda: source(2))
            first = instance.analyze(run, ["S00", "S01"], "analysis-S00-S01", {"cli_sha256": "a" * 64})
            second = instance.analyze(run, ["S00", "S01"], "analysis-S00-S01", {"cli_sha256": "a" * 64})
            self.assertEqual(first, second)
            self.assertEqual(1, model.call_count)

    def test_failed_tool_attempt_is_recorded_and_retries_are_bounded(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(runner, "bounded_process", side_effect=fake_process), \
             patch.object(runner, "parse_codex_events", side_effect=ValueError("tool event")):
            instance = runner.Runner(config(), Path(temp) / "run", validation=True)
            run = instance.artifact("source", lambda: source(2))
            for _ in range(2):
                with self.assertRaises(ValueError):
                    instance.analyze(run, ["S00", "S01"], "analysis-S00-S01", {})
            with self.assertRaisesRegex(ValueError, "attempt limit"):
                instance.analyze(run, ["S00", "S01"], "analysis-S00-S01", {})
            with closing(sqlite3.connect(instance.audit_db)) as connection:
                self.assertEqual(2, connection.execute("SELECT COUNT(*) FROM agent_audit WHERE status='FAILED'").fetchone()[0])

    def test_mismatched_runtime_model_fails(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(runner, "bounded_process", side_effect=fake_process), \
             patch.object(runner, "parse_codex_events", return_value=({}, {"runtime_reported_model": "another-model"})):
            instance = runner.Runner(config(), Path(temp) / "run", validation=True)
            run = instance.artifact("source", lambda: source(2))
            with self.assertRaisesRegex(ValueError, "model differs"):
                instance.analyze(run, ["S00", "S01"], "analysis-S00-S01", {})

    def test_changed_checkpoint_and_config_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "run"
            instance = runner.Runner(config(), directory, validation=True)
            instance.artifact("source", lambda: source(2))
            with self.assertRaises(ValueError):
                runner.Runner(config() | {"batch_size": 1}, directory, validation=True)
            audit.atomic_json(directory / "source.json", {"changed": True})
            with self.assertRaisesRegex(ValueError, "artifact changed"):
                instance.artifact("source", lambda: {})

    def test_installer_refuses_smoke_test(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(runner, "cli_preflight", return_value={"cli_sha256": "a" * 64}), \
             patch.object(runner, "run_health", return_value={"failures": []}), patch.object(runner, "bounded_process", side_effect=fake_process):
            root = Path(temp)
            path = root / "source.json"
            audit.immutable_json(path, source(14))
            instance = runner.Runner(config(), root / "run", validation=True)
            instance.execute(path, 2)
            with self.assertRaisesRegex(ValueError, "full-watchlist"):
                installer.verify_validation(config(), instance.directory)

    def test_plist_is_bounded_and_contains_no_credentials(self):
        value = installer.plist(config(), Path("/tmp/config"), Path("/tmp/logs"))
        self.assertNotIn("KeepAlive", value)
        self.assertEqual(300, value["StartInterval"])
        self.assertEqual([{"Weekday": day, "Hour": 6, "Minute": 30} for day in range(1, 6)], value["StartCalendarInterval"])
        self.assertIn("-is", value["ProgramArguments"])
        self.assertTrue(value["ProgramArguments"][3].endswith("run_morning_service.py"))
        self.assertIn("--audit-accepted", value["ProgramArguments"])
        self.assertNotIn("API_KEY", json.dumps(value))
        server = installer.server_plist(config(), Path("/tmp/logs"))
        self.assertIn("127.0.0.1", server["ProgramArguments"])
        self.assertNotIn("--live", server["ProgramArguments"])
        validation = installer.validation_plist(config(), Path("config"), Path("source"), Path("validation"))
        self.assertNotIn("StartInterval", validation)
        self.assertNotIn("StartCalendarInterval", validation)
        self.assertTrue(validation["ProgramArguments"][3].endswith("run_unattended_morning.py"))
        self.assertNotIn("KeepAlive", validation)
        self.assertIn("--validate-source", validation["ProgramArguments"])
        self.assertTrue(Path(validation["ProgramArguments"][-1]).is_absolute())

    def test_installer_requires_completed_launchd_context(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "validation.plist"
            digest = audit.immutable_bytes(path, b"test")
            audit.immutable_json(root / "launchd-dispatch.json", {"label": installer.VALIDATION_LABEL,
                "plist": str(path), "plist_sha256": digest})
            for output in ("state = running\nlast exit code = 0", "state = not running\nlast exit code = 2"):
                with patch.object(installer.subprocess, "run", return_value=type("Result", (), {"stdout": output})()):
                    with self.assertRaises(ValueError):
                        installer.verify_launchd_context(root)

    def test_upgrade_refuses_foreign_or_linked_job(self):
        import plistlib
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / (installer.LABEL + ".plist")
            value = installer.plist(config(), Path("config"), Path("logs"))
            target.write_bytes(plistlib.dumps(value))
            self.assertEqual(target.read_bytes(), installer.verify_owned_plist(target, config()))
            value["WorkingDirectory"] = "/another/operator"
            target.write_bytes(plistlib.dumps(value))
            with self.assertRaisesRegex(ValueError, "another operator"):
                installer.verify_owned_plist(target, config())
            other = Path(temp) / "linked.plist"
            other.write_bytes(target.read_bytes())
            target.unlink()
            target.symlink_to(other)
            with self.assertRaisesRegex(ValueError, "linked"):
                installer.verify_owned_plist(target, config())

    def test_legacy_identity_remains_unknown_and_trusted_identity_is_visible(self):
        entry = source(1)["watchlist"][0]
        entry.update(agent_enrichment_validated=True, agent_enrichment=record(entry["ticker"]))
        self.assertEqual("UNKNOWN_NOT_RECORDED", dashboard._entry(entry)["analysis"]["provenance"]["model_identity_status"])
        entry["agent_provenance"] = {"requested_model": "gpt-6-astra", "runtime_reported_model": None, "audit_id": "a" * 64}
        displayed = dashboard._entry(entry)["analysis"]["provenance"]
        self.assertEqual("gpt-6-astra", displayed["requested_model"])
        self.assertIsNone(displayed["runtime_reported_model"])


if __name__ == "__main__":
    unittest.main()

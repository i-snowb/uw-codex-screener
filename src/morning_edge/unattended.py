"""Bounded headless analyst execution and owner-private, immutable audit records."""

from __future__ import annotations

from datetime import datetime, UTC
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import tempfile
import time
from typing import Any

from .private_io import ensure_private_directory, harden_sqlite_files, write_private_bytes

AUDIT_SCHEMA = "codex-screener-agent-audit/v1"
PROMPT_VERSION = "morning-evidence-synthesis-v2"
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
ANALYST_PROVIDER = "screener_https"
ANALYST_ENDPOINT = "https://chatgpt.com/backend-api/codex"
ANALYST_TRANSPORT = "HTTPS_SSE"
DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "apps", "plugins", "hooks", "remote_plugin",
    "browser_use", "browser_use_external", "browser_use_full_cdp_access",
    "in_app_browser", "computer_use", "image_generation", "view_image",
    "multi_agent", "multi_agent_v2", "memories", "skill_search",
    "workspace_dependencies", "goals", "code_mode_host",
    "sleep_tool", "unbounded_connection_retries", "auth_elicitation",
    "tool_suggest", "skill_mcp_dependency_install",
)
DISABLED_CODE_MODE_NOTICE = (
    "Code Mode is unavailable because code-mode host is disabled. Code mode will fail closed; "
    "enable `features.code_mode_host` and install `codex-code-mode-host`."
)


def now_text() -> str:
    return datetime.now(UTC).isoformat()


def encoded(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def file_digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def read_object(path: Path, *, limit: int = 128 * 1024 * 1024) -> dict:
    if path.is_symlink() or path.stat().st_size > limit:
        raise ValueError("unsafe or oversized JSON artifact")
    value = json.loads(path.read_bytes(), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON")))
    if not isinstance(value, dict):
        raise ValueError("JSON artifact must be an object")
    return value


def immutable_bytes(path: Path, content: bytes) -> str:
    ensure_private_directory(path.parent)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != content:
            raise ValueError("immutable artifact conflict: " + path.name)
        return digest(content)
    descriptor, temporary = tempfile.mkstemp(prefix=".immutable-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != content:
                raise ValueError("immutable artifact conflict: " + path.name)
        parent_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        os.unlink(temporary)
    return digest(content)


def immutable_json(path: Path, value: Any) -> str:
    return immutable_bytes(path, encoded(value))


def atomic_json(path: Path, value: Any) -> None:
    write_private_bytes(path, encoded(value))


def analyst_environment() -> dict[str, str]:
    # Do not pass market-provider keys, alternate API endpoints, or app thread state.
    names = ("HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "CODEX_HOME")
    result = {key: os.environ[key] for key in names if key in os.environ}
    result["PATH"] = "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    result["NO_COLOR"] = "1"
    return result


def analyst_command(cli: Path, *, workspace: Path, schema: Path, model: str, reasoning: str) -> list[str]:
    command = [str(cli), "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
               "--sandbox", "read-only", "--json", "--color", "never", "--cd", str(workspace.resolve()),
               "--model", model, "--output-schema", str(schema.resolve()),
               "-c", 'approval_policy="never"', "-c", 'web_search="disabled"',
               "-c", 'project_doc_max_bytes=0',
               "-c", 'model_reasoning_effort=' + json.dumps(reasoning),
               "-c", 'model_provider=' + json.dumps(ANALYST_PROVIDER),
               "-c", 'model_providers.screener_https.name="OpenAI ChatGPT HTTPS"',
               "-c", 'model_providers.screener_https.base_url=' + json.dumps(ANALYST_ENDPOINT),
               "-c", 'model_providers.screener_https.wire_api="responses"',
               "-c", 'model_providers.screener_https.requires_openai_auth=true',
               "-c", 'model_providers.screener_https.supports_websockets=false',
               "-c", 'model_providers.screener_https.request_max_retries=2',
               "-c", 'model_providers.screener_https.stream_max_retries=2']
    for feature in DISABLED_FEATURES:
        command += ["--disable", feature]
    return command + ["-"]


def bounded_process(command: list[str], *, directory: Path, cwd: Path, timeout: float,
                    environment: dict[str, str], stdin: bytes = b"") -> dict:
    """A process success is not a workflow success. Preserve both logs privately."""
    ensure_private_directory(directory)
    started = now_text()
    stdout_path, stderr_path = directory / "stdout.log", directory / "stderr.log"
    timed_out = False
    wall_started, monotonic_started = time.time(), time.monotonic()
    with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
        os.fchmod(stdout.fileno(), 0o600)
        os.fchmod(stderr.fileno(), 0o600)
        process = subprocess.Popen(command, cwd=cwd, env=environment, stdin=subprocess.PIPE,
                                   stdout=stdout, stderr=stderr, start_new_session=True)
        pending_input = stdin
        try:
            while True:
                # Darwin's monotonic clock pauses during sleep. Wall time must
                # also consume the allowance so waking cannot resume expired work.
                elapsed = max(time.time() - wall_started, time.monotonic() - monotonic_started)
                remaining = timeout - elapsed
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    process.communicate(pending_input, timeout=min(1.0, remaining))
                    timed_out = time.time() - wall_started >= timeout
                    break
                except subprocess.TimeoutExpired:
                    pending_input = None
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
            if process.stdin is not None:
                process.stdin.close()
    result = {"started_at": started, "finished_at": now_text(), "exit_code": process.returncode,
              "timed_out": timed_out, "timeout_seconds": timeout,
              "timeout_clock": "WALL_AND_MONOTONIC", "wall_elapsed_seconds": round(time.time() - wall_started, 3),
              "stdout_sha256": file_digest(stdout_path),
              "stderr_sha256": file_digest(stderr_path),
              "logs_within_limit": max(stdout_path.stat().st_size, stderr_path.stat().st_size) <= MAX_ARTIFACT_BYTES}
    immutable_json(directory / "process.json", result)
    return result


def parse_codex_events(path: Path) -> tuple[dict, dict]:
    if path.stat().st_size > MAX_ARTIFACT_BYTES:
        raise ValueError("analyst event stream exceeds limit")
    messages, usage, thread_id, runtime_model = [], {}, None, None
    completed, turn_started = False, False
    startup_notices = []
    for line in path.read_text().splitlines():
        event = json.loads(line)
        kind = event.get("type")
        if kind in {"error", "turn.failed"}:
            raise ValueError("analyst runtime reported failure")
        if kind == "thread.started":
            thread_id = event.get("thread_id")
        if kind == "turn.started":
            turn_started = True
        if kind in {"thread.started", "turn.started"}:
            candidate = event.get("model")
            if isinstance(candidate, str) and candidate:
                runtime_model = candidate
        if kind == "turn.completed":
            completed = True
            usage = event.get("usage", {})
        if kind in {"item.started", "item.completed", "item.updated"}:
            item = event.get("item", {})
            # This exact pre-turn notice confirms our deliberate no-code policy.
            # Runtime/tool errors, changed wording, and in-turn notices still fail.
            if (kind == "item.completed" and not turn_started and thread_id
                    and item.get("type") == "error" and item.get("message") == DISABLED_CODE_MODE_NOTICE):
                startup_notices.append(item["message"])
                continue
            if item.get("type") not in {"agent_message", "reasoning"}:
                raise ValueError("analyst attempted a tool or unapproved item type")
            if kind == "item.completed" and item.get("type") == "agent_message":
                messages.append(item.get("text", ""))
    if not completed or not thread_id or not messages:
        raise ValueError("analyst did not finish with a traceable final message")
    result = json.loads(messages[-1])
    if not isinstance(result, dict):
        raise ValueError("analyst final message is not an object")
    return result, {"thread_id": thread_id, "usage": usage, "startup_notices": startup_notices, "runtime_reported_model": runtime_model,
                    "resolved_model_revision": None,
                    "model_identity_status": "RUNTIME_REPORTED" if runtime_model else "REQUESTED_ONLY_RUNTIME_ID_UNAVAILABLE"}


def append_audit(database: Path, record: dict) -> str:
    """Audit metadata is supplied by the runner, never by model-generated JSON."""
    payload = encoded(record)
    key = digest(payload)
    ensure_private_directory(database.parent)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS agent_audit (
                audit_id TEXT PRIMARY KEY, attempt_id TEXT UNIQUE NOT NULL,
                run_id TEXT NOT NULL, requested_model TEXT NOT NULL,
                status TEXT NOT NULL, inserted_at TEXT NOT NULL, payload_json TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS agent_audit_no_update BEFORE UPDATE ON agent_audit
            BEGIN SELECT RAISE(ABORT, 'agent audit is immutable'); END;
            CREATE TRIGGER IF NOT EXISTS agent_audit_no_delete BEFORE DELETE ON agent_audit
            BEGIN SELECT RAISE(ABORT, 'agent audit is immutable'); END;
        """)
        existing = connection.execute("SELECT audit_id FROM agent_audit WHERE attempt_id=?", (record["attempt_id"],)).fetchone()
        if existing and existing[0] != key:
            raise ValueError("agent audit attempt conflict")
        connection.execute("INSERT OR IGNORE INTO agent_audit VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (key, record["attempt_id"], record["run_id"], record["requested_model"],
                            record["status"], record["finished_at"], payload.decode()))
    harden_sqlite_files(database)
    return key


def verify_audit(database: Path, record: dict) -> None:
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        row = connection.execute("SELECT payload_json FROM agent_audit WHERE audit_id=?", (digest(encoded(record)),)).fetchone()
    if not row or row[0].encode() != encoded(record):
        raise ValueError("persisted analyst audit does not match")


def source_code_digest(root: Path) -> str:
    paths = sorted([*root.glob("src/**/*.py"), *root.glob("scripts/*.py"), *root.glob("prompts/*")])
    return digest(encoded({path.relative_to(root).as_posix(): file_digest(path) for path in paths if path.is_file()}))


def response_schema(tickers: list[str]) -> dict:
    def obj(fields):
        return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}
    def text_array(minimum=1, maximum=4):
        return {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 500}, "minItems": minimum, "maxItems": maximum}
    point = obj({"statement": {"type": "string", "maxLength": 500}, "field_refs": text_array(1, 8),
                 "source_snapshot_ids": {"type": "array", "items": {"type": "integer"}, "minItems": 1, "maxItems": 8}})
    point["properties"]["field_refs"]["items"]["pattern"] = (
        "^(ticker|action|price|technical|edge|whale_evidence|trade_thesis|benchmark_context|"
        "freshness_contract|data_quality|company_reference|relative_regime|coverage_status|gates)(\\.|$)"
    )
    scenario = obj({"name": {"type": "string", "enum": ["BULL", "BASE", "BEAR"]},
                    "conditions": text_array(), "outcome": {"type": "string", "maxLength": 500}, "invalidation": text_array()})
    record = obj({"ticker": {"type": "string", "enum": tickers}, "action": {"type": "string", "enum": ["NO_RECOMMENDATION"]},
                  "posture": {"type": "string", "enum": ["BULLISH_TREND", "BEARISH_TREND", "MIXED", "NEUTRAL"]},
                  "research_priority": {"type": "integer", "minimum": 0, "maximum": 100},
                  "evidence_confidence": {"type": "integer", "minimum": 0, "maximum": 100},
                  "summary": {"type": "string", "maxLength": 400}, "day_outlook": {"type": "string", "maxLength": 350},
                  "evidence_points": {"type": "array", "items": point, "minItems": 2, "maxItems": 5},
                  "counterevidence": text_array(2, 4), "unknowns": text_array(),
                  "option_context": {"type": "string", "maxLength": 500},
                  "scenarios": {"type": "array", "items": scenario, "minItems": 3, "maxItems": 3}})
    return obj({"schema": {"type": "string", "enum": ["codex_agent_enrichment/v1"]},
                "records": {"type": "array", "items": record, "minItems": len(tickers), "maxItems": len(tickers)}})


def evidence_packet(run: dict, tickers: list[str], source_hash: str) -> dict:
    """Preserve original field paths and values; list prefixes retain their indices."""
    omitted = []
    def project(value, path):
        if isinstance(value, dict):
            return {key: project(item, path + "." + key) for key, item in value.items()
                    if key not in {"bars", "history", "raw", "raw_payload", "agent_enrichment", "agent_analysis", "analyst", "price_only_trial"}}
        if isinstance(value, list):
            if len(value) > 8:
                omitted.append({"path": path, "available": len(value), "included_prefix": 8})
            return [project(item, path + f"[{index}]") for index, item in enumerate(value[:8])]
        return value
    entries = []
    selected = {"ticker", "action", "price", "technical", "edge", "whale_evidence", "trade_thesis", "benchmark_context",
                "freshness_contract", "data_quality", "company_reference", "relative_regime", "coverage_status", "gates"}
    for entry in run["watchlist"]:
        if entry["ticker"] not in tickers:
            continue
        result = {key: project(value, entry["ticker"] + "." + key) for key, value in entry.items() if key in selected}
        result["field_source_snapshot_ids"] = {key: values[-8:] for key, values in entry.get("field_source_snapshot_ids", {}).items()}
        entries.append(result)
    packet = {"schema": "bounded-morning-evidence/v1", "source_sha256": source_hash,
              "run_id": run["run_id"], "cutoff_at": run["cutoff_at"], "watchlist": entries,
              "data_health": run.get("data_health"), "operational_context": project(run.get("operational_context", {}), "operational_context"),
              "projection": {"list_limit": 8, "omitted_keys": ["bars", "history", "raw", "raw_payload", "agent_enrichment", "agent_analysis", "analyst", "price_only_trial"],
                             "truncated_lists": omitted, "rule": "Only cite fields and mapped snapshot IDs included here. Omitted evidence is not negative evidence."}}
    if len(encoded(packet)) > 700_000:
        raise ValueError("evidence packet exceeds bounded analyst input")
    return packet

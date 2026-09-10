#!/usr/bin/env python3
"""Generate or explicitly install the morning LaunchAgent after full validation."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import plistlib
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from morning_edge.unattended import encoded, digest, immutable_bytes, read_object, source_code_digest, verify_audit
from run_unattended_morning import validate_config, cli_preflight

LABEL = "com.codex-screener.morning"
SERVER_LABEL = "com.codex-screener.dashboard"
VALIDATION_LABEL = "com.codex-screener.validation"


def validation_plist(config: dict, config_path: Path, source: Path, directory: Path) -> dict:
    value = plist(config, config_path, directory.resolve())
    value["Label"] = VALIDATION_LABEL
    value.pop("StartInterval")
    value["ProgramArguments"] += ["--validate-source", str(source.resolve()),
                                   "--validation-directory", str(directory.resolve())]
    return value


def verify_launchd_context(directory: Path) -> dict:
    receipt = read_object(directory / "launchd-dispatch.json")
    from morning_edge.unattended import file_digest
    path = Path(receipt["plist"])
    if receipt["label"] != VALIDATION_LABEL or file_digest(path) != receipt["plist_sha256"]:
        raise ValueError("launchd validation dispatch changed")
    result = subprocess.run(["/bin/launchctl", "print", f"gui/{os.getuid()}/{VALIDATION_LABEL}"],
                            capture_output=True, text=True, check=True, timeout=20)
    if "last exit code = 0" not in result.stdout or "state = not running" not in result.stdout:
        raise ValueError("launchd validation did not finish successfully")
    return {"label": VALIDATION_LABEL, "launchctl_sha256": digest(result.stdout.encode()),
            "dispatch": receipt}


def plist(config: dict, config_path: Path, log_root: Path) -> dict:
    return {"Label": LABEL,
            "ProgramArguments": ["/usr/bin/caffeinate", "-is", config["python"], str(ROOT / "scripts/run_unattended_morning.py"),
                                 "--config", str(config_path.resolve()), "--live", "--audit-accepted"],
            "WorkingDirectory": str(ROOT), "StartInterval": 300, "RunAtLoad": True,
            "ProcessType": "Background", "LowPriorityIO": True, "Umask": 0o077,
            "ExitTimeOut": 20, "ThrottleInterval": 60,
            "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONDONTWRITEBYTECODE": "1"},
            "StandardOutPath": str(log_root / "launchd.stdout.log"), "StandardErrorPath": str(log_root / "launchd.stderr.log")}


def server_plist(config: dict, log_root: Path) -> dict:
    return {"Label": SERVER_LABEL, "ProgramArguments": [config["python"], str(ROOT / "scripts/serve_dashboard.py"),
            "--root", str(ROOT / "dashboard-app"), "--host", "127.0.0.1", "--port", "8765"],
            "WorkingDirectory": str(ROOT), "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 60, "ExitTimeOut": 20, "Umask": 0o077, "ProcessType": "Background",
            "EnvironmentVariables": {"PYTHONDONTWRITEBYTECODE": "1"},
            "StandardOutPath": str(log_root / "dashboard.stdout.log"), "StandardErrorPath": str(log_root / "dashboard.stderr.log")}


def verify_validation(config: dict, directory: Path) -> None:
    state = read_object(directory / "state.json")
    source = read_object(directory / "source.json")
    verification = read_object(directory / "verification.json")
    if state.get("status") != "VALIDATION_COMPLETE" or not state.get("validation_only"):
        raise ValueError("a completed validation is required")
    if state["config_sha256"] != digest(encoded(config)) or state["source_code_sha256"] != source_code_digest(ROOT):
        raise ValueError("validation is stale relative to code or configuration")
    if sorted(verification["validated_tickers"]) != sorted(entry["ticker"] for entry in source["watchlist"]):
        raise ValueError("installation requires a full-watchlist validation, not a smoke test")
    from morning_edge.unattended import file_digest
    for name, expected in state["artifacts"].items():
        if file_digest(directory / (name + ".json")) != expected:
            raise ValueError("validation artifact changed")
        if name.startswith("analysis-"):
            batch = read_object(directory / (name + ".json"))
            verify_audit(directory / "agent-audit.sqlite", batch["audit"])
            if batch["audit"]["status"] != "VALIDATED" or batch["audit"]["cli_sha256"] != config["cli_sha256"]:
                raise ValueError("validation used another runtime or failed")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--validated-run", type=Path)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--launchd-validation", action="store_true")
    parser.add_argument("--validate-source", type=Path)
    parser.add_argument("--validation-directory", type=Path)
    args = parser.parse_args(argv)
    config = read_object(args.config)
    validate_config(config)
    if args.launchd_validation:
        if args.install or not args.validate_source or not args.validation_directory:
            parser.error("launchd validation requires source and a separate directory; not --install")
        if args.validation_directory.exists():
            raise ValueError("launchd validation requires a new directory")
        cli_preflight(config)
        payload = plistlib.dumps(validation_plist(config, args.config, args.validate_source, args.validation_directory), sort_keys=True)
        immutable_bytes(args.output, payload)
        from morning_edge.unattended import immutable_json
        immutable_json(args.validation_directory / "launchd-dispatch.json",
                       {"label": VALIDATION_LABEL, "plist": str(args.output.resolve()), "plist_sha256": digest(payload)})
        subprocess.run(["/usr/bin/plutil", "-lint", str(args.output)], check=True)
        subprocess.run(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(args.output.resolve())], check=True)
        return 0
    logs = ROOT / "outputs/unattended-logs"
    from morning_edge.private_io import ensure_private_directory
    ensure_private_directory(logs)
    payload = plistlib.dumps(plist(config, args.config, logs), sort_keys=True)
    immutable_bytes(args.output, payload)
    subprocess.run(["/usr/bin/plutil", "-lint", str(args.output)], check=True)
    server_payload = plistlib.dumps(server_plist(config, logs), sort_keys=True)
    server_output = args.output.with_name(SERVER_LABEL + ".plist")
    immutable_bytes(server_output, server_payload)
    subprocess.run(["/usr/bin/plutil", "-lint", str(server_output)], check=True)
    if args.install:
        if config.get("authorized_recovery_date"):
            raise ValueError("a one-day recovery configuration cannot become a recurring job")
        if args.validated_run is None:
            parser.error("--install requires --validated-run")
        verify_validation(config, args.validated_run)
        verify_launchd_context(args.validated_run)
        cli_preflight(config)
        targets = [(Path.home() / "Library/LaunchAgents" / (label + ".plist"), value)
                   for label, value in ((SERVER_LABEL, server_payload), (LABEL, payload))]
        if any(target.exists() for target, _ in targets):
            raise ValueError("existing LaunchAgent must be inspected; refusing to overwrite")
        # Preflight the serving root before changing ownership of the existing service.
        from urllib.request import urlopen
        from morning_edge.unattended import file_digest
        with urlopen(config["dashboard_url"] + "data/latest.json", timeout=10) as response:
            served = response.read(128 * 1024 * 1024 + 1)
        if digest(served) != file_digest(ROOT / "dashboard-app/data/latest.json"):
            raise ValueError("port 8765 is not serving this operator's dashboard")
        for target, value in targets:
            immutable_bytes(target, value)
            subprocess.run(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(target)], check=True)
            subprocess.run(["/bin/launchctl", "print", f"gui/{os.getuid()}/{target.stem}"], check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

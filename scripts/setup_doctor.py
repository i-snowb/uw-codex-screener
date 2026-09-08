#!/usr/bin/env python3
"""Check local Codex Screener readiness without revealing credentials."""

from __future__ import annotations

import argparse
from datetime import datetime, UTC
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from morning_edge.config import Settings  # noqa: E402
from morning_edge.freshness import latest_complete_session


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    if path.stat().st_mode & 0o077:
        raise PermissionError(f"environment file must be owner-private: {path}")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def inspect(*, env_file: Path, database: Path | None, app_root: Path) -> dict[str, object]:
    _load_env(env_file)
    settings = Settings.from_env()
    database = database or settings.database_path
    checks: dict[str, object] = {
        "private_env": env_file.exists() and not bool(env_file.stat().st_mode & 0o077),
        "provider_key_configured": settings.provider_api_key is not None,
        "database_exists": database.is_file(),
        "dashboard_shell_exists": (app_root / "index.html").is_file(),
        "latest_data_exists": (app_root / "data" / "latest.json").is_file(),
    }
    tables: list[str] = []
    if database.is_file():
        connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
        try:
            tables = [str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        finally:
            connection.close()
    checks["required_snapshot_table"] = "snapshots" in tables
    writable = {}
    for name, directory in {'database': database.parent, 'dashboard': app_root, 'outputs': ROOT / 'outputs'}.items():
        try:
            with tempfile.TemporaryFile(dir=directory):
                writable[name] = True
        except OSError:
            writable[name] = False
    checks['runtime_write_probe'] = all(writable.values())
    publication = {}
    latest, manifest = app_root / 'data/latest.json', app_root / 'data/live-status.json'
    if latest.is_file() and manifest.is_file():
        data, status = json.loads(latest.read_text()), json.loads(manifest.read_text())
        checks['publication_hash_matches'] = hashlib.sha256(latest.read_bytes()).hexdigest() == status.get('sha256')
        publication = {'cutoff_at': data.get('asOf'), 'expected_complete_session': latest_complete_session(datetime.now(UTC)).isoformat(), 'mode': data.get('mode'), 'data_health': data.get('dataHealth', {}).get('status', 'UNMEASURED')}
    else:
        checks['publication_hash_matches'] = False
    passed = all(value is True for value in checks.values())
    return {"status": "LOCALLY_CONFIGURED" if passed else "NEEDS_ATTENTION", "checks": checks, "write_probes": writable, "publication": publication, "database_tables": tables,
            "provider_access": "NOT_TESTED", "network_called": False,
            "boundary": "Local checks do not establish provider connectivity, freshness, scheduler execution or readiness for a future run."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--database", type=Path)
    parser.add_argument("--app-root", type=Path, default=Path("dashboard-app"))
    args = parser.parse_args()
    try:
        result = inspect(env_file=args.env_file, database=args.database, app_root=args.app_root)
    except (OSError, PermissionError, sqlite3.Error) as error:
        result = {"status": "FAILED_CLOSED", "error": str(error)}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "LOCALLY_CONFIGURED" else 2


if __name__ == "__main__":
    raise SystemExit(main())

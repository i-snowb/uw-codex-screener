#!/usr/bin/env python3
"""Build a deterministic source-only Codex Screener release directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil


ROOT = Path(__file__).resolve().parents[1]
MAX_PUBLIC_FILE_BYTES = 5 * 1024 * 1024
PUBLIC_ROOT_FILES = (
    ".env.example",
    ".gitignore",
    "DATA_POLICY.md",
    "README.md",
    "SECURITY.md",
    "pyproject.toml",
)
PUBLIC_TREE_SUFFIXES = {
    "src": frozenset({".py"}),
    "scripts": frozenset({".py", ".sh"}),
    "tests": frozenset({".py"}),
    ".github": frozenset({".yml", ".yaml"}),
}
EXCLUDED_PUBLIC_SCRIPTS = frozenset({"build_historical_evidence_visualization.py"})
PUBLIC_DOCS = (
    "acceptance-criteria.md",
    "architecture.md",
    "codex-workflow.md",
    "daily-agent-runbook.md",
    "data-dictionary.md",
    "edge-methodology.md",
    "historical-backfill.md",
    "intraday-refresh.md",
    "local-dashboard-app.md",
    "provider-request-budget.md",
    "research-control-plane.md",
    "risk-policy-template.md",
    "sunday-trial-runbook.md",
    "unusual-whales-endpoint-audit.md",
)
PUBLIC_DASHBOARD_FILES = (
    "dashboard-app/index.html",
    "dashboard-app/assets/app.css",
    "dashboard-app/assets/app.js",
)
PUBLIC_FIXTURES = ("fixtures/demo_morning_snapshot.json",)
OPTIONAL_PUBLIC_FILES = ("LICENSE", "AGENTS.md", "examples/synthetic-demo-run.json")
FORBIDDEN_PARTS = frozenset({"data", "outputs", "artifacts", "research", "__pycache__"})
FORBIDDEN_SUFFIXES = (
    ".sqlite",
    ".sqlite-shm",
    ".sqlite-wal",
    ".db",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".pyc",
)
SECRET_ASSIGNMENT = re.compile(
    rb"(?im)^(?:export[ \t]+)?(?:UNUSUAL_WHALES_API_KEY|MORNING_EDGE_PROVIDER_API_KEY|"
    rb"CODEX_SCREENER_PROVIDER_API_KEY)[ \t]*=[ \t]*([^\r\n#]+)"
)
SENSITIVE_LITERAL = re.compile(
    rb"(?ix)(?:[\"']?[A-Z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*[\"']?)"
    rb"[ \t]*(?:=|:)[ \t]*([\"'])([^\"'\r\n]{8,})\1"
)
SAFE_TEST_SECRET_LITERALS = frozenset({
    b"alias-secret",
    b"do-not-disclose-api-key",
    b"local-secret",
    b"local-test-secret",
    b"must-not-store",
    b"not-a-real-key",
    b"secret-value",
    b"UNUSUAL_WHALES_API_KEY",
    b"your_api_key_here",
})


def _source_files() -> tuple[Path, ...]:
    files: set[Path] = set()
    for name in PUBLIC_ROOT_FILES:
        files.add(ROOT / name)
    for tree, suffixes in PUBLIC_TREE_SUFFIXES.items():
        files.update(
            path
            for path in (ROOT / tree).rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix.lower() in suffixes
            and path.name != ".DS_Store"
            and not (
                path.parent == ROOT / "scripts"
                and path.name in EXCLUDED_PUBLIC_SCRIPTS
            )
        )
    files.update(ROOT / "docs" / name for name in PUBLIC_DOCS)
    files.update(ROOT / name for name in PUBLIC_DASHBOARD_FILES)
    files.update(ROOT / name for name in PUBLIC_FIXTURES)
    files.update(ROOT / name for name in OPTIONAL_PUBLIC_FILES if (ROOT / name).is_file())
    return tuple(sorted(files, key=lambda path: path.relative_to(ROOT).as_posix()))


def _validate_synthetic_json(relative: Path, payload: bytes) -> None:
    if relative.as_posix() not in {*PUBLIC_FIXTURES, "examples/synthetic-demo-run.json"}:
        return
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ValueError(f"public synthetic JSON is invalid: {relative}") from error
    serialized = json.dumps(value, sort_keys=True).lower()
    if "synthetic" not in serialized:
        raise ValueError(f"public JSON lacks an explicit synthetic marker: {relative}")
    if relative.as_posix() in PUBLIC_FIXTURES:
        snapshots = value.get("snapshots") if isinstance(value, dict) else None
        if not isinstance(snapshots, list) or not snapshots:
            raise ValueError(f"public synthetic fixture has no snapshots: {relative}")
        if any(not isinstance(row, dict) or row.get("provider") != "fixture" for row in snapshots):
            raise ValueError(f"public synthetic fixture contains a non-fixture provider: {relative}")


def _validate_source(path: Path) -> bytes:
    if path.is_symlink():
        raise ValueError(f"public release refuses symbolic links: {path.relative_to(ROOT)}")
    if not path.is_file():
        raise FileNotFoundError(f"required public source file is missing: {path.relative_to(ROOT)}")
    relative = path.relative_to(ROOT)
    if any(part in FORBIDDEN_PARTS for part in relative.parts):
        raise ValueError(f"private runtime path entered public release: {relative}")
    if relative.name == ".env" or relative.name.startswith(".env.") and relative.name != ".env.example":
        raise ValueError(f"private environment file entered public release: {relative}")
    if relative.name.lower().endswith(FORBIDDEN_SUFFIXES):
        raise ValueError(f"private or generated file type entered public release: {relative}")
    payload = path.read_bytes()
    if len(payload) > MAX_PUBLIC_FILE_BYTES:
        raise ValueError(f"public source file exceeds {MAX_PUBLIC_FILE_BYTES} bytes: {relative}")
    for match in SECRET_ASSIGNMENT.finditer(payload):
        value = match.group(1).strip().strip(b"'\"")
        if value:
            raise ValueError(f"non-empty provider credential assignment in public source: {relative}")
    for match in SENSITIVE_LITERAL.finditer(payload):
        value = match.group(2).strip()
        if value not in SAFE_TEST_SECRET_LITERALS:
            raise ValueError(f"sensitive-looking literal in public source: {relative}")
    _validate_synthetic_json(relative, payload)
    return payload


def build_release(output: Path) -> dict[str, object]:
    destination = output.expanduser().resolve()
    if destination.exists():
        raise FileExistsError(f"release destination already exists: {destination}")
    destination.mkdir(parents=True, mode=0o755)
    manifest: list[dict[str, object]] = []
    try:
        for source in _source_files():
            payload = _validate_source(source)
            relative = source.relative_to(ROOT)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            target.write_bytes(payload)
            mode = 0o755 if relative.parts[0] == "scripts" else 0o644
            os.chmod(target, mode)
            manifest.append(
                {
                    "path": relative.as_posix(),
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
        manifest_payload = {
            "schema_version": "codex-screener-public-release-v1",
            "file_count": len(manifest),
            "files": manifest,
            "private_runtime_paths_included": False,
        }
        manifest_path = destination / "release-manifest.json"
        manifest_path.write_text(
            json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(manifest_path, 0o644)
        return manifest_payload | {"output": str(destination)}
    except BaseException:
        shutil.rmtree(destination)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = build_release(args.output)
    print(json.dumps({"status": "BUILT", **result}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

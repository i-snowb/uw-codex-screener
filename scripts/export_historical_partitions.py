#!/usr/bin/env python3
"""Export immutable monthly snapshot metadata partitions for audit and analysis."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import sqlite3
from typing import Sequence

from private_artifacts import (
    ensure_private_directory,
    private_runtime_path,
    write_private_bytes,
    write_private_text,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=private_runtime_path("data/morning-edge.sqlite3"))
    parser.add_argument("--output-root", type=Path, default=private_runtime_path("data/partitions"))
    args = parser.parse_args(argv)
    connection = sqlite3.connect(f"file:{args.database.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("SELECT id,provider,dataset,ticker,as_of,retrieved_at,raw_payload_hash FROM snapshots ORDER BY as_of,id").fetchall()
    finally:
        connection.close()
    ensure_private_directory(args.output_root)
    groups: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        month = str(row["as_of"])[:7]
        groups.setdefault(month, []).append(dict(row))
    manifest = []
    for month, values in sorted(groups.items()):
        path = args.output_root / month / "snapshots.jsonl.gz"
        payload = b"".join((json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode() for value in values)
        compressed = io.BytesIO()
        with gzip.GzipFile(fileobj=compressed, mode="wb", mtime=0) as archive:
            archive.write(payload)
        write_private_bytes(path, compressed.getvalue())
        manifest.append({"month": month, "rows": len(values), "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    manifest_path = args.output_root / "manifest.json"
    write_private_text(
        manifest_path,
        json.dumps(
            {"schema_version": "codex-screener-partitions-v1", "partitions": manifest},
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    print(json.dumps({"partitions": len(manifest), "rows": len(rows), "manifest": str(manifest_path)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIRECTORY = ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIRECTORY))
SCRIPT = SCRIPT_DIRECTORY / "export_historical_partitions.py"
SPEC = importlib.util.spec_from_file_location("export_historical_partitions", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
exporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exporter)


class HistoricalPartitionExportTests(unittest.TestCase):
    def _database(self, path: Path) -> None:
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                """
                CREATE TABLE snapshots (
                    id INTEGER PRIMARY KEY,
                    provider TEXT NOT NULL,
                    dataset TEXT NOT NULL,
                    ticker TEXT,
                    as_of TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    raw_payload_hash TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT INTO snapshots VALUES(1,?,?,?,?,?,?)",
                (
                    "fixture",
                    "option_chain",
                    "DEMO",
                    "2026-05-01T20:00:00Z",
                    "2026-05-02T10:00:00Z",
                    "0" * 64,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def test_export_is_private_and_partition_bytes_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "source.sqlite"
            self._database(database)
            outputs = []
            for name in ("first", "second"):
                output = root / name
                self.assertEqual(
                    0,
                    exporter.main(["--database", str(database), "--output-root", str(output)]),
                )
                partition = output / "2026-05" / "snapshots.jsonl.gz"
                manifest = output / "manifest.json"
                self.assertEqual(0o700, stat.S_IMODE(output.stat().st_mode))
                self.assertEqual(0o700, stat.S_IMODE(partition.parent.stat().st_mode))
                self.assertEqual(0o600, stat.S_IMODE(partition.stat().st_mode))
                self.assertEqual(0o600, stat.S_IMODE(manifest.stat().st_mode))
                self.assertEqual(1, json.loads(manifest.read_text())["partitions"][0]["rows"])
                outputs.append(hashlib.sha256(partition.read_bytes()).hexdigest())
            self.assertEqual(outputs[0], outputs[1])


if __name__ == "__main__":
    unittest.main()

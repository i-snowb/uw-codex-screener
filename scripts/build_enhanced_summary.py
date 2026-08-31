#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from morning_edge.enhanced_features import build_enhanced_summary  # noqa: E402
from private_artifacts import private_runtime_path, write_private_text  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a source-linked enhanced Codex Screener summary")
    parser.add_argument("--database", type=Path, default=private_runtime_path("data/morning-edge.sqlite"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = build_enhanced_summary(args.database)
    rendered = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    write_private_text(args.output, rendered)
    print(json.dumps({"output": str(args.output), "symbols": len(payload["symbols"]), "contexts": len(payload["contexts"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

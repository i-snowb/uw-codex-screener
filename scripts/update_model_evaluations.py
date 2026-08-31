#!/usr/bin/env python3
"""Register and score local Codex Screener forecasts without provider calls."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from morning_edge.evaluation import discover_runs, update_evaluations
from private_artifacts import write_private_text


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--runs-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    paths = discover_runs(args.runs_root)
    if not paths:
        raise SystemExit(f"no enriched run artifacts found under {args.runs_root}")
    report = update_evaluations(args.database, paths)
    write_private_text(args.output, json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

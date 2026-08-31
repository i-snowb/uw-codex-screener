#!/usr/bin/env bash
# Run the safe, local morning workflow. Provider access remains disabled.
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PYTHON_BIN:-python3}"

cd "$project_dir"
PYTHONPATH="$project_dir/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$python_bin" -m morning_edge.cli morning-run "$@"

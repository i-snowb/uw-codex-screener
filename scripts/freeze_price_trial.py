"""Freeze already fitted development coefficients; never train or backfill."""
import argparse
from datetime import datetime, UTC
import hashlib
import json
from pathlib import Path

from morning_edge.price_trial import freeze_trial
from morning_edge.experiments import encoded
from morning_edge.unattended import immutable_bytes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', type=Path, required=True)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--starts-at', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError('Frozen trial already exists; do not replace an active trial')
    raw = args.experiment.read_bytes()
    run = json.loads(args.run.read_bytes())
    trial = freeze_trial(json.loads(raw), source_sha256=hashlib.sha256(raw).hexdigest(),
                         starts_at=args.starts_at, frozen_at=datetime.now(UTC).isoformat(),
                         tickers=[row['ticker'] for row in run['watchlist']])
    immutable_bytes(args.output, encoded(trial))
    print(json.dumps({'trial_id': trial['trial_id'], 'starts_at': trial['starts_at'],
                      'horizons': sorted(trial['models']), 'promotion_eligible': False}))


if __name__ == '__main__':
    main()

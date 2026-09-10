"""Publish only an offline research summary; never modify forecast publications."""
import argparse
from datetime import datetime, UTC
import hashlib
import json
from pathlib import Path

from private_artifacts import write_private_bytes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--app-root', required=True, type=Path)
    args = parser.parse_args()
    original = args.input.read_bytes()
    value = json.loads(original)
    if (value.get('schema_version') != 'signal-contribution-v1' or value.get('promotion_eligible') is not False
            or value.get('holdout', {}).get('status') != 'SEALED_NOT_SCORED'
            or not value.get('comparisons')):
        raise ValueError('Not an eligible offline research summary')
    value['reviewed_at'] = datetime.now(UTC).isoformat()
    value['source_summary_sha256'] = hashlib.sha256(original).hexdigest()
    body = (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)+'\n').encode()
    if len(body) > 100000:
        raise ValueError('Research summary exceeds size limit')
    manifest = {'sha256': hashlib.sha256(body).hexdigest(), 'research_only': True,
                'experiment_id': value['experiment_id']}
    write_private_bytes(args.app_root/'data/signal-research.json', body)
    write_private_bytes(args.app_root/'data/signal-research-manifest.json',
                        (json.dumps(manifest, sort_keys=True)+'\n').encode())
    print(json.dumps(manifest))


if __name__ == '__main__':
    main()

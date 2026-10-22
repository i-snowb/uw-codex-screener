"""Retain raw chain pages and expose only verified complete page groups."""
from dataclasses import dataclass
from datetime import UTC, datetime, time
import hashlib
import json
from typing import Any, Mapping
from uuid import uuid4

from .models import Dataset, SnapshotEnvelope, timestamp_from_text

VERSION = 'option-chain-pages-v1'
PAGE_SIZE = 500
MAX_PAGES = 50


def incomplete_chain(metadata: Mapping) -> bool:
    if metadata.get('capture_mode') == 'chain_page':
        return True
    if metadata.get('chain_coverage_version') == VERSION:
        return metadata.get('pagination_status') != 'complete'
    # Old backfills asserted that pagination was unnecessary. That assertion
    # cannot establish completeness under the current provider contract.
    return bool(metadata.get('backfill_plan_id')) or metadata.get('response_row_count', 0) >= PAGE_SIZE


def expand_chain(connection, metadata, payload, *, verify_hashes=False):
    """Construct a read-only view; stored provider payloads remain unchanged."""
    if incomplete_chain(metadata):
        raise ValueError('option-chain coverage is incomplete or legacy-unverified')
    if metadata.get('chain_coverage_version') != VERSION:
        return payload
    rows = []
    ids = metadata.get('chain_page_ids', [])
    hashes = metadata.get('chain_page_hashes', [])
    if not isinstance(ids, list) or len(ids) != len(hashes) or len(set(ids)) != len(ids):
        raise ValueError('invalid chain page manifest')
    for index, sid in enumerate(ids):
        if isinstance(sid, bool) or not isinstance(sid, int) or sid <= 0:
            raise ValueError('invalid chain page identity')
        page = connection.execute('''SELECT s.metadata_json,s.raw_payload_hash,s.retrieved_at,p.content_json
            FROM snapshots s JOIN raw_payloads p ON p.content_hash=s.raw_payload_hash
            WHERE s.id=? AND s.dataset='option_chain' AND s.provider='unusual_whales' AND s.symbol=?''',
            (sid, metadata['chain_symbol'])).fetchone()
        if page is None:
            raise ValueError('chain page is missing')
        pm = json.loads(page[0])
        if (page[1] != hashes[index] or pm.get('chain_group') != metadata.get('chain_group')
            or pm.get('chain_page') != index or pm.get('capture_mode') != 'chain_page'
            or timestamp_from_text(page[2]) > timestamp_from_text(metadata['chain_completed_at'])):
            raise ValueError('chain page provenance mismatch')
        if verify_hashes and hashlib.sha256(page[3].encode()).hexdigest() != page[1]:
            raise ValueError('chain page payload hash mismatch')
        rows.extend(json.loads(page[3])['data'])
    rows.extend(payload['data'])
    if len(rows) != metadata.get('response_row_count'):
        raise ValueError('chain row count mismatch')
    symbols = [r.get('option_symbol') for r in rows]
    if len(set(symbols)) != len(symbols):
        raise ValueError('chain contains duplicate contract identities')
    return {'data': rows}


@dataclass(frozen=True)
class ChainCapture:
    snapshot: Any
    requests: int
    row_count: int
    complete: bool


class ChainValidationError(ValueError):
    def __init__(self, message, snapshot_id):
        super().__init__(message)
        self.snapshot_id = snapshot_id


def _validate_rows(data, seen, previous_key, requested_date):
    if not isinstance(data, list) or len(data) > PAGE_SIZE:
        raise ValueError('invalid option-chain page shape or size')
    for row in data:
        symbol = row.get('option_symbol') if isinstance(row, Mapping) else None
        if not isinstance(symbol, str) or not symbol or symbol in seen:
            raise ValueError('missing or repeated option-chain identity')
        seen.add(symbol)
        expiry = row.get('expires', row.get('expiry'))
        strike = row.get('strike')
        if expiry is not None and strike is not None:
            key = (str(expiry), float(strike), symbol)
            if previous_key is not None and key <= previous_key:
                raise ValueError('option-chain page ordering is unstable')
            previous_key = key
        if requested_date is not None and row.get('last_tape_time'):
            tape = row['last_tape_time']
            if isinstance(tape, (float, int)):
                tape_date = datetime.fromtimestamp(tape / 1000 if abs(tape) >= 100_000_000_000 else tape, UTC).date()
            else:
                parsed = datetime.fromisoformat(str(tape).replace('Z','+00:00'))
                if parsed.tzinfo is None:
                    raise ValueError('option-chain trade timestamp lacks timezone')
                tape_date = parsed.astimezone(UTC).date()
            if tape_date > requested_date:
                raise ValueError('option-chain trade timestamp is after requested date')
    return previous_key


def capture_chain(*, client, snapshots, ticker, requested_date=None, max_pages=MAX_PAGES,
                  metadata=None, progress=None):
    if not 1 <= max_pages <= MAX_PAGES:
        raise ValueError('chain page cap is outside the supported bound')
    group = uuid4().hex
    ids, hashes, seen = [], [], set()
    count = 0
    previous_key = None
    last = None
    for page in range(max_pages):
        if progress:
            progress(page)
        kwargs = {'greeks': True}
        if requested_date is not None:
            kwargs['as_of'] = requested_date
        if page:
            kwargs.update(limit=PAGE_SIZE, page=page)
        response = client.option_chain(ticker, **kwargs)
        data = response.data
        raw = response.response.raw
        as_of = datetime.combine(requested_date, time.min, UTC) if requested_date else raw.fetched_at
        try:
            previous_key = _validate_rows(data, seen, previous_key, requested_date)
        except (ValueError, TypeError, OverflowError) as error:
            invalid = snapshots.insert(SnapshotEnvelope(provider='unusual_whales',dataset=Dataset.OPTION_CHAIN,
                symbol=ticker,as_of=as_of,retrieved_at=raw.fetched_at,payload=response.response.payload,
                metadata=dict(metadata or {}) | {'capture_mode':'chain_page','chain_group':group,
                    'chain_page':page,'pagination_status':'invalid','validation_error':str(error)[:160],
                    'historical_scope_verified':False,'historical_scope_status':'scope_mismatch'}))
            raise ChainValidationError(str(error), invalid.id) from None
        count += len(data)
        complete = len(data) < PAGE_SIZE
        meta = dict(metadata or {}) | {
            'chain_coverage_version': VERSION, 'chain_group': group,
            'chain_symbol': ticker, 'chain_page': page,
            'provider_endpoint': response.endpoint, 'raw_only': True,
            'pagination_status': 'complete' if complete else 'in_progress',
            'response_row_count': count if complete else len(data),
            'transport_attempts': raw.attempts, 'recommendations_enabled': False,
        }
        if complete:
            meta.update(chain_page_ids=ids, chain_page_hashes=hashes,
                        chain_completed_at=raw.fetched_at.isoformat(), chain_pages=page + 1,
                        response_status='captured' if count else 'empty')
        else:
            meta['capture_mode'] = 'chain_page'
        last = snapshots.insert(SnapshotEnvelope(provider='unusual_whales',dataset=Dataset.OPTION_CHAIN,
            symbol=ticker,as_of=as_of,retrieved_at=raw.fetched_at,payload=response.response.payload,metadata=meta))
        if complete:
            return ChainCapture(last, page + 1, count, True)
        ids.append(last.id)
        hashes.append(last.envelope.raw_payload_hash)
    return ChainCapture(last, max_pages, count, False)

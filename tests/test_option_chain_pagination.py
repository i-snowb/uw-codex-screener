from datetime import UTC, date, datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from morning_edge.option_chain_collection import capture_chain, expand_chain, incomplete_chain, ChainValidationError
from morning_edge.store import SnapshotStore
from morning_edge.providers.base import JsonResponse, RawResponse, RateLimitMetadata
from morning_edge.providers.unusual_whales import EndpointResponse
from morning_edge.edge import EdgeAnalyzer
from morning_edge.daily import _latest_chain_snapshot


AT = datetime(2026, 10, 22, 21, tzinfo=UTC)


def rows(start, count, tape='2026-10-21T19:59:00Z'):
    return [{'option_symbol':f'QCOM261120C{(100+i)*1000:08d}', 'expires':'2026-11-20',
             'strike':100+i, 'option_type':'call', 'last_tape_time':tape} for i in range(start,start+count)]


class Pages:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def option_chain(self, ticker, *, as_of=None, greeks=True, page=None, limit=500):
        page = page or 0
        self.calls.append(page)
        raw = RawResponse(provider='unusual_whales',method='GET',url='https://api.unusualwhales.com/api/stock/QCOM/option-chains',
            status_code=200,headers={},body=b'',fetched_at=AT+timedelta(seconds=page),attempts=1,rate_limit=RateLimitMetadata())
        return EndpointResponse('/api/stock/QCOM/option-chains',JsonResponse(payload={'data':self.pages[page]},raw=raw))


class ChainPaginationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'db.sqlite'
        self.store = SnapshotStore(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def capture(self, pages, **kwargs):
        return capture_chain(client=Pages(pages),snapshots=self.store,ticker='QCOM',metadata={'capture_mode':'current'},**kwargs)

    def test_retains_raw_pages_and_expands_complete_chain(self):
        result = self.capture([rows(0,500),rows(500,2)])
        self.assertTrue(result.complete)
        self.assertEqual(502,result.row_count)
        self.assertEqual(2,len(result.snapshot.envelope.payload['data']))
        expanded = expand_chain(self.store.connection,result.snapshot.envelope.metadata,result.snapshot.envelope.payload,verify_hashes=True)
        self.assertEqual(502,len(expanded['data']))
        self.assertEqual(500,len(self.store.get(result.snapshot.envelope.metadata['chain_page_ids'][0]).envelope.payload['data']))

    def test_exact_page_requires_terminal_empty_page(self):
        result = self.capture([rows(0,500),[]])
        self.assertEqual(2,result.requests)
        self.assertEqual(500,len(expand_chain(self.store.connection,result.snapshot.envelope.metadata,result.snapshot.envelope.payload)['data']))

    def test_duplicate_page_fails_closed_and_preserves_raw(self):
        with self.assertRaises(ChainValidationError):
            self.capture([rows(0,500),rows(0,500)])
        self.assertEqual(2,self.store.count())
        self.assertTrue(all(incomplete_chain(s.envelope.metadata) for s in self.store.list()))

    def test_unstable_order_is_rejected(self):
        with self.assertRaises(ChainValidationError):
            self.capture([list(reversed(rows(0,2)))])

    def test_page_guard_does_not_create_complete_group(self):
        result = self.capture([rows(0,500)],max_pages=1)
        self.assertFalse(result.complete)
        self.assertTrue(incomplete_chain(result.snapshot.envelope.metadata))

    def test_older_trade_is_allowed_but_future_trade_is_rejected(self):
        result = self.capture([rows(0,2)],requested_date=date(2026,10,22))
        self.assertTrue(result.complete)
        with self.assertRaises(ChainValidationError):
            self.capture([rows(0,2,'2026-10-23T12:00:00Z')],requested_date=date(2026,10,22))

    def test_legacy_full_page_and_backfill_are_not_complete(self):
        self.assertTrue(incomplete_chain({'capture_mode':'current','response_row_count':500}))
        self.assertTrue(incomplete_chain({'backfill_plan_id':'old','pagination_status':'verified_not_required'}))

    def test_manifest_hash_and_cutoff_are_checked(self):
        result = self.capture([rows(0,500),rows(500,2)])
        meta = dict(result.snapshot.envelope.metadata)
        meta['chain_page_hashes']=['0'*64]
        with self.assertRaises(ValueError):
            expand_chain(self.store.connection,meta,result.snapshot.envelope.payload)

    def test_readers_exclude_partial_pages_and_expand_only_after_completion(self):
        result = self.capture([rows(0,500),rows(500,2)])
        with EdgeAnalyzer(self.path) as reader:
            self.assertEqual([],reader.snapshots('QCOM','option_chain',AT))
            sources=reader.snapshots('QCOM','option_chain',AT+timedelta(seconds=2))
            self.assertEqual(1,len(sources))
            self.assertEqual(502,len(sources[0].payload['data']))
        self.assertIsNone(_latest_chain_snapshot(self.path,'QCOM',AT))
        self.assertEqual(502,len(_latest_chain_snapshot(self.path,'QCOM',AT+timedelta(seconds=2))[2]['data']))
        meta = dict(result.snapshot.envelope.metadata,chain_completed_at=(AT-timedelta(seconds=1)).isoformat())
        with self.assertRaises(ValueError):
            expand_chain(self.store.connection,meta,result.snapshot.envelope.payload)


if __name__ == '__main__':
    unittest.main()

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from morning_edge.models import Dataset, SnapshotEnvelope
from morning_edge.normalization import EvidenceReader, build_evidence_bundle
from morning_edge.store import SnapshotStore


UTC = timezone.utc
BASE = datetime(2026, 8, 21, 20, tzinfo=UTC)


def envelope(dataset: Dataset, payload: object, *, as_of: datetime, retrieved: datetime, metadata: dict[str, object] | None = None) -> SnapshotEnvelope:
    return SnapshotEnvelope("test", dataset, as_of, retrieved, payload, "QCOM", metadata or {})


def bar(day: int, close: float) -> dict[str, object]:
    return {"date": f"2026-08-{day:02d}", "market_time": "r", "open": close - 1, "high": close + 2, "low": close - 2, "close": close, "volume": 1_000_000}


def contract(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "option_type": "call", "strike": 170, "open_interest": 1200, "volume": 150,
        "nbbo_bid": 2.1, "nbbo_ask": 2.3, "delta": .4, "gamma": .02,
        "theta": -.1, "vega": .12, "implied_volatility": .35,
    }
    value.update(changes)
    return value


class NormalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "evidence.sqlite"
        self.store = SnapshotStore(self.path)

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_bundle_uses_cutoff_safe_data_dedupes_dates_and_keeps_lineage(self) -> None:
        retrieved = BASE - timedelta(minutes=5)
        ohlc = self.store.insert(envelope(Dataset.OHLC, {"data": [bar(day, 100 + day) for day in range(1, 22)]}, as_of=BASE - timedelta(hours=1), retrieved=retrieved))
        first = self.store.insert(envelope(Dataset.OPTION_CHAIN, {"data": [contract(open_interest=1)]}, as_of=BASE - timedelta(hours=1), retrieved=retrieved, metadata={"requested_market_date": "2026-08-20"}))
        newest = self.store.insert(envelope(Dataset.OPTION_CHAIN, {"data": [contract(open_interest=2000), contract(option_type="put", open_interest=400)]}, as_of=BASE - timedelta(hours=1), retrieved=retrieved + timedelta(minutes=1), metadata={"requested_market_date": "2026-08-20"}))
        gex = self.store.insert(envelope(Dataset.DEALER_EXPOSURE, {"data": {"call_wall": 180, "put_wall": 155, "gamma_flip": 165, "gamma_magnet": 170}}, as_of=BASE - timedelta(hours=1), retrieved=retrieved, metadata={"requested_market_date": "2026-08-20", "derived_gex_eligible": True}))
        future = self.store.insert(envelope(Dataset.OPTION_CHAIN, {"data": [contract(open_interest=9999)]}, as_of=BASE, retrieved=BASE + timedelta(minutes=1), metadata={"requested_market_date": "2026-08-21"}))

        result = build_evidence_bundle(self.path, "qcom", cutoff_at=BASE)

        self.assertEqual(result.ticker, "QCOM")
        self.assertEqual(result.price.observations if result.price else None, 21)
        self.assertEqual(result.chain.requested_sessions, 1)
        self.assertEqual(result.chain.latest_valid_contract_count, 2)
        self.assertEqual(result.chain.latest_call_open_interest, 2000)
        self.assertEqual(result.chain.latest_put_open_interest, 400)
        self.assertEqual(result.gex.gamma_flip, 165)
        self.assertEqual({source.snapshot_id for source in result.source_refs}, {ohlc.id, newest.id, gex.id})
        self.assertNotIn(future.id, {source.snapshot_id for source in result.source_refs})

    def test_normalizes_only_regular_valid_bars_and_contracts(self) -> None:
        retrieved = BASE - timedelta(minutes=5)
        self.store.insert(envelope(Dataset.OHLC, {"data": [bar(20, 120), {**bar(21, 121), "market_time": "pr"}, {**bar(19, 119), "high": 100}]}, as_of=BASE - timedelta(hours=1), retrieved=retrieved))
        self.store.insert(envelope(Dataset.OPTION_CHAIN, {"data": [contract(), contract(nbbo_bid=0), contract(gamma=None), {"option_type": "call", "strike": -1}]}, as_of=BASE - timedelta(hours=1), retrieved=retrieved, metadata={"requested_market_date": "2026-08-20"}))

        with EvidenceReader(self.path) as reader:
            bars = reader.normalize_bars("QCOM", cutoff_at=BASE)
            chains = reader.normalize_chain("QCOM", cutoff_at=BASE)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].session_date.isoformat(), "2026-08-20")
        self.assertEqual(chains[0].raw_contract_count, 4)
        self.assertEqual(chains[0].valid_contract_count, 1)

    def test_merges_rolling_ohlc_windows_with_newest_source_precedence(self) -> None:
        older_retrieved = BASE - timedelta(minutes=10)
        newer_retrieved = BASE - timedelta(minutes=5)
        older = self.store.insert(envelope(
            Dataset.OHLC,
            {"data": [
                {**bar(19, 119), "date": "2025-08-19"},
                {**bar(20, 120), "date": "2025-08-20"},
            ]},
            as_of=BASE - timedelta(days=365), retrieved=older_retrieved,
        ))
        newer = self.store.insert(envelope(
            Dataset.OHLC,
            {"data": [
                {**bar(20, 220), "date": "2025-08-20"},
                {**bar(21, 221), "date": "2026-08-21"},
            ]},
            as_of=BASE - timedelta(hours=1), retrieved=newer_retrieved,
        ))

        with EvidenceReader(self.path) as reader:
            bars = reader.normalize_bars("QCOM", cutoff_at=BASE)

        self.assertEqual([item.session_date.isoformat() for item in bars], [
            "2025-08-19", "2025-08-20", "2026-08-21",
        ])
        self.assertEqual([item.close for item in bars], [119, 220, 221])
        self.assertEqual([item.source_snapshot_id for item in bars], [older.id, newer.id, newer.id])

    def test_excludes_partial_or_ineligible_gex_and_agent_input_is_compact(self) -> None:
        retrieved = BASE - timedelta(minutes=5)
        self.store.insert(envelope(Dataset.DEALER_EXPOSURE, {"data": {"call_wall": 180, "put_wall": 155, "gamma_flip": 165, "gamma_magnet": 170}}, as_of=BASE - timedelta(hours=1), retrieved=retrieved, metadata={"requested_market_date": "2026-08-19", "derived_gex_eligible": False}))
        self.store.insert(envelope(Dataset.DEALER_EXPOSURE, {"data": {"call_wall": 180, "put_wall": None, "gamma_flip": 165, "gamma_magnet": 170}}, as_of=BASE - timedelta(hours=1), retrieved=retrieved, metadata={"requested_market_date": "2026-08-20"}))

        result = build_evidence_bundle(self.path, "QCOM", cutoff_at=BASE)
        payload = result.to_agent_input()
        self.assertEqual(result.gex.eligible_sessions, 0)
        self.assertIn("no eligible complete GEX sessions", result.exclusions)
        self.assertEqual(payload["scope"], "deterministic historical evidence; not a recommendation or forecast")
        self.assertNotIn("raw_payload", str(payload))

    def test_daily_observed_summaries_and_bounded_ta_series_keep_no_directional_claim(self) -> None:
        retrieved = BASE - timedelta(minutes=5)
        start = date(2026, 4, 1)
        bars = []
        for index in range(130):
            session = start + timedelta(days=index)
            close = 100 + index * .5
            bars.append({"date": session.isoformat(), "market_time": "r", "open": close - 1,
                         "high": close + 2, "low": close - 2, "close": close, "volume": 1_000_000 + index})
        self.store.insert(envelope(Dataset.OHLC, {"data": bars}, as_of=BASE - timedelta(hours=1), retrieved=retrieved))
        flow = self.store.insert(envelope(Dataset.OPTION_FLOW, {"data": [
            {"created_at": "2026-08-21T14:00:00Z", "premium": 200_000, "side": "ask"},
            {"created_at": "2026-08-21T14:02:00Z", "premium": 100_000, "side": "bid"},
        ]}, as_of=BASE - timedelta(hours=1), retrieved=retrieved))
        oi = self.store.insert(envelope(Dataset.OPEN_INTEREST, {"data": [
            {"curr_date": "2026-08-21", "last_date": "2026-08-20", "open_interest_change": 120},
            {"curr_date": "2026-08-21", "last_date": "2026-08-20", "oi_change": -20},
        ]}, as_of=BASE - timedelta(hours=1), retrieved=retrieved, metadata={"requested_market_date": "2026-08-21", "page": 0}))
        dark = self.store.insert(envelope(Dataset.DARK_POOL, {"data": [
            {"tracking_id": "a", "executed_at": "2026-08-21T15:00:00Z", "premium": 500_000},
            {"tracking_id": "a", "executed_at": "2026-08-21T15:00:00Z", "premium": 500_000},
        ]}, as_of=BASE - timedelta(hours=1), retrieved=retrieved, metadata={"requested_market_date": "2026-08-21", "page": 0}))
        news = self.store.insert(envelope(Dataset.NEWS, {"data": [{"headline": "Product event", "source": "Wire", "created_at": "2026-08-21T13:00:00Z"}]}, as_of=BASE - timedelta(hours=1), retrieved=retrieved))
        earning = self.store.insert(envelope(Dataset.EARNINGS, {"data": [{"report_date": "2026-09-01", "report_time": "postmarket", "expected_move": .08}]}, as_of=BASE - timedelta(hours=1), retrieved=retrieved))

        result = build_evidence_bundle(self.path, "QCOM", cutoff_at=BASE)

        self.assertEqual(len(result.price.bars) if result.price else None, 126)
        self.assertIsNotNone(result.price.rsi_14 if result.price else None)
        self.assertIsNotNone(result.price.drawdown_126d_pct if result.price else None)
        self.assertEqual(result.flow.aggregate_reported_premium, 300_000)
        self.assertIn("not a directional inference", result.open_interest.market_time_semantics)
        self.assertEqual(result.open_interest.aggregate_explicit_change, 100)
        self.assertEqual(date(2026, 8, 21), result.open_interest.latest_market_date)
        self.assertEqual(2, result.open_interest.latest_row_count)
        self.assertEqual(100, result.open_interest.latest_aggregate_explicit_change)
        self.assertEqual(result.dark_pool.unique_print_count, 1)
        self.assertEqual(date(2026, 8, 21), result.dark_pool.latest_market_date)
        self.assertEqual(1, result.dark_pool.latest_unique_print_count)
        self.assertEqual(500_000, result.dark_pool.latest_aggregate_reported_premium)
        self.assertIn("do not identify beneficial owner or direction", result.dark_pool.market_time_semantics)
        self.assertEqual(result.news.latest_headlines[0].headline, "Product event")
        self.assertEqual(result.earnings.report_date, "2026-09-01")
        self.assertTrue({flow.id, oi.id, dark.id, news.id, earning.id}.issubset({item.snapshot_id for item in result.source_refs}))

    def test_current_retrieval_time_does_not_relabel_prior_session_rows(self) -> None:
        monday = datetime(2026, 8, 24, 12, 24, tzinfo=UTC)
        friday_chain = {"data": [contract(last_tape_time="2026-08-21T21:45:12Z")]}
        historical = self.store.insert(envelope(
            Dataset.OPTION_CHAIN,
            friday_chain,
            as_of=BASE - timedelta(hours=1),
            retrieved=BASE - timedelta(minutes=5),
            metadata={"requested_market_date": "2026-08-21"},
        ))
        current = self.store.insert(envelope(
            Dataset.OPTION_CHAIN,
            friday_chain,
            as_of=monday,
            retrieved=monday,
            metadata={"capture_mode": "current", "as_of_source": "retrieval_time_only"},
        ))
        self.store.insert(envelope(
            Dataset.OPTION_FLOW,
            {"data": [{"created_at": "2026-08-21T19:53:00Z", "premium": 10_000}]},
            as_of=monday,
            retrieved=monday,
            metadata={"capture_mode": "current", "as_of_source": "retrieval_time_only"},
        ))

        result = build_evidence_bundle(self.path, "QCOM", cutoff_at=monday + timedelta(seconds=1))

        self.assertEqual(date(2026, 8, 21), result.chain.latest_market_date)
        self.assertEqual(1, result.chain.requested_sessions)
        chain_sources = [source for source in result.source_refs if source.dataset == "option_chain"]
        self.assertEqual([current.id], [source.snapshot_id for source in chain_sources])
        self.assertNotIn(historical.id, {source.snapshot_id for source in result.source_refs})
        self.assertEqual(date(2026, 8, 21), result.flow.source.market_date)

    def test_past_earnings_is_preserved_but_not_labeled_as_next_event(self) -> None:
        retrieved = BASE - timedelta(minutes=5)
        self.store.insert(envelope(
            Dataset.EARNINGS,
            {"data": [
                {"report_date": "2026-08-12", "report_time": "premarket", "expected_move": .11},
                {"report_date": "2026-05-14", "report_time": "postmarket", "expected_move": .09},
            ]},
            as_of=BASE - timedelta(hours=1),
            retrieved=retrieved,
        ))

        result = build_evidence_bundle(self.path, "QCOM", cutoff_at=BASE)

        self.assertIsNone(result.earnings.report_date)
        self.assertIsNone(result.earnings.report_time)
        self.assertIsNone(result.earnings.expected_move)
        self.assertEqual("2026-08-12", result.earnings.last_report_date)
        self.assertEqual("premarket", result.earnings.last_report_time)


if __name__ == "__main__":
    unittest.main()

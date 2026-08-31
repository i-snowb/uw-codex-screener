from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest

from morning_edge.current_collection import (
    CurrentCaptureStatus,
    CurrentDataset,
    collect_current,
    reconstruct_current_report,
)
from morning_edge.models import Dataset, SnapshotEnvelope
from morning_edge.providers.base import JsonResponse, ProviderSchemaError, RateLimitMetadata, RawResponse
from morning_edge.providers.budget import WeeklyRequestBudget
from morning_edge.providers.unusual_whales import EndpointResponse
from morning_edge.store import SnapshotStore


FETCHED = datetime(2026, 8, 24, 10, 45, tzinfo=UTC)


def response(endpoint: str, data: object) -> EndpointResponse:
    raw = RawResponse(
        provider="unusual_whales", method="GET", url=f"https://api.unusualwhales.com{endpoint}",
        status_code=200, headers={"x-request-id": "current-test"}, body=b'{"data":[]}',
        fetched_at=FETCHED, attempts=1, rate_limit=RateLimitMetadata(limit=30_000, remaining=29_999),
    )
    return EndpointResponse(endpoint, JsonResponse(payload={"data": data}, raw=raw))  # type: ignore[arg-type]


class FakeCurrentClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def option_chain(self, ticker: str, *, as_of: object = None, greeks: bool = True) -> EndpointResponse:
        self.calls.append("option_chain")
        self.assert_current(as_of)
        return response(f"/api/stock/{ticker}/option-chains", [{"option_symbol": "QCOM260918C00180000"}])

    def oi_change(self, ticker: str, *, as_of: object = None, limit: int = 500, page: int | None = None, order: str = "desc") -> EndpointResponse:
        self.calls.append("open_interest")
        self.assert_current(as_of)
        if (limit, page, order) != (500, None, "desc"):
            raise AssertionError("unexpected OI collection parameters")
        return response(f"/api/stock/{ticker}/oi-change", [{
            "option_symbol": "QCOM260918C00180000", "curr_date": "2026-08-21", "last_date": "2026-08-20",
        }])

    def gex_levels(self, ticker: str, *, as_of: object = None) -> EndpointResponse:
        self.calls.append("dealer_exposure")
        self.assert_current(as_of)
        return response(f"/api/stock/{ticker}/gex-levels", {
            "date": "2026-08-21", "time": "2026-08-24T10:30:00Z",
            "call_wall": 180, "put_wall": 160, "gamma_flip": 170, "gamma_magnet": 172,
        })

    def ohlc(self, ticker: str, *, candle_size: str = "1d", timeframe: str = "1Y", end_date: object = None, limit: int | None = None) -> EndpointResponse:
        self.calls.append("ohlc")
        self.assert_current(end_date)
        if (candle_size, timeframe, limit) != ("1d", "1Y", 365):
            raise AssertionError("unexpected OHLC collection parameters")
        return response(f"/api/stock/{ticker}/ohlc/1d", [{"date": "2026-08-21"}])

    def earnings_history(self, ticker: str) -> EndpointResponse:
        self.calls.append("earnings")
        return response(f"/api/earnings/{ticker}", [{"report_date": "2026-09-01"}])

    def flow_alerts(self, ticker: str, *, unusual: bool = False, min_premium: float | None = None, max_dte: int | None = None, limit: int = 100, page: int | None = None) -> EndpointResponse:
        self.calls.append("flow_alerts")
        if unusual or limit != 100:
            raise AssertionError("unexpected flow collection parameters")
        return response("/api/option-trades/flow-alerts", [{"created_at": "2026-08-24T10:40:00Z"}])

    def darkpool_trades(self, ticker: str, *, as_of: object = None, newer_than: object = None, older_than: object = None, min_premium: float | None = None, limit: int = 500, order: str = "desc", order_by: str = "executed_at") -> EndpointResponse:
        self.calls.append("dark_pool")
        self.assert_current(as_of)
        if (limit, order, order_by) != (500, "desc", "executed_at"):
            raise AssertionError("unexpected dark pool collection parameters")
        return response(f"/api/darkpool/{ticker}", [{"executed_at": "2026-08-24T10:40:00Z"}])

    def news_headlines(self, ticker: str, *, major_only: bool = False, limit: int = 100, page: int | None = None) -> EndpointResponse:
        self.calls.append("news")
        if major_only or limit != 100:
            raise AssertionError("unexpected news collection parameters")
        return response("/api/news/headlines", [{"created_at": "2026-08-24T10:41:00Z"}])

    @staticmethod
    def assert_current(value: object) -> None:
        if value is not None:
            raise AssertionError("current collector must not request a prior-session date")


class SchemaFailClient(FakeCurrentClient):
    def news_headlines(self, ticker: str, **_kwargs: object) -> EndpointResponse:
        self.calls.append("news")
        raise ProviderSchemaError("headline timestamp unavailable")


class CurrentCollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.store = SnapshotStore(root / "edge.sqlite")
        self.budget = WeeklyRequestBudget(root / "usage.sqlite", weekly_cap=100, protected_reserve=10, clock=lambda: FETCHED)

    def tearDown(self) -> None:
        self.budget.close()
        self.store.close()
        self.directory.cleanup()

    def test_collects_current_raw_evidence_with_explicit_semantics(self) -> None:
        client = FakeCurrentClient()
        report = collect_current(
            client=client, snapshots=self.store, request_budget=self.budget, tickers=("qcom",),
            generated_at=FETCHED,
        )

        self.assertTrue(report.preflight_passed)
        self.assertEqual(24, report.max_transport_attempts)
        self.assertEqual(8, len(report.results))
        self.assertEqual(8, self.store.count())
        self.assertTrue(all(item.status is CurrentCaptureStatus.CAPTURED for item in report.results))
        self.assertEqual(
            ["option_chain", "open_interest", "dealer_exposure", "ohlc", "earnings", "flow_alerts", "dark_pool", "news"],
            client.calls,
        )
        chain = self.store.list(symbol="QCOM", dataset=Dataset.OPTION_CHAIN.value)[0]
        self.assertEqual("current", chain.envelope.metadata["capture_mode"])
        self.assertTrue(chain.envelope.metadata["raw_only"])
        self.assertFalse(chain.envelope.metadata["recommendations_enabled"])
        self.assertIn("last_tape_time", chain.envelope.metadata["current_vs_prior_session"])
        self.assertEqual(FETCHED, chain.envelope.as_of)
        oi = self.store.list(symbol="QCOM", dataset=Dataset.OPEN_INTEREST.value)[0]
        self.assertIn("curr_date", oi.envelope.metadata["current_vs_prior_session"])
        self.assertEqual("current", oi.envelope.metadata["capture_mode"])
        gex = self.store.list(symbol="QCOM", dataset=Dataset.DEALER_EXPOSURE.value)[0]
        self.assertEqual(datetime(2026, 8, 24, 10, 30, tzinfo=UTC), gex.envelope.as_of)
        self.assertFalse(report.recommendations_enabled)

    def test_explicit_empty_and_partial_gex_are_preserved_but_not_promoted(self) -> None:
        class GexStates(FakeCurrentClient):
            def gex_levels(self, ticker: str, *, as_of: object = None) -> EndpointResponse:
                self.calls.append("dealer_exposure")
                return response(f"/api/stock/{ticker}/gex-levels", {
                    "date": "2026-08-21", "time": "2026-08-24T10:30:00Z",
                    "call_wall": None, "put_wall": 160, "gamma_flip": 170, "gamma_magnet": 172,
                })

        report = collect_current(
            client=GexStates(), snapshots=self.store, request_budget=self.budget, tickers=("QCOM",),
            datasets=(CurrentDataset.DEALER_EXPOSURE,), generated_at=FETCHED,
        )
        self.assertEqual(CurrentCaptureStatus.PARTIAL, report.results[0].status)
        stored = self.store.list(symbol="QCOM", dataset=Dataset.DEALER_EXPOSURE.value)[0]
        self.assertEqual("partial", stored.envelope.metadata["response_status"])
        self.assertIn("partial GEX", report.results[0].reason or "")

    def test_preflight_blocks_every_item_without_provider_call(self) -> None:
        small_budget = WeeklyRequestBudget(
            Path(self.directory.name) / "small.sqlite", weekly_cap=10, protected_reserve=9, clock=lambda: FETCHED,
        )
        try:
            client = FakeCurrentClient()
            report = collect_current(
                client=client, snapshots=self.store, request_budget=small_budget, tickers=("QCOM",),
                datasets=(CurrentDataset.OPTION_CHAIN,), generated_at=FETCHED,
            )
        finally:
            small_budget.close()
        self.assertFalse(report.preflight_passed)
        self.assertEqual(CurrentCaptureStatus.BUDGET_BLOCKED, report.results[0].status)
        self.assertEqual([], client.calls)
        self.assertEqual(0, self.store.count())

    def test_one_dataset_failure_is_visible_and_does_not_block_other_raw_capture(self) -> None:
        report = collect_current(
            client=SchemaFailClient(), snapshots=self.store, request_budget=self.budget, tickers=("QCOM",),
            datasets=(CurrentDataset.OHLC, CurrentDataset.NEWS), generated_at=FETCHED,
        )
        self.assertEqual(CurrentCaptureStatus.CAPTURED, report.results[0].status)
        self.assertEqual(CurrentCaptureStatus.SCHEMA_MISMATCH, report.results[1].status)
        self.assertEqual(1, self.store.count())
        self.assertIsNone(report.results[1].snapshot_id)

    def test_rejects_invalid_transport_attempt_bound(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive integer"):
            collect_current(
                client=FakeCurrentClient(), snapshots=self.store, request_budget=self.budget, tickers=("QCOM",),
                max_transport_attempts_per_item="3",  # type: ignore[arg-type]
            )

    def test_reconstructs_latest_current_snapshot_and_marks_missing_pairs(self) -> None:
        initial = collect_current(
            client=FakeCurrentClient(), snapshots=self.store, request_budget=self.budget, tickers=("QCOM",),
            datasets=(CurrentDataset.OPTION_CHAIN,), generated_at=FETCHED,
        )
        later = datetime(2026, 8, 24, 10, 50, tzinfo=UTC)
        latest = self.store.insert(SnapshotEnvelope(
            provider="unusual_whales", dataset=Dataset.OPTION_CHAIN, symbol="QCOM",
            as_of=later, retrieved_at=later, payload={"data": []},
            metadata={
                "capture_mode": "current", "response_status": "empty", "response_row_count": 0,
                "provider_endpoint": "/api/stock/QCOM/option-chains",
            },
        ))
        before = self.store.count()
        report = reconstruct_current_report(
            self.store.path, datetime(2026, 8, 24, 10, 0, tzinfo=UTC),
            datetime(2026, 8, 24, 11, 0, tzinfo=UTC), ("QCOM",),
            datasets=(CurrentDataset.OPTION_CHAIN, CurrentDataset.NEWS), remaining_before_run=42,
        )

        self.assertEqual(before, self.store.count())
        self.assertEqual(datetime(2026, 8, 24, 10, 0, tzinfo=UTC).isoformat(), report.generated_at)
        self.assertEqual(42, report.remaining_transport_attempt_capacity_before_run)
        self.assertEqual(0, report.max_transport_attempts)
        self.assertEqual(latest.id, report.results[0].snapshot_id)
        self.assertEqual(CurrentCaptureStatus.EMPTY, report.results[0].status)
        self.assertEqual(CurrentCaptureStatus.UNAVAILABLE, report.results[1].status)
        self.assertIn("recovery window", report.results[1].reason or "")
        self.assertNotEqual(initial.results[0].snapshot_id, report.results[0].snapshot_id)

    def test_recovery_excludes_historical_or_out_of_window_snapshots(self) -> None:
        historical = self.store.insert(SnapshotEnvelope(
            provider="unusual_whales", dataset=Dataset.OPEN_INTEREST, symbol="QCOM",
            as_of=FETCHED, retrieved_at=FETCHED, payload={"data": [{"row": 1}]}, metadata={"capture_mode": "historical"},
        ))
        report = reconstruct_current_report(
            self.store.path, datetime(2026, 8, 24, 10, 0, tzinfo=UTC),
            datetime(2026, 8, 24, 11, 0, tzinfo=UTC), ("QCOM",),
            datasets=(CurrentDataset.OPEN_INTEREST,),
        )
        self.assertEqual(CurrentCaptureStatus.UNAVAILABLE, report.results[0].status)
        self.assertNotEqual(historical.id, report.results[0].snapshot_id)

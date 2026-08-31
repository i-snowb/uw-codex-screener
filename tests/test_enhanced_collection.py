from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest

from morning_edge.enhanced_collection import (
    EnhancedCaptureStatus,
    EnhancedDataset,
    collect_enhanced,
)
from morning_edge.models import Dataset
from morning_edge.providers.base import JsonResponse, RateLimitMetadata, RawResponse
from morning_edge.providers.budget import WeeklyRequestBudget
from morning_edge.providers.unusual_whales import EndpointResponse
from morning_edge.store import SnapshotStore


FETCHED = datetime(2026, 8, 24, 20, 30, tzinfo=UTC)


def response(endpoint: str, rows: object, *, field: str = "data") -> EndpointResponse:
    payload = {field: rows}
    raw = RawResponse(
        provider="unusual_whales", method="GET", url=f"https://api.unusualwhales.com{endpoint}",
        status_code=200, headers={}, body=b"{}", fetched_at=FETCHED, attempts=1,
        rate_limit=RateLimitMetadata(limit=30_000, remaining=25_000),
    )
    return EndpointResponse(endpoint, JsonResponse(payload=payload, raw=raw), data_field=field)  # type: ignore[arg-type]


class FakeEnhancedClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def greek_exposure_by_strike(self, ticker: str, *, as_of: object = None) -> EndpointResponse:
        self.calls.append(f"gex:{ticker}")
        return response(f"/api/stock/{ticker}/greek-exposure/strike", [{"strike": "170"}])

    def greek_flow(self, ticker: str, *, as_of: object = None) -> EndpointResponse:
        self.calls.append(f"flow:{ticker}")
        return response(f"/api/stock/{ticker}/greek-flow", [{"timestamp": "2026-08-24T15:55:00-04:00"}])

    def iv_term_structure(self, ticker: str, *, as_of: object = None) -> EndpointResponse:
        return response(f"/api/stock/{ticker}/volatility/term-structure", [])

    def volatility_stats(self, ticker: str, *, as_of: object = None) -> EndpointResponse:
        return response(f"/api/stock/{ticker}/volatility/stats", {})

    def interpolated_iv(self, ticker: str, *, as_of: object = None) -> EndpointResponse:
        return response(f"/api/stock/{ticker}/interpolated-iv", [])

    def darkpool_price_levels(self, ticker: str, *, as_of: object = None) -> EndpointResponse:
        return response(f"/api/darkpool/{ticker}/price-levels", [])

    def short_interest_float(self, ticker: str) -> EndpointResponse:
        return response(f"/api/shorts/{ticker}/interest-float/v2", [])

    def short_borrow(self, ticker: str) -> EndpointResponse:
        return response(f"/api/shorts/{ticker}/data", [])

    def short_volume_ratio(self, ticker: str) -> EndpointResponse:
        return response(f"/api/shorts/{ticker}/volume-and-ratio", [], field="si")

    def market_tide(self, *, as_of: object = None, otm_only: bool = False, interval_5m: bool = True) -> EndpointResponse:
        self.calls.append("market")
        return response("/api/market/market-tide", [{"timestamp": "2026-08-24T15:55:00-04:00"}])

    def sector_tide(self, sector: str, *, as_of: object = None) -> EndpointResponse:
        self.calls.append(f"sector:{sector}")
        return response(f"/api/market/{sector}/sector-tide", [{"timestamp": "2026-08-24T15:55:00-04:00"}])


class EnhancedCollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.store = SnapshotStore(root / "edge.sqlite")
        self.budget = WeeklyRequestBudget(root / "usage.sqlite", weekly_cap=100, protected_reserve=10, clock=lambda: FETCHED)

    def tearDown(self) -> None:
        self.budget.close()
        self.store.close()
        self.directory.cleanup()

    def test_global_feeds_are_collected_once_not_once_per_ticker(self) -> None:
        client = FakeEnhancedClient()
        report = collect_enhanced(
            client=client, snapshots=self.store, request_budget=self.budget,
            tickers=("QCOM", "AAOI"),
            datasets=(
                EnhancedDataset.GREEK_EXPOSURE_STRIKE,
                EnhancedDataset.MARKET_TIDE,
                EnhancedDataset.SECTOR_TIDE_TECHNOLOGY,
            ),
            generated_at=FETCHED,
        )

        self.assertEqual(4, report.logical_items)
        self.assertEqual(12, report.max_transport_attempts)
        self.assertEqual(["gex:QCOM", "gex:AAOI", "market", "sector:Technology"], client.calls)
        self.assertTrue(all(item.status is EnhancedCaptureStatus.CAPTURED for item in report.results))
        self.assertEqual(4, self.store.count())
        market = self.store.list(symbol="MARKET", dataset=Dataset.MARKET_TIDE.value)[0]
        self.assertEqual("enhanced_current", market.envelope.metadata["capture_mode"])

    def test_empty_response_is_stored_as_empty_not_zero_evidence(self) -> None:
        report = collect_enhanced(
            client=FakeEnhancedClient(), snapshots=self.store, request_budget=self.budget,
            tickers=("QCOM",), datasets=(EnhancedDataset.IV_TERM_STRUCTURE,), generated_at=FETCHED,
        )

        self.assertEqual(EnhancedCaptureStatus.EMPTY, report.results[0].status)
        self.assertEqual(0, report.results[0].row_count)
        self.assertEqual("empty", self.store.list(symbol="QCOM", dataset=Dataset.IV_TERM_STRUCTURE.value)[0].envelope.metadata["response_status"])

    def test_preflight_blocks_before_any_provider_call(self) -> None:
        small = WeeklyRequestBudget(Path(self.directory.name) / "small.sqlite", weekly_cap=3, protected_reserve=2, clock=lambda: FETCHED)
        try:
            client = FakeEnhancedClient()
            report = collect_enhanced(
                client=client, snapshots=self.store, request_budget=small,
                tickers=("QCOM",), datasets=(EnhancedDataset.GREEK_FLOW,), generated_at=FETCHED,
            )
        finally:
            small.close()
        self.assertFalse(report.preflight_passed)
        self.assertEqual([], client.calls)
        self.assertEqual(EnhancedCaptureStatus.BUDGET_BLOCKED, report.results[0].status)


if __name__ == "__main__":
    unittest.main()

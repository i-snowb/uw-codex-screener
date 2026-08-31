from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from morning_edge.backfill import BackfillStore, CoverageState, collect, make_plan, plan_preview
from morning_edge.models import Dataset
from morning_edge.providers.base import JsonResponse, RawResponse, RateLimitMetadata
from morning_edge.providers.unusual_whales import EndpointResponse
from morning_edge.store import SnapshotStore


FETCHED = datetime(2026, 8, 23, 15, 0, tzinfo=UTC)


def response(endpoint: str, data: object) -> EndpointResponse:
    raw = RawResponse(
        provider="unusual_whales",
        method="GET",
        url=f"https://api.unusualwhales.com{endpoint}",
        status_code=200,
        headers={"x-request-id": "test-request"},
        body=b'{"data":[]}',
        fetched_at=FETCHED,
        attempts=1,
        rate_limit=RateLimitMetadata(limit=30000, remaining=29999),
    )
    return EndpointResponse(endpoint, JsonResponse(payload={"data": data}, raw=raw))  # type: ignore[arg-type]


class FakeHistoricalClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def option_chain(self, ticker: str, *, as_of: date, greeks: bool = True) -> EndpointResponse:
        self.calls.append(("option_chain", as_of.isoformat()))
        return response(
            f"/api/stock/{ticker}/option-chains",
            [{"raw": True, "last_tape_time": f"{as_of.isoformat()}T21:45:00Z"}],
        )

    def oi_change(self, ticker: str, *, as_of: date, limit: int = 500, page: int | None = None, order: str = "desc") -> EndpointResponse:
        self.calls.append(("open_interest", f"{as_of.isoformat()}:{page}"))
        return response(f"/api/stock/{ticker}/oi-change", [{"raw": True}])

    def gex_levels(self, ticker: str, *, as_of: date) -> EndpointResponse:
        self.calls.append(("dealer_exposure", as_of.isoformat()))
        return response(f"/api/stock/{ticker}/gex-levels", {"raw": True})

    def darkpool_trades(self, ticker: str, *, as_of: date, limit: int = 500, older_than: str | int | None = None, order: str = "desc", order_by: str = "executed_at") -> EndpointResponse:
        self.calls.append(("dark_pool", as_of.isoformat()))
        return response(f"/api/darkpool/{ticker}", [])

    def ohlc(
        self, ticker: str, *, candle_size: str, timeframe: str,
        limit: int, end_date: date | str | None = None,
    ) -> EndpointResponse:
        self.calls.append(("ohlc", f"{ticker}:{end_date}"))
        return response(
            f"/api/stock/{ticker}/ohlc/1d",
            [{"date": str(end_date), "market_time": "r", "raw": True}],
        )

    def earnings_history(self, ticker: str) -> EndpointResponse:
        self.calls.append(("earnings", ticker))
        return response(f"/api/earnings/{ticker}", [{"raw": True}])

    def flow_alerts(
        self, ticker: str, *, unusual: bool = False,
        newer_than: str | int | None = None, older_than: str | int | None = None,
        limit: int = 100, page: int | None = None,
    ) -> EndpointResponse:
        self.calls.append(("flow_alerts", f"{newer_than}:{older_than}:{unusual}:{limit}"))
        return response(
            "/api/option-trades/flow-alerts",
            [{"created_at": "2026-08-21T14:30:00Z", "ticker": ticker}],
        )


class FailIfCalledClient:
    """A terminal plan must not resolve or call any provider method."""

    def __getattr__(self, name: str) -> object:
        def fail(*_args: object, **_kwargs: object) -> EndpointResponse:
            raise AssertionError(f"provider method should not be called: {name}")
        return fail


class EmptyGexHistoricalClient(FakeHistoricalClient):
    def gex_levels(self, ticker: str, *, as_of: date) -> EndpointResponse:
        self.calls.append(("dealer_exposure", as_of.isoformat()))
        return response(
            f"/api/stock/{ticker}/gex-levels",
            {
                "date": as_of.isoformat(),
                "time": None,
                "call_wall": None,
                "put_wall": None,
                "gamma_flip": None,
                "gamma_magnet": None,
            },
        )


class PartialGexHistoricalClient(FakeHistoricalClient):
    def gex_levels(self, ticker: str, *, as_of: date) -> EndpointResponse:
        self.calls.append(("dealer_exposure", as_of.isoformat()))
        return response(
            f"/api/stock/{ticker}/gex-levels",
            {
                "date": as_of.isoformat(),
                "time": f"{as_of.isoformat()}T19:59:36Z",
                "call_wall": None,
                "put_wall": "21",
                "gamma_flip": "20.5",
                "gamma_magnet": "21",
            },
        )


def oi_rows(as_of: date, start: int, count: int) -> list[dict[str, str]]:
    return [
        {
            "curr_date": as_of.isoformat(),
            "last_date": "2026-08-20",
            "option_symbol": f"QCOM260925C{start + index:08d}",
        }
        for index in range(count)
    ]


class PagedOiClient(FakeHistoricalClient):
    def __init__(self, pages: dict[int, object]) -> None:
        super().__init__()
        self.pages = pages

    def oi_change(self, ticker: str, *, as_of: date, limit: int = 500, page: int | None = None, order: str = "desc") -> EndpointResponse:
        self.calls.append(("open_interest", f"{as_of.isoformat()}:{page}:{limit}:{order}"))
        if limit != 500 or order != "desc" or page is None:
            raise AssertionError("OI pagination contract was not used")
        return response(f"/api/stock/{ticker}/oi-change?page={page}", self.pages[page])


def dark_pool_rows(end: datetime, start: int, count: int) -> list[dict[str, str]]:
    return [
        {
            "tracking_id": f"dark-{start + index}",
            "executed_at": (end - timedelta(seconds=index)).isoformat().replace("+00:00", "Z"),
        }
        for index in range(count)
    ]


class CursorDarkPoolClient(FakeHistoricalClient):
    def __init__(self, pages: list[object]) -> None:
        super().__init__()
        self.pages = list(pages)
        self.cursor_calls: list[tuple[str | int | None, int, str, str]] = []

    def darkpool_trades(self, ticker: str, *, as_of: date, limit: int = 500, older_than: str | int | None = None, order: str = "desc", order_by: str = "executed_at") -> EndpointResponse:
        self.cursor_calls.append((older_than, limit, order, order_by))
        if limit != 500 or order != "desc" or order_by != "executed_at":
            raise AssertionError("dark-pool cursor contract was not used")
        if not self.pages:
            raise AssertionError("unexpected dark-pool request")
        return response(f"/api/darkpool/{ticker}?older_than={older_than}", self.pages.pop(0))


class BackfillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = SnapshotStore(Path(self.directory.name) / "edge.sqlite")

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def test_dry_plan_is_bounded_and_weekday_only(self) -> None:
        plan = make_plan(
            provider="unusual_whales",
            start_date=date(2026, 8, 21),  # Friday
            end_date=date(2026, 8, 24),  # Monday
            tickers=("qcom",),
            datasets=("ohlc", "option_chain"),
        )
        preview = plan_preview(plan, logical_item_cap=2)
        self.assertEqual(3, preview["planned_items"])
        self.assertEqual(1, preview["will_defer"])
        self.assertFalse(preview["network_called"])
        self.assertEqual(0, self.store.count())
        ohlc_item = next(item for item in plan.items() if item.dataset == "ohlc")
        self.assertEqual(date(2026, 8, 24), ohlc_item.requested_date)

    def test_collects_complete_historical_option_chain_after_adapter_date_validation(self) -> None:
        plan = make_plan(
            provider="unusual_whales",
            start_date=date(2026, 8, 21),
            end_date=date(2026, 8, 21),
            tickers=("QCOM",),
            datasets=("option_chain",),
        )
        result = collect(
            client=FakeHistoricalClient(), snapshots=self.store, plan=plan,
            max_requests=1, audit_accepted=True,
        )
        self.assertEqual(1, self.store.count())
        self.assertEqual(1, result["state_counts"][CoverageState.COLLECTED.value])
        self.assertEqual(0, result["state_counts"][CoverageState.SCOPE_UNVERIFIED.value])
        stored = self.store.list(symbol="QCOM", dataset=Dataset.OPTION_CHAIN.value)[0]
        self.assertEqual(
            "requested_market_date_at_utc_midnight; response last_tape_time dates validated; "
            "historical scope confirmed by live probe; contract timestamps otherwise unnormalized",
            stored.envelope.metadata["as_of_source"],
        )
        self.assertTrue(stored.envelope.metadata["raw_only"])
        self.assertEqual("verified_not_required", stored.envelope.metadata["pagination_status"])
        self.assertEqual("verified_option_chain_date_scope", stored.envelope.metadata["historical_scope_status"])

    def test_flow_alert_history_is_bounded_to_new_york_session(self) -> None:
        plan = make_plan(
            provider="unusual_whales", start_date=date(2026, 8, 21), end_date=date(2026, 8, 21),
            tickers=("QCOM",), datasets=("flow_alerts",),
        )
        client = FakeHistoricalClient()

        result = collect(
            client=client, snapshots=self.store, plan=plan,
            max_requests=1, audit_accepted=True,
        )

        self.assertEqual(1, result["state_counts"][CoverageState.COLLECTED.value])
        self.assertEqual(
            [("flow_alerts", "2026-08-21T04:00:00.000000Z:2026-08-22T04:00:00.000000Z:False:200")],
            client.calls,
        )
        stored = self.store.list(symbol="QCOM", dataset=Dataset.OPTION_FLOW.value)[0]
        self.assertEqual("2026-08-21", stored.envelope.metadata["requested_market_date"])
        self.assertEqual("verified_flow_alert_date_scope", stored.envelope.metadata["historical_scope_status"])

    def test_flow_alert_history_rejects_rows_outside_requested_new_york_date(self) -> None:
        class MismatchedFlowClient(FakeHistoricalClient):
            def flow_alerts(self, ticker: str, **_kwargs: object) -> EndpointResponse:
                return response(
                    "/api/option-trades/flow-alerts",
                    [{"created_at": "2026-08-22T04:01:00Z", "ticker": ticker}],
                )

        plan = make_plan(
            provider="unusual_whales", start_date=date(2026, 8, 21), end_date=date(2026, 8, 21),
            tickers=("QCOM",), datasets=("flow_alerts",),
        )
        result = collect(
            client=MismatchedFlowClient(), snapshots=self.store, plan=plan,
            max_requests=1, audit_accepted=True,
        )
        self.assertEqual(1, result["state_counts"][CoverageState.SCOPE_UNVERIFIED.value])

    def test_historical_option_chain_scope_mismatch_is_preserved_but_not_complete(self) -> None:
        class MismatchedChainClient(FakeHistoricalClient):
            def option_chain(self, ticker: str, *, as_of: date, greeks: bool = True) -> EndpointResponse:
                return response(
                    f"/api/stock/{ticker}/option-chains",
                    [{"raw": True, "last_tape_time": "2026-08-20T21:45:00Z"}],
                )

        plan = make_plan(
            provider="unusual_whales", start_date=date(2026, 8, 21), end_date=date(2026, 8, 21),
            tickers=("QCOM",), datasets=("option_chain",),
        )
        result = collect(
            client=MismatchedChainClient(), snapshots=self.store, plan=plan,
            max_requests=1, audit_accepted=True,
        )

        self.assertEqual(1, self.store.count())
        self.assertEqual(1, result["state_counts"][CoverageState.SCOPE_UNVERIFIED.value])
        stored = self.store.list(symbol="QCOM", dataset=Dataset.OPTION_CHAIN.value)[0]
        self.assertEqual(
            "option_chain_date_scope_mismatch",
            stored.envelope.metadata["historical_scope_status"],
        )

    def test_resume_does_not_repeat_terminal_option_chain_capture(self) -> None:
        plan = make_plan(
            provider="unusual_whales", start_date=date(2026, 8, 21), end_date=date(2026, 8, 21),
            tickers=("QCOM",), datasets=("option_chain",),
        )
        client = FakeHistoricalClient()
        collect(client=client, snapshots=self.store, plan=plan, max_requests=1, audit_accepted=True)
        result = collect(
            client=FailIfCalledClient(),  # type: ignore[arg-type]
            snapshots=self.store,
            plan=plan,
            max_requests=1,
            audit_accepted=True,
        )
        self.assertEqual(1, len(client.calls))
        self.assertEqual(0, result["attempted_logical_items"])
        self.assertFalse(result["network_called"])
        self.assertEqual(1, self.store.count())

    def test_empty_response_is_explicit_and_resumable(self) -> None:
        plan = make_plan(
            provider="unusual_whales", start_date=date(2026, 8, 21), end_date=date(2026, 8, 21),
            tickers=("QCOM",), datasets=("dark_pool",),
        )
        result = collect(
            client=FakeHistoricalClient(), snapshots=self.store, plan=plan,
            max_requests=1, audit_accepted=True,
        )
        self.assertEqual(1, result["state_counts"][CoverageState.EMPTY.value])
        coverage = BackfillStore(self.store).coverage(plan)
        self.assertEqual("empty", coverage["incomplete_or_unverified"][0]["state"])

    def test_all_null_gex_shell_is_empty_not_collected(self) -> None:
        plan = make_plan(
            provider="unusual_whales", start_date=date(2026, 8, 21), end_date=date(2026, 8, 21),
            tickers=("CBRS",), datasets=("dealer_exposure",),
        )
        result = collect(
            client=EmptyGexHistoricalClient(), snapshots=self.store, plan=plan,
            max_requests=1, audit_accepted=True,
        )

        self.assertEqual(1, result["state_counts"][CoverageState.EMPTY.value])
        self.assertEqual(0, result["state_counts"][CoverageState.COLLECTED.value])
        coverage = BackfillStore(self.store).coverage(plan)
        self.assertEqual("empty", coverage["incomplete_or_unverified"][0]["state"])
        stored = self.store.list(symbol="CBRS", dataset=Dataset.DEALER_EXPOSURE.value)[0]
        self.assertEqual("empty_gex_no_observation_time", stored.envelope.metadata["historical_scope_status"])
        self.assertIn("no provider observation time", stored.envelope.metadata["as_of_source"])

    def test_partial_gex_is_retained_but_terminally_excluded_from_derived_gex(self) -> None:
        plan = make_plan(
            provider="unusual_whales", start_date=date(2026, 7, 27), end_date=date(2026, 7, 27),
            tickers=("AAOI",), datasets=("dealer_exposure",),
        )
        result = collect(
            client=PartialGexHistoricalClient(), snapshots=self.store, plan=plan,
            max_requests=1, audit_accepted=True,
        )

        self.assertEqual(1, self.store.count())
        self.assertEqual(1, result["state_counts"][CoverageState.SCOPE_UNVERIFIED.value])
        stored = self.store.list(symbol="AAOI", dataset=Dataset.DEALER_EXPOSURE.value)[0]
        self.assertEqual("partial_gex_level_set", stored.envelope.metadata["historical_scope_status"])
        self.assertFalse(stored.envelope.metadata["derived_gex_eligible"])
        self.assertEqual(["call_wall"], stored.envelope.metadata["gex_unusable_level_names"])
        self.assertIn("partial_gex_level_set: call_wall", stored.envelope.metadata["derived_gex_exclusion_reason"])
        coverage = BackfillStore(self.store).coverage(plan)
        self.assertEqual("scope_unverified", coverage["incomplete_or_unverified"][0]["state"])

    def test_oi_collects_all_pages_and_records_page_lineage(self) -> None:
        as_of = date(2026, 8, 21)
        plan = make_plan(
            provider="unusual_whales", start_date=as_of, end_date=as_of,
            tickers=("QCOM",), datasets=("open_interest",),
        )
        client = PagedOiClient({0: oi_rows(as_of, 0, 500), 1: oi_rows(as_of, 500, 2)})

        result = collect(client=client, snapshots=self.store, plan=plan, max_requests=2, audit_accepted=True)

        self.assertEqual(2, result["attempted_logical_items"])
        self.assertEqual(1, result["state_counts"][CoverageState.COLLECTED.value])
        self.assertEqual(2, self.store.count())
        stored = self.store.list(symbol="QCOM", dataset=Dataset.OPEN_INTEREST.value, limit=2)
        self.assertEqual({0, 1}, {snapshot.envelope.metadata["pagination_page"] for snapshot in stored})
        self.assertEqual(
            [("open_interest", "2026-08-21:0:500:desc"), ("open_interest", "2026-08-21:1:500:desc")],
            client.calls,
        )

    def test_oi_budget_resume_starts_at_next_page_without_refetch(self) -> None:
        as_of = date(2026, 8, 21)
        plan = make_plan(
            provider="unusual_whales", start_date=as_of, end_date=as_of,
            tickers=("QCOM",), datasets=("open_interest",),
        )
        first = PagedOiClient({0: oi_rows(as_of, 0, 500)})
        partial = collect(client=first, snapshots=self.store, plan=plan, max_requests=1, audit_accepted=True)
        self.assertEqual(1, partial["attempted_logical_items"])
        self.assertEqual(1, partial["state_counts"][CoverageState.BUDGET_EXHAUSTED.value])

        resumed = PagedOiClient({1: oi_rows(as_of, 500, 1)})
        complete = collect(client=resumed, snapshots=self.store, plan=plan, max_requests=1, audit_accepted=True)
        self.assertEqual(1, complete["attempted_logical_items"])
        self.assertEqual([("open_interest", "2026-08-21:1:500:desc")], resumed.calls)
        self.assertEqual(1, complete["state_counts"][CoverageState.COLLECTED.value])
        self.assertEqual(2, self.store.count())

    def test_oi_scope_mismatch_is_saved_then_terminal(self) -> None:
        as_of = date(2026, 8, 21)
        plan = make_plan(
            provider="unusual_whales", start_date=as_of, end_date=as_of,
            tickers=("QCOM",), datasets=("open_interest",),
        )
        client = PagedOiClient({0: [{"curr_date": "2026-08-20", "option_symbol": "QCOM260925C00000100"}]})
        result = collect(client=client, snapshots=self.store, plan=plan, max_requests=1, audit_accepted=True)

        self.assertEqual(1, self.store.count())
        self.assertEqual(1, result["state_counts"][CoverageState.SCOPE_UNVERIFIED.value])
        replay = collect(client=FailIfCalledClient(), snapshots=self.store, plan=plan, max_requests=1, audit_accepted=True)  # type: ignore[arg-type]
        self.assertEqual(0, replay["attempted_logical_items"])

    def test_oi_repeated_page_is_saved_then_terminal(self) -> None:
        as_of = date(2026, 8, 21)
        first_page = oi_rows(as_of, 0, 500)
        plan = make_plan(
            provider="unusual_whales", start_date=as_of, end_date=as_of,
            tickers=("QCOM",), datasets=("open_interest",),
        )
        result = collect(
            client=PagedOiClient({0: first_page, 1: first_page}),
            snapshots=self.store, plan=plan, max_requests=2, audit_accepted=True,
        )

        self.assertEqual(2, self.store.count())
        self.assertEqual(1, result["state_counts"][CoverageState.SCOPE_UNVERIFIED.value])
        replay = collect(client=FailIfCalledClient(), snapshots=self.store, plan=plan, max_requests=1, audit_accepted=True)  # type: ignore[arg-type]
        self.assertEqual(0, replay["attempted_logical_items"])

    def test_terminal_oi_replay_does_not_call_provider(self) -> None:
        as_of = date(2026, 8, 21)
        plan = make_plan(
            provider="unusual_whales", start_date=as_of, end_date=as_of,
            tickers=("QCOM",), datasets=("open_interest",),
        )
        collect(
            client=PagedOiClient({0: oi_rows(as_of, 0, 1)}),
            snapshots=self.store, plan=plan, max_requests=1, audit_accepted=True,
        )
        replay = collect(client=FailIfCalledClient(), snapshots=self.store, plan=plan, max_requests=1, audit_accepted=True)  # type: ignore[arg-type]
        self.assertEqual(0, replay["attempted_logical_items"])
        self.assertFalse(replay["network_called"])

    def test_dark_pool_short_first_page_completes_with_cursor_metadata(self) -> None:
        as_of = date(2026, 8, 21)
        plan = make_plan(
            provider="unusual_whales", start_date=as_of, end_date=as_of,
            tickers=("QCOM",), datasets=("dark_pool",),
        )
        client = CursorDarkPoolClient([dark_pool_rows(datetime(2026, 8, 21, 20, tzinfo=UTC), 0, 2)])

        result = collect(client=client, snapshots=self.store, plan=plan, max_requests=1, audit_accepted=True)

        self.assertEqual(1, result["state_counts"][CoverageState.COLLECTED.value])
        self.assertEqual([(None, 500, "desc", "executed_at")], client.cursor_calls)
        stored = self.store.list(symbol="QCOM", dataset=Dataset.DARK_POOL.value)[0]
        self.assertEqual("dark_pool_cursor", stored.envelope.metadata["pagination_family"])
        self.assertEqual(0, stored.envelope.metadata["pagination_page"])
        self.assertIsNone(stored.envelope.metadata["pagination_cursor_before"])

    def test_dark_pool_multi_page_overlap_dedupes_and_completes(self) -> None:
        as_of = date(2026, 8, 21)
        end = datetime(2026, 8, 21, 20, tzinfo=UTC)
        first = dark_pool_rows(end, 0, 500)
        oldest = end - timedelta(seconds=499)
        second = [first[-1], *dark_pool_rows(oldest - timedelta(seconds=1), 500, 499)]
        third = dark_pool_rows(oldest - timedelta(seconds=501), 999, 1)
        plan = make_plan(
            provider="unusual_whales", start_date=as_of, end_date=as_of,
            tickers=("QCOM",), datasets=("dark_pool",),
        )
        client = CursorDarkPoolClient([first, second, third])

        result = collect(client=client, snapshots=self.store, plan=plan, max_requests=3, audit_accepted=True)

        self.assertEqual(3, result["attempted_logical_items"])
        self.assertEqual(1, result["state_counts"][CoverageState.COLLECTED.value])
        self.assertEqual(3, self.store.count())
        self.assertIsNone(client.cursor_calls[0][0])
        self.assertIsNotNone(client.cursor_calls[1][0])
        self.assertIsNotNone(client.cursor_calls[2][0])

    def test_dark_pool_requested_to_older_date_boundary_completes(self) -> None:
        as_of = date(2026, 8, 21)
        requested_rows = dark_pool_rows(datetime(2026, 8, 21, 20, tzinfo=UTC), 0, 479)
        older_rows = dark_pool_rows(datetime(2026, 8, 21, 3, 59, tzinfo=UTC), 1000, 21)
        plan = make_plan(
            provider="unusual_whales", start_date=as_of, end_date=as_of,
            tickers=("INTC",), datasets=("dark_pool",),
        )

        result = collect(
            client=CursorDarkPoolClient([requested_rows + older_rows]), snapshots=self.store,
            plan=plan, max_requests=1, audit_accepted=True,
        )

        self.assertEqual(1, result["state_counts"][CoverageState.COLLECTED.value])
        event = BackfillStore(self.store).events(plan.items()[0])[-1]
        self.assertEqual("date_boundary_crossed", event[2]["stop_reason"])
        self.assertEqual(479, event[2]["requested_row_count"])
        self.assertEqual(21, event[2]["ignored_older_row_count"])
        self.assertEqual(500, len(self.store.list(symbol="INTC", dataset=Dataset.DARK_POOL.value)[0].envelope.payload["data"]))

    def test_dark_pool_interleaved_date_boundary_is_terminal_scope_failure(self) -> None:
        as_of = date(2026, 8, 21)
        rows = [
            {"tracking_id": "requested-first", "executed_at": "2026-08-21T20:00:00Z"},
            {"tracking_id": "older", "executed_at": "2026-08-21T03:59:00Z"},
            {"tracking_id": "requested-after", "executed_at": "2026-08-21T04:00:00Z"},
        ]
        plan = make_plan(
            provider="unusual_whales", start_date=as_of, end_date=as_of,
            tickers=("INTC",), datasets=("dark_pool",),
        )
        result = collect(
            client=CursorDarkPoolClient([rows]), snapshots=self.store,
            plan=plan, max_requests=1, audit_accepted=True,
        )
        self.assertEqual(1, result["state_counts"][CoverageState.SCOPE_UNVERIFIED.value])

    def test_dark_pool_non_descending_timestamps_are_terminal_scope_failure(self) -> None:
        as_of = date(2026, 8, 21)
        rows = [
            {"tracking_id": "later", "executed_at": "2026-08-21T19:00:00Z"},
            {"tracking_id": "earlier-in-response", "executed_at": "2026-08-21T20:00:00Z"},
        ]
        plan = make_plan(
            provider="unusual_whales", start_date=as_of, end_date=as_of,
            tickers=("INTC",), datasets=("dark_pool",),
        )
        result = collect(
            client=CursorDarkPoolClient([rows]), snapshots=self.store,
            plan=plan, max_requests=1, audit_accepted=True,
        )
        self.assertEqual(1, result["state_counts"][CoverageState.SCOPE_UNVERIFIED.value])

    def test_dark_pool_short_initial_all_older_page_is_explicit_empty_boundary(self) -> None:
        as_of = date(2026, 8, 21)
        plan = make_plan(
            provider="unusual_whales", start_date=as_of, end_date=as_of,
            tickers=("INTC",), datasets=("dark_pool",),
        )
        result = collect(
            client=CursorDarkPoolClient([dark_pool_rows(datetime(2026, 8, 21, 3, 59, tzinfo=UTC), 0, 2)]),
            snapshots=self.store, plan=plan, max_requests=1, audit_accepted=True,
        )
        self.assertEqual(1, result["state_counts"][CoverageState.EMPTY.value])
        event = BackfillStore(self.store).events(plan.items()[0])[-1]
        self.assertEqual("date_boundary_crossed", event[2]["stop_reason"])

    def test_dark_pool_budget_resume_reuses_saved_cursor(self) -> None:
        as_of = date(2026, 8, 21)
        first = dark_pool_rows(datetime(2026, 8, 21, 20, tzinfo=UTC), 0, 500)
        plan = make_plan(
            provider="unusual_whales", start_date=as_of, end_date=as_of,
            tickers=("QCOM",), datasets=("dark_pool",),
        )
        initial = CursorDarkPoolClient([first])
        collect(client=initial, snapshots=self.store, plan=plan, max_requests=1, audit_accepted=True)

        resumed = CursorDarkPoolClient([dark_pool_rows(datetime(2026, 8, 21, 19, tzinfo=UTC), 500, 1)])
        result = collect(client=resumed, snapshots=self.store, plan=plan, max_requests=1, audit_accepted=True)
        self.assertEqual(1, result["state_counts"][CoverageState.COLLECTED.value])
        self.assertIsNotNone(resumed.cursor_calls[0][0])
        self.assertEqual(2, self.store.count())

    def test_dark_pool_date_mismatch_is_stored_then_terminal(self) -> None:
        as_of = date(2026, 8, 21)
        plan = make_plan(
            provider="unusual_whales", start_date=as_of, end_date=as_of,
            tickers=("QCOM",), datasets=("dark_pool",),
        )
        bad = [{"tracking_id": "bad", "executed_at": "2026-08-22T16:00:00Z"}]
        result = collect(
            client=CursorDarkPoolClient([bad]), snapshots=self.store, plan=plan,
            max_requests=1, audit_accepted=True,
        )
        self.assertEqual(1, self.store.count())
        self.assertEqual(1, result["state_counts"][CoverageState.SCOPE_UNVERIFIED.value])
        replay = collect(client=FailIfCalledClient(), snapshots=self.store, plan=plan, max_requests=1, audit_accepted=True)  # type: ignore[arg-type]
        self.assertEqual(0, replay["attempted_logical_items"])

    def test_dark_pool_cursor_no_progress_is_stored_then_terminal(self) -> None:
        as_of = date(2026, 8, 21)
        end = datetime(2026, 8, 21, 20, tzinfo=UTC)
        first = dark_pool_rows(end, 0, 500)
        no_progress = dark_pool_rows(end, 1000, 500)
        plan = make_plan(
            provider="unusual_whales", start_date=as_of, end_date=as_of,
            tickers=("QCOM",), datasets=("dark_pool",),
        )
        result = collect(
            client=CursorDarkPoolClient([first, no_progress]), snapshots=self.store, plan=plan,
            max_requests=2, audit_accepted=True,
        )
        self.assertEqual(2, self.store.count())
        self.assertEqual(1, result["state_counts"][CoverageState.SCOPE_UNVERIFIED.value])

    def test_terminal_dark_pool_replay_does_not_call_provider(self) -> None:
        as_of = date(2026, 8, 21)
        plan = make_plan(
            provider="unusual_whales", start_date=as_of, end_date=as_of,
            tickers=("QCOM",), datasets=("dark_pool",),
        )
        collect(
            client=CursorDarkPoolClient([dark_pool_rows(datetime(2026, 8, 21, 20, tzinfo=UTC), 0, 1)]),
            snapshots=self.store, plan=plan, max_requests=1, audit_accepted=True,
        )
        replay = collect(client=FailIfCalledClient(), snapshots=self.store, plan=plan, max_requests=1, audit_accepted=True)  # type: ignore[arg-type]
        self.assertEqual(0, replay["attempted_logical_items"])
        self.assertFalse(replay["network_called"])

    def test_live_collection_refuses_without_audit_acknowledgement(self) -> None:
        plan = make_plan(
            provider="unusual_whales", start_date=date(2026, 8, 21), end_date=date(2026, 8, 21),
            tickers=("QCOM",), datasets=("ohlc",),
        )
        with self.assertRaisesRegex(ValueError, "audit acceptance"):
            collect(client=FakeHistoricalClient(), snapshots=self.store, plan=plan, max_requests=1, audit_accepted=False)
        self.assertEqual(0, self.store.count())


if __name__ == "__main__":
    unittest.main()

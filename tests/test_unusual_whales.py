"""Offline contract tests for the Unusual Whales adapter."""

from __future__ import annotations

from email.message import Message
import io
import json
import stat
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from morning_edge.audit import AuditStatus, run_trial_audit  # noqa: E402
from morning_edge.providers.base import (  # noqa: E402
    JsonlRawResponseCapture,
    PaginationLimitError,
    ProviderResponseError,
    ProviderSchemaError,
    SafeGetClient,
    _RejectRedirectHandler,
    read_bounded_body,
)
from morning_edge.providers.budget import WeeklyRequestBudget  # noqa: E402
from morning_edge.providers.unusual_whales import UnusualWhalesClient  # noqa: E402


class ScriptedTransport:
    def __init__(self, responses: list[tuple[int, dict[str, str], object]]) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[str, dict[str, str], float]] = []

    def __call__(self, url: str, headers: dict[str, str], timeout_seconds: float):
        self.requests.append((url, dict(headers), timeout_seconds))
        status, response_headers, payload = self.responses.pop(0)
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return status, response_headers, body


class RoutingTransport:
    """One deterministic JSON response per path; no network is ever opened."""

    def __init__(self) -> None:
        self.requests: list[str] = []

    def __call__(self, url: str, headers: dict[str, str], timeout_seconds: float):
        self.requests.append(url)
        path = urlparse(url).path
        data: object
        if path.endswith("/option-chains"):
            data = [{"option_symbol": "QCOM260117C00180000", "strike": "180", "expiry": "2026-01-17", "type": "call", "last_tape_time": 1_700_000_000_000}]
        elif path == "/api/option-trades/flow-alerts":
            data = [{"created_at": "2026-08-20T10:00:00Z", "ticker": "QCOM"}]
        elif path == "/api/option-trades":
            data = [{"executed_at": "2026-08-20T09:59:00Z"}]
        elif path.endswith("/oi-change"):
            data = [{"curr_date": "2026-08-19", "last_date": "2026-08-18", "option_symbol": "QCOM260117C00180000"}]
        elif path.endswith("/gex-levels"):
            data = {
                "date": "2026-08-20",
                "time": "2026-08-20T19:59:36Z",
                "call_wall": "190",
                "put_wall": "160",
                "gamma_flip": "175",
                "gamma_magnet": "180",
            }
        elif path.startswith("/api/darkpool/"):
            data = [{"executed_at": "2026-08-20T09:30:03Z", "trf_executed_at": "2026-08-20T09:30:01Z"}]
        elif "/ohlc/" in path:
            data = [{"end_time": "2026-08-19T20:00:00Z", "open": "160", "high": "165", "low": "159", "close": "162"}]
        elif path == "/api/news/headlines":
            data = [{"created_at": "2026-08-20T08:00:00Z", "headline": "Example", "source": "Test"}]
        elif path.startswith("/api/earnings/"):
            data = [{"report_date": "2026-07-30", "report_time": "postmarket"}]
        elif path.endswith("/stock-state"):
            data = [{"market_time": "regular", "price": "160"}]
        elif path.endswith("/options-pulse"):
            data = [{"date": "2026-08-24", "net_call_premium": "10"}]
        elif path.endswith("/option/stock-price-levels"):
            data = [{"date": "2026-08-24", "price": "160", "call_volume": "5", "put_volume": "4"}]
        elif path.endswith("/stock-volume-price-levels"):
            data = [{"date": "2026-08-24", "price": "160", "off_exchange_volume": "20"}]
        elif "/volatility/" in path:
            data = [{"date": "2026-08-24", "value": "0.2"}]
        elif path.endswith("/etf-tide"):
            data = [{"date": "2026-08-24", "net_call_premium": "10", "net_put_premium": "5"}]
        elif path == "/api/market/correlations":
            data = [{"ticker_1": "QCOM", "ticker_2": "QQQ", "correlation": "0.7"}]
        elif path == "/api/market/economic-calendar":
            data = [{"event": "Consumer confidence", "time": "2026-08-25T14:00:00Z"}]
        else:  # pragma: no cover - keeps tests strict if a route changes
            raise AssertionError(path)
        return 200, {"X-RateLimit-Remaining": "42"}, json.dumps({"data": data}).encode()


class MissingFlowTimestampTransport(RoutingTransport):
    def __call__(self, url: str, headers: dict[str, str], timeout_seconds: float):
        if urlparse(url).path == "/api/option-trades/flow-alerts":
            self.requests.append(url)
            return 200, {}, json.dumps({"data": [{"ticker": "QCOM"}]}).encode()
        return super().__call__(url, headers, timeout_seconds)


class EmptyHistoricalGexTransport(RoutingTransport):
    """Captured CBRS/SKHY historical shape: a dated shell with null levels."""

    def __call__(self, url: str, headers: dict[str, str], timeout_seconds: float):
        if urlparse(url).path.endswith("/gex-levels"):
            self.requests.append(url)
            data = {
                "date": "2026-05-22",
                "time": None,
                "source": "vol",
                "call_wall": None,
                "put_wall": None,
                "gamma_flip": None,
                "gamma_magnet": None,
            }
            return 200, {}, json.dumps({"data": data}).encode()
        return super().__call__(url, headers, timeout_seconds)


class MismatchedHistoricalGexTransport(RoutingTransport):
    def __call__(self, url: str, headers: dict[str, str], timeout_seconds: float):
        if urlparse(url).path.endswith("/gex-levels"):
            self.requests.append(url)
            data = {
                "date": "2026-05-21",
                "time": "2026-05-21T19:59:36Z",
                "call_wall": "240",
                "put_wall": "237.5",
                "gamma_flip": "242.48",
                "gamma_magnet": "240",
            }
            return 200, {}, json.dumps({"data": data}).encode()
        return super().__call__(url, headers, timeout_seconds)


class PartialHistoricalGexTransport(RoutingTransport):
    """Captured AAOI-style shape: valid session metadata, incomplete levels."""

    def __call__(self, url: str, headers: dict[str, str], timeout_seconds: float):
        if urlparse(url).path.endswith("/gex-levels"):
            self.requests.append(url)
            data = {
                "date": "2026-07-27",
                "time": "2026-07-27T19:59:36Z",
                "source": "vol",
                "call_wall": None,
                "put_wall": "21",
                "gamma_flip": "20.5",
                "gamma_magnet": "21",
            }
            return 200, {}, json.dumps({"data": data}).encode()
        return super().__call__(url, headers, timeout_seconds)


class LiveAliasTransport(RoutingTransport):
    """Payload aliases captured from the live QCOM audit, kept offline."""

    def __call__(self, url: str, headers: dict[str, str], timeout_seconds: float):
        path = urlparse(url).path
        if path.endswith("/option-chains"):
            self.requests.append(url)
            data = [{
                "option_symbol": "QCOM260925C00200000",
                "strike": "200",
                "expires": "2026-09-25",
                "option_type": "call",
                "last_tape_time": "2026-08-21T21:45:07Z",
            }]
            return 200, {}, json.dumps({"data": data}).encode()
        if "/ohlc/" in path:
            self.requests.append(url)
            data = [{"date": "2026-08-21", "market_time": "pr", "open": "160", "high": "165", "low": "159", "close": "162"}]
            return 200, {}, json.dumps({"data": data}).encode()
        return super().__call__(url, headers, timeout_seconds)


class UnusualWhalesAdapterTests(unittest.TestCase):
    def test_credentialed_redirect_is_rejected_before_follow_up(self) -> None:
        request = Request(
            "https://api.unusualwhales.com/api/test",
            headers={"Authorization": "Bearer must-not-forward"},
        )
        headers = Message()
        headers["Location"] = "https://attacker.example/collect"
        with self.assertRaises(HTTPError) as raised:
            _RejectRedirectHandler().redirect_request(
                request,
                io.BytesIO(b"redirect"),
                302,
                "Found",
                headers,
                headers["Location"],
            )
        self.assertEqual(302, raised.exception.code)
        self.assertNotIn("must-not-forward", str(raised.exception))
        raised.exception.close()

    def test_response_body_limit_rejects_declared_and_streamed_excess(self) -> None:
        with self.assertRaises(ProviderResponseError):
            read_bounded_body(io.BytesIO(b"small"), {"Content-Length": "99"}, maximum_bytes=8)
        with self.assertRaises(ProviderResponseError):
            read_bounded_body(io.BytesIO(b"123456789"), {}, maximum_bytes=8)
        self.assertEqual(
            b"12345678",
            read_bounded_body(io.BytesIO(b"12345678"), {}, maximum_bytes=8),
        )

    def test_injected_transport_cannot_bypass_downstream_body_limit(self) -> None:
        captured = []
        transport = ScriptedTransport([(200, {}, b"123456789")])
        client = SafeGetClient(
            provider="test",
            authorization="Bearer token",
            base_url="https://example.test",
            transport=transport,
            raw_response_hook=captured.append,
            max_response_bytes=8,
        )
        with self.assertRaises(ProviderResponseError):
            client.get_json("/oversized")
        self.assertEqual([], captured)

    def test_retries_safe_get_and_records_rate_limit_metadata(self) -> None:
        transport = ScriptedTransport(
            [
                (429, {"Retry-After": "0", "X-RateLimit-Remaining": "0"}, {"error": "slow down"}),
                (200, {"X-RateLimit-Limit": "100", "X-RateLimit-Remaining": "99"}, {"data": []}),
            ]
        )
        waits: list[float] = []
        client = SafeGetClient(
            provider="test",
            authorization="Bearer secret-not-to-log",
            base_url="https://example.test",
            transport=transport,
            sleep=waits.append,
            random_float=lambda: 0.5,
        )

        response = client.get_json("/safe")

        self.assertEqual(2, len(transport.requests))
        self.assertEqual([0.0], waits)
        self.assertEqual(99, response.raw.rate_limit.remaining)
        self.assertEqual(2, response.raw.attempts)
        self.assertEqual("Bearer secret-not-to-log", transport.requests[0][1]["Authorization"])
        self.assertNotIn("secret-not-to-log", repr(response))

    def test_pagination_has_a_hard_guard(self) -> None:
        transport = ScriptedTransport(
            [(200, {}, {"data": [{"id": 1}]}) for _ in range(2)]
        )
        client = SafeGetClient(
            provider="test",
            authorization="Bearer token",
            base_url="https://example.test",
            transport=transport,
        )

        with self.assertRaises(PaginationLimitError):
            tuple(client.iter_pages("/rows", params={"limit": 1}, max_pages=2))

    def test_raw_capture_excludes_request_authorization(self) -> None:
        transport = ScriptedTransport([(200, {"X-Request-Id": "abc", "Set-Cookie": "session=do-not-store", "X-Proxy-Token": "must-not-store", "ETag": "v1"}, {"data": []})])
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "responses.jsonl"
            client = SafeGetClient(
                provider="test",
                authorization="Bearer confidential-token",
                base_url="https://example.test",
                transport=transport,
                raw_response_hook=JsonlRawResponseCapture(destination),
            )
            client.get_json("/capture")
            captured = destination.read_text(encoding="utf-8")
            capture_mode = stat.S_IMODE(destination.stat().st_mode)

        self.assertIn('"X-Request-Id":"abc"', captured)
        self.assertIn('"ETag":"v1"', captured)
        self.assertNotIn("confidential-token", captured)
        self.assertNotIn("Authorization", captured)
        self.assertNotIn("do-not-store", captured)
        self.assertEqual(0o600, capture_mode)
        self.assertNotIn("must-not-store", captured)

    def test_default_transport_rejects_non_allowlisted_or_ambiguous_base_urls(self) -> None:
        with self.assertRaisesRegex(ValueError, "explicit shared request_budget"):
            UnusualWhalesClient("test-key")
        with tempfile.TemporaryDirectory() as directory, WeeklyRequestBudget(Path(directory) / "usage.sqlite") as budget:
            self.assertEqual(
                "https://api.unusualwhales.com",
                UnusualWhalesClient("test-key", request_budget=budget)._http.base_url,
            )
            for base_url in (
                "http://api.unusualwhales.com",
                "https://evil.example",
                "https://api.unusualwhales.com/path",
                "https://api.unusualwhales.com?query=1",
                "https://api.unusualwhales.com#fragment",
                "https://user:pass@api.unusualwhales.com",
                "https://api.unusualwhales.com:8443",
            ):
                with self.subTest(base_url=base_url):
                    with self.assertRaises(ValueError):
                        UnusualWhalesClient("test-key", base_url=base_url, request_budget=budget)

    def test_injected_transport_allows_a_clean_https_test_origin_only(self) -> None:
        client = UnusualWhalesClient("test-key", base_url="https://example.test", transport=ScriptedTransport([(200, {}, {"data": []})]))
        self.assertEqual("https://example.test", client._http.base_url)

        with self.assertRaises(ValueError):
            UnusualWhalesClient("test-key", base_url="http://example.test", transport=RoutingTransport())
        with self.assertRaises(ValueError):
            UnusualWhalesClient("test-key", base_url="https://example.test/path", transport=RoutingTransport())

    def test_client_errors_and_repr_do_not_expose_api_key(self) -> None:
        api_key = "do-not-disclose-api-key"

        def failing_transport(url: str, headers: dict[str, str], timeout_seconds: float):
            raise RuntimeError(api_key)

        client = UnusualWhalesClient(api_key, transport=failing_transport, max_attempts=1)
        with self.assertRaises(ProviderResponseError) as raised:
            client.option_chain("QCOM")

        self.assertNotIn(api_key, str(raised.exception))
        self.assertNotIn(api_key, repr(raised.exception))
        self.assertNotIn(api_key, repr(client))

    def test_chain_query_requests_enriched_rows(self) -> None:
        transport = RoutingTransport()
        client = UnusualWhalesClient("test-key", transport=transport)

        result = client.option_chain("qcom")

        request_url = transport.requests[0]
        self.assertEqual("/api/stock/QCOM/option-chains", urlparse(request_url).path)
        self.assertEqual(["true"], parse_qs(urlparse(request_url).query)["greeks"])
        self.assertEqual("QCOM260117C00180000", result.data[0]["option_symbol"])

    def test_flow_alert_query_preserves_bounded_history_parameters(self) -> None:
        transport = RoutingTransport()
        client = UnusualWhalesClient("test-key", transport=transport)

        client.flow_alerts(
            "qcom", unusual=True,
            newer_than="2026-08-21T04:00:00Z",
            older_than="2026-08-22T04:00:00Z",
            limit=200,
        )

        query = parse_qs(urlparse(transport.requests[0]).query)
        self.assertEqual(["QCOM"], query["ticker_symbol"])
        self.assertEqual(["true"], query["unusual"])
        self.assertEqual(["2026-08-21T04:00:00Z"], query["newer_than"])
        self.assertEqual(["2026-08-22T04:00:00Z"], query["older_than"])
        self.assertEqual(["200"], query["limit"])

    def test_supplemental_signal_routes_are_bounded_and_explicit(self) -> None:
        transport = RoutingTransport()
        client = UnusualWhalesClient("test-key", transport=transport)
        market_date = "2026-08-24"

        client.stock_state("qcom")
        client.options_pulse("qcom", as_of=market_date)
        client.option_price_levels("qcom", as_of=market_date)
        client.stock_volume_price_levels("qcom", as_of=market_date)
        client.volatility_anomaly("qcom", as_of=market_date)
        client.volatility_character("qcom", as_of=market_date)
        client.variance_risk_premium("qcom", as_of=market_date)
        client.etf_tide("qqq", as_of=market_date)
        client.market_correlations(
            ["qcom", "qqq", "qcom"], interval="1d",
            start_date="2026-07-24", end_date=market_date,
        )
        client.economic_calendar(as_of="2026-08-25")

        parsed = [urlparse(url) for url in transport.requests]
        paths = [item.path for item in parsed]
        self.assertEqual([
            "/api/stock/QCOM/stock-state",
            "/api/stock/QCOM/options-pulse",
            "/api/stock/QCOM/option/stock-price-levels",
            "/api/stock/QCOM/stock-volume-price-levels",
            "/api/stock/QCOM/volatility/anomaly",
            "/api/stock/QCOM/volatility/character",
            "/api/stock/QCOM/volatility/variance-risk-premium",
            "/api/market/QQQ/etf-tide",
            "/api/market/correlations",
            "/api/market/economic-calendar",
        ], paths)
        for item in parsed[1:8]:
            self.assertEqual([market_date], parse_qs(item.query)["date"])
        correlation_query = parse_qs(parsed[8].query)
        self.assertEqual(["QCOM,QQQ"], correlation_query["tickers"])
        self.assertEqual(["1d"], correlation_query["interval"])
        self.assertEqual(["2026-07-24"], correlation_query["start_date"])
        self.assertEqual([market_date], correlation_query["end_date"])
        self.assertEqual(["2026-08-25"], parse_qs(parsed[9].query)["date"])

    def test_live_chain_and_ohlc_aliases_are_accepted_without_changing_time_precision(self) -> None:
        client = UnusualWhalesClient("test-key", transport=LiveAliasTransport())

        chain = client.option_chain("QCOM")
        bars = client.ohlc("QCOM")
        report = run_trial_audit(UnusualWhalesClient("test-key", transport=LiveAliasTransport()), "QCOM")

        self.assertEqual("2026-09-25", chain.data[0]["expires"])
        self.assertEqual("call", chain.data[0]["option_type"])
        self.assertEqual("2026-08-21", bars.data[0]["date"])
        ohlc = next(result for result in report.results if result.dataset == "ohlc")
        self.assertIs(ohlc.status, AuditStatus.AVAILABLE)
        self.assertEqual("date", ohlc.timestamp_checks[0].field)
        self.assertIn("day-granularity", ohlc.timestamp_checks[0].documented_meaning)

    def test_chain_aliases_must_be_present_and_unambiguous(self) -> None:
        transport = ScriptedTransport([
            (200, {}, {"data": [{
                "option_symbol": "QCOM260925C00200000",
                "strike": "200",
                "expiry": "2026-09-25",
                "expires": "2026-10-16",
                "option_type": "call",
            }]})
        ])

        with self.assertRaisesRegex(ProviderSchemaError, "conflicting values"):
            UnusualWhalesClient("test-key", transport=transport).option_chain("QCOM")

    def test_ohlc_date_alias_must_be_an_iso_date(self) -> None:
        transport = ScriptedTransport([
            (200, {}, {"data": [{"date": "2026/08/21"}]})
        ])

        with self.assertRaisesRegex(ProviderSchemaError, "YYYY-MM-DD"):
            UnusualWhalesClient("test-key", transport=transport).ohlc("QCOM")

    def test_ohlc_documented_end_time_must_include_a_timezone(self) -> None:
        transport = ScriptedTransport([
            (200, {}, {"data": [{"end_time": "2026-08-21T20:00:00"}]})
        ])

        with self.assertRaisesRegex(ProviderSchemaError, "timezone"):
            UnusualWhalesClient("test-key", transport=transport).ohlc("QCOM")

    def test_short_volume_preserves_live_legacy_si_envelope(self) -> None:
        transport = ScriptedTransport([
            (200, {}, {"si": [{"date": "2026-08-21", "short_volume_ratio": "0.42"}]})
        ])

        response = UnusualWhalesClient("test-key", transport=transport).short_volume_ratio("QCOM")

        self.assertEqual("si", response.data_field)
        self.assertEqual("2026-08-21", response.data[0]["date"])
        self.assertNotIn("data", response.response.payload)

    def test_trial_audit_covers_all_required_datasets_offline(self) -> None:
        client = UnusualWhalesClient("test-key", transport=RoutingTransport())

        report = run_trial_audit(client, "qcom")

        self.assertEqual(9, len(report.results))
        self.assertTrue(all(result.status is AuditStatus.AVAILABLE for result in report.results))
        darkpool = next(result for result in report.results if result.dataset == "darkpool")
        self.assertEqual({"executed_at", "trf_executed_at"}, {check.field for check in darkpool.timestamp_checks})

    def test_required_timestamp_failure_promotes_endpoint_schema_status(self) -> None:
        report = run_trial_audit(
            UnusualWhalesClient("test-key", transport=MissingFlowTimestampTransport()),
            "QCOM",
        )
        flow = next(result for result in report.results if result.dataset == "flow_alerts")
        self.assertIs(flow.status, AuditStatus.SCHEMA_MISMATCH)
        self.assertIn("created_at", flow.note)

    def test_null_historical_gex_levels_are_empty_not_available(self) -> None:
        report = run_trial_audit(
            UnusualWhalesClient("test-key", transport=EmptyHistoricalGexTransport()),
            "CBRS",
            as_of="2026-05-22",
        )
        gex = next(result for result in report.results if result.dataset == "gex_levels")

        self.assertIs(gex.status, AuditStatus.EMPTY)
        self.assertEqual(1, gex.row_count)
        self.assertEqual((), gex.timestamp_checks)
        self.assertIn("no usable GEX levels", gex.note)

    def test_populated_historical_gex_date_must_match_requested_scope(self) -> None:
        report = run_trial_audit(
            UnusualWhalesClient("test-key", transport=MismatchedHistoricalGexTransport()),
            "QCOM",
            as_of="2026-05-22",
        )
        gex = next(result for result in report.results if result.dataset == "gex_levels")

        self.assertIs(gex.status, AuditStatus.SCHEMA_MISMATCH)
        self.assertIn("does not match requested date", gex.note)

    def test_partial_historical_gex_is_retained_for_audit_but_unverified(self) -> None:
        client = UnusualWhalesClient("test-key", transport=PartialHistoricalGexTransport())
        response = client.gex_levels("AAOI", as_of="2026-07-27")
        report = run_trial_audit(
            UnusualWhalesClient("test-key", transport=PartialHistoricalGexTransport()),
            "AAOI",
            as_of="2026-07-27",
        )
        gex = next(result for result in report.results if result.dataset == "gex_levels")

        self.assertIsNone(response.data["call_wall"])
        self.assertEqual("21", response.data["put_wall"])
        self.assertIs(gex.status, AuditStatus.SCOPE_UNVERIFIED)
        self.assertIn("call_wall", gex.note)

    def test_historical_scope_is_explicit_when_endpoint_is_unbounded(self) -> None:
        report = run_trial_audit(
            UnusualWhalesClient("test-key", transport=RoutingTransport()),
            "QCOM",
            as_of="2026-08-19",
        )
        flow = next(result for result in report.results if result.dataset == "flow_alerts")
        chain = next(result for result in report.results if result.dataset == "option_chain")
        self.assertIs(flow.status, AuditStatus.SCOPE_UNVERIFIED)
        self.assertFalse(flow.scope_parameter_applied)
        self.assertEqual("2026-08-19", flow.requested_scope)
        self.assertIs(chain.status, AuditStatus.AVAILABLE)
        self.assertTrue(chain.scope_parameter_applied)


if __name__ == "__main__":
    unittest.main()

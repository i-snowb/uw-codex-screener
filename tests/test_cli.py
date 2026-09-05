from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from morning_edge.audit import AuditStatus
from morning_edge.cli import (
    add_provider_baseline_adjustment,
    audit_provider,
    current_capture,
    enhanced_capture,
    historical_backfill,
    live_morning_run,
    main,
    snapshot_datetime,
)
from morning_edge.config import Settings


class CliTest(unittest.TestCase):
    def run_command(self, args: list[str]) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(args)
        return status, json.loads(output.getvalue())

    def test_init_then_load_fixture(self) -> None:
        fixture = Path(__file__).parents[1] / "fixtures" / "demo_morning_snapshot.json"
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "edge.sqlite"
            status, result = self.run_command(["--database", str(database), "init-db"])
            self.assertEqual(status, 0)
            self.assertEqual(result["status"], "initialized")
            status, result = self.run_command(["--database", str(database), "load-fixture", str(fixture)])
            self.assertEqual(status, 0)
            self.assertEqual(result["inserted_snapshots"], 2)

    def test_morning_run_is_dry_and_uses_et_snapshot_time(self) -> None:
        status, result = self.run_command([
            "morning-run", "--as-of", "2026-08-20T12:00:00+00:00"
        ])
        self.assertEqual(status, 0)
        self.assertFalse(result["network_called"])
        self.assertEqual(result["status"], "no_recommendation")
        self.assertEqual(result["snapshot_at"], "2026-08-20T08:00:00-04:00")

    def test_morning_run_reserve_override_requires_live_and_bounded_floor(self) -> None:
        status, result = self.run_command([
            "morning-run", "--authorized-reserve-floor", "0",
        ])
        self.assertEqual(2, status)
        self.assertIn("require --live", str(result["error"]))

        settings = Settings.from_env({
            "MORNING_EDGE_PROVIDER": "unusual_whales",
            "UNUSUAL_WHALES_API_KEY": "local-test-secret",
        })
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
        with self.assertRaisesRegex(ValueError, "between 0 and 39999"):
                live_morning_run(
                    settings,
                    tickers=("QCOM",),
                    datasets=(),
                    audit_accepted=True,
                    database_path=root / "edge.sqlite",
                    output_path=root / "run.json",
                    authorized_reserve_floor=-1,
                )

    def test_default_snapshot_is_configured_time(self) -> None:
        settings = Settings.from_env({})
        planned = snapshot_datetime(settings)
        self.assertEqual(planned.hour, 6)
        self.assertEqual(planned.minute, 55)

    def test_sunday_default_targets_monday_snapshot(self) -> None:
        settings = Settings.from_env({})
        sunday = datetime.fromisoformat("2026-08-23T20:00:00-04:00")
        planned = snapshot_datetime(settings, now=sunday)
        self.assertEqual("2026-08-24T06:55:00-04:00", planned.isoformat())

    def test_audit_never_calls_network(self) -> None:
        status, result = self.run_command(["audit-provider"])
        self.assertEqual(status, 0)
        self.assertFalse(result["network_called"])

    def test_live_audit_requires_explicit_ticker(self) -> None:
        settings = Settings.from_env({
            "MORNING_EDGE_PROVIDER": "unusual_whales",
            "UNUSUAL_WHALES_API_KEY": "local-test-secret",
        })
        with self.assertRaisesRegex(ValueError, "explicit --ticker"):
            audit_provider(settings, None, live=True)

    def test_live_audit_is_opt_in_and_never_enables_recommendations(self) -> None:
        settings = Settings.from_env({
            "MORNING_EDGE_PROVIDER": "unusual_whales",
            "UNUSUAL_WHALES_API_KEY": "local-test-secret",
        })
        fake_report = Mock()
        fake_report.to_dict.return_value = {
            "ticker": "QCOM",
            "results": [{"status": AuditStatus.AVAILABLE}],
        }
        with (
            patch("morning_edge.cli.UnusualWhalesClient") as client_type,
            patch("morning_edge.cli.run_trial_audit", return_value=fake_report) as trial_audit,
            patch("morning_edge.cli.WeeklyRequestBudget") as budget_type,
        ):
            budget = budget_type.return_value.__enter__.return_value
            budget.usage.return_value.remaining_before_reserve = 10_000
            result = audit_provider(
                settings,
                None,
                live=True,
                tickers=("qcom", "QCOM"),
                as_of="2026-08-19",
            )

        client_type.assert_called_once_with(
            "local-test-secret", raw_response_hook=None, request_budget=budget
        )
        trial_audit.assert_called_once_with(client_type.return_value, "QCOM", as_of="2026-08-19")
        self.assertTrue(result["network_called"])
        self.assertFalse(result["recommendations_enabled"])
        self.assertEqual(result["status"], "audit_complete")
        self.assertNotIn("local-test-secret", json.dumps(result, default=str))

    def test_live_only_parameters_fail_closed_without_live_flag(self) -> None:
        with self.assertRaisesRegex(ValueError, "require --live"):
            audit_provider(Settings.from_env({}), None, tickers=("QCOM",))

    def test_current_capture_is_bounded_and_dry_by_default(self) -> None:
        result = current_capture(
            Settings.from_env({}), tickers=("QCOM",), datasets=(), live=False
        )
        self.assertEqual("dry_run", result["status"])
        self.assertFalse(result["network_called"])
        self.assertFalse(result["recommendations_enabled"])
        self.assertEqual(8, result["logical_items"])
        self.assertEqual(24, result["maximum_transport_attempts"])

    def test_current_capture_live_requires_audit_acknowledgement(self) -> None:
        settings = Settings.from_env({
            "MORNING_EDGE_PROVIDER": "unusual_whales",
            "UNUSUAL_WHALES_API_KEY": "local-test-secret",
        })
        with self.assertRaisesRegex(ValueError, "requires --audit-accepted"):
            current_capture(settings, tickers=("QCOM",), datasets=(), live=True)

    def test_enhanced_capture_counts_global_feeds_once(self) -> None:
        result = enhanced_capture(
            Settings.from_env({}), tickers=("QCOM", "AAOI"), datasets=(), live=False
        )
        self.assertEqual("dry_run", result["status"])
        self.assertEqual(35, result["logical_items"])
        self.assertEqual(105, result["maximum_transport_attempts"])
        self.assertFalse(result["recommendations_enabled"])

    def test_baseline_adjustment_requires_identical_idempotent_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings.from_env({
                "MORNING_EDGE_PROVIDER_USAGE_DATABASE": str(Path(directory) / "usage.sqlite"),
            })
            arguments = {
                "adjustment_id": "preledger-test-v1",
                "attempted_requests": 12,
                "evidence_id": "local-evidence-v1",
                "effective_at": "2026-08-24T02:16:25+00:00",
                "clock": lambda: datetime.fromisoformat("2026-08-25T02:16:25+00:00"),
            }
            first = add_provider_baseline_adjustment(settings, **arguments)
            replay = add_provider_baseline_adjustment(settings, **arguments)
            self.assertEqual(12, first["baseline_adjustment_attempts"])
            self.assertEqual(first["attempted_requests"], replay["attempted_requests"])
            with self.assertRaisesRegex(ValueError, "different immutable"):
                add_provider_baseline_adjustment(settings, **{**arguments, "attempted_requests": 13})

    def test_live_backfill_rejects_logical_cap_that_could_exceed_transport_capacity(self) -> None:
        settings = Settings.from_env({
            "MORNING_EDGE_PROVIDER": "unusual_whales",
            "UNUSUAL_WHALES_API_KEY": "local-test-secret",
        })
        with tempfile.TemporaryDirectory() as temporary, patch("morning_edge.cli.WeeklyRequestBudget") as budget_type:
            budget = budget_type.return_value.__enter__.return_value
            budget.usage.return_value.remaining_before_reserve = 5
            with self.assertRaisesRegex(ValueError, "maximum, 5 available"):
                historical_backfill(
                    settings,
                    start_date="2026-08-21",
                    end_date="2026-08-21",
                    tickers=("QCOM",),
                    datasets=("ohlc",),
                    max_requests=2,
                    database_path=Path(temporary) / "edge.sqlite",
                    live=True,
                    audit_accepted=True,
                )

    def test_backfill_reserve_override_requires_live_and_bounded_floor(self) -> None:
        settings = Settings.from_env({
            "MORNING_EDGE_PROVIDER": "unusual_whales",
            "UNUSUAL_WHALES_API_KEY": "local-test-secret",
        })
        with self.assertRaisesRegex(ValueError, "requires --live"):
            historical_backfill(
                settings,
                start_date="2026-08-21", end_date="2026-08-21",
                tickers=("QCOM",), datasets=("ohlc",), max_requests=1,
                authorized_reserve_floor=5_000,
            )
        with self.assertRaisesRegex(ValueError, "between 0 and 39999"):
            historical_backfill(
                settings,
                start_date="2026-08-21", end_date="2026-08-21",
                tickers=("QCOM",), datasets=("ohlc",), max_requests=1,
                live=True, audit_accepted=True, authorized_reserve_floor=-1,
            )


if __name__ == "__main__":
    unittest.main()

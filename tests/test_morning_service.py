from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import run_morning_service as service
import run_unattended_morning as runner
from morning_edge.unattended import atomic_json

CONFIG = {"start_et": "06:45", "deadline_et": "08:00", "max_attempts": 2}


def moment(hhmm):
    return datetime.fromisoformat("2026-09-08T" + hhmm + ":00-04:00")


class MorningServiceTests(unittest.TestCase):
    def test_missing_capture_warns_before_market_open(self):
        self.assertEqual("WAITING_FOR_START", service.assess(CONFIG, None, None, moment("06:30"))["status"])
        self.assertEqual("RUNNING", service.assess(CONFIG, None, None, moment("06:50"))["status"])
        self.assertEqual("NOT_STARTED", service.assess(CONFIG, None, None, moment("06:55"))["status"])
        self.assertEqual("MISSED_DEADLINE", service.assess(CONFIG, None, None, moment("08:00"))["status"])

    def test_deadline_overrides_stale_running_or_waiting(self):
        for status in ("RUNNING", "WAITING_FOR_START"):
            state = {"session_date": "2026-09-08", "status": status, "attempts": {"capture": 1}}
            self.assertEqual("MISSED_DEADLINE", service.assess(CONFIG, state, None, moment("08:05"))["status"])

    def test_progress_is_only_current_session_and_aware(self):
        state = {"session_date": "2026-09-08", "status": "RUNNING", "attempts": {"capture": 1},
                 "updated_at": moment("06:45").isoformat()}
        self.assertEqual("STALLED", service.assess(CONFIG, state, {"updated_at": "2026-09-07T07:04:00-04:00"}, moment("07:05"))["status"])
        self.assertEqual("RUNNING", service.assess(CONFIG, state, {"updated_at": moment("07:04").isoformat()}, moment("07:05"))["status"])
        self.assertEqual("STALLED", service.assess(CONFIG, state, {"updated_at": "2026-09-08T07:04:00"}, moment("07:05"))["status"])

    def test_failure_reports_before_deadline(self):
        state = {"session_date": "2026-09-08", "status": "FAILED", "stage": "capture", "reason": "Synthetic failure"}
        result = service.assess(CONFIG, state, None, moment("06:50"))
        self.assertEqual("FAILED", result["status"])
        self.assertFalse(result["recommendations_enabled"])

    def test_failed_stage_retries_only_after_backoff_and_with_budget(self):
        state = {"status": "FAILED", "stage": "capture", "attempts": {"capture": 1},
                 "updated_at": moment("06:50").isoformat()}
        self.assertFalse(service.retry_available(CONFIG, state, moment("06:54")))
        self.assertTrue(service.retry_available(CONFIG, state, moment("06:55")))
        state["attempts"]["capture"] = 2
        self.assertFalse(service.retry_available(CONFIG, state, moment("07:00")))
        self.assertFalse(service.retry_available(CONFIG, {"status": "FAILED"}, moment("07:00")))

    def test_closed_session_is_quiet(self):
        result = service.assess(CONFIG, None, None, datetime.fromisoformat("2026-09-07T08:05:00-04:00"))
        self.assertEqual("MARKET_CLOSED", result["status"])
        self.assertNotIn(result["status"], service.ISSUES)

    def test_claimed_completion_requires_verification(self):
        self.assertEqual("VERIFYING", service.assess(CONFIG,
            {"session_date": "2026-09-08", "status": "COMPLETE"}, None, moment("07:45"))["status"])

    def test_read_only_inspection_preserves_state(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(service, "ROOT", Path(temp)):
            path = Path(temp) / "outputs/unattended/2026-09-08/state.json"
            atomic_json(path, {"session_date": "2026-09-08", "status": "WAITING_FOR_START"})
            before = path.read_bytes()
            self.assertEqual("MISSED_DEADLINE", service.inspect(CONFIG, moment("08:05"))["status"])
            self.assertEqual(before, path.read_bytes())
            self.assertFalse((Path(temp) / "dashboard-app").exists())

    def test_terminal_worker_status_clears_running_pipeline(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(runner, "ROOT", Path(temp)), patch("builtins.print"):
            config = CONFIG | {"root": temp}
            instance = runner.Runner(config, Path(temp) / "run")
            instance.status("FAILED", "capture", "Synthetic timeout")
            public = json.loads((Path(temp) / "dashboard-app/data/pipeline-status.json").read_text())
            self.assertEqual("FAILED", public["status"])
            self.assertEqual("capture", public["stage"])

    def test_service_waits_for_start_and_stops_worker_at_absolute_deadline(self):
        moments = iter([moment("06:45"), moment("08:00"), moment("08:15")])
        current = [moment("06:30")]
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return current[0].astimezone(tz)
        child = Mock(pid=123, poll=Mock(return_value=None))
        stop_times = []
        def stop(process, directory):
            stop_times.append(current[0])
            child.poll.return_value = 0
        with tempfile.TemporaryDirectory() as temp, patch.object(service, "ROOT", Path(temp)), \
             patch.object(service, "datetime", FixedDateTime), patch.object(service, "validate_config"), \
             patch.object(service, "read_object", return_value=CONFIG | {"python": sys.executable}), \
             patch.object(service, "report", return_value={"status": "RUNNING"}), \
             patch.object(service.subprocess, "Popen", return_value=child) as launch, \
             patch.object(service, "stop_worker", side_effect=stop), \
             patch.object(service.clock, "sleep", side_effect=lambda _: current.__setitem__(0, next(moments))), \
             patch("builtins.print"):
            self.assertEqual(0, service.main(["--config", str(Path(temp) / "config.json"), "--live", "--audit-accepted"]))
            launch.assert_called_once()
            self.assertEqual([moment("08:00")], stop_times)

    def test_read_only_check_never_launches_or_sends_notification(self):
        with patch.object(service, "read_object", return_value=CONFIG), patch.object(service, "validate_config"), \
             patch.object(service, "inspect", return_value={"status": "MISSED_DEADLINE"}), \
             patch.object(service, "report") as report, patch.object(service.subprocess, "Popen") as launch, \
             patch("builtins.print"):
            self.assertEqual(0, service.main(["--config", "/tmp/config.json", "--check"]))
            report.assert_not_called()
            launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()

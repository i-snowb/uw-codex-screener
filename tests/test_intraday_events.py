from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import stat
import tempfile
import unittest

from morning_edge.intraday_events import IntradayEventLedger, IntradayEventRecord, shadow_intraday_event_model


class IntradayEventTests(unittest.TestCase):
    def test_ledger_is_idempotent_and_model_fails_closed(self) -> None:
        record = IntradayEventRecord(
            ticker="QCOM", observed_at=datetime(2026, 8, 28, 14, tzinfo=timezone.utc),
            daily_origin_session="2026-08-27", event_type="FLOW_OUTLIER",
            features={"signed_premium_z": 2.8}, source_snapshot_ids=(10, 10, 11),
        )
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "private-ledger"
            parent.mkdir(mode=0o755)
            database = parent / "events.sqlite"
            with IntradayEventLedger(database) as ledger:
                self.assertTrue(ledger.insert(record))
                self.assertFalse(ledger.insert(record))
                self.assertEqual(1, len(ledger.comparable(ticker="qcom", event_type="flow_outlier")))
                self.assertEqual(0o700, stat.S_IMODE(parent.stat().st_mode))
                self.assertEqual(0o600, stat.S_IMODE(database.stat().st_mode))
                for sidecar in (Path(f"{database}-wal"), Path(f"{database}-shm")):
                    if sidecar.exists():
                        self.assertEqual(0o600, stat.S_IMODE(sidecar.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(database.stat().st_mode))
        result = shadow_intraday_event_model(event=record, comparable_events=[])
        self.assertEqual("UNAVAILABLE_INSUFFICIENT_POINT_IN_TIME_HISTORY", result["status"])
        self.assertFalse(result["promotion_eligible"])
        self.assertEqual(["30_MINUTE", "TO_CLOSE", "NEXT_OPEN"], result["horizons"])


if __name__ == "__main__":
    unittest.main()

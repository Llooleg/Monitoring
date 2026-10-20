"""Collector parsing tests (no network)."""
import datetime as dt
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT))
import db as dbmod  # noqa: E402
from collectors import bgp  # noqa: E402


def fake_ripestat():
    # real shape of stat.ripe.net bgp-update-activity: naive UTC timestamps
    return {"data": {"sampling_period": 3600, "updates": [
        {"starttime": "2025-10-18T12:00:00", "announcements": 3, "withdrawals": 1}]}}


class TestBgp(unittest.TestCase):
    def test_buckets_stored_as_utc_regardless_of_host_tz(self):
        old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/Moscow"  # any non-UTC host must not shift buckets
        time.tzset()
        try:
            with tempfile.TemporaryDirectory() as td:
                con = dbmod.open_events(Path(td) / "e.db")
                with patch.object(bgp.common, "get_json", return_value=fake_ripestat()):
                    bgp.collect(con, ["13335"])
                row = con.execute("SELECT observed_at FROM observations").fetchone()
                # 12:00 UTC start + 3600s sampling period = bucket applies at 13:00 UTC
                want = int(dt.datetime(2025, 10, 18, 13, 0, tzinfo=dt.timezone.utc).timestamp())
                self.assertEqual(row["observed_at"], want)
        finally:
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            time.tzset()


if __name__ == "__main__":
    unittest.main()

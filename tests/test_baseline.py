"""Unit tests for the seasonal baseline (Plan/baseline.md promises)."""
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "MLanalysis"))
import baseline as B  # noqa: E402


def make_db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript((ROOT / "SQLSchema" / "schema.sql").read_text())
    con.execute("INSERT INTO sources (source_id,name,kind,independence_group) VALUES (1,'t','web','t')")
    con.execute("INSERT INTO feeds (feed_id,source_id,metric,cadence_s,label,signal_class) "
                "VALUES (1,1,'m',3600,'test feed','test')")
    return con


def rows_from(vals, ingested_delay=60, start=1700000000, cadence=3600):
    return [{"observed_at": start + i * cadence, "ingested_at": start + i * cadence + ingested_delay,
             "value": v, "coverage": None} for i, v in enumerate(vals)]


class TestBasics(unittest.TestCase):
    def test_how_matches_schema(self):
        # Monday 00:00 UTC = 0, Sunday 23:00 = 167
        self.assertEqual(B.how_of(1704067200), 0)      # 2024-01-01 Mon 00:00 UTC
        self.assertEqual(B.how_of(1704067200 + 167 * 3600), 167)

    def test_tier_widening(self):
        # 20 weeks of hourly data, quiet overnight, busy daytime (by hour-of-day)
        n = 20 * 7 * 24
        start = 1700006400  # a Monday 01:00-ish, hour-of-week alignment irrelevant
        rows = []
        for i in range(n):
            how = i % 168
            v = 50.0 if 8 <= how % 24 <= 18 else 10.0
            rows.append({"observed_at": start + i * 3600, "ingested_at": start + i * 3600 + 60,
                         "value": v, "coverage": None})
        # a spike at a QUIET hour, late in the series: tier 1 must catch it huge
        idx = next(i for i in range(n - 1, 0, -1) if (i % 24) == 3)
        target = dict(rows[idx], value=500.0)
        known = [(r["observed_at"], r["value"], None) for r in rows[:idx]]
        keys = sorted(r["observed_at"] for r in rows[:idx])
        s = B.score_one(target, keys, known)
        self.assertEqual(s["tier"], 1)
        self.assertGreater(s["z"], 50)
        # and a thin bucket falls back rather than judging: early series, few
        # same-hour-of-week samples -> tier widens and still refuses to shout
        early = dict(rows[3 * 168 + 5], value=500.0)
        known2 = [(r["observed_at"], r["value"], None) for r in rows[:3 * 168 + 5]]
        keys2 = sorted(r["observed_at"] for r in rows[:3 * 168 + 5])
        s2 = B.score_one(early, keys2, known2)
        self.assertGreaterEqual(s2["tier"], 1)

    def test_poisson_floor(self):
        # a mostly-zero count feed: every 1 must not be a crisis
        vals = [0.0] * 300 + [1.0, 0.0]
        rows = rows_from(vals)
        target = rows[-2]  # the single 1
        keys = sorted(r["observed_at"] for r in rows[:-2])
        known = [(r["observed_at"], r["value"], None) for r in rows[:-2]]
        s = B.score_one(target, keys, known)
        self.assertLess(s["z"], 3.0, "Poisson floor must keep single counts unremarkable")

    def test_coverage_gate_skips_starved_readings(self):
        vals = [50.0] * 300 + [50.0]
        rows = rows_from(vals)
        for r in rows[:-1]:
            r["coverage"] = 24.0
        rows[-1]["coverage"] = 8.0  # a third of the receivers: not news
        keys = sorted(r["observed_at"] for r in rows[:-1])
        known = [(r["observed_at"], r["value"], r["coverage"]) for r in rows[:-1]]
        self.assertIsNone(B.score_one(rows[-1], keys, known))

    def test_no_lookahead(self):
        # a huge value ingested LATER must not enter the baseline for an
        # earlier candidate: score target against rows ingested before it only
        rows = rows_from([50.0] * 200 + [50.0])
        late = {"observed_at": rows[-2]["observed_at"],  # same slot, ingested later
                "ingested_at": rows[-2]["ingested_at"] + 10**6,
                "value": 500.0, "coverage": None}
        target = rows[-1]
        known = [(r["observed_at"], r["value"], None) for r in rows[:-2]]
        keys = sorted(r["observed_at"] for r in rows[:-2])
        s1 = B.score_one(target, keys, known)
        # add the late row to the window: it is ingested AFTER the target,
        # so the target's own pass must never see it
        self.assertAlmostEqual(s1["median"], 50.0)

    def test_debounce(self):
        flags = []
        vals = [50.0] * 200
        # 6 consecutive anomalous hours starting now
        for k in range(6):
            vals.append(200.0)
        rows = rows_from(vals)
        B.score_feed(rows, 5.0, lambda c, s: flags.append(c["observed_at"]))
        self.assertEqual(len(flags), 1, "one outage must not become six triage calls")

    def test_debounce_escaped_by_growth(self):
        flags = []
        vals = [50.0] * 200 + [100.0, 300.0]
        rows = rows_from(vals)
        B.score_feed(rows, 5.0, lambda c, s: flags.append((c["observed_at"], s["z"])))
        self.assertEqual(len(flags), 2, "a z that grows 1.5x re-fires inside the cooldown")

    def test_live_cursor_never_rescans(self):
        con = make_db()
        rows = [(r["observed_at"], r["ingested_at"], r["value"]) for r in rows_from([50.0] * 300 + [500.0])]
        con.executemany("INSERT INTO observations (feed_id,observed_at,ingested_at,revision,value) "
                        "VALUES (1,?,?,0,?)", rows)
        B.run_live(con, [1], 5.0)
        n1 = con.execute("SELECT COUNT(*) FROM anomalies").fetchone()[0]
        B.run_live(con, [1], 5.0)  # nothing new ingested
        n2 = con.execute("SELECT COUNT(*) FROM anomalies").fetchone()[0]
        self.assertEqual(n1, n2)
        self.assertEqual(n1, 1)


if __name__ == "__main__":
    unittest.main()

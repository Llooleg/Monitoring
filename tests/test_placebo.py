"""Placebo blinding tests (Plan/placebofeeds.md design rules)."""
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import placebo  # noqa: E402

HOUR = 3600


def db_with_feed(n_hours=6 * 7 * 24):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript((ROOT / "SQLSchema" / "schema.sql").read_text())
    truth = sqlite3.connect(":memory:")
    truth.row_factory = sqlite3.Row
    truth.executescript((ROOT / "SQLSchema" / "truth.sql").read_text())
    con.execute("INSERT INTO sources (source_id,name,kind,independence_group) VALUES (1,'real','web','real')")
    con.execute("INSERT INTO feeds (feed_id,source_id,metric,cadence_s,label,signal_class) "
                "VALUES (1,1,'m',3600,'real feed','real.class')")
    for i in range(n_hours):
        con.execute("INSERT INTO observations (feed_id,observed_at,ingested_at,revision,value) "
                    "VALUES (1,?,?,0,?)", (1700000000 + i * HOUR, 1700000000 + i * HOUR + 60,
                                           float(i % 24) + 10))
    con.commit()
    return con, truth


class TestPlacebo(unittest.TestCase):
    def test_roll_matches_numpy_semantics(self):
        s = [1, 2, 3, 4, 5]
        self.assertEqual(placebo.roll(s, 1), [5, 1, 2, 3, 4])
        self.assertEqual(placebo.roll(s, 2), [4, 5, 1, 2, 3])
        self.assertEqual(placebo.roll(s, 0), s)

    def test_labels_stay_neutral(self):
        con, truth = db_with_feed()
        rng = __import__("random").Random(1)
        src = placebo.real_feeds(con)[0]
        fid = placebo.make_time_shifted(con, truth, src, "Sector flow", 0, rng)
        label = con.execute("SELECT label FROM feeds WHERE feed_id = ?", (fid,)).fetchone()["label"]
        for banned in ("placebo", "control", "fake", "dummy", "synthetic"):
            self.assertNotIn(banned, label.lower())

    def test_truth_registered_and_hidden_from_feed_row(self):
        con, truth = db_with_feed()
        rng = __import__("random").Random(1)
        src = placebo.real_feeds(con)[0]
        fid = placebo.make_time_shifted(con, truth, src, "Node throughput", 1, rng)
        row = truth.execute("SELECT * FROM feed_truth WHERE feed_id = ?", (fid,)).fetchone()
        self.assertEqual(row["is_placebo"], 1)
        self.assertEqual(row["placebo_kind"], "time_shifted")
        self.assertGreaterEqual(row["shift_s"], 2 * 7 * 24 * HOUR)
        # events.db itself must carry no placebo marker anywhere agent-visible
        cols = [r["name"] for r in con.execute("PRAGMA table_info(feeds)")]
        self.assertNotIn("is_placebo", cols)

    def test_shift_breaks_alignment(self):
        con, truth = db_with_feed()
        rng = __import__("random").Random(3)
        src = placebo.real_feeds(con)[0]
        fid = placebo.make_time_shifted(con, truth, src, "Beacon rate", 2, rng)
        a = con.execute("SELECT value FROM observations WHERE feed_id = 1 ORDER BY observed_at LIMIT 1") \
            .fetchone()[0]
        b = con.execute("SELECT value FROM observations WHERE feed_id = ? ORDER BY observed_at LIMIT 1",
                        (fid,)).fetchone()[0]
        # the shifted feed's first value comes from somewhere >= 2 weeks in:
        # same distribution, different alignment
        self.assertNotEqual((a, b), (a, a))
        self.assertIn(b, [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0, 20.0,
                          21.0, 22.0, 23.0, 24.0, 25.0, 26.0, 27.0, 28.0, 29.0, 30.0, 31.0, 32.0, 33.0])


if __name__ == "__main__":
    unittest.main()

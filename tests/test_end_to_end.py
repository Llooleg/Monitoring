"""End-to-end test: the same pipeline as demo.py, asserted, in-process."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "MLanalysis"))
import baseline  # noqa: E402
import digest as digest_mod  # noqa: E402
import resolve  # noqa: E402
import scorer  # noqa: E402
import triage  # noqa: E402
from collectors import synthetic  # noqa: E402

HOUR = 3600


class TestEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        cls.events = d / "events.db"
        cls.truth = d / "truth.db"
        os.environ["EVENTS_DB"] = str(cls.events)
        os.environ["TRUTH_DB"] = str(cls.truth)
        import db as dbmod
        dbmod.open_events(cls.events)
        dbmod.open_truth(cls.truth)

        con = sqlite3.connect(cls.events)
        con.row_factory = sqlite3.Row
        cls.ground = synthetic.build_world(con, seed=99, weeks=12)
        con.close()
        import placebo
        import random
        con = sqlite3.connect(cls.events)
        con.row_factory = sqlite3.Row
        truth_con = sqlite3.connect(cls.truth)
        rng = random.Random(11)
        feeds = placebo.real_feeds(con)
        placebo.make_time_shifted(con, truth_con, feeds[0], "Sector flow", 0, rng)
        placebo.make_synthetic(con, truth_con, feeds[0], "Node throughput", 1, rng)
        con.commit()
        truth_con.commit()
        con.close()

        baseline.main([str(cls.events), "--z", "5"])
        triage.main([str(cls.events), "--llm", "mock", "--limit", "200"])
        cls.score_first = scorer_run(cls.events, cls.truth)
        os.environ["MON_FAKE_NOW"] = str(cls.ground["start"] + 12 * 7 * 24 * HOUR + 4 * 86400)
        resolve.main([str(cls.events)])
        cls.score_second = scorer_run(cls.events, cls.truth)
        con = sqlite3.connect(cls.events)
        con.row_factory = sqlite3.Row
        con.execute("ATTACH DATABASE ? AS t", (str(cls.truth),))
        con.executescript(digest_mod.DDL)
        cls.digest = digest_mod.build_digest(con, int(os.environ["MON_FAKE_NOW"]))
        cls.con = con

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("MON_FAKE_NOW", None)
        cls.tmp.cleanup()

    def real_feed_anomalies(self):
        return [r["observed_at"] for r in self.con.execute(
            "SELECT a.observed_at FROM anomalies a JOIN feeds f USING (feed_id) "
            "WHERE f.signal_class <> 'activity.watch'")]

    def test_spikes_detected(self):
        flagged = self.real_feed_anomalies()
        caught = [t for t in self.ground["spikes"] if any(t - HOUR <= o <= t + 2 * HOUR for o in flagged)]
        self.assertGreaterEqual(len(caught), 5, f"expected most spikes caught, got {len(caught)}")

    def test_outage_not_flagged(self):
        flagged = self.real_feed_anomalies()
        out = self.ground["outage"]
        self.assertFalse(any(out <= o < out + 6 * HOUR for o in flagged),
                         "coverage starvation must not produce anomalies")

    def test_backfill_not_flagged(self):
        flagged = self.real_feed_anomalies()
        bf = self.ground["backfill"]
        self.assertFalse(any(bf <= o < bf + 6 * HOUR for o in flagged))

    def test_calls_and_resolutions(self):
        n_pred = self.con.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        n_res = self.con.execute("SELECT COUNT(*) FROM resolutions").fetchone()[0]
        self.assertGreaterEqual(n_pred, 2)
        self.assertEqual(n_res, n_pred, "every mock prediction resolves by rule")
        outcomes = {r[0] for r in self.con.execute("SELECT outcome FROM resolutions")}
        self.assertEqual(outcomes, {"yes", "no"}, "both hits and misses must occur")

    def test_ledger_discipline(self):
        for r in self.con.execute("SELECT p.pred_id, p.signal_class, p.claim, p.probability, "
                                  "p.resolve_by, p.falsifier, p.resolution_rule, p.prompt_version "
                                  "FROM predictions p"):
            self.assertTrue(r["claim"] and r["falsifier"] and r["resolution_rule"])
            self.assertTrue(0 < r["probability"] < 1)
            json.loads(r["resolution_rule"])

    def test_no_alerts_before_class_earns_it(self):
        alerted = self.con.execute("SELECT COUNT(*) FROM predictions WHERE alerted = 1").fetchone()[0]
        self.assertEqual(alerted, 0, "a new class starts in shadow and must not alert")

    def test_scorer_tracks_class(self):
        self.assertIn("traffic.mesh", self.score_second)
        self.assertIn("SHADOW", self.score_second)

    def test_placebo_blind_and_counted(self):
        # no placebo label leaked into anything the agent sees
        for r in self.con.execute("SELECT label, signal_class FROM feeds"):
            self.assertNotIn("placebo", (r["label"] + r["signal_class"]).lower())
        self.assertIn("Placebo check:", self.digest)

    def test_digest_sections(self):
        for section in ("DAILY DIGEST", "PIPELINE", "Placebo check:", "CALLS (", "UNEXPLAINED (",
                        "RESOLVED SINCE LAST DIGEST (", "TRACK RECORD", "DISMISSED (", "SHADOW:",
                        "NEEDS YOU:"):
            self.assertIn(section, self.digest, f"digest must contain {section!r}")
        self.assertIn("HIT.", self.digest)
        self.assertIn("MISS.", self.digest)

    def test_miscounts_logged_when_claimed_too_high(self):
        # the mock reports honest counts; nothing should be logged
        n = self.con.execute("SELECT COUNT(*) FROM prompt_miscounts").fetchone()[0]
        self.assertEqual(n, 0)


def scorer_run(events, truth):
    """Run the scorer CLI (the only process allowed to open truth.db)."""
    import subprocess
    r = subprocess.run([sys.executable, str(ROOT / "MLanalysis" / "scorer.py"),
                        str(events), str(truth)], capture_output=True, text=True)
    return r.stdout + r.stderr


if __name__ == "__main__":
    unittest.main()

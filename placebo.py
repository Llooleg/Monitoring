#!/usr/bin/env python3
"""
placebo.py: build control feeds that go through the same machinery as real data.

    python placebo.py events.db truth.db [--n-shift 2] [--n-noise 1] [--seed 7]

Whatever the pipeline "finds" in a placebo is, by construction, a false
positive (Plan/placebofeeds.md). Two kinds are generated here:

  time_shifted  a real feed's history rolled by a random offset >= 2 weeks.
                Distribution, autocorrelation and weirdness identical; any
                link to real-world events broken. The workhorse.
  synthetic     Poisson counts tuned to a real feed's hour-of-week means.
                Tests the statistical baseline alone, no LLM involved.

Blinding rules: placebo feeds get neutral labels and their own signal_class
("activity.watch"), the is_placebo flag lives ONLY in truth.db, and the triage
process never attaches truth.db. The scorer and digest do, to compare alert
rates and verify placebos look like noise.
"""
import argparse
import random
import sqlite3
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from collectors import common

HOUR = 3600
PLACEBO_CLASS = "activity.watch"
LABELS = ["Sector flow", "Node throughput", "Registry updates", "Beacon rate",
          "North corridor traffic", "South corridor traffic", "Relay counts"]


def roll(series: list, shift: int) -> list:
    """np.roll equivalent: new[i] = old[(i - shift) % n]."""
    n = len(series)
    return [series[(i - shift) % n] for i in range(n)]


def real_feeds(con: sqlite3.Connection) -> list[dict]:
    """Feeds with >= 4 weeks of history that are not already placebos.
    (truth.db is authoritative, but this process is the one that writes it,
    so checking the placebo signal_class is enough here.)"""
    rows = con.execute(
        "SELECT f.feed_id, f.metric, f.cadence_s, COUNT(*) n, "
        "       MIN(o.observed_at) a, MAX(o.observed_at) b "
        "FROM feeds f JOIN observations o USING (feed_id) "
        "WHERE f.signal_class <> ? GROUP BY f.feed_id "
        "HAVING n >= 4*7*24 AND b > a + 4*7*24*3600",
        (PLACEBO_CLASS,)).fetchall()
    return [dict(r) for r in rows]


def series_of(con: sqlite3.Connection, feed_id: int) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT observed_at, value FROM observations WHERE feed_id = ? AND revision = 0 "
        "ORDER BY observed_at", (feed_id,))]


def how_of(t: int) -> int:
    return (((t // 86400) + 3) % 7) * 24 + (t // 3600) % 24


def make_time_shifted(con, truth_con, src_feed: dict, label: str, idx: int, rng) -> int:
    series = series_of(con, src_feed["feed_id"])
    min_shift = 2 * 7 * 24  # >= 2 weeks keeps seasonality, kills the event link
    shift = rng.randint(min_shift, max(min_shift + 1, len(series) - min_shift))
    vals = roll([s["value"] for s in series], shift)
    src = common.ensure_source(con, f"mesh-{idx}", "web", f"mesh-{idx}", "aggregator feed")
    entity = common.ensure_entity(con, "region", f"Region {chr(68 + idx)}")
    feed = common.ensure_feed(con, src, entity, src_feed["metric"], src_feed["cadence_s"],
                              f"{label} (mesh)", PLACEBO_CLASS)
    for s, v in zip(series, vals):
        common.put_observation(con, feed, s["observed_at"], float(v), ingested_at=s["observed_at"] + 90)
    truth_con.execute("INSERT OR REPLACE INTO feed_truth (feed_id, is_placebo, placebo_kind, "
                      "derived_from_feed_id, shift_s) VALUES (?,1,'time_shifted',?,?)",
                      (feed, src_feed["feed_id"], shift * HOUR))
    return feed


def make_synthetic(con, truth_con, src_feed: dict, label: str, idx: int, rng) -> int:
    series = series_of(con, src_feed["feed_id"])
    if not series:
        raise ValueError("no source history")
    by_how: dict[int, list[float]] = {}
    for s in series:
        by_how.setdefault(how_of(s["observed_at"]), []).append(s["value"])
    lam = {h: max(statistics.fmean(v), 0.1) for h, v in by_how.items()}
    src = common.ensure_source(con, f"mesh-{idx}", "web", f"mesh-{idx}", "aggregator feed")
    entity = common.ensure_entity(con, "region", f"Region {chr(68 + idx)}")
    feed = common.ensure_feed(con, src, entity, src_feed["metric"], src_feed["cadence_s"],
                              f"{label} (mesh)", PLACEBO_CLASS)
    for s in series:
        common.put_observation(con, feed, s["observed_at"],
                               float(rng.randint(0, int(2 * lam[how_of(s["observed_at"])]))),
                               ingested_at=s["observed_at"] + 90)
    truth_con.execute("INSERT OR REPLACE INTO feed_truth (feed_id, is_placebo, placebo_kind, "
                      "derived_from_feed_id, shift_s) VALUES (?,1,'synthetic',NULL,NULL)", (feed,))
    return feed


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("events_db")
    ap.add_argument("truth_db")
    ap.add_argument("--n-shift", type=int, default=2)
    ap.add_argument("--n-noise", type=int, default=1)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)

    con = sqlite3.connect(args.events_db)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    truth_con = sqlite3.connect(args.truth_db)
    truth_con.executescript((Path(__file__).parent / "SQLSchema" / "truth.sql").read_text())

    feeds = real_feeds(con)
    if not feeds:
        sys.exit("no real feed with >= 4 weeks of history; run a collector or synthetic.py first")
    rng = random.Random(args.seed)
    made = []
    for i in range(args.n_shift):
        src = feeds[i % len(feeds)]
        made.append((make_time_shifted(con, truth_con, src, LABELS[i % len(LABELS)], i, rng),
                     "time_shifted"))
    for i in range(args.n_noise):
        src = feeds[(i + 1) % len(feeds)]
        made.append((make_synthetic(con, truth_con, src,
                                    LABELS[(args.n_shift + i) % len(LABELS)], args.n_shift + i, rng),
                     "synthetic"))
    con.commit()
    truth_con.commit()
    for fid, kind in made:
        label = con.execute("SELECT label FROM feeds WHERE feed_id = ?", (fid,)).fetchone()["label"]
        assert "placebo" not in label.lower()
        print(f"placebo feed {fid}: {label} [{kind}]")


if __name__ == "__main__":
    main()

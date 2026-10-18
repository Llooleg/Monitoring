#!/usr/bin/env python3
"""
baseline.py: seasonal anomaly detection for numeric feeds.

    python baseline.py events.db                     # live: score new observations
    python baseline.py events.db --feed 3            # one feed
    python baseline.py events.db --backtest          # replay full history, no writes
    python baseline.py events.db --backtest --sweep 3,3.5,4,5

Core idea (Plan/baseline.md): judge each reading against readings from the same
hour of the week (+/- 1 hour) over the last 8 weeks, using median and MAD so
past anomalies don't pollute the baseline. Monday 9am is compared to other
Monday mornings, not to Sunday 3am.

Details that make it work on real feeds:
  Thin buckets fall back. Below MIN_SAMPLES samples, widen to same hour-of-day
  and weekday/weekend, then to everything in the window.
  Scale floors. Count feeds get a Poisson floor (sigma >= sqrt(median + 0.5)),
  because MAD = 0 on a mostly-zero feed would turn every 1 into a crisis.
  Coverage. Values are normalized by coverage; readings under half the bucket's
  normal coverage are skipped, not scored.
  Debounce. After an anomaly the feed is quiet for DEBOUNCE_S unless the z-score
  grows by DEBOUNCE_GROWTH, so one outage doesn't become 60 triage calls.
  No lookahead. Baselines use only rows ingested by the target's own
  ingested_at, at the as-first-reported vintage (revision = 0).

Defaults (8 weeks, 12 samples, z = 5, 6-hour cooldown) are guesses; run
--backtest --sweep and pick the z that gives roughly one flag a week per feed.
"""
import argparse
import sqlite3
import sys
import time
from bisect import bisect_left
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root

HOUR, DAY, WEEK = 3600, 86400, 7 * 86400
WINDOW_S = 8 * WEEK
MIN_SAMPLES = 12
Z_DEFAULT = 5.0
DEBOUNCE_S = 6 * HOUR
DEBOUNCE_GROW = 1.5
METHOD = "median_mad_v1"

DDL = """
CREATE TABLE IF NOT EXISTS score_cursor (
  feed_id    INTEGER PRIMARY KEY REFERENCES feeds,
  watermark  INTEGER NOT NULL,               -- max ingested_at processed; rows above get scored
  updated_at INTEGER NOT NULL,
  UNIQUE (feed_id)
) STRICT;
"""


def how_of(observed_at: int) -> int:
    """Hour-of-week, Monday 00:00 UTC = 0. Mirrors the generated column in schema.sql."""
    return (((observed_at // DAY) + 3) % 7) * 24 + (observed_at // HOUR) % 24


def median(xs: list[float]) -> float:
    xs = sorted(xs)
    n = len(xs)
    if n == 0:
        raise ValueError("median of empty list")
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2.0


def mad_of(xs: list[float], med: float) -> float:
    return median([abs(x - med) for x in xs])


def bucket_hours(how: int, tier: int) -> set[int]:
    """Tier 1: same hour-of-week +/-1. Tier 2: same hour-of-day +/-1 within
    weekday/weekend. Tier 3: everything."""
    if tier == 1:
        return {(how - 1) % 168, how, (how + 1) % 168}
    if tier == 2:
        day, hour = divmod(how, 24)
        days = range(0, 5) if day <= 4 else range(5, 7)
        return {d * 24 + (hour + dh) % 24 for d in days for dh in (-1, 0, 1)}
    return set(range(168))


def score_one(cand: dict, keys: list[int], known: list[tuple[int, float, float | None]],
              min_samples: int = MIN_SAMPLES) -> dict | None:
    """Score one candidate against the no-lookahead baseline.

    known/keys: rows already ingested when the candidate was ingested, as
    (observed_at, value, coverage) sorted by observed_at, with a parallel
    index for bisection.
    Returns None when the reading is skipped (thin window, low coverage),
    else {tier, n, median, mad, sigma, z, value_adj, cov_norm}.
    """
    lo = bisect_left(keys, cand["observed_at"] - WINDOW_S)
    hi = bisect_left(keys, cand["observed_at"])
    window = known[lo:hi]  # strictly before the candidate, within 8 weeks
    if not window:
        return None

    how = how_of(cand["observed_at"])
    chosen, tier = None, 0
    for t in (1, 2, 3):
        hows = bucket_hours(how, t)
        sel = [w for w in window if how_of(w[0]) in hows]
        if len(sel) >= min_samples or t == 3:
            chosen, tier = sel, t
            break
    if not chosen or len(chosen) < 3:  # nothing to say about a 2-sample bucket
        return None

    values = [w[1] for w in chosen]
    med = median(values)
    mad = mad_of(values, med)

    is_count = all(abs(v - round(v)) < 1e-9 and v >= 0 for v in values)
    sigma = 1.4826 * mad
    if is_count:
        sigma = max(sigma, (max(med, 0.0) + 0.5) ** 0.5)  # Poisson floor
    else:
        sigma = max(sigma, abs(med) * 1e-6 + 1e-9)
    if sigma <= 0:
        return None

    # Coverage: normalize to the bucket's normal coverage; skip starved readings.
    value, coverage = cand["value"], cand.get("coverage")
    cov_norm = None
    covs = [w[2] for w in chosen if w[2] is not None]
    if covs:
        cov_norm = median(covs)
    if coverage is not None and cov_norm:
        if coverage < 0.5 * cov_norm:
            return None  # a feeder going offline is not news
        if coverage > 0:
            value = value * cov_norm / coverage

    z = (value - med) / sigma
    return {"tier": tier, "n": len(chosen), "median": med, "mad": mad,
            "sigma": sigma, "z": z, "value_adj": value, "cov_norm": cov_norm}


def score_feed(rows: list[dict], z_thresh: float, on_anomaly, min_samples: int = MIN_SAMPLES) -> dict:
    """Replay a feed's history in ingestion order, calling
    on_anomaly(row, score_dict) for each reading that trips the gate.

    rows: revision-0 observations sorted by (ingested_at, observed_at) -- the
    strict sort order plus the PK means candidate i sees exactly rows[:i].
    Returns counters. The debounce state is local to the pass, so backtest
    sweeps at different thresholds stay independent.
    """
    known: list[tuple[int, float, float | None]] = []  # sorted by observed_at
    keys: list[int] = []                                # parallel observed_at index
    stats = {"scored": 0, "skipped": 0, "flagged": 0, "suppressed": 0}
    last = None  # (observed_at, |z|) of last anomaly on this feed
    for i, c in enumerate(rows):
        # No lookahead: only rows already ingested when this row arrived, at
        # first-reported vintage. Rows[:i] is exactly that set (see docstring).
        for r in rows[len(keys):i]:
            obs = r["observed_at"]
            idx = bisect_left(keys, obs)
            keys.insert(idx, obs)
            known.insert(idx, (obs, r["value"], r.get("coverage")))
        s = score_one(c, keys, known, min_samples)
        if s is None:
            stats["skipped"] += 1
            continue
        stats["scored"] += 1
        az = abs(s["z"])
        if az < z_thresh:
            continue
        if last is not None and c["observed_at"] - last[0] < DEBOUNCE_S and az < DEBOUNCE_GROW * last[1]:
            stats["suppressed"] += 1
            continue
        last = (c["observed_at"], az)
        stats["flagged"] += 1
        on_anomaly(c, s)
    return stats


def _load_rows(con: sqlite3.Connection, feed_id: int) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT observed_at, ingested_at, value, coverage FROM observations "
        "WHERE feed_id = ? AND revision = 0 ORDER BY ingested_at, observed_at", (feed_id,))]


def _feed_span(con: sqlite3.Connection, feed_id: int) -> tuple[int | None, int | None]:
    r = con.execute("SELECT MIN(observed_at) a, MAX(observed_at) b FROM observations WHERE feed_id = ?",
                    (feed_id,)).fetchone()
    return r["a"], r["b"]


def run_live(con: sqlite3.Connection, feed_ids: list[int], z_thresh: float) -> None:
    con.executescript(DDL)
    now = int(time.time())
    for fid in feed_ids:
        label = con.execute("SELECT label FROM feeds WHERE feed_id = ?", (fid,)).fetchone()
        label = label["label"] if label else f"feed{fid}"
        cur = con.execute("SELECT watermark FROM score_cursor WHERE feed_id = ?", (fid,)).fetchone()
        watermark = cur["watermark"] if cur else -1
        rows = [dict(r) for r in con.execute(
            "SELECT observed_at, ingested_at, value, coverage FROM observations "
            "WHERE feed_id = ? AND revision = 0 AND ingested_at > ? ORDER BY ingested_at, observed_at",
            (fid, watermark))]
        if not rows:
            continue

        def on_anomaly(c, s, fid=fid):
            detected = c["ingested_at"]
            con.execute("INSERT OR IGNORE INTO anomalies (feed_id, observed_at, detected_at, score, method) "
                        "VALUES (?,?,?,?,?)", (fid, c["observed_at"], detected, s["z"], METHOD))
            con.execute("INSERT OR IGNORE INTO baseline_stats (feed_id, how, as_of, method, n, median, mad) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (fid, how_of(c["observed_at"]), detected, METHOD, s["n"], s["median"], s["mad"]))

        st = score_feed(rows, z_thresh, on_anomaly)
        span = _feed_span(con, fid)
        weeks = max(((span[1] - span[0]) / WEEK) if span[0] and span[1] and span[1] > span[0] else 1, 1e-9)
        # flagged includes history already flagged on first run; per-week figure is still useful
        con.execute("INSERT OR REPLACE INTO score_cursor (feed_id, watermark, updated_at) VALUES (?,?,?)",
                    (fid, max(r["ingested_at"] for r in rows), now))
        print(f"feed {fid:>3} {label:<28} new={st['scored'] + st['skipped']:>5} flagged={st['flagged']:>3} "
              f"(suppressed {st['suppressed']}, skipped {st['skipped']})")
    con.commit()


def run_backtest(con: sqlite3.Connection, feed_ids: list[int], z_values: list[float],
                 min_samples: int = MIN_SAMPLES, verbose: bool = False) -> None:
    print(f"{'feed':>4} {'label':<26} {'weeks':>5}  "
          + "   ".join(f"{'z>=' + str(z):>6} {'/wk':>5}" for z in z_values))
    for fid in feed_ids:
        r = con.execute("SELECT label FROM feeds WHERE feed_id = ?", (fid,)).fetchone()
        label = r["label"] if r else f"feed{fid}"
        rows = _load_rows(con, fid)
        span = _feed_span(con, fid)
        weeks = max(((span[1] - span[0]) / WEEK) if span[0] and span[1] and span[1] > span[0] else 1, 1e-9)
        cells = []
        for z in z_values:
            hits = []

            def on_anomaly(c, s, hits=hits):
                hits.append((c["observed_at"], s["z"]))

            st = score_feed(rows, z, on_anomaly, min_samples)
            cells.append(f"{len(hits):>6} {len(hits) / weeks:>5.2f}")
            if verbose:
                for t, zz in hits:
                    print(f"    flag feed{fid} @{t} z={zz:+.1f}")
        print(f"{fid:>4} {label[:26]:<26} {weeks:>5.1f}  " + "   ".join(cells))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db")
    ap.add_argument("--feed", type=int, action="append", help="restrict to feed_id (repeatable)")
    ap.add_argument("--z", type=float, default=Z_DEFAULT, help=f"flag threshold on |z| (default {Z_DEFAULT})")
    ap.add_argument("--min-samples", type=int, default=MIN_SAMPLES)
    ap.add_argument("--backtest", action="store_true", help="replay history, write nothing")
    ap.add_argument("--sweep", type=str, help="comma-separated z values, implies --backtest")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row

    if args.feed:
        feed_ids = args.feed
    else:
        feed_ids = [r["feed_id"] for r in con.execute("SELECT feed_id FROM feeds WHERE active = 1 ORDER BY feed_id")]
    if not feed_ids:
        sys.exit("no feeds registered; run a collector or placebo.py first")

    if args.sweep:
        run_backtest(con, feed_ids, [float(x) for x in args.sweep.split(",")],
                     args.min_samples, args.verbose)
    elif args.backtest:
        run_backtest(con, feed_ids, [args.z], args.min_samples, args.verbose)
    else:
        run_live(con, feed_ids, args.z)


if __name__ == "__main__":
    main()

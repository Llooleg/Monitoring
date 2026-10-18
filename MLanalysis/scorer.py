#!/usr/bin/env python3
"""
scorer.py: score resolved predictions, then promote or demote signal classes.

    python scorer.py events.db truth.db

Run daily from cron. This is the only process allowed to open truth.db.

The math
  Brier score  = mean((p - outcome)^2). Lower is better, bounded in [0, 1].
  Reference    = "always predict the base rate", using only outcomes that were
                 already resolved when the prediction was made (no lookahead).
  d_i          = brier_model_i - brier_reference_i. Negative means the model helped.
  Interval     = cluster bootstrap over anomalies (predictions born from one
                 anomaly are not independent, so they get resampled together).

The status machine, per signal_class
  shadow -> active   only if the 90% interval for mean(d) is entirely below 0
                     and n >= MIN_N. Earn it with confidence.
  active -> shadow   as soon as mean(d) >= 0 over the recent window.
                     Keep it with a point estimate. Hysteresis stops flapping.
  New classes start in shadow: predicted, logged, scored, never alerted.
  A muted class keeps getting scored, so it can earn its way back.
"""
import random
import sqlite3
import sys
import time
from collections import defaultdict

MIN_N, WINDOW, BOOT, SEED = 30, 60, 2000, 1337

DDL = """
CREATE TABLE IF NOT EXISTS signal_status (
  signal_class TEXT PRIMARY KEY,
  status       TEXT NOT NULL CHECK (status IN ('shadow', 'active')),
  changed_at   INTEGER NOT NULL,
  n INTEGER, brier REAL, brier_ref REAL, skill REAL, d_mean REAL, ci_lo REAL, ci_hi REAL
) STRICT;
CREATE TABLE IF NOT EXISTS signal_status_log (
  log_id       INTEGER PRIMARY KEY,
  at           INTEGER NOT NULL,
  signal_class TEXT NOT NULL,
  old_status   TEXT,
  new_status   TEXT NOT NULL,
  n INTEGER, d_mean REAL, ci_lo REAL, ci_hi REAL,
  reason       TEXT NOT NULL
) STRICT;
CREATE TRIGGER IF NOT EXISTS status_log_no_update BEFORE UPDATE ON signal_status_log BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS status_log_no_delete BEFORE DELETE ON signal_status_log BEGIN SELECT RAISE(ABORT, 'append-only'); END;
"""

# Placebo predictions are excluded from real scoring. Otherwise noise feeds that
# share a signal_class would quietly dilute (or flatter) the real track record.
REAL = """
SELECT p.signal_class, p.made_at, r.resolved_at, p.probability AS p,
       (r.outcome = 'yes') AS y, COALESCE(p.anomaly_id, -p.pred_id) AS cluster
FROM predictions p
JOIN resolutions r USING (pred_id)
LEFT JOIN anomalies a ON a.anomaly_id = p.anomaly_id
LEFT JOIN t.feed_truth ft ON ft.feed_id = a.feed_id
WHERE r.outcome <> 'void' AND COALESCE(ft.is_placebo, 0) = 0
"""
PLACEBO = """
SELECT 'PLACEBO' AS signal_class, p.made_at, r.resolved_at, p.probability AS p,
       (r.outcome = 'yes') AS y, COALESCE(p.anomaly_id, -p.pred_id) AS cluster
FROM predictions p
JOIN resolutions r USING (pred_id)
JOIN anomalies a ON a.anomaly_id = p.anomaly_id
JOIN t.feed_truth ft ON ft.feed_id = a.feed_id AND ft.is_placebo = 1
WHERE r.outcome <> 'void'
"""


def losses(rows):
    """Per prediction: (cluster, model loss, reference loss, p, y), sorted by made_at.
    Reference = Laplace-smoothed hit rate of outcomes resolved before made_at."""
    by_resolved = sorted(rows, key=lambda r: r["resolved_at"])
    j = hits = seen = 0
    out = []
    for r in sorted(rows, key=lambda r: r["made_at"]):
        while j < len(by_resolved) and by_resolved[j]["resolved_at"] <= r["made_at"]:
            hits += by_resolved[j]["y"]
            seen += 1
            j += 1
        base = (hits + 1) / (seen + 2)
        out.append((r["cluster"], (r["p"] - r["y"]) ** 2, (base - r["y"]) ** 2, r["p"], r["y"]))
    return out


def interval(L, rng):
    """Mean of d = model - reference, with a 90% cluster-bootstrap interval."""
    groups = defaultdict(list)
    for cluster, lm, lr, *_ in L:
        groups[cluster].append(lm - lr)
    groups = list(groups.values())
    mean_d = sum(map(sum, groups)) / len(L)
    means = []
    for _ in range(BOOT):
        picked = [rng.choice(groups) for _ in groups]
        means.append(sum(map(sum, picked)) / sum(map(len, picked)))
    means.sort()
    return mean_d, means[int(0.05 * BOOT)], means[int(0.95 * BOOT) - 1]


def decide(current, n, mean_d, lo, hi):
    if n < MIN_N:
        return current, f"n={n} < {MIN_N}, not enough resolved predictions"
    if current == "shadow" and hi < 0:
        return "active", "confidently beats the base rate"
    if current == "active" and mean_d >= 0:
        return "shadow", "no longer beats the base rate"
    return current, "hold"


def may_alert(con, signal_class, n_independent, probability, min_p=0.6):
    """Gate for the orchestrator, evaluated when a prediction is inserted
    (predictions are immutable, so `alerted` is decided once). Unknown class = shadow."""
    row = con.execute("SELECT status FROM signal_status WHERE signal_class = ?", (signal_class,)).fetchone()
    return bool(row and row[0] == "active" and n_independent >= 2 and probability >= min_p)


def main(events_db, truth_db):
    con = sqlite3.connect(events_db)
    con.row_factory = sqlite3.Row
    con.executescript(DDL)
    con.execute("ATTACH ? AS t", (truth_db,))
    # tolerate a truth.db that was never initialized (placebos not set up yet)
    con.execute("CREATE TABLE IF NOT EXISTS t.feed_truth (feed_id INTEGER PRIMARY KEY, "
                "is_placebo INTEGER NOT NULL CHECK (is_placebo IN (0, 1)), "
                "placebo_kind TEXT CHECK (placebo_kind IN ('synthetic', 'irrelevant_real', 'time_shifted')), "
                "derived_from_feed_id INTEGER, shift_s INTEGER) STRICT")
    rng, now = random.Random(SEED), int(time.time())

    by_class = defaultdict(list)
    for r in con.execute(REAL):
        by_class[r["signal_class"]].append(r)

    print(f"{'class':<16}{'n':>4} {'brier':>6} {'ref':>6} {'skill':>6}  {'d_mean [90% CI]':<26}{'p-bar/hit':>10}  status")
    for cls, rows in sorted(by_class.items()):
        L = losses(rows)[-WINDOW:]
        n = len(L)
        brier, ref = sum(x[1] for x in L) / n, sum(x[2] for x in L) / n
        skill = 1 - brier / ref if ref > 0 else float("nan")
        mean_d, lo, hi = interval(L, rng)
        pbar, hit = sum(x[3] for x in L) / n, sum(x[4] for x in L) / n

        cur = con.execute("SELECT status, changed_at FROM signal_status WHERE signal_class = ?", (cls,)).fetchone()
        current = cur["status"] if cur else "shadow"
        new, why = decide(current, n, mean_d, lo, hi)
        if cur is None and why == "hold":
            why = "new class, starts in shadow"
        changed = cur is None or new != current
        con.execute("INSERT OR REPLACE INTO signal_status VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (cls, new, now if changed else cur["changed_at"], n, brier, ref, skill, mean_d, lo, hi))
        if changed:
            con.execute("INSERT INTO signal_status_log(at, signal_class, old_status, new_status, n, d_mean, ci_lo, ci_hi, reason)"
                        " VALUES (?,?,?,?,?,?,?,?,?)",
                        (now, cls, cur["status"] if cur else None, new, n, mean_d, lo, hi, why))
        flag = f"{new.upper()}" + (f"  <- {why}" if changed else "")
        print(f"{cls:<16}{n:>4} {brier:>6.3f} {ref:>6.3f} {skill:>6.2f}  "
              f"{mean_d:>+6.3f} [{lo:>+6.3f},{hi:>+6.3f}]   {pbar:>4.2f}/{hit:<4.2f}  {flag}")

    # Sanity check: placebos must look like noise. Confident skill on a coin flip
    # means lookahead leakage or a weak reference, not a discovery.
    prows = list(con.execute(PLACEBO))
    if prows:
        L = losses(prows)
        mean_d, lo, hi = interval(L, rng)
        verdict = "!! LEAK: placebos show real skill, fix the pipeline before trusting anything" \
            if len(L) >= MIN_N and hi < 0 else "ok, placebos look like noise"
        print(f"\nPLACEBO n={len(L)} d_mean={mean_d:+.3f} [{lo:+.3f},{hi:+.3f}]  {verdict}")
    con.commit()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])


#!/usr/bin/env python3
"""
demo.py: the whole pipeline, offline, on synthetic ground truth.

    python demo.py [--dir demo] [--keep]

Builds a 12-week synthetic world (6 spikes, 3 corroborated by two independent
origins; 3 drops; 1 holiday; 1 coverage outage; 1 collector backfill), adds
placebo feeds, runs the baseline, triages every anomaly with the deterministic
mock agent, fast-forwards 4 days, resolves predictions by rule, scores the
ledger, and prints the daily digest. No network, no API keys.
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "MLanalysis"))
import baseline  # noqa: E402
import triage  # noqa: E402
import digest as digest_mod  # noqa: E402

HOUR = 3600


def run(cmd, **kw):
    print(f"$ {' '.join(str(c) for c in cmd)}")
    return subprocess.run(cmd, check=True, **kw)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default="demo")
    ap.add_argument("--keep", action="store_true", help="keep an existing demo dir")
    args = ap.parse_args(argv)

    d = Path(args.dir)
    if d.exists() and not args.keep:
        shutil.rmtree(d)
    d.mkdir(exist_ok=True)
    events, truth = d / "events.db", d / "truth.db"
    os.environ["EVENTS_DB"] = str(events)
    os.environ["TRUTH_DB"] = str(truth)

    # 1. schema
    run([sys.executable, str(ROOT / "run.py"), "init"])

    # 2. synthetic world + placebos
    import collectors.synthetic as synth
    con = sqlite3.connect(events)
    con.row_factory = sqlite3.Row
    ground = synth.build_world(con, seed=42, weeks=12)
    con.close()
    run([sys.executable, str(ROOT / "placebo.py"), str(events), str(truth),
         "--n-shift", "2", "--n-noise", "1", "--seed", "7"])

    # 3. baseline sweep, then live scoring at the chosen z
    baseline.main([str(events), "--backtest", "--sweep", "4,5"])
    baseline.main([str(events), "--z", "5"])

    # 4. triage every pending anomaly with the mock agent
    triage.main([str(events), "--llm", "mock", "--limit", "100"])

    # 5. score the ledger as of now
    run([sys.executable, str(ROOT / "MLanalysis" / "scorer.py"), str(events), str(truth)])

    # 6. fast-forward 4 days: follow-up notices land, predictions come due
    os.environ["MON_FAKE_NOW"] = str(int(__import__("time").time()) + 4 * 86400)
    import resolve
    print("$ resolve (fast-forwarded 4 days)")
    resolve.main([str(events)])
    run([sys.executable, str(ROOT / "MLanalysis" / "scorer.py"), str(events), str(truth)])
    os.environ.pop("MON_FAKE_NOW", None)  # the digest is written today, like a real one

    # 7. the daily digest
    digest_mod.main([str(events), str(truth), "--out", str(d / "digest.md")])

    # 8. report against ground truth (real feed only; placebos are noise by design)
    con = sqlite3.connect(events)
    con.row_factory = sqlite3.Row
    flagged = [r["observed_at"] for r in con.execute(
        "SELECT a.observed_at FROM anomalies a JOIN feeds f USING (feed_id) "
        "WHERE a.method = 'median_mad_v1' AND f.signal_class <> 'activity.watch'")]
    caught = [t for t in ground["spikes"] if any(t - HOUR <= o <= t + 2 * HOUR for o in flagged)]
    n_pred = con.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
    n_res = con.execute("SELECT COUNT(*) FROM resolutions").fetchone()[0]
    truth_con = sqlite3.connect(truth)
    truth_con.execute("ATTACH DATABASE ? AS ev", (str(events),))
    placebo_preds = truth_con.execute(
        "SELECT COUNT(*) FROM ev.predictions p JOIN ev.anomalies a USING (anomaly_id) "
        "JOIN feed_truth ft ON ft.feed_id = a.feed_id WHERE ft.is_placebo = 1").fetchone()[0]
    print("\n=== ground truth ===")
    print(json.dumps({k: (v if not isinstance(v, list) else len(v)) for k, v in ground.items()}, indent=1))
    print(f"spike-events flagged: {len(caught)}/{len(ground['spikes'])}")
    print(f"holiday flagged: {any(ground['holiday'] <= o < ground['holiday'] + 86400 for o in flagged)}")
    print(f"outage flagged (should be False): "
          f"{any(ground['outage'] <= o < ground['outage'] + 6 * HOUR for o in flagged)}")
    print(f"predictions: {n_pred}, resolved: {n_res}, placebo predictions: {placebo_preds}")


if __name__ == "__main__":
    main()

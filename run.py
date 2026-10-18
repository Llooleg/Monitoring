#!/usr/bin/env python3
"""
run.py: wire the pipeline together.

    python run.py init                     # create events.db + truth.db
    python run.py collect [--only adsb,bgp,edgar]
    python run.py placebo [--n-shift 2] [--n-noise 1]
    python run.py baseline [--z 5] [--backtest --sweep 3.5,4,5]
    python run.py triage [--llm mock|anthropic|openai] [--limit 5]
    python run.py resolve
    python run.py score                    # MLanalysis/scorer.py
    python run.py digest [--out digest.md]
    python run.py demo                     # offline end-to-end on synthetic data

Databases come from EVENTS_DB / TRUTH_DB (default ./events.db ./truth.db).
A cron wiring that keeps every promise in the plans:

    */15 * * * *  run.py collect
    */10 * * * *  run.py baseline
    */30 * * * *  run.py triage --llm anthropic --limit 10
    5  8 * * *    run.py resolve && run.py score && run.py digest --out /var/www/digest.md

Plus a heartbeat monitor OUTSIDE this box: a missing digest means something
broke, and nothing in here can tell you that it stopped talking (Plan/alertdigest.md).
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "MLanalysis"))

EVENTS = os.environ.get("EVENTS_DB", str(ROOT / "events.db"))
TRUTH = os.environ.get("TRUTH_DB", str(ROOT / "truth.db"))


def cmd_init(args):
    import db
    db.open_events(EVENTS)
    db.open_truth(TRUTH)
    print(f"initialized {EVENTS} and {TRUTH}")


def cmd_collect(args):
    import db
    con = db.open_events(EVENTS)
    names = (args.only or "adsb,bgp,edgar").split(",")
    for name in names:
        name = name.strip()
        if name == "adsb":
            from collectors import adsb
            adsb.collect(con)
        elif name == "bgp":
            from collectors import bgp
            bgp.collect(con)
        elif name == "edgar":
            from collectors import edgar
            edgar.collect(con)
        elif name == "synthetic":
            from collectors import synthetic
            synthetic.build_world(con)
        else:
            sys.exit(f"unknown collector {name!r}")
        con.commit()


def cmd_placebo(args):
    subprocess.run([sys.executable, str(ROOT / "placebo.py"), EVENTS, TRUTH,
                    "--n-shift", str(args.n_shift), "--n-noise", str(args.n_noise)], check=True)


def cmd_baseline(args):
    argv = [EVENTS]
    if args.feed:
        argv += sum((["--feed", str(f)] for f in args.feed), [])
    if args.backtest:
        argv.append("--backtest")
    if args.sweep:
        argv += ["--sweep", args.sweep]
    if args.z:
        argv += ["--z", str(args.z)]
    if args.verbose:
        argv.append("--verbose")
    import baseline
    baseline.main(argv)


def cmd_triage(args):
    import triage
    argv = [EVENTS, "--llm", args.llm, "--limit", str(args.limit)]
    if args.model:
        argv += ["--model", args.model]
    if args.anomaly:
        argv += ["--anomaly", str(args.anomaly)]
    triage.main(argv)


def cmd_resolve(args):
    import resolve
    resolve.main([EVENTS])


def cmd_score(args):
    r = subprocess.run([sys.executable, str(ROOT / "MLanalysis" / "scorer.py"), EVENTS, TRUTH])
    sys.exit(r.returncode)


def cmd_digest(args):
    import digest
    argv = [EVENTS, TRUTH]
    if args.out:
        argv += ["--out", args.out]
    digest.main(argv)


def cmd_demo(args):
    r = subprocess.run([sys.executable, str(ROOT / "demo.py")] + sys.argv[2:])
    sys.exit(r.returncode)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(fn=cmd_init)

    p = sub.add_parser("collect")
    p.add_argument("--only", help="comma list: adsb,bgp,edgar,synthetic")
    p.set_defaults(fn=cmd_collect)

    p = sub.add_parser("placebo")
    p.add_argument("--n-shift", type=int, default=2)
    p.add_argument("--n-noise", type=int, default=1)
    p.set_defaults(fn=cmd_placebo)

    p = sub.add_parser("baseline")
    p.add_argument("--z", type=float)
    p.add_argument("--feed", type=int, action="append")
    p.add_argument("--backtest", action="store_true")
    p.add_argument("--sweep")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(fn=cmd_baseline)

    p = sub.add_parser("triage")
    p.add_argument("--llm", choices=("mock", "anthropic", "openai"), default="mock")
    p.add_argument("--model")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--anomaly", type=int)
    p.set_defaults(fn=cmd_triage)

    sub.add_parser("resolve").set_defaults(fn=cmd_resolve)
    sub.add_parser("score").set_defaults(fn=cmd_score)

    p = sub.add_parser("digest")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_digest)

    sub.add_parser("demo").set_defaults(fn=cmd_demo)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
adsb.py: aircraft counts from community ADS-B aggregators.

Default feed is the global military aircraft count from adsb.lol (/v2/mil,
ODbL, no key). Extra region counts via ADSB_QUERIES="label:url,label:url"
(any endpoint returning the ADSBExchange-v2 aircraft-list schema, e.g.
https://api.adsb.lol/v2/lat/52.5/13.4/250). Counts are point-in-time; poll
slowly (1 rps ceiling per aggregator) and let the baseline do the judging.
"""
import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from collectors import common


def collect(con: sqlite3.Connection, queries: dict[str, str] | None = None) -> None:
    queries = dict(queries or {"Global military aircraft": "https://api.adsb.lol/v2/mil"})
    src = common.ensure_source(con, "adsb.lol", "adsb", "adsb.community",
                               "ODbL; no key; be gentle, 1 rps")
    entity = common.ensure_entity(con, "region", "global")
    now = int(time.time())
    for label, url in queries.items():
        data = common.get_json(url)
        ac = data.get("ac") or []
        n, mil = len(ac), sum(1 for a in ac if a.get("dbFlags"))
        feed = common.ensure_feed(con, src, entity, f"aircraft_count[{label}]", 900,
                                  f"{label} (adsb)", "traffic.adsb")
        common.put_observation(con, feed, now, n, raw_ref=url)
        if mil:
            feed_mil = common.ensure_feed(con, src, entity, f"mil_count[{label}]", 900,
                                          f"{label} military (adsb)", "traffic.adsb")
            common.put_observation(con, feed_mil, now, mil, raw_ref=url)
        print(f"adsb.lol {label}: {n} aircraft ({mil} military)")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("db")
    args = ap.parse_args(argv)
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    queries = None
    if os.environ.get("ADSB_QUERIES"):
        queries = dict(q.split(":", 1) for q in os.environ["ADSB_QUERIES"].split(","))
    collect(con, queries)
    con.commit()


if __name__ == "__main__":
    main()

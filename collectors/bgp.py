#!/usr/bin/env python3
"""
bgp.py: BGP update activity per ASN from RIPEstat.

GET https://stat.ripe.net/data/bgp-update-activity/data.json?resource=AS<n>
returns 6-hour buckets of announcements/withdrawals for the last week, so the
first poll already backfills enough history to warm a baseline. One feed per
tracked ASN (BGP_ASN, default "13335,15169"). Counts per bucket go in as
observations; nothing here drinks the firehose or interprets a hijack.
"""
import argparse
import datetime as dt
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from collectors import common

URL = "https://stat.ripe.net/data/bgp-update-activity/data.json?resource=AS{asn}&timemode=live"
CADENCE = 6 * 3600


def collect(con: sqlite3.Connection, asns: list[str] | None = None) -> None:
    asns = asns or ["13335", "15169"]
    src = common.ensure_source(con, "ripe.ripestat", "bgp", "ripe.ris",
                               "free REST API; generous limits; CC-BY 4.0 data")
    for asn in asns:
        data = common.get_json(URL.format(asn=asn))["data"]
        period = int(data.get("sampling_period") or CADENCE)
        entity = common.ensure_entity(con, "asn", f"AS{asn}", {"asn": int(asn)})
        feed = common.ensure_feed(con, src, entity, "updates_per_bucket", period,
                                  f"AS{asn} BGP updates (ripe)", "bgp.routing")
        n_new = 0
        for row in data.get("updates", []):
            start = dt.datetime.fromisoformat(row["starttime"])
            start = start.astimezone(dt.timezone.utc)
            observed_at = int(start.timestamp()) + period  # bucket applies at its end
            ann = row.get("announcements") or 0
            wd = row.get("withdrawals") or 0
            n_new += common.put_observation(con, feed, observed_at, ann + wd,
                                            raw_ref=f"ripestat:bgp-update-activity:AS{asn}")
        print(f"ripestat AS{asn}: {n_new} new buckets (ann+withdrawals per {period}s)")


def main(argv=None):
    import os
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("db")
    args = ap.parse_args(argv)
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    collect(con, [a.strip() for a in os.environ.get("BGP_ASN", "13335,15169").split(",")])
    con.commit()


if __name__ == "__main__":
    main()

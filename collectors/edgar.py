#!/usr/bin/env python3
"""
edgar.py: SEC filings per tracked company from EDGAR submissions JSON.

EDGAR is free and keyless with a declared User-Agent (set MON_USER_AGENT with
your contact; bursts without one get 403s). Each new filing becomes an event
(origin_key = accession number, so later press coverage can be checked for
dependence against it), and each company gets a count feed (filings per hour).
EDGAR accepts filings on business days 06:00-22:00 ET -- an anomaly on a
Sunday 3am is a pipeline bug, and the baseline's hour-of-week key knows it.

Track list via EDGAR_CIKS="0000320193,0000789019" (Apple, Microsoft by default).
"""
import argparse
import datetime as dt
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from collectors import common

BASE = "https://data.sec.gov/submissions/CIK{cik:010d}.json"


def parse_date(s: str) -> int | None:
    if not s:
        return None
    try:
        return int(dt.datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp())
    except ValueError:
        return None


def collect(con: sqlite3.Connection, ciks: list[str] | None = None) -> None:
    ciks = ciks or ["0000320193", "0000789019"]
    src = common.ensure_source(con, "sec.edgar", "filing", "sec.edgar",
                               "free, keyless, declared UA, 10 rps ceiling")
    now = int(time.time())
    for cik in ciks:
        data = common.get_json(BASE.format(cik=int(cik)))
        name = data.get("name", f"CIK {cik}")
        entity = common.ensure_entity(con, "company", name, {"cik": cik})
        recent = data.get("filings", {}).get("recent", {})
        feed = common.ensure_feed(con, src, entity, "filings_per_hour", 3600,
                                  f"{name} filings (edgar)", "filings.us")
        counts: dict[int, int] = {}
        n_new = 0
        for i, acc in enumerate(recent.get("accessionNumber", [])):
            filed = parse_date(recent["filingDate"][i])
            if filed is None:
                continue
            form = recent["form"][i]
            _, created = common.put_event(
                con, src, form, filed,
                title=f"{name} {form} {recent['reportDate'][i] or ''}".strip(),
                origin_key=acc.replace("-", ""),
                payload={"form": form, "accession": acc, "cik": cik,
                         "reportDate": recent["reportDate"][i], "items": recent.get("items", [""])[i] or ""},
                entity_id=entity, magnitude=None)
            n_new += created
            counts[filed - filed % 3600] = counts.get(filed - filed % 3600, 0) + 1
        obs = 0
        for hour, n in sorted(counts.items()):
            obs += common.put_observation(con, feed, hour + 3600, n)
        print(f"edgar {name}: {n_new} new filings, {obs} new hour buckets")


def main(argv=None):
    import os
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("db")
    args = ap.parse_args(argv)
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    ciks = [c.strip() for c in os.environ.get("EDGAR_CIKS", "0000320193,0000789019").split(",")]
    collect(con, ciks)
    con.commit()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
synthetic.py: a deterministic synthetic world for offline runs and tests.

Generates 12 weeks of hourly count feeds with hour-of-week seasonality, plus
injected events the docs describe (Plan/baseline.md backtest scenario):
  - 6 scattered spikes; 3 with two INDEPENDENT corroborating events posted
    hours before, 3 followed only by dependent reposts of one blog post
  - 3 60% drops with no story attached
  - 1 holiday (a Wednesday that behaves like a Sunday)
  - 1 6-hour coverage outage where values drop with coverage
  - 1 collector backfill batch (6 hours re-ingested late, values normal)

Everything ingests with a realistic delay, so no-lookahead triage tools see
exactly what a live pipeline would have seen.
"""
import math
import random
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from collectors import common

HOUR = 3600


def _base(how: int, weekend_like: bool = False) -> float:
    day = 6 if (weekend_like or how // 24 >= 5) else 1
    hour = how % 24
    return 30 + 22 * math.sin((hour - 6) / 24 * 2 * math.pi) + day * 5


def build_world(con: sqlite3.Connection, seed: int = 42, weeks: int = 12,
               end: int | None = None) -> dict:
    """end defaults to now so the demo triages 'fresh' anomalies; tests pass
    a fixed end for determinism."""
    rng = random.Random(seed)
    n = weeks * 7 * 24
    START = (end or int(time.time())) - n * HOUR
    truth: dict = {"start": START, "spikes": [], "drops": [], "holiday": None, "outage": None, "backfill": None}

    src_synth = common.ensure_source(con, "mesh-a", "web", "mesh-a", "synthetic demo feed")
    src_notam = common.ensure_source(con, "notam.faa", "notice", "notam.faa", "synthetic demo events")
    src_news = common.ensure_source(con, "news.example", "web", "news.example", "synthetic demo events")
    src_social = common.ensure_source(con, "social.example", "social", "social.example", "synthetic demo events")
    entity = common.ensure_entity(con, "region", "Region A")
    feed = common.ensure_feed(con, src_synth, entity, "traffic_count", HOUR,
                              "Region A traffic (mesh)", "traffic.mesh")

    vals, covs = [], []
    for i in range(n):
        t = START + i * HOUR
        how = (((t // 86400) + 3) % 7) * 24 + (t // 3600) % 24
        vals.append(max(0.0, rng.gauss(_base(how), 4.0)))
        covs.append(24.0)

    # --- injected spikes, scattered, 3 corroborated + 3 reposted
    spikes = rng.sample(range(3 * 24, n - 24), 6)
    for k, s in enumerate(spikes):
        for j in range(3):
            vals[s + j] *= 4.0
        t = START + s * HOUR
        truth["spikes"].append(t)
        if k < 3:  # two independent origins, posted before the traffic shows
            for src, kind, origin in ((src_notam, "notice", f"notam:{t}"),
                                      (src_news, "news_post", f"news:{t}")):
                occurred = t - rng.randint(2, 4) * HOUR
                common.put_event(con, src, kind, occurred,
                                 title=f"Airspace restriction over Region A sector {k}",
                                 origin_key=origin,
                                 payload={"sector": f"A{k}", "effective": t, "source_hint": kind},
                                 entity_id=entity, ingested_at=occurred + 90)
            # two of the three get a follow-up notice the next day: some calls hit
            if k < 2:
                occurred = t + 24 * HOUR
                common.put_event(con, src_notam, "notice", occurred,
                                 title=f"Airspace restriction extended, Region A sector {k}",
                                 origin_key=f"notam:{t}:ext", payload={"sector": f"A{k}", "extends": t},
                                 entity_id=entity, ingested_at=occurred + 90)
            # dependent noise: one blog post, five reposts sharing its origin
            blog_origin = f"blog:{t}"
            common.put_event(con, src_news, "blog_post", t - HOUR,
                             title="Blog: unusual Region A activity",
                             origin_key=blog_origin, payload={"sector": f"A{k}"}, entity_id=entity,
                             ingested_at=t - HOUR + 90)
            for r in range(5):
                occurred = t + r * 900
                common.put_event(con, src_social, "social_post", occurred,
                                 title=f"RT: unusual Region A activity #{r}",
                                 origin_key=blog_origin, payload={"repost_of": blog_origin},
                                 entity_id=entity, ingested_at=occurred + 60)

    # --- drops
    for s in rng.sample([i for i in range(3 * 24, n - 24)
                         if not any(sp - 4 < i < sp + 4 for sp in spikes)], 3):
        vals[s] *= 0.4
        truth["drops"].append(START + s * HOUR)

    # --- holiday: the first Wednesday of week 8 behaves like a Sunday
    wed = next(i for i in range(n) if (((START + i * HOUR) // 86400 + 3) % 7) == 2 and i > 7 * 7 * 24)
    hol = wed - (wed % 24)
    for k in range(24):
        t = START + (hol + k) * HOUR
        hows = (((t // 86400) + 3) % 7) * 24 + (t // 3600) % 24
        vals[hol + k] = max(0.0, rng.gauss(_base(hows, weekend_like=True), 4.0))
    truth["holiday"] = START + hol * HOUR

    # --- 6h coverage outage at 40% receivers, values fall with them
    out = 6 * 7 * 24 + 14
    for k in range(6):
        vals[out + k] *= 0.55
        covs[out + k] = 9.6
    truth["outage"] = START + out * HOUR

    # --- collector backfill: 6 normal hours land in one late batch
    bf = 5 * 7 * 24 + 2
    truth["backfill"] = START + bf * HOUR

    for i in range(n):
        t = START + i * HOUR
        if bf <= i < bf + 6:
            ingested = t + 6 * HOUR + 300  # late, batched: the backfill signature
        else:
            ingested = t + 90 + rng.randint(0, 120)
        common.put_observation(con, feed, t, float(round(vals[i])), coverage=covs[i],
                               ingested_at=ingested)
    con.commit()
    return truth


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("db")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--weeks", type=int, default=12)
    args = ap.parse_args(argv)
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    truth = build_world(con, args.seed, args.weeks)
    print(f"synthetic world: {args.weeks} weeks, "
          f"{len(truth['spikes'])} spikes (3 corroborated), {len(truth['drops'])} drops, "
          f"holiday @{truth['holiday']}, outage @{truth['outage']}")


if __name__ == "__main__":
    main()

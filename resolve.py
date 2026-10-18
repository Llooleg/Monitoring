#!/usr/bin/env python3
"""
resolve.py: resolve predictions by rule or by human, never by the model.

    python resolve.py events.db                    # resolve everything due
    python resolve.py events.db --human 118 yes --note "manual check"
    python resolve.py events.db --list-unresolved

A prediction is due when now >= resolve_by. Its resolution_rule JSON decides:

  {"type": "count_events", "entity": "Acme Corp", "event_type": "warn_notice",
   "min_count": 2, "window_hours": 720, "exclude_states": ["CA"]}
     yes iff >= min_count matching events occurred in [made_at, resolve_by]
     (ingested by resolve time; exclude_states filters payload.state).

  {"type": "feed_max", "feed_label": "Region A traffic (mesh)", "threshold": 250}
     yes iff the feed's max first-reported value in [made_at, resolve_by]
     crosses threshold. void if the feed is unknown.

  {"type": "human"} or anything unrecognized
     left open; the digest lists it under NEEDS YOU, and resolutions come in
     via --human, which is where `resolver = 'human'` rows come from.
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import db as dbmod  # noqa: E402


def rule_count_events(con, pred: dict, rule: dict, now: int) -> tuple[str, str]:
    t0, t1 = pred["made_at"], min(pred["resolve_by"], now)
    q = ("SELECT COUNT(*) FROM events e LEFT JOIN entities ent USING (entity_id) "
         "WHERE e.event_type = ? AND e.occurred_at >= ? AND e.occurred_at <= ? "
         "AND e.ingested_at <= ?")
    args: list = [rule.get("event_type"), t0, t1, now]
    if rule.get("entity"):
        q += " AND ent.name = ?"
        args.append(rule["entity"])
    excl = rule.get("exclude_states") or []
    n = con.execute(q, args).fetchone()[0]
    if excl:  # cheap post-filter on payload.state
        rows = con.execute(q.replace("COUNT(*)", "e.payload", 1), args).fetchall()
        n = sum(1 for r in rows if json.loads(r[0] or "{}").get("state") not in excl)
    want = int(rule.get("min_count", 1))
    return ("yes" if n >= want else "no"), f"{n}/{want} matching events"


def rule_feed_max(con, pred: dict, rule: dict, now: int) -> tuple[str, str]:
    r = con.execute("SELECT feed_id FROM feeds WHERE label = ?", (rule.get("feed_label"),)).fetchone()
    if not r:
        return "void", f"feed {rule.get('feed_label')!r} not found"
    mx = con.execute("SELECT MAX(value) FROM observations WHERE feed_id = ? AND revision = 0 "
                     "AND observed_at > ? AND observed_at <= ? AND ingested_at <= ?",
                     (r["feed_id"], pred["made_at"], min(pred["resolve_by"], now), now)).fetchone()[0]
    thr = float(rule.get("threshold", 1e18))
    ok = mx is not None and mx >= thr
    return ("yes" if ok else "no"), f"max {mx} vs threshold {thr}"


RULES = {"count_events": rule_count_events, "feed_max": rule_feed_max}


def run_due(con: sqlite3.Connection, now: int) -> None:
    due = con.execute(
        "SELECT p.pred_id, p.signal_class, p.made_at, p.resolve_by, p.claim, p.resolution_rule "
        "FROM predictions p LEFT JOIN resolutions r USING (pred_id) "
        "WHERE r.pred_id IS NULL AND p.resolve_by <= ? ORDER BY p.resolve_by", (now,)).fetchall()
    for p in due:
        rule = json.loads(p["resolution_rule"]) if p["resolution_rule"] else {}
        fn = RULES.get(rule.get("type"))
        if fn is None:
            print(f"pred {p['pred_id']}: rule type {rule.get('type')!r} not machine-resolvable -> NEEDS YOU")
            continue
        outcome, note = fn(con, dict(p), rule, now)
        con.execute("INSERT INTO resolutions (pred_id, resolved_at, outcome, resolver, note) "
                    "VALUES (?,?,?,?,?)",
                    (p["pred_id"], now, outcome, f"rule:{rule['type']}", note))
        print(f"pred {p['pred_id']} [{p['signal_class']}]: {outcome.upper()} ({note})")
    con.commit()
    if not due:
        print("nothing due")


def human_resolve(con: sqlite3.Connection, pred_id: int, outcome: str, note: str, now: int) -> None:
    r = con.execute("SELECT pred_id FROM resolutions WHERE pred_id = ?", (pred_id,)).fetchone()
    if r:
        sys.exit(f"pred {pred_id} already resolved (resolutions are append-only)")
    con.execute("INSERT INTO resolutions (pred_id, resolved_at, outcome, resolver, note) "
                "VALUES (?,?,?,?,?)", (pred_id, now, outcome, "human", note))
    con.commit()
    print(f"pred {pred_id}: {outcome.upper()} by human")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db")
    ap.add_argument("--human", nargs=3, metavar=("PRED_ID", "OUTCOME", "NOTE"),
                    help="resolve a prediction manually (yes|no|void)")
    ap.add_argument("--list-unresolved", action="store_true")
    args = ap.parse_args(argv)
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    now = dbmod.now()

    if args.human:
        pid, outcome, note = args.human
        if outcome not in ("yes", "no", "void"):
            sys.exit("outcome must be yes|no|void")
        human_resolve(con, int(pid), outcome, note, now)
    elif args.list_unresolved:
        for r in con.execute(
                "SELECT p.pred_id, p.signal_class, p.resolve_by, p.resolution_rule, t.verdict "
                "FROM predictions p LEFT JOIN resolutions r USING (pred_id) "
                "LEFT JOIN triage_runs t ON t.anomaly_id = p.anomaly_id "
                "WHERE r.pred_id IS NULL ORDER BY p.resolve_by"):
            rule = json.loads(r["resolution_rule"] or "{}").get("type")
            print(f"pred {r['pred_id']:>4} due {r['resolve_by']} [{r['signal_class']}] "
                  f"rule={rule} verdict={r['verdict']}")
    else:
        run_due(con, now)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
digest.py: the daily digest and the rare alert.

    python digest.py events.db truth.db                 # print to stdout
    python digest.py events.db truth.db --out digest.md

Design rules from Plan/alertdigest.md:
  - The digest shows up daily whether or not anything happened; pipeline
    health goes first, or a dead collector looks like a calm day.
  - Alerts are rare and tiny: active class, >= 2 independent origins, p >= 0.6,
    decided in code at prediction time. Here we only render the ones that
    passed, capped at 2/day with a 24h per-class cooldown.
  - Misses get the same space as hits. Dismissed anomalies are counted, not
    listed. Shadow predictions are a count. Numbers come from SQL; the only
    narrative reused is the 80-word triage summary.
"""
import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "MLanalysis"))
import db as dbmod  # noqa: E402
import scorer  # noqa: E402

HOUR = 3600

DDL = """
CREATE TABLE IF NOT EXISTS alert_log (
  pred_id  INTEGER PRIMARY KEY REFERENCES predictions,
  sent_at  INTEGER NOT NULL,
  channel  TEXT NOT NULL DEFAULT 'push'
) STRICT;
CREATE TABLE IF NOT EXISTS digest_log (
  run_id INTEGER PRIMARY KEY,
  at     INTEGER NOT NULL,
  n_calls INTEGER, n_alerts INTEGER, n_dismissed INTEGER
) STRICT;
"""


def utc(t: int) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d %H:%M")


def day(t: int) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%a %d %b %Y")


def last_digest_at(con) -> int:
    r = con.execute("SELECT MAX(at) FROM digest_log").fetchone()
    return r[0] or 0


# ---------------------------------------------------------------- alerts

def render_alert(con, pred: dict, n_ind: int, status: dict | None) -> str:
    base = con.execute("SELECT hit_rate, n_resolved FROM v_brier_by_signal "
                       "WHERE signal_class = ?", (pred["signal_class"],)).fetchone()
    hit, n_res = (base["hit_rate"], base["n_resolved"]) if base else (0.0, 0)
    skill = status["skill"] if status else 0.0
    st = status["status"].upper() if status else "SHADOW"
    return (f"CALL · {pred['signal_class']} · p={pred['probability']:.2f}  "
            f"(class base rate {hit:.2f}, {n_res} resolved)\n"
            f"Claim: {pred['claim']}\n"
            f"Evidence: {n_ind} independent origins (recomputed from ledger).\n"
            f"Falsifier: {pred['falsifier']}\n"
            f"Class status: {st}, skill {skill:+.2f} vs base rate.\n"
            f"Details: triage/{pred['anomaly_id']}")


def pending_alerts(con, now: int) -> list[str]:
    """Alerts that passed the code gate at prediction time, minus the sent ones,
    capped at 2 per day and 24h per class."""
    sent_today = con.execute("SELECT COUNT(*) FROM alert_log WHERE sent_at > ?",
                             (now - 86400,)).fetchone()[0]
    out = []
    rows = con.execute(
        "SELECT p.*, v.n_independent, ss.status, ss.skill FROM predictions p "
        "JOIN v_independent_support v USING (pred_id) "
        "LEFT JOIN signal_status ss USING (signal_class) "
        "LEFT JOIN alert_log al USING (pred_id) "
        "WHERE p.alerted = 1 AND al.pred_id IS NULL ORDER BY p.made_at").fetchall()
    for r in rows:
        if sent_today >= 2:
            break
        last_cls = con.execute("SELECT MAX(a.sent_at) FROM alert_log a JOIN predictions p2 USING (pred_id) "
                               "WHERE p2.signal_class = ?", (r["signal_class"],)).fetchone()[0]
        if last_cls and now - last_cls < 24 * HOUR:
            continue
        status = {"status": r["status"], "skill": r["skill"]} if r["status"] else None
        out.append(render_alert(con, dict(r), r["n_independent"], status))
        con.execute("INSERT INTO alert_log (pred_id, sent_at) VALUES (?,?)", (r["pred_id"], now))
        sent_today += 1
    con.commit()
    return out


# ---------------------------------------------------------------- digest

def pipeline_health(con, now: int) -> str:
    rows = con.execute(
        "SELECT s.source_id, s.name, MIN(f.cadence_s) cadence, MAX(o.ingested_at) last "
        "FROM sources s JOIN feeds f USING (source_id) "
        "LEFT JOIN observations o ON o.feed_id = f.feed_id GROUP BY s.source_id").fetchall()
    fresh, stale, oldest = 0, [], 0
    for r in rows:
        age = (now - r["last"]) if r["last"] else 10**9
        oldest = max(oldest, age)
        if age <= 3 * r["cadence"]:
            fresh += 1
        else:
            stale.append(f"{r['name']} ({age // 3600}h stale)")
    head = (f"Pipeline OK: {fresh}/{len(rows)} collectors fresh "
            f"(oldest {max(oldest // 60, 0)} min)" if not stale
            else f"PIPELINE DEGRADED: {fresh}/{len(rows)} fresh; stale: {', '.join(stale)}")
    return head


def placebo_line(con, now: int) -> str:
    """con has truth.db attached as `t`; the digest is human-facing, so it may
    look. The triage process never does."""
    n_feeds = con.execute("SELECT COUNT(*) FROM t.feed_truth WHERE is_placebo = 1").fetchone()[0]
    if not n_feeds:
        return "Placebo check: no control feeds registered"
    esc = con.execute(
        "SELECT COUNT(*) FROM predictions p JOIN anomalies a USING (anomaly_id) "
        "JOIN t.feed_truth ft ON ft.feed_id = a.feed_id "
        "WHERE ft.is_placebo = 1").fetchone()[0]
    return (f"Placebo check: {esc} escalations on {n_feeds} control feeds"
            + (" -- noise, as intended" if esc == 0 else "  !! investigate coincidence rate"))


def dismiss_category(payload_json: str) -> str:
    p = json.loads(payload_json or "{}")
    explains = [c.get("explanation", "") for c in p.get("mundane_checks", [])
                if c.get("result") == "explains"]
    if not explains:
        return "no mechanism"
    expl = " ".join(explains).lower()
    if any(k in expl for k in ("backfill", "coverage", "duplicate", "schema", "collector", "gap", "outage")):
        return "collector backfill/coverage"
    if any(k in expl for k in ("weekend", "holiday", "quarter", "month", "calendar", "dst",
                               "market", "earnings", "filing window", "patch")):
        return "calendar"
    return "other mundane"


def build_digest(con, now: int) -> str:
    since = last_digest_at(con)
    L: list[str] = []

    n_calls = con.execute("SELECT COUNT(*) FROM predictions WHERE made_at > ?", (since,)).fetchone()[0]
    n_alerts = con.execute("SELECT COUNT(*) FROM alert_log WHERE sent_at > ?", (since,)).fetchone()[0]
    dismissed = con.execute(
        "SELECT t.verdict, COUNT(*) n, t.payload FROM triage_runs t "
        "JOIN anomalies a ON a.anomaly_id = t.anomaly_id "
        "JOIN feeds f ON f.feed_id = a.feed_id "
        "LEFT JOIN t.feed_truth ft ON ft.feed_id = f.feed_id "
        "WHERE t.run_at > ? AND COALESCE(ft.is_placebo, 0) = 0 "
        "GROUP BY t.verdict, t.payload", (since,)).fetchall()
    n_dismissed = sum(r["n"] for r in dismissed if r["verdict"] == "noise")
    L.append(f"DAILY DIGEST · {day(now)} · {n_calls} calls · {n_alerts} alerts")
    L.append(pipeline_health(con, now))
    L.append(placebo_line(con, now))

    calls = con.execute(
        "SELECT p.pred_id, p.signal_class, p.claim, p.probability, p.resolve_by, p.falsifier, "
        "       p.anomaly_id, v.n_independent, b.hit_rate "
        "FROM predictions p LEFT JOIN v_brier_by_signal b USING (signal_class) "
        "LEFT JOIN v_independent_support v USING (pred_id) "
        "WHERE p.made_at > ? AND p.alerted = 0 ORDER BY p.made_at", (since,)).fetchall()
    L.append(f"\nCALLS ({len(calls)})")
    for c in calls:
        L.append(f"· {c['claim']} p={c['probability']:.2f} "
                 f"(base {c['hit_rate'] if c['hit_rate'] is not None else 0:.2f}), "
                 f"{c['n_independent'] or 0} independent origins. Resolves {utc(c['resolve_by'])}. "
                 f"Falsifier: {c['falsifier']} [triage/{c['anomaly_id']}]")

    unex = con.execute(
        "SELECT t.anomaly_id, t.summary, a.score FROM triage_runs t JOIN anomalies a USING (anomaly_id) "
        "JOIN feeds f ON f.feed_id = a.feed_id "
        "LEFT JOIN t.feed_truth ft ON ft.feed_id = f.feed_id "
        "WHERE t.verdict = 'unexplained' AND t.run_at > ? AND COALESCE(ft.is_placebo, 0) = 0 "
        "ORDER BY t.run_at", (since,)).fetchall()
    L.append(f"\nUNEXPLAINED ({len(unex)})   <- human attention goes here")
    for u in unex:
        L.append(f"· {u['summary']} z={u['score']:+.1f}. [triage/{u['anomaly_id']}]")

    res = con.execute(
        "SELECT r.outcome, p.claim, p.probability, p.signal_class FROM resolutions r "
        "JOIN predictions p USING (pred_id) WHERE r.resolved_at > ? AND r.outcome <> 'void' "
        "ORDER BY r.resolved_at", (since,)).fetchall()
    L.append(f"\nRESOLVED SINCE LAST DIGEST ({len(res)})")
    for r in res:
        word = "HIT" if (r["outcome"] == "yes") == (r["probability"] >= 0.5) else "MISS"
        L.append(f"· {word}. \"{r['claim']}\" Said {r['probability']:.2f}, outcome {r['outcome']} "
                 f"[{r['signal_class']}]")

    L.append("\nTRACK RECORD")
    L.append(f"{'class':<16}{'n':>4}  {'hit':>4}  {'skill':>6}  status")
    for s in con.execute("SELECT signal_class, n, brier, brier_ref, skill, status FROM signal_status "
                         "ORDER BY signal_class"):
        hit = con.execute("SELECT AVG(r.outcome='yes') FROM predictions p JOIN resolutions r USING (pred_id) "
                          "WHERE p.signal_class = ?", (s["signal_class"],)).fetchone()[0] or 0
        L.append(f"{s['signal_class']:<16}{s['n']:>4}  {hit:>4.2f}  {s['skill']:>+6.2f}  {s['status']}")

    if dismissed:
        cats: dict[str, int] = {}
        for r in dismissed:
            if r["verdict"] == "noise":
                cats[dismiss_category(r["payload"])] = cats.get(dismiss_category(r["payload"]), 0) + r["n"]
        detail = ", ".join(f"{v} {k}" for k, v in sorted(cats.items(), key=lambda kv: -kv[1]))
        L.append(f"\nDISMISSED ({n_dismissed}): {detail}.")
    shadow = con.execute(
        "SELECT COUNT(*) FROM predictions p JOIN anomalies a ON a.anomaly_id = p.anomaly_id "
        "LEFT JOIN t.feed_truth ft ON ft.feed_id = a.feed_id "
        "LEFT JOIN signal_status ss ON ss.signal_class = p.signal_class "
        "WHERE p.made_at > ? AND COALESCE(ft.is_placebo, 0) = 0 "
        "AND (ss.status IS NULL OR ss.status = 'shadow')",
        (since,)).fetchone()[0]
    L.append(f"SHADOW: {shadow} predictions logged, not shown.")

    needs = con.execute(
        "SELECT p.pred_id, p.claim FROM predictions p LEFT JOIN resolutions r USING (pred_id) "
        "WHERE r.pred_id IS NULL AND p.resolve_by <= ?", (now,)).fetchall()
    L.append(f"NEEDS YOU: {len(needs)} prediction(s) can't be resolved by rule"
             + (": " + ", ".join(f"[pred {n['pred_id']}]" for n in needs) if needs else "."))
    con.execute("INSERT INTO digest_log (at, n_calls, n_alerts, n_dismissed) VALUES (?,?,?,?)",
                (now, n_calls, n_alerts, n_dismissed))
    con.commit()
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("events_db")
    ap.add_argument("truth_db", nargs="?", default=None)
    ap.add_argument("--out", help="also write the digest to this file")
    args = ap.parse_args(argv)
    con = sqlite3.connect(args.events_db)
    con.row_factory = sqlite3.Row
    con.execute("ATTACH DATABASE ? AS t", (args.truth_db or str(dbmod.truth_path()),))
    # tolerate a truth.db that was never initialized (placebos not set up yet)
    con.execute("CREATE TABLE IF NOT EXISTS t.feed_truth (feed_id INTEGER PRIMARY KEY, "
                "is_placebo INTEGER NOT NULL CHECK (is_placebo IN (0, 1)), "
                "placebo_kind TEXT CHECK (placebo_kind IN ('synthetic', 'irrelevant_real', 'time_shifted')), "
                "derived_from_feed_id INTEGER, shift_s INTEGER) STRICT")
    con.executescript(DDL)
    con.executescript(scorer.DDL)  # pending_alerts reads signal_status
    now = dbmod.now()

    for alert in pending_alerts(con, now):
        print(alert + "\n")
    text = build_digest(con, now)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n")


if __name__ == "__main__":
    main()

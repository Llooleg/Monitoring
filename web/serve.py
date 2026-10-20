#!/usr/bin/env python3
"""web/serve.py -- read-only dashboard server for events.db.

Serves the static frontend in web/ plus a small JSON API over the pipeline's
databases. Deliberately stdlib-only (same rule as the rest of the repo):

  python3 web/serve.py [--db events.db] [--digest digest.md] [--port 8417]
  python3 demo.py --dir demo && python3 web/serve.py --db demo/events.db

Rules honored here:
  - READ-ONLY. SQLite is opened with mode=ro (enforced by SQLite, not by
    convention); the pipeline stays the sole writer.
  - The blind trial is preserved: this server never opens truth.db. The only
    truth-derived artifact it touches is the already-published digest.md.
  - Replays/backtests stay honest: time windows anchor to the newest data in
    the store (MAX(observed_at)/MAX(occurred_at)), never wall clock, matching
    v_firing_rate's philosophy.
  - No websockets: the client polls /api/pulse and refetches only on change.
"""
import argparse
import contextlib
import json
import re
import sqlite3
import statistics
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

ROOT = Path(__file__).resolve().parent          # web/
DAY = 86400

RANGES = {"3d": 72, "14d": 336, "12w": 2016}    # range name -> hours

MIME = {".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".md": "text/markdown; charset=utf-8"}


# ------------------------------------------------------------------ store

class Store:
    """Read-only view of events.db + the published digest, cached by mtime."""

    def __init__(self, db_path: Path, digest_path: Path | None):
        self.db = Path(db_path)
        self.digest = Path(digest_path) if digest_path else None
        self._gen = None
        self._cache: dict = {}

    # -- generation-based invalidation: one pipeline tick = one new snapshot
    def generation(self):
        def mtime(p):
            try:
                return p.stat().st_mtime_ns
            except OSError:
                return None
        return (mtime(self.db), mtime(self.digest))

    def cached(self, key, builder):
        gen = self.generation()
        if gen != self._gen:
            self._cache.clear()
            self._gen = gen
        if key not in self._cache:
            self._cache[key] = builder()
        return self._cache[key]

    def db_ok(self):
        return self.db.exists()

    @contextlib.contextmanager
    def ro(self):
        con = sqlite3.connect(f"file:{quote(str(self.db))}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout = 2000")   # coexist with the writer
        try:
            yield con
        finally:
            con.close()

    def digest_info(self) -> dict:
        if not self.digest or not self.digest.exists():
            return {"available": False, "mtime": None, "text": None}
        return {"available": True, "mtime": self.digest.stat().st_mtime,
                "text": self.digest.read_text(encoding="utf-8", errors="replace")}


def tables(con) -> set:
    return {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}


def rows(con, sql, params=()):
    return [dict(r) for r in con.execute(sql, params)]


# ------------------------------------------------------------------ queries

def q_pulse(store: Store) -> dict:
    def mtime(p):
        try:
            return p.stat().st_mtime
        except OSError:
            return None

    out = {"db_mtime": mtime(store.db), "digest_mtime": mtime(store.digest),
           "counts": {}}
    if not store.db_ok():
        return out
    with store.ro() as con:
        t = tables(con)
        for name in ("observations", "anomalies", "predictions", "resolutions"):
            out["counts"][name] = con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        out["counts"]["triage_runs"] = (con.execute("SELECT COUNT(*) FROM triage_runs").fetchone()[0]
                                        if "triage_runs" in t else 0)
    return out


def q_overview(store: Store) -> dict:
    with store.ro() as con:
        t = tables(con)
        # store-anchored "now": replays must not depend on the wall clock
        as_of = con.execute("SELECT MAX(observed_at) FROM observations").fetchone()[0] or 0

        feeds = rows(con, """
            SELECT f.feed_id, f.label, f.signal_class,
                   MAX(o.observed_at) AS last_obs,
                   COUNT(o.observed_at) AS n_obs
            FROM feeds f LEFT JOIN observations o USING (feed_id)
            GROUP BY f.feed_id ORDER BY f.feed_id""")
        for f in feeds:
            f["stale_h"] = None if f["last_obs"] is None else round((as_of - f["last_obs"]) / 3600, 1)

        verdicts = ({r["verdict"]: r["n"] for r in rows(con,
                     "SELECT verdict, COUNT(*) AS n FROM triage_runs GROUP BY verdict")}
                    if "triage_runs" in t else {})

        anom = {"h24": con.execute("SELECT COUNT(*) FROM anomalies WHERE observed_at > ?",
                                   (as_of - DAY,)).fetchone()[0],
                "d7": con.execute("SELECT COUNT(*) FROM anomalies WHERE observed_at > ?",
                                  (as_of - 7 * DAY,)).fetchone()[0],
                "total": con.execute("SELECT COUNT(*) FROM anomalies").fetchone()[0]}

        pred = rows(con, """
            SELECT COUNT(*) AS total,
                   SUM(resolved_at IS NULL) AS open,
                   SUM(outcome = 'yes') AS yes, SUM(outcome = 'no') AS no
            FROM predictions p LEFT JOIN resolutions r USING (pred_id)""")[0]

        # track record: everything the ledger knows about a class, plus the
        # scorer's verdict (shadow/active) when it has run
        track_sql = """
            SELECT p.signal_class,
                   COUNT(*) AS n_total,
                   SUM(p.alerted) AS n_alerted,
                   b.n_resolved, b.mean_p, b.hit_rate, b.brier{status_cols}
            FROM predictions p
            LEFT JOIN v_brier_by_signal b ON b.signal_class = p.signal_class
            {status_join}
            GROUP BY p.signal_class ORDER BY p.signal_class"""
        if "signal_status" in t:
            track = rows(con, track_sql.format(
                status_cols=", s.status, s.skill, s.d_mean, s.ci_lo, s.ci_hi, s.brier, s.brier_ref",
                status_join="LEFT JOIN signal_status s ON s.signal_class = p.signal_class"))
        else:
            track = rows(con, track_sql.format(status_cols="", status_join=""))

        alerts = (rows(con, """
                    SELECT a.pred_id, a.sent_at, p.claim, p.signal_class
                    FROM alert_log a JOIN predictions p USING (pred_id)
                    ORDER BY a.sent_at DESC LIMIT 20""")
                  if "alert_log" in t else [])

        miscounts = (con.execute("SELECT COUNT(*) FROM prompt_miscounts").fetchone()[0]
                     if "prompt_miscounts" in t else 0)

    d = store.digest_info()
    return {"as_of": as_of, "feeds": feeds, "verdicts": verdicts, "anomalies": anom,
            "predictions": pred, "track_record": track, "alerts": alerts,
            "miscounts": miscounts,
            "digest": {"available": d["available"], "mtime": d["mtime"]}}


def q_feeds(store: Store) -> dict:
    with store.ro() as con:
        t = tables(con)
        fr = {r["feed_id"]: r["fired_365d"] for r in rows(
            con, "SELECT feed_id, fired_365d FROM v_firing_rate")} \
            if "v_firing_rate" in t else {}
        fs = rows(con, """
            SELECT f.feed_id, f.label, f.metric, f.signal_class, f.active,
                   s.name AS source, s.kind AS source_kind,
                   MIN(o.observed_at) AS first_obs, MAX(o.observed_at) AS last_obs,
                   COUNT(o.observed_at) AS n_obs,
                   AVG(o.coverage) AS mean_coverage
            FROM feeds f
            JOIN sources s USING (source_id)
            LEFT JOIN observations o USING (feed_id)
            GROUP BY f.feed_id ORDER BY f.feed_id""")
        anom = {r["feed_id"]: (r["n"], r["n_open"]) for r in rows(con, """
            SELECT a.feed_id, COUNT(*) AS n,
                   SUM(t.anomaly_id IS NULL) AS n_open
            FROM anomalies a LEFT JOIN triage_runs t USING (anomaly_id)
            GROUP BY a.feed_id""")} if "triage_runs" in t else {}
        for f in fs:
            f["fired_365d"] = fr.get(f["feed_id"])
            f["n_anomalies"], f["n_untriaged"] = anom.get(f["feed_id"], (0, 0))
    return {"feeds": fs}


def q_series(store: Store, feed_id: int, range_name: str) -> dict:
    hours = RANGES.get(range_name)
    if hours is None and range_name.isdigit():
        hours = min(int(range_name), 24 * 400)
    if not hours:
        raise ApiError(400, f"bad range {range_name!r}; use 3d|14d|12w or hours")
    with store.ro() as con:
        t = tables(con)
        feed = con.execute(
            "SELECT feed_id, label, signal_class FROM feeds WHERE feed_id = ?",
            (feed_id,)).fetchone()
        if not feed:
            raise ApiError(404, f"no feed {feed_id}")
        end = con.execute("SELECT MAX(observed_at) FROM observations WHERE feed_id = ?",
                          (feed_id,)).fetchone()[0]
        if end is None:
            return {"feed": dict(feed), "start": None, "end": None, "points": [],
                    "baseline": [], "band": [], "anomalies": []}
        start = end - hours * 3600
        # revision=0: first-reported vintage, the no-lookahead rule
        points = [[r[0], r[1], r[2]] for r in con.execute(
            "SELECT observed_at, value, coverage FROM observations "
            "WHERE feed_id = ? AND observed_at BETWEEN ? AND ? AND revision = 0 "
            "ORDER BY observed_at", (feed_id, start, end))]
        baseline = rows(con, """
            SELECT how, median, mad, n FROM baseline_stats b
            WHERE feed_id = ? AND as_of = (
                SELECT MAX(as_of) FROM baseline_stats
                WHERE feed_id = ? AND how = b.how AND method = b.method)
            ORDER BY how""", (feed_id, feed_id))
        # baseline_stats is z-score evidence (one row per flagged anomaly),
        # not a seasonal profile: derive the display band from the raw series,
        # same trailing window the detector trusts (12 weeks, revision 0).
        band_src = {}
        for how, value in con.execute(
                "SELECT how, value FROM observations "
                "WHERE feed_id = ? AND revision = 0 AND observed_at > ?",
                (feed_id, end - 12 * 7 * 24 * 3600)):
            band_src.setdefault(how, []).append(value)
        band = []
        for how in sorted(band_src):
            vals = band_src[how]
            med = statistics.median(vals)
            band.append({"how": how, "median": med,
                         "mad": statistics.median([abs(v - med) for v in vals]),
                         "n": len(vals)})
        anom = rows(con, """
            SELECT a.anomaly_id, a.observed_at, a.detected_at, a.score AS z,
                   t.verdict, t.summary
            FROM anomalies a LEFT JOIN triage_runs t USING (anomaly_id)
            WHERE a.feed_id = ? AND a.observed_at BETWEEN ? AND ?
            ORDER BY a.observed_at""", (feed_id, start, end)) if "triage_runs" in t else \
            rows(con, "SELECT anomaly_id, observed_at, detected_at, score AS z, "
                      "NULL AS verdict, NULL AS summary FROM anomalies "
                      "WHERE feed_id = ? AND observed_at BETWEEN ? AND ? "
                      "ORDER BY observed_at", (feed_id, start, end))
    return {"feed": dict(feed), "start": start, "end": end, "range_hours": hours,
            "points": points, "baseline": baseline, "band": band, "anomalies": anom}


def q_anomalies(store: Store, args: dict) -> dict:
    limit = min(int(args.get("limit", ["500"])[0]), 2000)
    offset = max(int(args.get("offset", ["0"])[0]), 0)
    where, params = ["1=1"], []
    if args.get("verdict"):
        where.append("t.verdict = ?")
        params.append(args["verdict"][0])
    if args.get("feed_id"):
        where.append("a.feed_id = ?")
        params.append(int(args["feed_id"][0]))
    with store.ro() as con:
        t_all = tables(con)
        if "triage_runs" not in t_all:
            # no triage yet: no verdict column, no verdict filter
            if args.get("verdict"):
                return {"anomalies": [], "total": 0, "limit": limit, "offset": offset}
            sql = f"""
                SELECT a.anomaly_id, a.feed_id, f.label, f.signal_class,
                       a.observed_at, a.detected_at, a.score AS z,
                       NULL AS verdict, NULL AS summary, p.pred_id
                FROM anomalies a
                JOIN feeds f USING (feed_id)
                LEFT JOIN predictions p USING (anomaly_id)
                WHERE {' AND '.join(where)}
                ORDER BY a.detected_at DESC LIMIT ? OFFSET ?"""
        else:
            sql = f"""
                SELECT a.anomaly_id, a.feed_id, f.label, f.signal_class,
                       a.observed_at, a.detected_at, a.score AS z,
                       t.verdict, t.summary, p.pred_id
                FROM anomalies a
                JOIN feeds f USING (feed_id)
                LEFT JOIN triage_runs t USING (anomaly_id)
                LEFT JOIN predictions p USING (anomaly_id)
                WHERE {' AND '.join(where)}
                ORDER BY a.detected_at DESC LIMIT ? OFFSET ?"""
        items = rows(con, sql, (*params, limit, offset))
        if "triage_runs" in t_all:
            total = con.execute(
                f"SELECT COUNT(*) FROM anomalies a LEFT JOIN triage_runs t USING (anomaly_id) "
                f"WHERE {' AND '.join(where)}", params).fetchone()[0]
        else:   # where can only reference a.* here (verdict filter early-returned)
            total = con.execute(
                f"SELECT COUNT(*) FROM anomalies a WHERE {' AND '.join(where)}", params).fetchone()[0]
    return {"anomalies": items, "total": total, "limit": limit, "offset": offset}


def q_predictions(store: Store) -> dict:
    with store.ro() as con:
        items = rows(con, """
            SELECT p.pred_id, p.signal_class, p.made_at, p.claim, p.probability,
                   p.resolve_by, p.falsifier, p.resolution_rule, p.model,
                   p.prompt_version, p.alerted,
                   r.outcome, r.resolved_at, r.resolver, r.note AS resolution_note,
                   f.label AS anomaly_feed, a.observed_at AS anomaly_at, a.score AS z,
                   v.n_independent
            FROM predictions p
            LEFT JOIN resolutions r USING (pred_id)
            LEFT JOIN anomalies a ON a.anomaly_id = p.anomaly_id
            LEFT JOIN feeds f ON f.feed_id = a.feed_id
            LEFT JOIN v_independent_support v ON v.pred_id = p.pred_id
            ORDER BY p.made_at DESC""")
    for p in items:
        try:
            p["resolution_rule"] = json.loads(p["resolution_rule"]) if p["resolution_rule"] else None
        except (TypeError, ValueError):
            pass
    return {"predictions": items}


def q_events(store: Store, args: dict) -> dict:
    limit = min(int(args.get("limit", ["500"])[0]), 5000)
    offset = max(int(args.get("offset", ["0"])[0]), 0)
    where, params = ["1=1"], []
    with store.ro() as con:
        until = int(args["until"][0]) if args.get("until") else \
            (con.execute("SELECT MAX(occurred_at) FROM events").fetchone()[0] or 0)
        since = int(args["since"][0]) if args.get("since") else until - 14 * DAY
        if args.get("type"):
            where.append("e.event_type = ?")
            params.append(args["type"][0])
        items = rows(con, f"""
            SELECT e.event_id, s.name AS source, s.kind AS source_kind,
                   e.event_type, e.occurred_at, e.ingested_at, e.title,
                   e.magnitude, e.origin_key, ent.name AS entity
            FROM events e
            JOIN sources s USING (source_id)
            LEFT JOIN entities ent ON ent.entity_id = e.entity_id
            WHERE e.occurred_at BETWEEN ? AND ? AND {' AND '.join(where)}
            ORDER BY e.occurred_at LIMIT ? OFFSET ?""",
            (since, until, *params, limit, offset))
    return {"events": items, "since": since, "until": until,
            "limit": limit, "offset": offset}


# ------------------------------------------------------------------ server

class ApiError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code, self.msg = code, msg


ROUTES = [
    ("GET", re.compile(r"^/api/pulse$"),        lambda s, a, m: q_pulse(s)),
    ("GET", re.compile(r"^/api/overview$"),      lambda s, a, m: q_overview(s)),
    ("GET", re.compile(r"^/api/feeds$"),         lambda s, a, m: q_feeds(s)),
    ("GET", re.compile(r"^/api/feed/(\d+)/series$"),
     lambda s, a, m: q_series(s, int(m.group(1)), a.get("range", ["14d"])[0])),
    ("GET", re.compile(r"^/api/anomalies$"),     lambda s, a, m: q_anomalies(s, a)),
    ("GET", re.compile(r"^/api/predictions$"),   lambda s, a, m: q_predictions(s)),
    ("GET", re.compile(r"^/api/events$"),        lambda s, a, m: q_events(s, a)),
    ("GET", re.compile(r"^/api/digest$"),        lambda s, a, m: s.digest_info()),
]


# Static serving: the frontend is a fixed set of flat files in web/. The
# request path is only ever a DICTIONARY KEY -- it never enters a path
# expression -- so there is nothing to traverse and nothing to decode
# (CodeQL: uncontrolled data used in path expression). New files opt in here.
STATIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/style.css": "style.css",
    "/app.js": "app.js",
    "/charts.js": "charts.js",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "monitoring-dashboard/1.0"
    store: Store = None                                   # set by make_server

    def do_GET(self):                                     # noqa: N802 (http.server API)
        u = urlparse(self.path)
        try:
            if u.path.startswith("/api/"):
                self._api(u.path, parse_qs(u.query))
            else:
                self._static(u.path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:                            # noqa: BLE001
            self._json(500, {"error": f"{type(e).__name__}: {e}"})
            traceback.print_exc(file=sys.stderr)
        finally:
            self._head = False

    def do_HEAD(self):                                    # noqa: N802 (http.server API)
        # same routing as GET; _send suppresses the body per RFC 9110
        self._head = True
        self.do_GET()

    def _api(self, path, args):
        for method, rx, fn in ROUTES:
            m = rx.match(path)
            if not m:
                continue
            if not self.store.db_ok():
                return self._json(503, {"error": f"events.db not found at {self.store.db}"})
            key = (path, tuple(sorted((k, v[0]) for k, v in args.items())))
            try:
                return self._json(200, self.store.cached(key, lambda: fn(self.store, args, m)))
            except ApiError as e:
                return self._json(e.code, {"error": e.msg})
        self._json(404, {"error": f"no such endpoint: {path}"})

    def _static(self, path):
        name = STATIC_FILES.get(path)
        if name is None:
            return self._json(404, {"error": "not found"})
        fp = ROOT / name
        if not fp.is_file():
            if name == "index.html":
                return self._text(200, "dashboard API is up; try /api/pulse /api/overview /api/feeds")
            return self._json(404, {"error": f"{name} is not built yet"})
        self._send(200, MIME.get(fp.suffix, "application/octet-stream"), fp.read_bytes())

    _head = False

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not self._head:                 # HEAD gets headers only
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(obj, default=str).encode())

    def _text(self, code, text):
        self._send(code, "text/plain; charset=utf-8", text.encode())

    def log_message(self, fmt, *args):                    # quiet the poll spam
        try:
            msg = fmt % args
        except TypeError:                                  # e.g. HTTPStatus args
            msg = str(args)
        if "/api/pulse" in msg:
            return
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(db_path, digest_path=None, host="127.0.0.1", port=0) -> Server:
    handler = type("BoundHandler", (Handler,), {"store": Store(db_path, digest_path)})
    return Server((host, port), handler)


def main(argv=None):
    import os
    ap = argparse.ArgumentParser(description="read-only dashboard server")
    ap.add_argument("--db", default=os.environ.get("EVENTS_DB", "events.db"),
                    help="path to events.db (default: $EVENTS_DB or events.db)")
    ap.add_argument("--digest", default=os.environ.get("DIGEST_OUT", "digest.md"),
                    help="path to digest.md (default: $DIGEST_OUT or digest.md)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8417)
    args = ap.parse_args(argv)

    srv = make_server(args.db, args.digest, args.host, args.port)
    print(f"dashboard: http://{args.host}:{srv.server_address[1]}  "
          f"(db={args.db}, digest={args.digest})")
    print("read-only; the pipeline keeps writing. Ctrl-C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()

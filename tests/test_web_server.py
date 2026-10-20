"""Smoke tests for web/serve.py: in-process server against a real demo world.

Covers the read-only API end to end: a world is built with the same fixed
world-end as test_end_to_end (a Thursday, so the mock's calendar check leaves
the corroborated spikes alone), pushed through baseline -> triage -> resolve
-> score, then served and polled over HTTP like the browser would.
"""
import datetime as dt
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "MLanalysis"))
sys.path.insert(0, str(ROOT / "web"))
import baseline  # noqa: E402
import resolve  # noqa: E402
import triage  # noqa: E402
import serve   # noqa: E402
from collectors import synthetic  # noqa: E402

HOUR = 3600
DAY = 86400
# Fixed Thursday, matching test_end_to_end.WORLD_END: the mock dismisses
# weekend anomalies as calendar effects, so the world layout must be pinned.
WORLD_END = int(dt.datetime(2026, 10, 15, 12, tzinfo=dt.timezone.utc).timestamp())


def http_get(port, path):
    """GET, returning (status, parsed-json-or-raw-text)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as r:
            body = r.read().decode()
            code = r.status
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        code = e.code
    try:
        return code, json.loads(body)
    except ValueError:
        return code, body


def raw_get(port, path):
    """GET with the path sent verbatim (urllib normalizes //x and friends,
    which would defeat the point of hostile-path tests)."""
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.putrequest("GET", path)
    c.putheader("Host", f"127.0.0.1:{port}")
    c.endheaders()
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, body


class TestWebServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        cls.events = d / "events.db"
        cls.truth = d / "truth.db"
        import db as dbmod
        dbmod.open_events(cls.events)
        dbmod.open_truth(cls.truth)

        con = sqlite3.connect(cls.events)
        con.row_factory = sqlite3.Row
        cls.ground = synthetic.build_world(con, seed=99, weeks=12, end=WORLD_END)
        con.commit()
        con.close()

        baseline.main([str(cls.events), "--z", "5"])
        triage.main([str(cls.events), "--llm", "mock", "--limit", "200"])
        os_env_set("MON_FAKE_NOW", WORLD_END + 4 * DAY)   # let follow-ups resolve
        resolve.main([str(cls.events)])
        r = subprocess.run([sys.executable, str(ROOT / "MLanalysis" / "scorer.py"),
                            str(cls.events), str(cls.truth)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

        cls.digest = d / "digest.md"
        cls.digest.write_text("# DAILY DIGEST \xc2\xb7 smoke test\n\nCALLS (1)\n", encoding="utf-8")

        cls.srv = serve.make_server(cls.events, cls.digest, "127.0.0.1", 0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        os_env_set("MON_FAKE_NOW", None)
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    # ------------------------------------------------------------ endpoints

    def test_pulse_reports_counts_and_mtimes(self):
        code, j = http_get(self.port, "/api/pulse")
        self.assertEqual(code, 200)
        self.assertGreater(j["counts"]["observations"], 2000)
        self.assertGreater(j["counts"]["anomalies"], 0)
        self.assertGreaterEqual(j["counts"]["predictions"], 2)
        self.assertEqual(j["counts"]["predictions"], j["counts"]["resolutions"],
                         "mock world resolves every prediction by rule")
        self.assertIsNotNone(j["db_mtime"])
        self.assertIsNotNone(j["digest_mtime"])

    def test_overview(self):
        code, j = http_get(self.port, "/api/overview")
        self.assertEqual(code, 200)
        self.assertEqual(j["as_of"], WORLD_END - HOUR)               # store-anchored
        self.assertTrue(j["feeds"], "mesh feed must be present")
        self.assertEqual(j["feeds"][0]["stale_h"], 0.0)
        self.assertGreaterEqual(j["verdicts"].get("call", 0), 2)
        self.assertGreaterEqual(j["predictions"]["total"], 2)
        self.assertGreaterEqual(j["predictions"]["yes"], 1)
        self.assertGreaterEqual(j["predictions"]["no"], 1)
        track = {t["signal_class"]: t for t in j["track_record"]}
        self.assertIn("traffic.mesh", track)
        self.assertEqual(track["traffic.mesh"]["status"], "shadow",
                         "a new class must start in shadow")
        self.assertTrue(j["digest"]["available"])

    def test_feeds(self):
        code, j = http_get(self.port, "/api/feeds")
        self.assertEqual(code, 200)
        f = j["feeds"][0]
        self.assertEqual(f["signal_class"], "traffic.mesh")
        self.assertEqual(f["n_obs"], 12 * 7 * 24)
        self.assertGreater(f["fired_365d"], 0)
        self.assertEqual(f["n_untriaged"], 0)

    def test_series_ranges_and_baseline(self):
        code, j = http_get(self.port, "/api/feed/1/series?range=14d")
        self.assertEqual(code, 200)
        self.assertEqual(j["end"], WORLD_END - HOUR)
        self.assertAlmostEqual(j["start"], WORLD_END - HOUR - 336 * HOUR, delta=HOUR)
        self.assertGreater(len(j["points"]), 300)
        self.assertLessEqual(len(j["points"]), 337)
        # stored snapshots: one row per flagged anomaly (z-score evidence)
        self.assertGreaterEqual(len(j["baseline"]), 5)
        # display band: full hour-of-week profile over the trailing 12 weeks
        self.assertEqual(len(j["band"]), 168)
        for b in j["band"]:
            self.assertGreater(b["n"], 0)
            self.assertGreaterEqual(b["mad"], 0)
        self.assertGreaterEqual(len(j["anomalies"]), 1)
        for a in j["anomalies"]:
            self.assertIn(a["verdict"], ("noise", "unexplained", "call"))
        code, j3 = http_get(self.port, "/api/feed/1/series?range=3d")
        self.assertEqual(code, 200)
        self.assertLessEqual(len(j3["points"]), 73)

    def test_series_errors(self):
        self.assertEqual(http_get(self.port, "/api/feed/999/series")[0], 404)
        self.assertEqual(http_get(self.port, "/api/feed/1/series?range=bogus")[0], 400)

    def test_anomalies_listing_and_filter(self):
        code, j = http_get(self.port, "/api/anomalies?limit=50")
        self.assertEqual(code, 200)
        self.assertGreaterEqual(j["total"], 5)
        self.assertTrue(all(a["label"] for a in j["anomalies"]))
        calls = [a for a in j["anomalies"] if a["verdict"] == "call"]
        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(all(a["pred_id"] for a in calls), "calls link into the ledger")
        _, jn = http_get(self.port, "/api/anomalies?verdict=noise")
        self.assertTrue(jn["anomalies"])
        self.assertTrue(all(a["verdict"] == "noise" for a in jn["anomalies"]))

    def test_predictions_ledger(self):
        code, j = http_get(self.port, "/api/predictions")
        self.assertEqual(code, 200)
        preds = j["predictions"]
        self.assertGreaterEqual(len(preds), 2)
        outcomes = {p["outcome"] for p in preds}
        self.assertEqual(outcomes, {"yes", "no"})
        for p in preds:
            self.assertTrue(p["claim"] and p["falsifier"])
            self.assertGreaterEqual(p["n_independent"], 2)
            self.assertIn("count_events", p["resolution_rule"]["type"])
            self.assertEqual(p["alerted"], 0)

    def test_events_window_and_type_filter(self):
        _, j = http_get(self.port, f"/api/events?since={WORLD_END - 14 * DAY}&until={WORLD_END}")
        self.assertTrue(j["events"])
        self.assertTrue(all(e["origin_key"] for e in j["events"]))
        _, jn = http_get(self.port, f"/api/events?type=notice&since={WORLD_END - 14 * DAY}")
        self.assertTrue(jn["events"])
        self.assertTrue(all(e["event_type"] == "notice" for e in jn["events"]))

    def test_digest_text(self):
        code, j = http_get(self.port, "/api/digest")
        self.assertEqual(code, 200)
        self.assertTrue(j["available"])
        self.assertIn("DAILY DIGEST", j["text"])

    def test_read_only_no_such_writes(self):
        # the connection is mode=ro: any write must fail, proving the guarantee
        with self.srv.RequestHandlerClass.store.ro() as con:
            with self.assertRaises(sqlite3.OperationalError):
                con.execute("INSERT INTO feeds (source_id, metric, cadence_s, label, signal_class) "
                            "VALUES (1,'x',1,'x','x')")

    def test_unknown_api_404_and_static_guard(self):
        code, j = http_get(self.port, "/api/nope")
        self.assertEqual(code, 404)
        self.assertIn("error", j)
        # traversal in every spelling must die at the allowlist lookup:
        # the request path is a dict key, never a path expression.
        # Sent raw -- urllib would normalize some of these client-side.
        # ("//x" vectors are absent: the stdlib itself collapses leading "//"
        # before handler code runs -- CPython gh-87389.)
        for evil in ("/../serve.py", "/..%2fserve.py", "/%2e%2e/serve.py",
                     "/../../etc/passwd", "/vendor/sql-wasm.js", "/web/serve.py",
                     "/index.html/../../../db.py", "/style.css/..", "/.",
                     "/C:/windows/win.ini"):
            code, body = raw_get(self.port, evil)
            self.assertEqual(code, 404, f"{evil!r} must not be served")
            self.assertIn(b"error", body)

    def test_head_requests(self):
        # curl -I / proxies: same headers, no body, no server-side traceback
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/", method="HEAD")
        with urllib.request.urlopen(req) as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(len(r.read()), 0)
            self.assertIn("text/html", r.headers["Content-Type"])
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/pulse", method="HEAD")
        with urllib.request.urlopen(req) as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(len(r.read()), 0)

    def test_static_shell_served(self):
        code, body = http_get(self.port, "/")
        self.assertEqual(code, 200)
        self.assertIsInstance(body, str)
        for marker in ('id="main"', 'id="tabs"', "app.js", "style.css", "api/pulse"):
            self.assertIn(marker, body, f"shell must reference {marker!r}")
        for path, ctype in (("/style.css", "text/css"),
                            ("/app.js", "application/javascript")):
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as r:
                self.assertEqual(r.status, 200)
                self.assertIn(ctype, r.headers["Content-Type"])
                self.assertGreater(len(r.read()), 100)


def os_env_set(key, value):
    import os
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = str(value)


if __name__ == "__main__":
    unittest.main()

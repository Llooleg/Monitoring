"""Validator, independence and blinding tests for triage.py."""
import datetime as dt
import json
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "MLanalysis"))
import triage  # noqa: E402
import scorer  # noqa: E402

HOUR = 3600
T0 = 1700000000


def mini_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript((ROOT / "SQLSchema" / "schema.sql").read_text())
    con.executescript(scorer.DDL)
    con.executescript(triage.DDL)
    con.execute("INSERT INTO sources (source_id,name,kind,independence_group) VALUES (1,'s1','web','g1')")
    con.execute("INSERT INTO sources (source_id,name,kind,independence_group) VALUES (2,'s2','web','g2')")
    con.execute("INSERT INTO entities (entity_id,kind,name) VALUES (1,'region','Region A')")
    con.execute("INSERT INTO feeds (feed_id,source_id,entity_id,metric,cadence_s,label,signal_class) "
                "VALUES (1,1,1,'m',3600,'feed one','test.class')")
    con.execute("INSERT INTO observations (feed_id,observed_at,ingested_at,revision,value) "
                "VALUES (1,?,?,0,?)", (T0, T0 + 60, 42.0))
    con.execute("INSERT INTO anomalies (anomaly_id,feed_id,observed_at,detected_at,score,method) "
                "VALUES (7,1,?,?,8.0,'median_mad_v1')", (T0, T0 + 60))
    # two events with the SAME origin_key (a repost), one with a different origin
    con.execute("INSERT INTO events (event_id,source_id,event_type,occurred_at,ingested_at,title,"
                "origin_key,content_hash,payload) VALUES (1,1,'notice',?,?,?, 'o1','h1','{}')",
                (T0 - HOUR, T0 - HOUR + 30, "post"))
    con.execute("INSERT INTO events (event_id,source_id,event_type,occurred_at,ingested_at,title,"
                "origin_key,content_hash,payload) VALUES (2,2,'notice',?,?,?,'o1','h2','{}')",
                (T0 - HOUR, T0 - HOUR + 40, "repost"))
    con.execute("INSERT INTO events (event_id,source_id,event_type,occurred_at,ingested_at,title,"
                "origin_key,content_hash,payload) VALUES (3,2,'notice',?,?,?,'o2','h3','{}')",
                (T0 - HOUR, T0 - HOUR + 50, "independent"))
    # an event ingested AFTER detection: invisible
    con.execute("INSERT INTO events (event_id,source_id,event_type,occurred_at,ingested_at,title,"
                "origin_key,content_hash,payload) VALUES (4,2,'notice',?,?,?,'o3','h4','{}')",
                (T0 - HOUR, T0 + 9999, "future"))
    return con


def base_out(**kw):
    out = {"anomaly_id": 7, "verdict": "noise",
           "base_rate": {"fired_365d": 0, "class_hit_rate": 0.0, "class_n_resolved": 0},
           "mundane_checks": [
               {"explanation": "a", "how_checked": "x", "result": "ruled_out", "evidence": []},
               {"explanation": "b", "how_checked": "x", "result": "ruled_out", "evidence": []},
               {"explanation": "c", "how_checked": "x", "result": "explains", "evidence": []}],
           "mechanism": None, "hypotheses": [], "independent_origins": [],
           "dependent_items_discarded": [], "n_independent": 0, "prediction": None,
           "summary": "s"}
    out.update(kw)
    return out


class TestTools(unittest.TestCase):
    def test_cutoff_blinds_future(self):
        con = mini_db()
        tools = triage.Tools(con, T0 + 60)
        evs = tools.search_events(since=T0 - 2 * HOUR, until=T0)
        ids = {e["event_id"] for e in evs}
        self.assertEqual(ids, {1, 2, 3}, "the future-ingested event 4 must be invisible")
        ser = tools.get_series(1, T0, T0 + 1)
        self.assertEqual(len(ser), 1)

    def test_series_cutoff(self):
        con = mini_db()
        con.execute("INSERT INTO observations (feed_id,observed_at,ingested_at,revision,value) "
                    "VALUES (1,?,?,0,?)", (T0 + HOUR, T0 + HOUR + 10, 43.0))
        tools = triage.Tools(con, T0 + 60)
        self.assertEqual(len(tools.get_series(1, T0, T0 + 2 * HOUR)), 1)


class TestValidator(unittest.TestCase):
    def setUp(self):
        self.con = mini_db()
        self.tools = triage.Tools(self.con, T0 + 60)
        self.anomaly = {"anomaly_id": 7, "feed_id": 1, "observed_at": T0, "detected_at": T0 + 60}
        self.now = T0 + 60

    def v(self, out):
        return triage.validate(out, self.anomaly, self.tools, self.now)

    def test_valid_noise(self):
        self.assertEqual(self.v(base_out()), [])

    def test_needs_three_checks(self):
        out = base_out(mundane_checks=base_out()["mundane_checks"][:2])
        self.assertTrue(any("3 mundane_checks" in e for e in self.v(out)))

    def test_noise_cannot_predict(self):
        out = base_out(prediction={"x": 1})
        self.assertTrue(any("prediction: null" in e for e in self.v(out)))

    def test_call_needs_hypothesis_and_prediction(self):
        out = base_out(verdict="call", hypotheses=[], prediction=None)
        errs = self.v(out)
        self.assertTrue(any("hypothesis" in e for e in errs))

    def call_out(self, **kw):
        pred = {"signal_class": "test.class", "claim": "c", "probability": 0.5,
                "resolve_by": dt.datetime.fromtimestamp(self.now + 72 * HOUR, dt.timezone.utc)
                              .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "falsifier": "f",
                "resolution_rule": {"type": "count_events", "event_type": "notice", "min_count": 1},
                "evidence": [{"event_id": 1, "role": "supports"}, {"event_id": 3, "role": "supports"}]}
        out = base_out(verdict="call", mechanism="m",
                       hypotheses=[{"id": "H1", "statement": "s",
                                    "contradicting_evidence_searched": "x",
                                    "contradicting_evidence_found": []}],
                       independent_origins=[
                           {"origin": "o1", "source": "s1", "event_ids": [1]},
                           {"origin": "o2", "source": "s2", "event_ids": [3]}],
                       n_independent=2, prediction=pred)
        for k, v_ in kw.items():
            if k == "prediction":
                pred.update(v_)
            else:
                out[k] = v_
        return out

    def test_valid_call(self):
        self.assertEqual(self.v(self.call_out()), [])

    def test_probability_guardrails(self):
        out = self.call_out(prediction={"probability": 0.99})
        self.assertTrue(any("0.02..0.95" in e for e in self.v(out)))
        out = self.call_out(prediction={"probability": 0.0})
        self.assertTrue(any("0.02..0.95" in e for e in self.v(out)))

    def test_single_origin_probability_cap(self):
        # class base rate exists and is low -> p far above it with n=1 fails
        self.con.execute("INSERT INTO predictions (pred_id,anomaly_id,signal_class,made_at,claim,"
                         "probability,resolve_by,falsifier,model,prompt_version) "
                         "VALUES (1,NULL,'test.class',?,'c',0.5,?,'f','t','v')", (T0 - 100, T0 - 50))
        self.con.execute("INSERT INTO resolutions (pred_id,resolved_at,outcome,resolver) "
                         "VALUES (1,?, 'no','rule:x')", (T0 - 40,))
        out = self.call_out(prediction={"probability": 0.9},
                            independent_origins=[{"origin": "o1", "source": "s1", "event_ids": [1]}],
                            n_independent=1)
        self.assertTrue(any("within 0.15 of the class base rate" in e for e in self.v(out)))

    def test_resolve_by_window(self):
        for hours, bad in ((1, True), (23, True), (25, False), (31 * 24, True)):
            out = self.call_out(prediction={
                "resolve_by": dt.datetime.fromtimestamp(self.now + hours * HOUR, dt.timezone.utc)
                              .strftime("%Y-%m-%dT%H:%M:%SZ")})
            errs = self.v(out)
            self.assertEqual(any("24 hours to 30 days" in e for e in errs), bad, f"hours={hours}")

    def test_n_independent_consistency(self):
        out = self.call_out(n_independent=3)
        self.assertTrue(any("n_independent must equal" in e for e in self.v(out)))

    def test_invisible_evidence_rejected(self):
        out = self.call_out(prediction={"evidence": [{"event_id": 4, "role": "supports"}]})
        self.assertTrue(any("not visible" in e for e in self.v(out)))

    def test_unknown_rule_type_rejected(self):
        out = self.call_out(prediction={"resolution_rule": {"type": "vibes"}})
        self.assertTrue(any("resolution_rule" in e for e in self.v(out)))


class TestIndependenceAndGate(unittest.TestCase):
    def test_origin_key_collapses_reposts(self):
        con = mini_db()
        ev = [{"event_id": 1, "role": "supports"},
              {"event_id": 2, "role": "supports"},   # same origin_key 'o1'
              {"event_id": 3, "role": "supports"}]   # origin 'o2'
        self.assertEqual(triage.computed_independence(con, ev), 2)

    def test_view_agrees(self):
        con = mini_db()
        con.execute("INSERT INTO predictions (pred_id,anomaly_id,signal_class,made_at,claim,"
                    "probability,resolve_by,falsifier,model,prompt_version) "
                    "VALUES (1,7,'test.class',?,'c',0.5,?,'f','t','v')", (T0, T0 + 10**6))
        for eid, role in ((1, "supports"), (2, "supports"), (3, "supports")):
            con.execute("INSERT INTO prediction_evidence (pred_id,event_id,role) VALUES (1,?,?)",
                        (eid, role))
        con.execute("INSERT INTO prediction_evidence (pred_id,anomaly_id,role) VALUES (1,7,'supports')")
        n = con.execute("SELECT n_independent FROM v_independent_support WHERE pred_id = 1").fetchone()[0]
        # o1 + o2 + anomaly's feed group g1 = 3
        self.assertEqual(n, 3)

    def test_may_alert_gate(self):
        con = mini_db()
        con.execute("INSERT INTO signal_status (signal_class,status,changed_at) "
                    "VALUES ('test.class','shadow',?)", (T0,))
        self.assertFalse(scorer.may_alert(con, "test.class", 2, 0.7))   # shadow
        con.execute("INSERT OR REPLACE INTO signal_status (signal_class,status,changed_at) "
                    "VALUES ('test.class','active',?)", (T0,))
        self.assertFalse(scorer.may_alert(con, "test.class", 1, 0.7))   # 1 origin
        self.assertFalse(scorer.may_alert(con, "test.class", 2, 0.5))   # p < 0.6
        self.assertTrue(scorer.may_alert(con, "test.class", 2, 0.6))

    def test_miscount_logged(self):
        con = mini_db()
        client = type("M", (), {"name": "mock"})()
        prompt, version = triage.load_prompt()
        out = json.loads(triage.mock_run(triage.Tools(con, T0 + 60), 7, T0 + 60))
        # force an overcount: claim 5 origins where the DB sees 3 (o1, o2, feed group)
        n_real = len(out["independent_origins"])
        out["n_independent"] = 5
        out["independent_origins"] = out["independent_origins"] + [
            {"origin": f"fake{i}", "source": "x", "event_ids": []} for i in range(5 - n_real)]
        import triage as T

        orig = T.parse_json
        T.parse_json = lambda text: out  # inject our doctored payload
        try:
            T.triage_anomaly(con, client, 7, prompt, version)
        finally:
            T.parse_json = orig
        n = con.execute("SELECT COUNT(*) FROM prompt_miscounts").fetchone()[0]
        self.assertGreaterEqual(n, 1, "overcounted n_independent must be logged")


class TestPrompt(unittest.TestCase):
    def test_prompt_loads_and_versions(self):
        prompt, version = triage.load_prompt()
        self.assertIn("triage analyst", prompt)
        self.assertEqual(len(version), 10)

    def test_json_extraction_with_fences(self):
        self.assertEqual(triage.parse_json('blurb ```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(triage.parse_json('noise {"a": {"b": 2}} tail'), {"a": {"b": 2}})
        self.assertIsNone(triage.parse_json("no json at all"))


if __name__ == "__main__":
    unittest.main()

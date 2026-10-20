#!/usr/bin/env python3
"""
triage.py: the LLM triage agent.

    python triage.py events.db --llm mock                     # offline, deterministic
    python triage.py events.db --llm anthropic                # needs ANTHROPIC_API_KEY
    python triage.py events.db --llm openai --limit 5         # needs OPENAI_API_KEY
    python triage.py events.db --anomaly 4412

The system prompt is read live from Plan/triageprompt.md (everything below the
`---` line) so there is one source of truth; prompt_version is its content
hash and bumps on every edit. Code, not the model, decides:

  - tools are read-only and every query cuts off at ingested_at <= the
    anomaly's detected_at, so the agent cannot see the future;
  - this process never attaches truth.db (placebo blinds hold);
  - validation rules from the prompt are enforced here, with one retry;
  - alerts come from scorer.may_alert(): active class, >= 2 independent
    origins recomputed from the database via v_independent_support, p >= 0.6;
  - a claimed n_independent above the recomputed count is logged as a
    miscount against this prompt version.
"""
import argparse
import datetime as dt
import hashlib
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "MLanalysis"))
import db as dbmod  # noqa: E402
import scorer  # noqa: E402  (may_alert, DDL)

PROMPT_FILE = ROOT / "Plan" / "triageprompt.md"
HOUR = 3600

DDL = """
CREATE TABLE IF NOT EXISTS triage_runs (
  run_id         INTEGER PRIMARY KEY,
  anomaly_id     INTEGER NOT NULL REFERENCES anomalies,
  verdict        TEXT NOT NULL CHECK (verdict IN ('noise','unexplained','call','invalid')),
  summary        TEXT,
  payload        TEXT NOT NULL,
  model          TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  run_at         INTEGER NOT NULL,
  UNIQUE (anomaly_id)
) STRICT;
CREATE TABLE IF NOT EXISTS prompt_miscounts (
  mc_id         INTEGER PRIMARY KEY,
  prompt_version TEXT NOT NULL,
  anomaly_id    INTEGER NOT NULL,
  claimed       INTEGER NOT NULL,
  computed      INTEGER NOT NULL,
  at            INTEGER NOT NULL
) STRICT;
"""

RESOLVER_TYPES = ("count_events", "feed_max", "human")


def load_prompt() -> tuple[str, str]:
    """System prompt = Plan/triageprompt.md below the --- line. Version = hash."""
    text = PROMPT_FILE.read_text()
    _, _, prompt = text.partition("\n---\n")
    if not prompt.strip():
        sys.exit("could not find the system prompt below the --- line in Plan/triageprompt.md")
    return prompt.strip(), hashlib.sha1(prompt.encode()).hexdigest()[:10]


# ---------------------------------------------------------------- tools

class Tools:
    """Read-only, cutoff-bound views over events.db.

    detected_at is the anomaly's own detection time; every query filters
    ingested_at <= detected_at (and resolutions/predictions likewise), so the
    agent sees exactly the world as it was when the anomaly fired.
    """

    def __init__(self, con: sqlite3.Connection, detected_at: int):
        self.con = con
        self.cutoff = detected_at

    def get_anomaly(self, anomaly_id: int) -> dict:
        r = self.con.execute(
            "SELECT a.anomaly_id, a.feed_id, a.observed_at, a.detected_at, a.score z, a.method, "
            "       f.label, f.metric, f.signal_class, s.name source, e.name entity, "
            "       o.value, o.coverage "
            "FROM anomalies a JOIN feeds f USING (feed_id) JOIN sources s USING (source_id) "
            "LEFT JOIN entities e ON e.entity_id = f.entity_id "
            "LEFT JOIN observations o ON o.feed_id = a.feed_id AND o.observed_at = a.observed_at "
            "                        AND o.revision = 0 "
            "WHERE a.anomaly_id = ?", (anomaly_id,)).fetchone()
        if not r:
            return {"error": "not found"}
        d = dict(r)
        how = (((d["observed_at"] // 86400) + 3) % 7) * 24 + (d["observed_at"] // 3600) % 24
        b = self.con.execute(
            "SELECT n, median, mad FROM baseline_stats WHERE feed_id = ? AND how = ? "
            "AND as_of <= ? ORDER BY as_of DESC LIMIT 1", (d["feed_id"], how, self.cutoff)).fetchone()
        d["baseline"] = dict(b) if b else None
        d["hour_of_week"] = how
        return d

    def get_firing_rate(self, feed_id: int) -> dict:
        fired = self.con.execute(
            "SELECT COUNT(*) FROM anomalies WHERE feed_id = ? AND detected_at > ? AND detected_at <= ?",
            (feed_id, self.cutoff - 365 * 86400, self.cutoff)).fetchone()[0]
        disp = self.con.execute(
            "SELECT t.verdict, COUNT(*) n FROM anomalies a JOIN triage_runs t USING (anomaly_id) "
            "WHERE a.feed_id = ? AND t.run_at <= ? GROUP BY t.verdict", (feed_id, self.cutoff)).fetchall()
        return {"fired_365d": fired, "dispositions": {r["verdict"]: r["n"] for r in disp}}

    def class_stats(self, signal_class: str) -> dict:
        r = self.con.execute(
            "SELECT COUNT(*) n, AVG(r.outcome = 'yes') hit FROM predictions p "
            "JOIN resolutions r USING (pred_id) "
            "WHERE p.signal_class = ? AND r.outcome <> 'void' AND r.resolved_at <= ?",
            (signal_class, self.cutoff)).fetchone()
        return {"class_hit_rate": r["hit"], "class_n_resolved": r["n"]}

    def get_series(self, feed_id: int, start: int, end: int) -> list:
        rows = self.con.execute(
            "SELECT observed_at, ingested_at, value, coverage FROM observations "
            "WHERE feed_id = ? AND revision = 0 AND observed_at BETWEEN ? AND ? AND ingested_at <= ? "
            "ORDER BY observed_at", (feed_id, start, end, self.cutoff)).fetchall()
        return [dict(r) for r in rows]

    def search_events(self, entity: str | None = None, event_type: str | None = None,
                      since: int | None = None, until: int | None = None,
                      text: str | None = None) -> list:
        q = ("SELECT e.event_id, s.name source, e.event_type, e.entity_id, ent.name entity, "
             "e.occurred_at, e.ingested_at, e.title, e.magnitude, e.origin_key "
             "FROM events e JOIN sources s USING (source_id) LEFT JOIN entities ent USING (entity_id) "
             "WHERE e.ingested_at <= ?")
        args: list = [self.cutoff]
        if entity:
            q += " AND ent.name = ?"
            args.append(entity)
        if event_type:
            q += " AND e.event_type = ?"
            args.append(event_type)
        if since is not None:
            q += " AND e.occurred_at >= ?"
            args.append(since)
        if until is not None:
            q += " AND e.occurred_at <= ?"
            args.append(until)
        if text:
            q += " AND (e.title LIKE ? OR e.payload LIKE ?)"
            args += [f"%{text}%", f"%{text}%"]
        q += " ORDER BY e.occurred_at DESC LIMIT 100"
        return [dict(r) for r in self.con.execute(q, args)]


TOOL_SPECS = [
    {"name": "get_anomaly", "description": "The anomaly under triage: feed, entity, value, z-score, baseline median/MAD and sample size for that hour-of-week, coverage.",
     "params": {"type": "object", "properties": {"anomaly_id": {"type": "integer"}}, "required": ["anomaly_id"]}},
    {"name": "get_firing_rate", "description": "How often this feed fired in the last 365 days, and how past anomalies were dispositioned.",
     "params": {"type": "object", "properties": {"feed_id": {"type": "integer"}}, "required": ["feed_id"]}},
    {"name": "class_stats", "description": "Share of resolved predictions in this class that came true, and how many resolved.",
     "params": {"type": "object", "properties": {"signal_class": {"type": "string"}}, "required": ["signal_class"]}},
    {"name": "get_series", "description": "Raw observations for a feed (revision 0, ingested as of the anomaly).",
     "params": {"type": "object", "properties": {"feed_id": {"type": "integer"}, "start": {"type": "integer"},
                                                 "end": {"type": "integer"}},
                "required": ["feed_id", "start", "end"]}},
    {"name": "search_events", "description": "Discrete events: filings, notices, hijack alerts, posts. Every field is optional.",
     "params": {"type": "object", "properties": {"entity": {"type": "string"}, "event_type": {"type": "string"},
                                                 "since": {"type": "integer"}, "until": {"type": "integer"},
                                                 "text": {"type": "string"}}}},
]


def call_tool(tools: Tools, name: str, args: dict):
    fn = getattr(tools, name, None)
    if fn is None or name.startswith("_"):
        return {"error": f"unknown tool {name}"}
    try:
        return fn(**args)
    except TypeError as e:
        return {"error": f"bad arguments for {name}: {e}"}


# ---------------------------------------------------------------- LLM clients

def _post(url: str, headers: dict, body: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} from {url}: {e.read()[:400]!r}") from e


class AnthropicClient:
    name = "anthropic"

    def __init__(self, model: str | None = None):
        self.key = os.environ.get("ANTHROPIC_API_KEY") or sys.exit("set ANTHROPIC_API_KEY")
        self.model = model or os.environ.get("TRIAGE_MODEL", "claude-sonnet-4-5")
        self.base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")

    def run(self, system: str, user_msg: str, tools: Tools) -> str:
        headers = {"x-api-key": self.key, "anthropic-version": "2023-06-01"}
        schemas = [{"name": t["name"], "description": t["description"], "input_schema": t["params"]}
                   for t in TOOL_SPECS]
        messages = [{"role": "user", "content": user_msg}]
        for _ in range(14):  # tool-use budget
            resp = _post(f"{self.base}/v1/messages", headers,
                         {"model": self.model, "max_tokens": 4000, "temperature": 0.2,
                          "system": system, "messages": messages, "tools": schemas})
            if resp.get("type") == "error":
                raise RuntimeError(resp.get("error", {}).get("message", "api error"))
            content = resp["content"]
            messages.append({"role": "assistant", "content": content})
            if resp.get("stop_reason") != "tool_use":
                return "".join(b.get("text", "") for b in content if b.get("type") == "text")
            results = []
            for block in content:
                if block.get("type") != "tool_use":
                    continue
                out = call_tool(tools, block["name"], block.get("input") or {})
                results.append({"type": "tool_result", "tool_use_id": block["id"],
                                "content": json.dumps(out, default=str)[:12000]})
            messages.append({"role": "user", "content": results})
        raise RuntimeError("tool-use budget exhausted")


class OpenAIClient:
    name = "openai"

    def __init__(self, model: str | None = None):
        self.key = os.environ.get("OPENAI_API_KEY") or sys.exit("set OPENAI_API_KEY")
        self.model = model or os.environ.get("TRIAGE_MODEL", "gpt-4o")
        self.base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")

    def run(self, system: str, user_msg: str, tools: Tools) -> str:
        headers = {"Authorization": f"Bearer {self.key}"}
        schemas = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                     "parameters": t["params"]}} for t in TOOL_SPECS]
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user_msg}]
        for _ in range(14):
            resp = _post(f"{self.base}/chat/completions", headers,
                         {"model": self.model, "temperature": 0.2, "messages": messages,
                          "tools": schemas, "tool_choice": "auto"})
            msg = resp["choices"][0]["message"]
            messages.append(msg)
            calls = msg.get("tool_calls") or []
            if not calls:
                return msg.get("content") or ""
            for c in calls:
                fn = c["function"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                out = call_tool(tools, fn["name"], args)
                messages.append({"role": "tool", "tool_call_id": c["id"],
                                 "content": json.dumps(out, default=str)[:12000]})
        raise RuntimeError("tool-use budget exhausted")


# ---------------------------------------------------------------- mock agent

def mock_run(tools: Tools, anomaly_id: int, now: int) -> str:
    """Deterministic stand-in that follows the procedure with the same tools.
    Used by the demo and tests; not a substitute for a real model."""
    a = tools.get_anomaly(anomaly_id)
    feed_id, obs_t = a["feed_id"], a["observed_at"]
    fr = tools.get_firing_rate(feed_id)
    cs = tools.class_stats(a["signal_class"])
    base_rate = cs["class_hit_rate"] if cs["class_n_resolved"] else None
    checks = []

    # 1. collector backfill: many old observations ingested in one batch at detection time
    series = tools.get_series(feed_id, obs_t - 3 * 86400, obs_t + HOUR)
    if series:
        same_batch = [r for r in series if abs(r["ingested_at"] - a["detected_at"]) < HOUR
                      and r["observed_at"] < obs_t - 2 * HOUR]
        checks.append({"explanation": "Collector backfill or gap",
                       "how_checked": "counted older observations ingested in the same batch",
                       "result": "explains" if len(same_batch) >= 5 else "ruled_out",
                       "evidence": []})
    # 2. coverage starvation
    if a.get("coverage") is not None and a["baseline"]:
        cov_norm = None
        covs = [r["coverage"] for r in series if r["coverage"] is not None]
        if covs:
            covs.sort()
            cov_norm = covs[len(covs) // 2]
        starved = a["coverage"] is not None and cov_norm and a["coverage"] < 0.5 * cov_norm
        checks.append({"explanation": "Coverage drop (feeder outage)",
                       "how_checked": "compared coverage to the bucket median",
                       "result": "explains" if starved else "ruled_out",
                       "evidence": []})
    # 3. calendar: weekend/holiday behavior
    how = a["hour_of_week"]
    day = how // 24
    checks.append({"explanation": "Weekend or holiday calendar effect",
                   "how_checked": "checked day-of-week of the reading",
                   "result": "explains" if day >= 5 else "ruled_out", "evidence": []})
    while len(checks) < 3:
        checks.append({"explanation": "Ordinary variance in a feed that fires often",
                       "how_checked": f"feed fired {fr['fired_365d']} times in 365d",
                       "result": "ruled_out", "evidence": []})

    def out(verdict, mech=None, hyps=None, origins=None, discarded=None, pred=None, summary=""):
        return {"anomaly_id": anomaly_id, "verdict": verdict,
                "base_rate": {"fired_365d": fr["fired_365d"],
                              "class_hit_rate": base_rate if base_rate is not None else 0.0,
                              "class_n_resolved": cs["class_n_resolved"]},
                "mundane_checks": checks, "mechanism": mech, "hypotheses": hyps or [],
                "independent_origins": origins or [], "dependent_items_discarded": discarded or [],
                "n_independent": len(origins or []), "prediction": pred, "summary": summary}

    if any(c["result"] == "explains" for c in checks):
        return json.dumps(out("noise", summary="A mundane explanation fully accounts for this reading: "
                              + next(c["explanation"] for c in checks if c["result"] == "explains")
                              + ". No signal."))

    # corroborating events near the anomaly, grouped by origin
    evs = tools.search_events(since=obs_t - 48 * HOUR, until=obs_t + HOUR)
    supporting = [e for e in evs if e["event_type"] in ("notice", "news_post")]
    reposts = [e for e in evs if e["event_type"] in ("social_post", "blog_post")]
    by_origin: dict[str, list[int]] = {}
    for e in supporting:
        by_origin.setdefault(e["origin_key"] or e["source"], []).append(e["event_id"])
    independent = [{"origin": o, "source": "events", "event_ids": ids} for o, ids in by_origin.items()]
    feed_origin = {"origin": f"feed:{feed_id}", "source": a["source"], "event_ids": []}

    if len(independent) >= 2:
        origins = [feed_origin] + independent
        p = 0.55
        n_further = 1
        claim = (f"At least {n_further} further notice event(s) for {a.get('entity') or 'the entity'} "
                 f"land within 72h of this {a['label']} change, extending the restriction")
        # the triggering anomaly counts as one origin (its own feed), so it goes
        # into evidence and v_independent_support agrees with n_independent
        pred = {"signal_class": a["signal_class"], "claim": claim, "probability": p,
                "resolve_by": dt.datetime.fromtimestamp(now + 72 * HOUR, dt.timezone.utc)
                              .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "falsifier": "No further notice by then means the restriction was not extended",
                "resolution_rule": {"type": "count_events", "entity": a.get("entity"),
                                    "event_type": "notice", "min_count": n_further, "window_hours": 72},
                "evidence": [{"anomaly_id": anomaly_id, "role": "supports"}]
                         + [{"event_id": i, "role": "supports"}
                            for o in independent for i in o["event_ids"]]}
        return json.dumps(out(
            "call",
            mech=f"An independent real-world cause ({independent[0]['origin']}) moves both "
                 f"the event feed and the metric feed.",
            hyps=[{"id": "H1", "statement": claim,
                   "contradicting_evidence_searched": "searched events contradicting the mechanism",
                   "contradicting_evidence_found": []}],
            origins=origins,
            discarded=({"event_ids": [e["event_id"] for e in reposts],
                        "reason": "reposts share one origin"} if reposts else None),
            pred=pred,
            summary=f"{len(independent)} independent origins posted hours before the reading moved, "
                    f"and the mechanism is stated. Probability held near the class base rate."))

    if abs(a["z"]) >= 6:
        return json.dumps(out("unexplained",
                              summary=f"{a['label']} moved to z={a['z']:+.1f} at "
                                      f"{dt.datetime.fromtimestamp(obs_t, dt.timezone.utc).strftime('%a %H:%M')} UTC "
                                      f"and survives the mundane checks, but no testable claim with a "
                                      f"mechanism is available. Human attention."))
    return json.dumps(out("noise", summary=f"{a['label']} at z={a['z']:+.1f}: no mechanism connects this "
                                          f"feed to a real-world cause; ordinary variance is the best "
                                          f"explanation."))


# ---------------------------------------------------------------- validation

def parse_json(text: str) -> dict | None:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # tolerate prose fences around the JSON
    if "```" in text:
        for part in text.split("```"):
            p = part.strip().removeprefix("json").strip()
            if p.startswith("{"):
                try:
                    return json.loads(p)
                except json.JSONDecodeError:
                    continue
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def validate(out: dict, anomaly: dict, tools: Tools, now: int) -> list[str]:
    """The rules the prompt marks '(validator)' plus wiring rules, in code."""
    errs: list[str] = []

    def err(m):
        errs.append(m)

    if out.get("anomaly_id") != anomaly["anomaly_id"]:
        err("anomaly_id must be the anomaly under triage")
    verdict = out.get("verdict")
    if verdict not in ("noise", "unexplained", "call"):
        err(f"bad verdict {verdict!r}")
        return errs

    checks = out.get("mundane_checks") or []
    if len(checks) < 3:
        err("at least 3 mundane_checks are required before any hypothesis")
    for i, c in enumerate(checks):
        if not isinstance(c, dict) or c.get("result") not in ("explains", "partly", "ruled_out", "unresolved"):
            err(f"mundane_check[{i}].result must be explains|partly|ruled_out|unresolved")
        if not (c.get("explanation") and c.get("how_checked")):
            err(f"mundane_check[{i}] needs explanation and how_checked")

    origins = out.get("independent_origins") or []
    if out.get("n_independent") != len(origins):
        err("n_independent must equal the length of independent_origins")

    pred = out.get("prediction")
    if verdict in ("noise", "unexplained"):
        if pred is not None:
            err(f"{verdict} must carry prediction: null")
        if verdict == "unexplained" and out.get("hypotheses"):
            err("unexplained carries no hypotheses")
    else:
        if not out.get("hypotheses"):
            err("call needs at least one hypothesis")
        if not isinstance(pred, dict):
            err("call needs exactly one prediction")
            return errs
        p = pred.get("probability")
        if not isinstance(p, (int, float)) or not (0.02 <= p <= 0.95):
            err("probability must be within 0.02..0.95")
        base = tools.class_stats(pred.get("signal_class", ""))
        base_rate = base["class_hit_rate"] if base["class_n_resolved"] else None
        n_ind = out.get("n_independent") or 0
        if isinstance(p, (int, float)):
            if n_ind <= 0 and base_rate is not None and p > base_rate + 1e-9:
                err("with n_independent 0, probability must be the class base rate or lower")
            if n_ind == 1 and base_rate is not None and abs(p - base_rate) > 0.15 + 1e-9:
                err("with n_independent 1, probability must stay within 0.15 of the class base rate")
        rb = pred.get("resolve_by")
        try:
            t_rb = int(dt.datetime.fromisoformat(str(rb).replace("Z", "+00:00")).timestamp())
            if not (now + 24 * HOUR <= t_rb <= now + 30 * 86400):
                err("resolve_by must be 24 hours to 30 days from now")
        except (ValueError, TypeError):
            err("resolve_by must be ISO 8601 UTC, e.g. 2026-11-17T00:00:00Z")
        if not pred.get("falsifier"):
            err("prediction needs a falsifier")
        rule = pred.get("resolution_rule")
        if not (isinstance(rule, dict) and rule.get("type") in RESOLVER_TYPES):
            err(f"resolution_rule.type must be one of {RESOLVER_TYPES}")
        evidence = pred.get("evidence") or []
        if not any(e.get("role") == "supports" for e in evidence):
            err("prediction needs at least one evidence entry with role=supports")
        # cited ids must exist and be visible as of detected_at
        for e in evidence:
            if "event_id" in e and e["event_id"] is not None:
                r = tools.con.execute("SELECT ingested_at FROM events WHERE event_id = ?",
                                      (e["event_id"],)).fetchone()
                if not r or r["ingested_at"] > tools.cutoff:
                    err(f"evidence event {e['event_id']} does not exist or is not visible as of detected_at")
            elif "anomaly_id" in e and e["anomaly_id"] is not None:
                r = tools.con.execute("SELECT detected_at FROM anomalies WHERE anomaly_id = ?",
                                      (e["anomaly_id"],)).fetchone()
                if not r or r["detected_at"] > tools.cutoff:
                    err(f"evidence anomaly {e['anomaly_id']} does not exist or is not visible as of detected_at")
            else:
                err("evidence entries need event_id or anomaly_id")
    for o in origins:
        if not (o.get("origin") and o.get("source") is not None and isinstance(o.get("event_ids"), list)):
            err("each independent origin needs origin, source and event_ids")
    return errs


# ---------------------------------------------------------------- driver

def computed_independence(con: sqlite3.Connection, evidence: list) -> int:
    """Same logic as v_independent_support, evaluated BEFORE the insert:
    predictions are immutable, so `alerted` is decided once, in code."""
    origins: set[str] = set()
    for e in evidence or []:
        if e.get("role") != "supports":
            continue
        if e.get("event_id") is not None:
            r = con.execute("SELECT e.origin_key, s.independence_group FROM events e "
                            "JOIN sources s USING (source_id) WHERE e.event_id = ?",
                            (e["event_id"],)).fetchone()
            if r:
                origins.add(r["origin_key"] or r["independence_group"])
        elif e.get("anomaly_id") is not None:
            r = con.execute("SELECT s.independence_group FROM anomalies a JOIN feeds f USING (feed_id) "
                            "JOIN sources s USING (source_id) WHERE a.anomaly_id = ?",
                            (e["anomaly_id"],)).fetchone()
            if r:
                origins.add(r["independence_group"])
    return len(origins)


def triage_anomaly(con: sqlite3.Connection, client, anomaly_id: int, prompt: str, version: str) -> dict | None:
    row = con.execute("SELECT * FROM anomalies WHERE anomaly_id = ?", (anomaly_id,)).fetchone()
    if not row:
        print(f"anomaly {anomaly_id}: not found", file=sys.stderr)
        return None
    anomaly = dict(row)
    tools = Tools(con, anomaly["detected_at"])
    # Time anchors to detection: live, an anomaly is triaged moments after it
    # fires, and replaying history must not leak 'wall now' into made_at or
    # resolve_by. resolve windows stay relative to when the anomaly happened.
    now = min(dbmod.now(), anomaly["detected_at"])
    user_msg = (f"Anomaly {anomaly_id} is ready for triage. Current time: "
                f"{dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}. "
                f"Use the tools, follow the procedure in order, and return only the JSON.")

    out, prev_errs = None, None
    for _attempt in (1, 2):  # one retry on failed validation
        msg = user_msg
        if prev_errs:
            msg += "\n\nYour previous output failed validation:\n- " + "\n- ".join(prev_errs) \
                   + "\nReturn corrected JSON only."
        text = (mock_run(tools, anomaly_id, now) if client.name == "mock"
                else client.run(prompt, msg, tools))
        out = parse_json(text)
        errs = ["output was not valid JSON"] if out is None else validate(out, anomaly, tools, now)
        if not errs:
            break
        prev_errs = errs
        print(f"anomaly {anomaly_id}: {_attempt} invalid: {'; '.join(errs)}", file=sys.stderr)
    else:
        out = {"verdict": "invalid"}

    verdict = out.get("verdict", "invalid")
    con.execute("INSERT OR REPLACE INTO triage_runs (anomaly_id, verdict, summary, payload, model, "
                "prompt_version, run_at) VALUES (?,?,?,?,?,?,?)",
                (anomaly_id, verdict, out.get("summary"), json.dumps(out, default=str),
                 client.name, version, now))

    pred_id = None
    if verdict == "call":
        if con.execute("SELECT 1 FROM predictions WHERE anomaly_id = ?", (anomaly_id,)).fetchone():
            # the ledger is immutable and holds ONE prediction per anomaly: a
            # re-triage (or an overlapping cron run) keeps the original call
            print(f"anomaly {anomaly_id}: re-triaged into a call; keeping the original "
                  f"prediction (ledger is append-only)", file=sys.stderr)
        else:
            pred = out["prediction"]
            p = float(pred["probability"])
            t_rb = int(dt.datetime.fromisoformat(pred["resolve_by"].replace("Z", "+00:00")).timestamp())
            n_ind = computed_independence(con, pred.get("evidence"))
            alerted = 1 if scorer.may_alert(con, pred["signal_class"], n_ind, p) else 0
            try:
                cur = con.execute(
                    "INSERT INTO predictions (anomaly_id, signal_class, made_at, claim, probability, "
                    "resolve_by, falsifier, resolution_rule, model, prompt_version, alerted) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (anomaly_id, pred["signal_class"], now, pred["claim"], p, t_rb, pred["falsifier"],
                     json.dumps(pred.get("resolution_rule")), client.name, version, alerted))
            except sqlite3.IntegrityError:
                # lost a race with a concurrent triage process on the unique index
                pred_id = None
                print(f"anomaly {anomaly_id}: a concurrent triage logged this prediction first; "
                      f"keeping theirs", file=sys.stderr)
            else:
                pred_id = cur.lastrowid
                for e in pred.get("evidence", []):
                    con.execute("INSERT INTO prediction_evidence (pred_id, event_id, anomaly_id, role) "
                                "VALUES (?,?,?,?)",
                                (pred_id, e.get("event_id"), e.get("anomaly_id"), e.get("role", "supports")))
            if pred_id:
                # cross-check the claimed count against the view; code, not trust
                v = con.execute("SELECT n_independent FROM v_independent_support WHERE pred_id = ?",
                                (pred_id,)).fetchone()
                n_view = v["n_independent"] if v else 0
                if (out.get("n_independent") or 0) > n_view:
                    con.execute("INSERT INTO prompt_miscounts (prompt_version, anomaly_id, claimed, computed, at) "
                                "VALUES (?,?,?,?,?)", (version, anomaly_id, out.get("n_independent"), n_view, now))
    con.commit()
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db")
    ap.add_argument("--llm", choices=("mock", "anthropic", "openai"), default="mock")
    ap.add_argument("--model", help="override TRIAGE_MODEL")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--anomaly", type=int, help="triage one anomaly by id")
    args = ap.parse_args(argv)

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(DDL)
    con.executescript(scorer.DDL)  # may_alert reads signal_status
    dbmod.ensure_one_prediction_per_anomaly(con)  # unique index; tolerant of old dupes
    prompt, version = load_prompt()

    if args.anomaly:
        ids = [args.anomaly]
    else:
        # dbmod.now() honors MON_FAKE_NOW; wall clock here would break replays
        ids = [r["anomaly_id"] for r in con.execute(
            "SELECT a.anomaly_id FROM anomalies a LEFT JOIN triage_runs t USING (anomaly_id) "
            "WHERE t.anomaly_id IS NULL AND a.detected_at <= ? "
            "ORDER BY a.detected_at LIMIT ?", (dbmod.now(), args.limit))]
    if not ids:
        print("nothing pending triage")
        return

    client = {"mock": lambda: type("M", (), {"name": "mock"})(),
              "anthropic": lambda: AnthropicClient(args.model),
              "openai": lambda: OpenAIClient(args.model)}[args.llm]()
    for aid in ids:
        out = triage_anomaly(con, client, aid, prompt, version)
        if out:
            print(f"anomaly {aid}: {out.get('verdict')}"
                  + (f" p={out['prediction']['probability']}" if out.get("prediction") else ""))


if __name__ == "__main__":
    main()

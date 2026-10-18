#!/usr/bin/env python3
"""
common.py: registry helpers + polite HTTP + append-only inserts.

Every collector is a dumb pipe: fetch, parse, INSERT. No decisions.
Rate-limit etiquette per Plan/dumbcollectors.md: a declared User-Agent
(set MON_USER_AGENT with your contact), >= 1s between requests to the same
host, small timeouts, and idempotent re-runs.
"""
import hashlib
import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys_path = Path(__file__).resolve().parent.parent  # repo root
import sys
sys.path.insert(0, str(sys_path))

USER_AGENT = os.environ.get(
    "MON_USER_AGENT", "osint-early-warning/0.1 (personal research; set MON_USER_AGENT)")
_last_hit: dict[str, float] = {}


def polite_get(url: str, timeout: int = 15, min_interval: float = 1.0) -> bytes:
    """GET with a declared UA and >= min_interval seconds between hits per host."""
    host = urllib.parse.urlsplit(url).netloc
    wait = _last_hit.get(host, 0) + min_interval - time.time()
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                _last_hit[host] = time.time()
                return r.read()
        except urllib.error.HTTPError as e:
            _last_hit[host] = time.time()
            if e.code in (429, 503) and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            raise
    raise last


def get_json(url: str, timeout: int = 15) -> dict | list:
    return json.loads(polite_get(url, timeout))


# ---------------------------------------------------------------- registry

def ensure_source(con: sqlite3.Connection, name: str, kind: str, group: str, terms: str = "") -> int:
    r = con.execute("SELECT source_id FROM sources WHERE name = ?", (name,)).fetchone()
    if r:
        return r["source_id"]
    cur = con.execute("INSERT INTO sources (name, kind, independence_group, terms_note) VALUES (?,?,?,?)",
                      (name, kind, group, terms))
    return cur.lastrowid


def ensure_entity(con: sqlite3.Connection, kind: str, name: str, ext_ids: dict | None = None) -> int:
    r = con.execute("SELECT entity_id FROM entities WHERE kind = ? AND name = ?", (kind, name)).fetchone()
    if r:
        return r["entity_id"]
    cur = con.execute("INSERT INTO entities (kind, name, ext_ids) VALUES (?,?,?)",
                      (kind, name, json.dumps(ext_ids) if ext_ids else None))
    return cur.lastrowid


def ensure_feed(con: sqlite3.Connection, source_id: int, entity_id: int | None, metric: str,
                cadence_s: int, label: str, signal_class: str) -> int:
    r = con.execute("SELECT feed_id FROM feeds WHERE source_id = ? AND entity_id IS ? AND metric = ?",
                    (source_id, entity_id, metric)).fetchone()
    if r:
        return r["feed_id"]
    cur = con.execute("INSERT INTO feeds (source_id, entity_id, metric, cadence_s, label, signal_class) "
                      "VALUES (?,?,?,?,?,?)", (source_id, entity_id, metric, cadence_s, label, signal_class))
    return cur.lastrowid


# ---------------------------------------------------------------- inserts

def put_observation(con: sqlite3.Connection, feed_id: int, observed_at: int, value: float,
                    coverage: float | None = None, raw_ref: str | None = None,
                    ingested_at: int | None = None) -> bool:
    """Append one reading at first-reported vintage. Re-runs are no-ops
    (same PK); late backfill gets a fresh ingested_at, which is the point.
    ingested_at override is for synthetic replay only."""
    cur = con.execute(
        "INSERT OR IGNORE INTO observations (feed_id, observed_at, ingested_at, revision, value, coverage, raw_ref) "
        "VALUES (?,?,?,0,?,?,?)",
        (feed_id, observed_at, int(ingested_at or time.time()), value, coverage, raw_ref))
    return cur.rowcount > 0


def put_event(con: sqlite3.Connection, source_id: int, event_type: str, occurred_at: int,
              title: str | None, origin_key: str | None, payload: dict,
              entity_id: int | None = None, magnitude: float | None = None,
              ingested_at: int | None = None) -> tuple[int | None, bool]:
    """Insert a discrete event, deduped by (source, content_hash). Returns
    (event_id, created?). origin_key is the canonical upstream id so reposts
    across sources can be recognized as dependent later. ingested_at override
    is for synthetic replay only."""
    body = json.dumps(payload, sort_keys=True)
    chash = hashlib.sha256(body.encode()).hexdigest()
    r = con.execute("SELECT event_id FROM events WHERE source_id = ? AND content_hash = ?",
                    (source_id, chash)).fetchone()
    if r:
        return r["event_id"], False
    cur = con.execute(
        "INSERT OR IGNORE INTO events (source_id, entity_id, event_type, occurred_at, ingested_at, title, "
        "magnitude, origin_key, content_hash, payload) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (source_id, entity_id, event_type, occurred_at, int(ingested_at or time.time()), title, magnitude,
         origin_key, chash, body))
    return cur.lastrowid, cur.rowcount > 0

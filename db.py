#!/usr/bin/env python3
"""
db.py: open events.db / truth.db, apply schemas, run migrations.

    from db import open_events, open_truth

Events and truth live in SEPARATE files. The triage agent's process opens only
events.db; truth.db (which marks placebo feeds) is opened by the scorer, the
placebo generator and the digest. That separation is the blind trial.
"""
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCHEMA = ROOT / "SQLSchema" / "schema.sql"
TRUTH = ROOT / "SQLSchema" / "truth.sql"


def now() -> int:
    """Pipeline clock. MON_FAKE_NOW lets the demo and backtests fast-forward
    time without touching wall clocks."""
    import os
    return int(os.environ.get("MON_FAKE_NOW") or time.time())


def events_path() -> Path:
    import os
    return Path(os.environ.get("EVENTS_DB", ROOT / "events.db"))


def truth_path() -> Path:
    import os
    return Path(os.environ.get("TRUTH_DB", ROOT / "truth.db"))


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA busy_timeout = 5000")
    return con


def _columns(con: sqlite3.Connection, table: str) -> set:
    return {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}


# One prediction per anomaly: the ledger is append-only, so a re-triage or two
# overlapping triage runs must not double-count an anomaly's call. Duplicate
# rows from before this invariant existed block the index; triage's insertion
# guard still prevents new ones.
ONE_PREDICTION_PER_ANOMALY = (
    "CREATE UNIQUE INDEX IF NOT EXISTS predictions_one_per_anomaly "
    "ON predictions (anomaly_id) WHERE anomaly_id IS NOT NULL")

# v_firing_rate anchored on wall-clock now(); SQL cannot see MON_FAKE_NOW, so
# replays and backtests would silently get different numbers than live runs.
# Anchor the 365d window to the newest detection in the store instead.
V_FIRING_RATE = """
CREATE VIEW IF NOT EXISTS v_firing_rate AS
SELECT f.feed_id, f.label, f.signal_class, COUNT(a.anomaly_id) AS fired_365d
FROM feeds f
LEFT JOIN anomalies a
  ON a.feed_id = f.feed_id
 AND a.detected_at > (SELECT MAX(detected_at) FROM anomalies) - 365 * 86400
GROUP BY f.feed_id
"""


def ensure_one_prediction_per_anomaly(con: sqlite3.Connection) -> None:
    if not _columns(con, "predictions"):  # core schema not applied yet; nothing to guard
        return
    try:
        con.execute(ONE_PREDICTION_PER_ANOMALY)
    except sqlite3.IntegrityError:
        print("warning: events.db holds duplicate predictions for one anomaly; "
              "unique index skipped, triage's re-triage guard still applies", file=sys.stderr)


def _refresh_firing_rate_view(con: sqlite3.Connection) -> None:
    con.execute("DROP VIEW IF EXISTS v_firing_rate")  # IF NOT EXISTS alone can't upgrade old DBs
    con.execute(V_FIRING_RATE)


def migrate(con: sqlite3.Connection) -> None:
    """Idempotent migrations for databases created before a column existed.
    triageprompt.md wiring note: predictions get a machine-readable resolution_rule."""
    cols = _columns(con, "predictions")
    if cols and "resolution_rule" not in cols:
        # Append-only triggers block UPDATEs of rows, not ALTER of the table.
        con.execute("ALTER TABLE predictions ADD COLUMN resolution_rule TEXT")
    if cols:
        ensure_one_prediction_per_anomaly(con)
    if _columns(con, "feeds"):
        _refresh_firing_rate_view(con)


def open_events(path: Path | None = None, init: bool = True) -> sqlite3.Connection:
    path = Path(path) if path else events_path()
    con = _connect(path)
    if init:
        con.executescript(SCHEMA.read_text())
        migrate(con)
        con.commit()
    return con


def open_truth(path: Path | None = None, init: bool = True) -> sqlite3.Connection:
    path = Path(path) if path else truth_path()
    con = _connect(path)
    if init:
        con.executescript(TRUTH.read_text())
        con.commit()
    return con


def attach_truth(events_con: sqlite3.Connection, truth_path: Path | None = None) -> None:
    """For scorer/digest/placebo only. NEVER call this in the triage process."""
    events_con.execute("ATTACH DATABASE ? AS t", (str(truth_path or truth_path()),))

#!/usr/bin/env python3
"""
db.py: open events.db / truth.db, apply schemas, run migrations.

    from db import open_events, open_truth

Events and truth live in SEPARATE files. The triage agent's process opens only
events.db; truth.db (which marks placebo feeds) is opened by the scorer, the
placebo generator and the digest. That separation is the blind trial.
"""
import sqlite3
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


def migrate(con: sqlite3.Connection) -> None:
    """Idempotent migrations for databases created before a column existed.
    triageprompt.md wiring note: predictions get a machine-readable resolution_rule."""
    cols = _columns(con, "predictions")
    if cols and "resolution_rule" not in cols:
            # Append-only triggers block UPDATEs of rows, not ALTER of the table.
            con.execute("ALTER TABLE predictions ADD COLUMN resolution_rule TEXT")


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

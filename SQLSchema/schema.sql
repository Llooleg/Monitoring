-- events.db: event store + ledger for the agentic OSINT pipeline.
-- SQLite >= 3.37 (STRICT tables). Ports to DuckDB with minor edits.
--
-- Rules the schema enforces or assumes:
--   1. Append-only. Corrections are new rows, never UPDATEs.
--   2. Every row carries when it happened AND when we learned it (ingested_at).
--      Baselines and backtests filter on ingested_at <= :as_of, or you get lookahead.
--   3. The agent never sees whether a feed is a placebo. That lives in truth.db.
--   4. Predictions are immutable. A ledger you can edit is a diary.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- ============================================================ registry

CREATE TABLE IF NOT EXISTS sources (
  source_id          INTEGER PRIMARY KEY,
  name               TEXT NOT NULL UNIQUE,   -- 'adsb.fi', 'sec.edgar', 'ripe.ris-live'
  kind               TEXT NOT NULL,          -- 'adsb' | 'bgp' | 'filing' | 'notice' | 'social' | 'web'
  independence_group TEXT NOT NULL,          -- sources sharing upstream data share a group
  terms_note         TEXT                    -- license, rate limit, non-commercial flag
) STRICT;

CREATE TABLE IF NOT EXISTS entities (
  entity_id INTEGER PRIMARY KEY,
  kind      TEXT NOT NULL,                   -- 'region' | 'asn' | 'company' | 'ticker' | 'us_state' | 'account'
  name      TEXT NOT NULL,
  ext_ids   TEXT CHECK (ext_ids IS NULL OR json_valid(ext_ids)),  -- {"cik":"...","asn":13335}
  UNIQUE (kind, name)
) STRICT;

-- One numeric stream = source x entity x metric.
CREATE TABLE IF NOT EXISTS feeds (
  feed_id      INTEGER PRIMARY KEY,
  source_id    INTEGER NOT NULL REFERENCES sources,
  entity_id    INTEGER REFERENCES entities,
  metric       TEXT NOT NULL,                -- 'aircraft_count', 'updates_per_min', 'warn_workers'
  cadence_s    INTEGER NOT NULL,
  label        TEXT NOT NULL,                -- agent-visible. Neutral. Never 'placebo_anything'.
  signal_class TEXT NOT NULL,                -- 'layoffs.us', 'bgp.routing', 'lab.incident'
  active       INTEGER NOT NULL DEFAULT 1,
  UNIQUE (source_id, entity_id, metric)
) STRICT;

-- ============================================================ raw data

-- Numeric time series (counts, rates, prices).
CREATE TABLE IF NOT EXISTS observations (
  feed_id     INTEGER NOT NULL REFERENCES feeds,
  observed_at INTEGER NOT NULL,              -- unix s, UTC: when the measurement applies
  ingested_at INTEGER NOT NULL,              -- when WE learned it
  revision    INTEGER NOT NULL DEFAULT 0,    -- a correction is revision+1, never an UPDATE
  value       REAL NOT NULL,
  coverage    REAL,                          -- e.g. active receivers; a feeder going offline is not news
  raw_ref     TEXT,                          -- hash/path of the stored raw payload
  how         INTEGER GENERATED ALWAYS AS
                ((((observed_at / 86400) + 3) % 7) * 24 + ((observed_at / 3600) % 24)) VIRTUAL,
                                             -- hour-of-week, Mon 00:00 UTC = 0. Baseline seasonality key.
  PRIMARY KEY (feed_id, observed_at, revision)
) STRICT, WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS obs_ingested ON observations (ingested_at);

-- Discrete things: a filing, a WARN notice, a hijack alert, a lab post, a tweet.
CREATE TABLE IF NOT EXISTS events (
  event_id     INTEGER PRIMARY KEY,
  source_id    INTEGER NOT NULL REFERENCES sources,
  entity_id    INTEGER REFERENCES entities,
  event_type   TEXT NOT NULL,                -- '8-K', 'warn_notice', 'bgp_hijack', 'lab_post', 'social_post'
  occurred_at  INTEGER NOT NULL,
  ingested_at  INTEGER NOT NULL,
  title        TEXT,
  magnitude    REAL,                         -- workers affected, prefixes hijacked, ...
  origin_key   TEXT,                         -- canonical upstream id (original URL, accession no.).
                                             -- Ten reposts of one 480p image share it.
  content_hash TEXT NOT NULL,                -- dedupe
  payload      TEXT NOT NULL CHECK (json_valid(payload)),
  UNIQUE (source_id, content_hash)
) STRICT;
CREATE INDEX IF NOT EXISTS ev_entity_time ON events (entity_id, occurred_at);

-- ============================================================ derived (rebuildable)

-- Median/MAD per feed per hour-of-week, frozen at an as_of so backtests are honest.
CREATE TABLE IF NOT EXISTS baseline_stats (
  feed_id INTEGER NOT NULL REFERENCES feeds,
  how     INTEGER NOT NULL,
  as_of   INTEGER NOT NULL,
  method  TEXT NOT NULL,                     -- 'median_mad_v1'
  n       INTEGER NOT NULL,
  median  REAL NOT NULL,
  mad     REAL NOT NULL,
  PRIMARY KEY (feed_id, how, as_of, method)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS anomalies (
  anomaly_id  INTEGER PRIMARY KEY,
  feed_id     INTEGER NOT NULL REFERENCES feeds,
  observed_at INTEGER NOT NULL,
  detected_at INTEGER NOT NULL,
  score       REAL NOT NULL,                 -- robust z-score
  method      TEXT NOT NULL,
  UNIQUE (feed_id, observed_at, method)
) STRICT;

-- ============================================================ ledger

CREATE TABLE IF NOT EXISTS predictions (
  pred_id        INTEGER PRIMARY KEY,
  anomaly_id     INTEGER REFERENCES anomalies,
  signal_class   TEXT NOT NULL,
  made_at        INTEGER NOT NULL,
  claim          TEXT NOT NULL,              -- concrete and checkable, or it doesn't get logged
  probability    REAL NOT NULL CHECK (probability > 0 AND probability < 1),  -- no 100% prophecies
  resolve_by     INTEGER NOT NULL CHECK (resolve_by > made_at),
  falsifier      TEXT NOT NULL,              -- 'nothing by <date> => noise'
  resolution_rule TEXT CHECK (resolution_rule IS NULL OR json_valid(resolution_rule)),
                                             -- machine-readable, evaluated by resolve.py
  model          TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  alerted        INTEGER NOT NULL DEFAULT 0
) STRICT;

CREATE TABLE IF NOT EXISTS prediction_evidence (
  pred_id    INTEGER NOT NULL REFERENCES predictions,
  event_id   INTEGER REFERENCES events,
  anomaly_id INTEGER REFERENCES anomalies,
  role       TEXT NOT NULL CHECK (role IN ('supports', 'contradicts', 'mundane_explanation')),
  CHECK ((event_id IS NULL) <> (anomaly_id IS NULL))
) STRICT;

-- Resolved by rules or a human, never by the model that made the call.
CREATE TABLE IF NOT EXISTS resolutions (
  pred_id     INTEGER PRIMARY KEY REFERENCES predictions,
  resolved_at INTEGER NOT NULL,
  outcome     TEXT NOT NULL CHECK (outcome IN ('yes', 'no', 'void')),
  resolver    TEXT NOT NULL,                 -- 'rule:warn_count_gt_500' | 'human'
  note        TEXT
) STRICT;

-- ============================================================ append-only enforcement

CREATE TRIGGER IF NOT EXISTS observations_no_update BEFORE UPDATE ON observations BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS observations_no_delete BEFORE DELETE ON observations BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_update       BEFORE UPDATE ON events       BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete       BEFORE DELETE ON events       BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS predictions_no_update  BEFORE UPDATE ON predictions  BEGIN SELECT RAISE(ABORT, 'predictions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS predictions_no_delete  BEFORE DELETE ON predictions  BEGIN SELECT RAISE(ABORT, 'predictions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS resolutions_no_update  BEFORE UPDATE ON resolutions  BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS resolutions_no_delete  BEFORE DELETE ON resolutions  BEGIN SELECT RAISE(ABORT, 'append-only'); END;

-- ============================================================ views

-- How many genuinely independent origins back a prediction?
-- Unknown origin falls back to the source's independence_group, so ten posts
-- from one unattributed group count once.
CREATE VIEW IF NOT EXISTS v_independent_support AS
SELECT pred_id, COUNT(DISTINCT origin) AS n_independent
FROM (
  SELECT pe.pred_id, COALESCE(e.origin_key, s.independence_group) AS origin
  FROM prediction_evidence pe
  JOIN events  e ON e.event_id  = pe.event_id
  JOIN sources s ON s.source_id = e.source_id
  WHERE pe.role = 'supports'
  UNION ALL
  SELECT pe.pred_id, s.independence_group
  FROM prediction_evidence pe
  JOIN anomalies a ON a.anomaly_id = pe.anomaly_id
  JOIN feeds     f ON f.feed_id    = a.feed_id
  JOIN sources   s ON s.source_id  = f.source_id
  WHERE pe.role = 'supports'
)
GROUP BY pred_id;

-- Track record per signal class. Brier 0.25 = always saying 50%. Lower is better.
-- Auto-mute anything that can't beat the base rate.
CREATE VIEW IF NOT EXISTS v_brier_by_signal AS
SELECT p.signal_class,
       COUNT(*)                                        AS n_resolved,
       AVG(p.probability)                              AS mean_p,
       AVG(r.outcome = 'yes')                          AS hit_rate,
       AVG((p.probability - (r.outcome = 'yes')) *
           (p.probability - (r.outcome = 'yes')))      AS brier
FROM predictions p
JOIN resolutions r USING (pred_id)
WHERE r.outcome <> 'void'
GROUP BY p.signal_class;

-- "G550s circling fires 40 times a year" lives here. The window anchors to
-- the newest detection in the store, not wall clock: SQL cannot see
-- MON_FAKE_NOW, so replays must not silently get different numbers.
CREATE VIEW IF NOT EXISTS v_firing_rate AS
SELECT f.feed_id, f.label, f.signal_class, COUNT(a.anomaly_id) AS fired_365d
FROM feeds f
LEFT JOIN anomalies a
  ON a.feed_id = f.feed_id
 AND a.detected_at > (SELECT MAX(detected_at) FROM anomalies) - 365 * 86400
GROUP BY f.feed_id;

-- truth.db: a SEPARATE file from events.db. Only the scorer process opens it.
-- The triage agent's process never attaches it, so it cannot learn which feeds are placebos.

CREATE TABLE IF NOT EXISTS feed_truth (
  feed_id              INTEGER PRIMARY KEY,   -- matches events.db feeds.feed_id
  is_placebo           INTEGER NOT NULL CHECK (is_placebo IN (0, 1)),
  placebo_kind         TEXT CHECK (placebo_kind IN ('synthetic', 'irrelevant_real', 'time_shifted')),
  derived_from_feed_id INTEGER,               -- for time_shifted: the real feed it copies
  shift_s              INTEGER,               -- the shift applied (>= 2 weeks keeps seasonality)
  CHECK (is_placebo = 1 OR placebo_kind IS NULL)
) STRICT;

-- Scorer-side query: how often does the pipeline escalate noise?
-- Compare the placebo row to the real row. Real signals must beat it by a clear margin.
--
--   ATTACH 'events.db' AS ev;
--   SELECT t.is_placebo,
--          COUNT(*)                          AS predictions,
--          SUM(p.alerted)                    AS alerts,
--          ROUND(1.0 * SUM(p.alerted) / COUNT(*), 3) AS alert_rate
--   FROM ev.predictions p
--   JOIN ev.anomalies  a ON a.anomaly_id = p.anomaly_id
--   JOIN feed_truth    t ON t.feed_id    = a.feed_id
--   GROUP BY t.is_placebo;

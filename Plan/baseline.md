baseline.py is built and tested. The core idea is to judge each reading against readings from the same hour of the week (±1 hour) over the last 8 weeks, using median and MAD so past anomalies don’t pollute the baseline. Monday 9am gets compared to other Monday mornings, not to Sunday 3am.
A few details make it work on real feeds:
Thin buckets fall back. One sample per week is too few, so below 12 samples it widens to same-hour-of-day and weekday/weekend, then to everything. Daily feeds like WARN notices live on that fallback.
Scale floors. Count feeds get a Poisson floor, because MAD = 0 on a mostly-zero feed would turn every 1 into a crisis.
Coverage is handled. Values are divided by coverage, and readings with under half the normal coverage are skipped, not scored.
Debounce. After an anomaly, the feed is quiet for 6 hours unless the z-score grows by 1.5x, so one outage doesn’t become 60 triage calls.
No lookahead. Baselines use only rows ingested by the target’s own ingested_at, at the as-first-reported vintage.
On 12 weeks of synthetic data with injected events, the seasonal baseline caught 9 of 10 spikes at z ≥ 4, with no false flags other than the holiday. A single global z-score caught 2 of 20 injected events, which is why seasonality matters. Coverage gating skipped a 6-hour feeder outage, and with coverage ignored it produced 2 false flags.
The weak spots:
Drops are hard. I caught only 3 of 10 injected 60% drops, because a count can’t fall far below a low overnight baseline. Don’t expect miracles on quiet hours.
Holidays fool it. A Wednesday that behaved like a Sunday produced 2-3 flags. The baseline can’t know about holidays, so the triage agent’s calendar check has to dismiss them, or you add a holiday calendar later.
Low-count feeds have low power. A 4x spike on a weekend feed averaging 2 per day only reaches z ≈ 3.4. A level shift also trips it until the 8-week window catches up.
This was synthetic data with well-behaved noise, so real feeds will be messier. The defaults (8 weeks, 12 samples, z = 5, 6-hour cooldown) are guesses. My backtest’s “flags per year” figures are inflated by the injected events, so on real data run --backtest ... --sweep and pick the z that gives roughly one flag a week per feed. The script adds one small table, score_cursor, so it never rescans or skips observations.


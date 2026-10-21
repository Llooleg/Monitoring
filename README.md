# Agentic OSINT early-warning pipeline

Anomaly detection over open data feeds (ADS-B, BGP, SEC filings, ...), an LLM
triage agent that judges what anomalies mean, an immutable prediction ledger,
rule-based resolution, calibration scoring against placebo controls, and a
daily digest with rare, code-gated alerts.

The design documents in `Plan/` are the specification. This repository is the
implementation.

## What exists

| piece | file | role |
|---|---|---|
| schema | `SQLSchema/schema.sql` | append-only event store + ledger (`events.db`) |
| blind trial | `SQLSchema/truth.sql` | `truth.db`, placebo flags; only scorer/digest/placebo open it |
| collectors | `collectors/` | dumb pipes: ADS-B (adsb.lol), BGP (RIPEstat), EDGAR, synthetic |
| baseline | `MLanalysis/baseline.py` | hour-of-week median/MAD detector, tiered fallbacks, coverage gating, debounce, no-lookahead, `score_cursor` |
| placebos | `placebo.py` | time-shifted surrogates + tuned synthetic noise, neutral labels |
| triage agent | `triage.py` | 5 read-only cutoff-bound tools, prompt from `Plan/triageprompt.md`, validator + one retry, miscount logging, code-decided alerts |
| resolver | `resolve.py` | `resolution_rule` engine (`count_events`, `feed_max`), human queue |
| scorer | `MLanalysis/scorer.py` | Brier skill vs base rate, cluster bootstrap, shadow↔active state machine |
| digest | `digest.py` | alert renderer (2/day cap, class cooldown) + daily digest from SQL |
| wiring | `run.py` | subcommands; cron recipe in its docstring |
| demo | `demo.py` | whole pipeline offline on synthetic ground truth |

## Quickstart

```sh
python3 demo.py          # offline, no keys: world -> baseline -> triage -> resolve -> score -> digest
python3 -m unittest discover -s tests   # 41 tests
```

The demo builds 12 weeks of hourly data with 6 injected spikes (3 corroborated
by two independent origins), 3 drops, a holiday, a coverage outage and a
collector backfill batch, adds placebo feeds, and prints a digest whose track
record honestly includes both hits and misses.

Live use:

```sh
export MON_USER_AGENT="your-name your-contact"   # required etiquette, esp. EDGAR
python3 run.py init
python3 run.py collect                 # adsb + bgp + edgar (or --only bgp)
python3 run.py placebo                 # after >= 4 weeks of history exist
python3 run.py baseline --backtest --sweep 3.5,4,5   # pick z: ~1 flag/feed/week
python3 run.py baseline                # live scoring
python3 run.py triage --llm mock       # or anthropic / openai (keys in env)
python3 run.py resolve
python3 run.py score
python3 run.py digest --out digest.md
```

A cron wiring and the external-heartbeat rule (a missing digest means
something broke) are in `run.py`'s docstring.

Docker: `docker compose up -d --build` (see `docker-compose.yml`). Data lives
in the `monitoring-data` volume (`/data/events.db`, `/data/truth.db`), the
digest at `/data/digest.md`, and the container healthcheck fails if the digest
goes stale. The same compose file also starts a **read-only web dashboard**
(`web/serve.py`) at http://localhost:8417 — no auth, bound to localhost,
database mounted `:ro`. One-shots work too:
`docker run --rm monitoring demo --dir /data/demo`.

## Invariants worth keeping

- **Append-only.** `observations`, `events`, `predictions`, `resolutions` have
  UPDATE/DELETE triggers. Corrections are new rows; the ledger is not a diary.
  And one prediction per anomaly: a unique index plus an insertion guard mean
  a re-triage (or overlapping runs) keeps the original call, never a duplicate.
- **No lookahead.** Baselines see only rows ingested before the target row,
  at first-reported vintage (`revision = 0`). Triage tools cut off at the
  anomaly's `detected_at`. Scoring reference uses only outcomes already
  resolved when each prediction was made.
- **Blind placebos.** The triage process never attaches `truth.db`. Placebo
  feeds carry neutral labels and their own signal class; whatever the pipeline
  finds in them is a false positive by construction.
- **Code decides, models argue.** Alerts (`scorer.may_alert`: active class +
  ≥2 independent origins recomputed via `v_independent_support` + p ≥ 0.6),
  independence counts, validation, resolution. The LLM supplies judgment and
  an 80-word summary only. Overcounted origins are logged against the prompt
  version.
- **Earn the alert.** Classes start in `shadow`: logged, scored, never shown,
  until their Brier skill beats the base rate with confidence (90% CI < 0).

## Environment

`EVENTS_DB`, `TRUTH_DB` (paths), `MON_USER_AGENT` (polite UA with contact),
`MON_FAKE_NOW` (unix time; demo/backtests fast-forward the clock),
`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `TRIAGE_MODEL` (triage LLM),
`ADSB_QUERIES`, `BGP_ASN`, `EDGAR_CIKS` (collector scope).

## Status and honest limits

- The seasonal baseline reproduces the `Plan/baseline.md` backtest story:
  spikes caught, coverage outages skipped, holiday flagged (calendar effects
  are the triage agent's job to dismiss), drops hard.
- The triage agent runs with a deterministic mock offline; the Anthropic and
  OpenAI-compatible client paths are implemented but not exercised in tests.
- Collectors cover the three sources scoped in `Plan/dumbcollectors.md`;
  airplanes.live refuses unregistered consumers, so ADS-B defaults to adsb.lol.
- Real feeds need ~8 weeks of history before tier-1 baselines are populated;
  early runs are noisy by design (see `--sweep`).

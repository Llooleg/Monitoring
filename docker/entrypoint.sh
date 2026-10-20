#!/bin/sh
# Scheduler entrypoint for the monitoring pipeline.
#
#   docker run monitoring                        -> init + scheduling loop
#   docker run monitoring demo --dir /data/demo  -> any `run.py` subcommand, one-shot
#
# Cadence (the cron recipe from run.py, folded into one loop): collect +
# baseline + triage every TICK_SECONDS (default 15 min); resolve + score +
# digest at most once per 24h, gated by the mtime of /data/.last-daily. The
# digest is always written, quiet day or not -- the container healthcheck
# watches its mtime, and a watchdog OUTSIDE the box should too: a dead
# pipeline cannot report that it stopped talking (Plan/alertdigest.md).
set -u

say() { echo "[entrypoint $(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

DATA_DIR="${DATA_DIR:-/data}"
: "${TICK_SECONDS:=900}" "${TRIAGE_LLM:=mock}" "${DIGEST_OUT:=${DATA_DIR}/digest.md}"

# one-shot mode: anything other than the bare/scheduler invocation goes to run.py
if [ "$#" -gt 0 ] && [ "$1" != "scheduler" ]; then
    exec python3 run.py "$@"
fi

if [ -z "${MON_USER_AGENT:-}" ]; then
    say "WARNING: MON_USER_AGENT is unset; set it to 'name contact' (EDGAR will refuse you)"
fi

python3 run.py init || { say "init FAILED"; exit 1; }

tick() {
    say "collect";  python3 run.py collect  || say "collect stage FAILED, continuing"
    say "baseline"; python3 run.py baseline || say "baseline stage FAILED, continuing"
    say "triage (llm=${TRIAGE_LLM})"; python3 run.py triage --llm "${TRIAGE_LLM}" \
        --limit "${TRIAGE_LIMIT:-10}" || say "triage stage FAILED, continuing"
}

daily() {
    say "resolve"; python3 run.py resolve || say "resolve stage FAILED, continuing"
    say "score";   python3 run.py score   || say "score stage FAILED, continuing"
    say "digest -> ${DIGEST_OUT}"
    python3 run.py digest --out "${DIGEST_OUT}" || say "digest stage FAILED; healthcheck will notice"
    touch "${DATA_DIR}/.last-daily"
}

daily_due() { [ ! -f "${DATA_DIR}/.last-daily" ] || [ -n "$(find "${DATA_DIR}/.last-daily" -mmin +1440)" ]; }

trap 'say "stopping"; exit 0' INT TERM
say "scheduler up: tick=${TICK_SECONDS}s llm=${TRIAGE_LLM} tz=${TZ:-UTC}"

while :; do
    tick
    if daily_due; then daily; fi
    sleep "${TICK_SECONDS}" &
    wait $!
done

# Agentic OSINT early-warning pipeline, containerized.
#
# The pipeline is a cron-style scheduler, not a server: nothing listens, the
# container just keeps the cadence from run.py's docstring (collect/baseline/
# triage every tick; resolve/score/digest daily) and writes the digest where
# an external watchdog can check it.
#
#   docker build -t monitoring .
#   docker run -d --name monitoring \
#     -v monitoring-data:/data \
#     -e MON_USER_AGENT="your-name your-contact" \
#     -e TRIAGE_LLM=anthropic -e ANTHROPIC_API_KEY=sk-... \
#     monitoring
#
# One-shots work too (any `run.py` subcommand):
#   docker run --rm monitoring demo --dir /data/demo
#   docker run --rm monitoring baseline --backtest --sweep 4,5
#
# Data (events.db, truth.db, digest.md) lives in /data. Use a named volume
# (ownership is set up here); with a bind mount, chown it to UID 1000 first.
FROM python:3.13-slim

# tzdata so TZ= works for the daily digest hour; everything internal is UTC.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# schema.sql uses STRICT tables; fail the build early if the base image's
# bundled SQLite is too old (python:3.13-slim ships 3.4x, this guards drift).
RUN python3 -c "import sqlite3; assert sqlite3.sqlite_version_info >= (3, 37), sqlite3.sqlite_version"

WORKDIR /app
COPY . /app
RUN chmod +x docker/entrypoint.sh \
    && useradd -m -u 1000 mon \
    && mkdir -p /data \
    && chown -R mon:mon /app /data
USER mon

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC \
    EVENTS_DB=/data/events.db \
    TRUTH_DB=/data/truth.db \
    TICK_SECONDS=900 \
    TRIAGE_LLM=mock \
    TRIAGE_LIMIT=10 \
    DIGEST_OUT=/data/digest.md

VOLUME /data

# In-box approximation of the external-heartbeat rule: the digest must exist
# and be no older than 36h. Still run a watchdog OUTSIDE the container.
HEALTHCHECK --interval=30m --timeout=10s --start-period=15m --retries=2 \
    CMD python3 -c "import os,time,sys; p=os.environ['DIGEST_OUT']; \
sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p) < 36*3600 else 1)"

ENTRYPOINT ["/app/docker/entrypoint.sh"]

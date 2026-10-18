"""Collectors: dumb pipes that turn public endpoints into observations/events.

Each collector registers its sources/feeds idempotently and appends rows with
ingested_at = now. Collectors never decide anything; the baseline scores,
the triage agent judges. (Plan/dumbcollectors.md)
"""

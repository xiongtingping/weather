#!/bin/bash
# Backfill ECCC GEPS d2 at 12Z across the window dd.weather.gc.ca still serves.
#
# The source keeps roughly 30 days of run-day partitions, so the reachable
# window ends the day the oldest run falls off -- every day this waits is a
# training day that cannot be recovered later.  Each date is captured and
# committed on its own so an interrupted run resumes at the next date rather
# than restarting.
set -u
cd /Users/xiong/weather
OUT=output/audits/eccc_geps_12z_backfill
mkdir -p "$OUT"
LOG="$OUT/backfill.log"

for d in $(python3 -c "
from datetime import date, timedelta
start=date(2026,7,15); end=date(2026,8,10)
day=start
while day<=end:
    print(day.isoformat())
    day+=timedelta(days=1)
"); do
  f="$OUT/d2_${d}.json"
  if [ -f "$f" ]; then
    echo "$(date -u +%FT%TZ) skip-capture ${d} (json present)" >>"$LOG"
  else
    echo "$(date -u +%FT%TZ) capture ${d}" >>"$LOG"
    python3 scripts/backfill_eccc_geps_multistation_archive.py \
      --event-date "$d" --lead-days 2 --run-cycle-hour 12 \
      --output "$f" --allow-current-registry-scope \
      >>"$LOG" 2>&1 || { echo "$(date -u +%FT%TZ) CAPTURE_FAILED ${d}" >>"$LOG"; continue; }
  fi
  echo "$(date -u +%FT%TZ) commit ${d}" >>"$LOG"
  python3 scripts/backfill_eccc_geps_multistation_archive.py \
    --event-date "$d" --lead-days 2 --run-cycle-hour 12 \
    --input "$f" --allow-current-registry-scope --commit \
    >>"$LOG" 2>&1 || echo "$(date -u +%FT%TZ) COMMIT_FAILED ${d}" >>"$LOG"
done
echo "$(date -u +%FT%TZ) done" >>"$LOG"

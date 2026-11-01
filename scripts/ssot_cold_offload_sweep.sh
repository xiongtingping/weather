#!/bin/bash
# Charter 272: the SSOT cold offload on its own clock -- every three hours,
# 2,400 s a run.  Back from once a day (2026-11-01, user 「Back to every
# 3h」): at one 18:00 run planning took 34 of the 60 minutes and the lane
# moved 0.04-0.06 GiB a day (10-30/31) against 2.6-4.8 GiB of growth; at
# eight runs a day it had moved 1.3-2.6 GiB a day.  The file-retention job's
# own pass at 05:20/17:20 stays gone.  The history below is why it got a
# clock of its own.
#
# It ran only inside the twice-daily NWP raw retention job, one 3,600 s
# budget each, and could not keep up: each batch costs ~10 s whatever its
# size (copy to the cold tier, read-back verify, WAL wait, delete, pause),
# small-row tables move 1,000 rows (<1 MiB) a batch, and planning took 400-
# 1,100 s of each run -- on 10-15/16 every run stopped "partial" with most
# tables "budget spent, not started" while the store climbed 0.7-1 GiB a day
# into its 70 GiB ceiling.  Eight shorter runs a day give the lane several
# times the moving time it needs; the batch, lock and pause rules are the
# offload's own and unchanged, and its single-instance lock keeps this and
# the daily run from overlapping.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance ssot_cold_offload_sweep
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
COLD_TIER_ROOT="${PM_SSOT_COLD_TIER_ROOT-/Volumes/weather_data/weather_cold}"
if [ -z "$COLD_TIER_ROOT" ] || [ "${PM_SSOT_COLD_OFFLOAD_APPLY:-1}" != "1" ]; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] SSOT cold offload off"
  exit 0
fi
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] SSOT cold offload"
nice -n 10 "$PY" scripts/ssot_cold_offload.py --apply \
  --cold-tier-root "$COLD_TIER_ROOT" \
  --budget-seconds "${PM_SSOT_COLD_OFFLOAD_SWEEP_BUDGET_SEC:-2400}" \
  --receipt "$PROJECT_DIR/output/audits/ssot_cold_offload_latest.json" \
  || echo "  stopped early (moved batches are verified; the rest stay in the SSOT)"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] done"

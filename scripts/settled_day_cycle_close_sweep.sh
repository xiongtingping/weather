#!/bin/bash
# Release the capital reservations of cycles whose settlement day is over.
#
# Supersession is the only release path production reaches, and it needs a
# newer decision on the same station-day.  A closed day never gets one, so
# the last cycle of every day that traded holds its reservation for good --
# 161.32 USDC across 54 station-days as of 2026-08-26, against a Tier-1
# breach threshold of 180.00 that zeroes every station's budget at once.
#
# Idempotent and bounded: a cycle already settled writes nothing, and each
# cycle is its own short transaction, so this never holds the write lock
# across its own scan -- which would fail-close P0 and P1 for a whole
# production cycle rather than merely slow it down.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance settled_day_cycle_close_sweep
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] settled day cycle close sweep"
"$PY" scripts/settled_day_cycle_close_sweep.py

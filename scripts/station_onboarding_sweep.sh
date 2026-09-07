#!/bin/bash
# Discover newly listed markets and onboard the ones that are ready.
#
# Operator ruling 2026-09-07: "新市场自动发现、自动接入，而不是每次人工".
# Market discovery runs days ahead of onboarding, and the join between them
# used to be a person -- which is how Istanbul, Moscow and Tel Aviv sat
# listed with resolved stations and full timezones since 2026-08-22 while
# the fleet stayed at forty-five.
#
# Every stage the driver runs is idempotent, so on the ordinary day this
# does nothing and says so.  It will not clear a blocker it cannot honestly
# clear: a station with no observations at source is left in the report
# with its reason and picked up by whichever pass runs after they arrive.
#
# Daily, not hourly: markets are listed days ahead, and the stages this
# drives write the fleet's own identity rather than a row of data.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance station_onboarding
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] station onboarding"
# PM_STATION_ONBOARDING_COMMIT=1 lets the pass write.  Default is report
# only: adding a coordinate changes the fleet, which rewrites the
# content-addressed manifests several builders stamp the fleet size into,
# and that wants to land on a suite that can tell the difference.
if [ "${PM_STATION_ONBOARDING_COMMIT:-0}" = "1" ]; then
  "$PY" scripts/onboard_station.py --stage auto --commit
else
  "$PY" scripts/onboard_station.py --stage auto
fi

#!/bin/bash
# Capture JMA MSM for the one station it was admitted at.
#
# Slow by nature: a station-day is about 86 seconds of ranged reads against a
# third-party mirror, and the mirror times out often enough that the archive
# module retries with backoff.  The cadence is hourly rather than tight
# because MSM publishes eight runs a day and only two of them are captured,
# so there is rarely anything new within an hour -- and a pass with nothing
# new is a handful of indexed reads and exits immediately.
#
# Writes to the SSOT, so it takes the sweep lock like the other writers and
# holds it only around each single-row insert.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance capture_jma_msm_forward
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] jma msm forward capture"
"$PY" scripts/capture_jma_msm_forward.py

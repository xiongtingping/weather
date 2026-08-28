#!/bin/bash
# Capture HRRR and NBM daily maxima for the CONUS stations the 46-pair
# sweep admitted.
#
# Heavier per run than the MSM lane (an NBM station-day is ~50 MB of
# ranged reads, an HRRR one ~30 MB) but against S3 rather than a mirror
# that times out, so a pass of six runs finishes in a few minutes.  Hourly
# because only the 00Z and 12Z cycles are captured and the peak-coverage
# pre-filter prunes most candidates arithmetically -- a pass with nothing
# new exits on indexed reads alone.
#
# Writes to the SSOT, so it takes the sweep lock like the other writers
# and holds it only around each single-row insert.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance capture_noaa_conus_forward
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] noaa conus forward capture"
"$PY" scripts/capture_noaa_conus_forward.py

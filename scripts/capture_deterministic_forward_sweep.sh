#!/bin/bash
# Charter 201: capture the deterministic-forecast family's global members --
# AIFS Single (ECMWF open data) and the relayed IFS 9 km and UKMO 10 km runs --
# at every roster station, every current run, D-1 .. D+2.
#
# Twice per cycle against six-hourly products, and at once whenever the probe
# (probe_deterministic_runs) sees a new run published.  A pass with nothing
# new takes no write lock.  Writes the SSOT (commit before each next fetch).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance capture_deterministic_forward
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] deterministic family forward capture"
# The capture's network-lost lines are said again at the very end (user
# 2026-10-21, the red 「上次尝试 network_retry」 whose kept tail began after them
# and so never named the source that dropped).
CAPTURE_OUT="$(mktemp -t deterministic_capture.XXXXXX)"
trap 'rm -f "$CAPTURE_OUT"' EXIT
"$PY" scripts/capture_deterministic_forward.py | tee "$CAPTURE_OUT"

# Charter 267 补记八 (user 2026-10-18 「接，从现在开始采」): the same runs'
# radiation, cloud, dew point and precipitation at the same cells, for every
# roster station.  Runs after the temperature capture it plans from, in the
# seconds this sweep has left: the pass checks its budget between runs and a
# full-roster run of the slowest family (GDPS / ICON) takes ~5 minutes, so the
# last run starts at least 420 s before the hosted timeout (1800 s).
PHYSICS_BUDGET=$((1380 - SECONDS))
if [ "$PHYSICS_BUDGET" -gt 120 ]; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] deterministic physics forward capture (${PHYSICS_BUDGET}s)"
  "$PY" scripts/capture_deterministic_physics.py --max-seconds "$PHYSICS_BUDGET" || \
    echo "deterministic physics capture failed (exit $?)" >&2
else
  echo "deterministic physics: no time left in this pass (${SECONDS}s used)"
fi

# Charter 267 补记九 (round 77, user 2026-10-18 「最终链…并行写进 F 文件作影子」):
# the round-75 centre chain as of each listing instant, appended to its own
# ledger beside the F file (SSOT read-only).  Once per station-day.
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] listing centre shadow"
"$PY" scripts/record_listing_mos_shadow.py || echo "listing centre shadow failed (exit $?)" >&2

if grep -q '网络断了' "$CAPTURE_OUT"; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] 本遍断网明细（温度采集）："
  grep '网络断了' "$CAPTURE_OUT" | tail -3
fi

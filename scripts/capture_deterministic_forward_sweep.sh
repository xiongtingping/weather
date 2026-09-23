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
"$PY" scripts/capture_deterministic_forward.py

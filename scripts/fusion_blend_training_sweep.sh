#!/bin/bash
# Charter 199/242: the P0 fusion layer's training facts and station x model
# weights, on the resident's sweep clock.
#
# One row per settled station-day (and truth revision): the day-ahead P0 the
# blend would have seen, each seated system's centre error and CRPS terms
# against the settled high, and the root's own seat/shadow weights for the
# forward record.  SSOT opened mode=ro; writes only data/fusion_blend_training.db,
# append-only.  Six days back each pass, so a late truth revision is caught.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance fusion_blend_training_sweep
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] fusion blend training"
"$PY" scripts/build_fusion_blend_training.py --budget-seconds 600
# Charter 242: the station x model weights P0 serves are fitted here, after the
# facts they read, into the same file (params + forward veto, append-only).
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] station x model weights"
"$PY" scripts/fit_station_model_weights.py

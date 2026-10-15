#!/bin/bash
# Daily refit of the station board's read-only D0 card (user 2026-10-15
# 「自动化」): yesterday's closed day joins the window, every coefficient is
# refitted, and the board picks the new file up on its next read.
#
# A reader, not a writer: JAXA/GSMaP over the network (only the new day),
# Open-Meteo day-ahead hourlies for the new day (charged to the research
# lane of the quota ledger), the SSOT and archive through read-only
# connections, and files under data/runtime/d0_refit/ only.  Any failed step
# leaves the previous model in place; the receipt says which step failed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
. "$PROJECT_DIR/scripts/lib/run_with_timeout.sh"
single_instance d0_refit_daily
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
export D0_DATA_DIR="$PROJECT_DIR/data/runtime/d0_refit"
export D0_FIRST="2026-09-01"
export D0_LAST="$(date -u -v-1d +%Y-%m-%d)"
export D0_MODEL_OUT="$D0_DATA_DIR/d0_obs_model_v1.json"
mkdir -p "$D0_DATA_DIR"
LOG="$PROJECT_DIR/data/logs/d0_refit_daily.step.log"
RECEIPT="$D0_DATA_DIR/receipt.json"
JX="docs/research/jaxa_cloud_rad_p1_20261015"
D0="docs/research/d0_obs_model_20261015"
started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

receipt() {
  printf '{"schema":"d0_refit_receipt_v1","started_at_utc":"%s","finished_at_utc":"%s","window":["%s","%s"],"state":"%s","step":"%s"}\n' \
    "$started" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$D0_FIRST" "$D0_LAST" "$1" "$2" > "$RECEIPT.tmp"
  mv "$RECEIPT.tmp" "$RECEIPT"
}

step() {
  local name="$1" timeout="$2"
  shift 2
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $name"
  if ! run_with_timeout "$timeout" "$LOG" nice -n 10 "$@"; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $name failed; previous model kept" >&2
    receipt failed "$name"
    exit 1
  fi
}

echo "[$started] d0 refit through $D0_LAST"
step gsmap_extract 1200 "$PY" "$JX/gsmap_extract.py" --max-minutes 15
step jaxa_extract 1200 "$PY" "$JX/jaxa_extract.py" --max-minutes 15
step build_rows 1800 env ROWS_SET=gsmap "$PY" "$JX/build_rows.py"
step build_d0 1800 "$PY" "$D0/build_d0.py"
step fit_d0 1800 "$PY" "$D0/fit_d0.py"
step export_d0 600 "$PY" "$D0/export_d0.py"
receipt ok export_d0
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] done"

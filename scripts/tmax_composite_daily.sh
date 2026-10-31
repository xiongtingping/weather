#!/bin/bash
# Daily refresh of the station board's composite ΔT card (user 2026-10-21
# 「继续3、2、6、4、5」, items 3 and 2): the closed days join the M1 store
# (docs/research/tmax_physics_m1_ingest_20261020), every fitted version is
# scored on the days after its window (forward record), and once a week the
# card is refitted on the whole store into a new version the board reads.
#
# A reader, not a writer of anything shared: IEM, NOAA GOES and Open-Meteo
# over the network (Open-Meteo charged to the research lane of the quota
# ledger, ~200 a day), the SSOT read-only (timezones), files under
# data/tmax_physics/ and data/runtime/tmax_composite/ only.  A failed step
# keeps the previous card; the receipt names the step.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
. "$PROJECT_DIR/scripts/lib/run_with_timeout.sh"
single_instance tmax_composite_daily
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
OUT="$PROJECT_DIR/data/runtime/tmax_composite"
mkdir -p "$OUT" "$PROJECT_DIR/data/logs"
LOG="$PROJECT_DIR/data/logs/tmax_composite_daily.step.log"
RECEIPT="$OUT/receipt.json"
M1="docs/research/tmax_physics_m1_ingest_20261020"
M3="docs/research/tmax_physics_m3_eval_20261021"
started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

receipt() {
  printf '{"schema":"tmax_composite_daily_receipt_v1","started_at_utc":"%s","finished_at_utc":"%s","state":"%s","step":"%s"}\n' \
    "$started" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" > "$RECEIPT.tmp"
  mv "$RECEIPT.tmp" "$RECEIPT"
}

step() {
  local name="$1" timeout="$2"
  shift 2
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $name"
  if ! run_with_timeout "$timeout" "$LOG" nice -n 10 "$@"; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $name failed; previous card kept" >&2
    receipt failed "$name"
    exit 1
  fi
}

echo "[$started] tmax composite daily refresh"
step iem 900 "$PY" "$M1/ingest_iem.py" --recent-days 5 --budget 800
step prev1 900 "$PY" "$M1/ingest_open_meteo.py" --task prev1 --lane research --refresh-days 10 --max-weight 200 --budget 800
step sat 900 "$PY" "$M1/ingest_open_meteo.py" --task sat --lane research --refresh-days 10 --max-weight 150 --budget 800
step sarah3 900 "$PY" "$M1/ingest_open_meteo.py" --task sarah3 --lane research --refresh-days 10 --max-weight 64 --budget 800
step goes 1800 "$PY" "$M1/ingest_goes.py" --budget 1500
# features rebuilds every station-day from the 2 GB store on the spinning disk:
# 7 min by day, past 15 min at night beside WRF and the cold offload (10-30 and
# 10-31 both killed at 900 s, the card stuck on 10-25's days), so 30 min.
step features 1800 "$PY" "$M3/features.py"
step forward 900 "$PY" "$M3/forward.py"
# Weekly refit (Mondays UTC, or when no refit exists yet): a new version the
# board reads from its next request; the old ones stay for the forward record.
# Admission (user 2026-10-28 「加准入门」, refit_gate.py): written only when the
# procedure fitted without the newest 14 days is not worse on them than the card
# in use; refused, the card stays and refit_gate.json says why.
if [ "$(date -u +%u)" = "1" ] || [ ! -f "$OUT/tmax_composite_shift_v1.json" ]; then
  step refit 900 "$PY" "$M3/fit_board.py" --runtime
fi
receipt ok done

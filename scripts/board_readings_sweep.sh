#!/bin/bash
# The backend board-readings job (bayes_basket_target/board_readings.py):
# what the board shows that production's serving path does not store,
# computed here with production's own functions and written to
# data/board_readings/ -- the board only reads those files (user rulings
# 2026-10-02 「只读是读后端算出来的」 / 「新开一个后端读数任务」).
#
# A reader of the store (mode=ro), outside the writer turn.  Like the
# board's research render it waits a running page render out rather than
# reading beside it, skips a start with the WAL past the resident's line,
# runs at utility priority and carries its own deadline: a killed run keeps
# every reading it already wrote and the previous file of the one it was on.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
. "$PROJECT_DIR/scripts/lib/run_with_timeout.sh"
single_instance board_readings
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
PAGE_RENDER_PATTERN="${PM_DASHBOARD_PAGE_RENDER_PATTERN:-calibration_dashboard.py --compute-pages}"
WAIT_SEC="${PM_BOARD_READINGS_WAIT_SEC:-1500}"
POLL_SEC="${PM_BOARD_READINGS_WAIT_POLL_SEC:-15}"
waited=0
while pgrep -f "$PAGE_RENDER_PATTERN" >/dev/null 2>&1; do
  if [ "$waited" -ge "$WAIT_SEC" ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] board readings skipped: the page render is running (waited ${waited}s)"
    exit 0
  fi
  sleep "$POLL_SEC"
  waited=$((waited + POLL_SEC))
done
WAL="data/bayes_basket_target_ssot.db-wal"
MAX_WAL_BYTES="${PM_DASHBOARD_RENDER_MAX_WAL_BYTES:-268435456}"
if [ -f "$WAL" ]; then
  WAL_BYTES="$(stat -f '%z' "$WAL")"
  if [ "$WAL_BYTES" -gt "$MAX_WAL_BYTES" ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] board readings skipped: SSOT WAL ${WAL_BYTES} bytes > ${MAX_WAL_BYTES}"
    exit 0
  fi
fi
# Charter 254: the realisation layer's nightly fit -- the same read-only
# discipline (mode=ro, waits the page render out, WAL line, utility
# priority), writing data/realisation_layer/active.json, which the betting
# lanes' shadow and the board read.  It fits only when the artifact is 20
# hours old or was fitted by other code, so most hourly runs pass at once.
# Never fatal: a failed fit leaves the previous artifact standing.
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] realisation layer"
if run_with_timeout "${PM_REALISATION_REFIT_TIMEOUT_SEC:-900}" "" \
    taskpolicy -d utility nice -n 10 "$PY" scripts/refit_realisation_layer.py; then
  :
else
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] realisation layer refit ended rc=$?; the previous artifact stands"
fi
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] board readings"
TIMEOUT_SEC="${PM_BOARD_READINGS_TIMEOUT_SEC:-2400}"
if run_with_timeout "$TIMEOUT_SEC" "" \
    taskpolicy -d utility nice -n 10 "$PY" -m bayes_basket_target.board_readings; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] done"
else
  rc=$?
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] board readings ended rc=${rc}; readings already written are kept"
  exit "$rc"
fi

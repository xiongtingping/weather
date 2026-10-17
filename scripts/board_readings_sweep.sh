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
# One background SSOT reader at a time (the research render shares the
# turn), and wait for the WAL to come down instead of skipping the hour
# (scripts/lib/ssot_reader_turn.sh, 2026-10-17).
. "$PROJECT_DIR/scripts/lib/ssot_reader_turn.sh"
# The turn may be held a long while (the board readings' realisation refits
# run up to 30 min a stage), so wait most of the hour for it: launchd does not
# stack these hourly jobs, and a skipped hour is what starved the pages.
if ! ssot_reader_turn board_readings "${PM_SSOT_READER_TURN_WAIT_SEC:-3000}"; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] board readings skipped: $SSOT_READER_REASON"
  exit 0
fi
# Charter 254: the realisation layer's nightly fit -- the same read-only
# discipline (mode=ro, waits the page render out, WAL line, utility
# priority), writing data/realisation_layer/active.json, which the betting
# lanes' shadow and the board read.  It fits only when the artifact is 20
# hours old or was fitted by other code, so most hourly runs pass at once.
# Never fatal: a failed fit leaves the previous artifact standing.
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] realisation layer"
# Each due stage is its own call with its own timeout, stalest first
# (2026-10-16): the three ran in one process under one 900 s timeout, and
# from 10-14 every hourly run was killed -- P0 alone took 207-1,589 s with the
# machine's load -- so the artifacts (fitted only when 20 h old) stopped
# renewing (P0-M at 10-12, P1 at 10-10) and the job read the SSOT fifteen
# minutes an hour for nothing.  Each call yields to the writer past the WAL
# reader line (resource_bounds).
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] realisation layer"
for stage in $("$PY" scripts/refit_realisation_layer.py --list-due); do
  if run_with_timeout "${PM_REALISATION_REFIT_TIMEOUT_SEC:-1800}" "" \
      taskpolicy -d utility nice -n 10 "$PY" scripts/refit_realisation_layer.py --stage "$stage"; then
    :
  else
    rc=$?
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] realisation layer ${stage} refit ended rc=${rc}; its previous artifact stands"
  fi
done
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

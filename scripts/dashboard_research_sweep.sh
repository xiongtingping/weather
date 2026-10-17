#!/bin/bash
# The board's research render: the overdue pages the page render cannot
# refresh, on its own clock (user ruling 2026-09-24 「单开研究节渲染」) --
# heavy sections past their lifetime and, since 2026-09-26, any page starved
# for three hours by the page render's budget (calibration_dashboard.
# STARVED_PAGE_AGE_SEC), most overdue first, until RESEARCH_RUN_BUDGET_SEC.
#
# The page render (dashboard_render_sweep.sh) has 1,500 s and serves every
# section that cost 300 s or more last time from its page ("research lane").
# This refreshes those pages, the one furthest past its lifetime first:
# bias_half_life (1,249 s), member_depth (686-1,000 s), charter_ev
# (413-580 s) and whatever else grows past the line.  It is called exactly as
# the page render last called it (data/dashboard_section_cache/
# _section_specs.json), reads only in production gaps, gives its read up if
# the store's WAL crosses the page render's yield line, and writes that
# section's page and nothing else -- the next page render serves it.
#
# It does not start beside the page render: two readers of the store at once
# are what made the page render miss its deadline on 2026-09-24 10:24Z.  It
# waits for a running page render to finish (up to 25 minutes) and starts in
# the gap after it; a start that cannot wait it out exits 0.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
. "$PROJECT_DIR/scripts/lib/run_with_timeout.sh"
single_instance dashboard_research
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
PAGE_RENDER_PATTERN="${PM_DASHBOARD_PAGE_RENDER_PATTERN:-calibration_dashboard.py --compute-pages}"
# Wait the page render out rather than skip the hour.  The page render runs
# 17-25 minutes and restarts 30 minutes after it ends, so an hourly start
# lands inside one about half the time: on 2026-10-01 seven of fourteen
# research starts were skipped, and the pages only this render refreshes
# (member_depth, run_cadence, certificate_authority, ssot_capacity) sat 10-25
# hours old on the board.  Its end opens a 30-minute gap with no page
# render; that gap is this render's.
WAIT_SEC="${PM_DASHBOARD_RESEARCH_WAIT_SEC:-1500}"
POLL_SEC="${PM_DASHBOARD_RESEARCH_WAIT_POLL_SEC:-15}"
waited=0
while pgrep -f "$PAGE_RENDER_PATTERN" >/dev/null 2>&1; do
  if [ "$waited" -ge "$WAIT_SEC" ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] research render skipped: the page render is running (waited ${waited}s)"
    exit 0
  fi
  sleep "$POLL_SEC"
  waited=$((waited + POLL_SEC))
done
if [ "$waited" -gt 0 ]; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] research render waited ${waited}s for the page render to finish"
fi
# The same start line as the page render (the resident's WAL maintenance line).
# One background SSOT reader at a time (the board readings share the turn),
# and wait for the WAL to come down instead of skipping the hour
# (scripts/lib/ssot_reader_turn.sh, 2026-10-17).
. "$PROJECT_DIR/scripts/lib/ssot_reader_turn.sh"
if ! ssot_reader_turn research_render "$WAIT_SEC"; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] research render skipped: $SSOT_READER_REASON"
  exit 0
fi
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] research render (overdue pages: heavy and starved)"
# Five minutes under the hourly StartInterval, so a killed run leaves a gap
# with no research read in it, as the page render's deadline does.
RESEARCH_TIMEOUT_SEC="${PM_DASHBOARD_RESEARCH_TIMEOUT_SEC:-3300}"
if run_with_timeout "$RESEARCH_TIMEOUT_SEC" "" \
    taskpolicy -d utility nice -n 10 "$PY" scripts/calibration_dashboard.py --refresh-research; then
  :
else
  rc=$?
  if [ "$rc" = "124" ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] research render killed after ${RESEARCH_TIMEOUT_SEC}s (timeout); the page it was building keeps its previous version"
  else
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] research render failed (exit $rc)"
  fi
  exit "$rc"
fi
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] done"

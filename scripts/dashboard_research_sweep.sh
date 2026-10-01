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
# are what made the page render miss its deadline on 2026-09-24 10:24Z.  A
# skipped start exits 0; the next hour is the retry.

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
PAGE_RENDER_PATTERN="${PM_DASHBOARD_PAGE_RENDER_PATTERN:-calibration_dashboard.py --html-out}"
if pgrep -f "$PAGE_RENDER_PATTERN" >/dev/null 2>&1; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] research render skipped: the page render is running"
  exit 0
fi
# The same start line as the page render (the resident's WAL maintenance line).
WAL="data/bayes_basket_target_ssot.db-wal"
MAX_WAL_BYTES="${PM_DASHBOARD_RENDER_MAX_WAL_BYTES:-268435456}"
if [ -f "$WAL" ]; then
  WAL_BYTES="$(stat -f '%z' "$WAL")"
  if [ "$WAL_BYTES" -gt "$MAX_WAL_BYTES" ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] research render skipped: SSOT WAL ${WAL_BYTES} bytes > ${MAX_WAL_BYTES}"
    exit 0
  fi
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

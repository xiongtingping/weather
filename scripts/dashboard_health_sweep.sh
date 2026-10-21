#!/bin/bash
# 系统健康快刷 (user 2026-10-21 「系统健康的渲染提高优先级」): every five minutes,
# recompute the health page's cheap every-run sections (the sweeps' clocks,
# the observation doors, the TAF rows -- ``HEALTH_REFRESH_SECTIONS``) and draw
# the page read-only, which also re-reads the resident's status file for the
# banner's lamps.  The half-hourly compute run (dashboard_render_sweep.sh)
# keeps everything else; this one only reads, with the same WAL rule: no
# compute starts with the store's WAL past the resident's line, and the page
# is drawn either way.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
. "$PROJECT_DIR/scripts/lib/run_with_timeout.sh"
single_instance dashboard_health
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
. "$PROJECT_DIR/scripts/lib/ssot_reader_turn.sh"
if ssot_wal_under 0; then
  rc=0
  run_with_timeout "${PM_DASHBOARD_HEALTH_TIMEOUT_SEC:-280}" "" \
      taskpolicy -d utility nice -n 10 "$PY" scripts/calibration_dashboard.py --refresh-health || rc=$?
  if [ "$rc" != "0" ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] health refresh failed or timed out (exit $rc)"
  fi
else
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] health refresh skipped: $SSOT_READER_REASON"
fi
if run_with_timeout "${PM_DASHBOARD_PAGE_TIMEOUT_SEC:-300}" "" \
    "$PY" scripts/calibration_dashboard.py --html-out tmp/cal_dashboard.html >/dev/null; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] page drawn (read-only)"
else
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] page draw failed (exit $?)"
fi

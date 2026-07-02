#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# 加载环境变量
if [ -f "$ROOT/scripts/lib/load_launchctl_env.sh" ]; then
  . "$ROOT/scripts/lib/load_launchctl_env.sh"
  load_repo_env_file "$ROOT/.env"
fi

. "$ROOT/scripts/lib/process_governance.sh"
. "$ROOT/scripts/lib/resolve_project_python.sh"
. "$ROOT/scripts/lib/weather_refresh_coordinator.sh"

LOG="$ROOT/data/market_refresh_frequent.log"
if ! weather_refresh_register_entrypoint "market_refresh_frequent" "market" "${PM_MARKET_REFRESH_LANE_NAME:-${PM_REFRESH_COORDINATOR_LANE_NAME:-weather_refresh_lane}}" "$LOG" "market_refresh_frequent"; then
  exit 0
fi
PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  PY="$(resolve_project_python "$ROOT" || true)"
fi
if [ -z "${PY:-}" ] || [ ! -x "$PY" ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh_frequent] python_missing" >> "$LOG"
  exit 1
fi
if weather_refresh_skip_if_live_ssot "market_refresh_frequent" "$LOG" "market_refresh_frequent"; then
  exit 0
fi
. "$ROOT/scripts/lib/run_with_timeout.sh"
REFRESH_TIMEOUT_SEC="${PM_MARKET_REFRESH_FREQUENT_TIMEOUT_SEC:-180}"
export PM_ARCHIVE_WRITE_SCOPE="${PM_ARCHIVE_WRITE_SCOPE:-chengdu_zuuu}"
if ! [[ "$REFRESH_TIMEOUT_SEC" =~ ^[0-9]+$ ]]; then
  REFRESH_TIMEOUT_SEC="180"
fi
REFRESH_NICE="$(normalize_nice_level "${PM_MARKET_REFRESH_FREQUENT_NICE:-15}" 15)"

cleanup_locks() {
  weather_refresh_db_writer_release
  weather_scan_lane_release
}
trap cleanup_locks EXIT

if ! weather_scan_lane_acquire "market_refresh_frequent" 0 "$LOG" "market_refresh_frequent"; then
  exit 0
fi

: "${PM_MARKET_REFRESH_DB_LOCK_ENABLED:=1}"
if [ "${PM_MARKET_REFRESH_DB_LOCK_ENABLED}" = "1" ]; then
  : "${PM_DB_WRITER_LOCK_WAIT_SEC:=15}"
  if ! weather_refresh_db_writer_acquire "market_refresh_frequent" "$PM_DB_WRITER_LOCK_WAIT_SEC" "$LOG" "market_refresh_frequent"; then
    exit 0
  fi
fi

echo "---- $(date '+%Y-%m-%d %H:%M:%S') market_refresh_frequent start timeout=${REFRESH_TIMEOUT_SEC}s nice=${REFRESH_NICE} ----" >> "$LOG"

# 只刷新市场数据，不更新模型预测
# 针对有持仓或高edge的事件
run_with_timeout "$REFRESH_TIMEOUT_SEC" "$LOG" nice -n "$REFRESH_NICE" "$PY" scan_weather_market.py \
  --scan-weather-today \
  --days-ahead 2 \
  --market-refresh-only \
  --write-scope "${PM_ARCHIVE_WRITE_SCOPE}" \
  --depth-levels 5

exit_code=$?
echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh_frequent] completed exit=$exit_code" >> "$LOG"
exit $exit_code

#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# 加载环境变量
if [ -f "$ROOT/scripts/lib/load_launchctl_env.sh" ]; then
  . "$ROOT/scripts/lib/load_launchctl_env.sh"
  load_repo_env_file "$ROOT/.env"
fi

LOG="$ROOT/data/market_refresh_urgent.log"
. "$ROOT/scripts/lib/resolve_project_python.sh"
. "$ROOT/scripts/lib/process_governance.sh"
. "$ROOT/scripts/lib/weather_refresh_coordinator.sh"
if ! weather_refresh_register_entrypoint "market_refresh_urgent" "market" "${PM_MARKET_REFRESH_LANE_NAME:-${PM_REFRESH_COORDINATOR_LANE_NAME:-weather_refresh_lane}}" "$LOG" "urgent_refresh"; then
  exit 0
fi
PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  PY="$(resolve_project_python "$ROOT" || true)"
fi
if [ -z "${PY:-}" ] || [ ! -x "$PY" ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [urgent_refresh] python_missing" >> "$LOG"
  exit 1
fi
if weather_refresh_skip_if_live_ssot "market_refresh_urgent" "$LOG" "urgent_refresh"; then
  exit 0
fi
. "$ROOT/scripts/lib/run_with_timeout.sh"
REFRESH_TIMEOUT_SEC="${PM_MARKET_REFRESH_URGENT_TIMEOUT_SEC:-45}"
export PM_ARCHIVE_WRITE_SCOPE="${PM_ARCHIVE_WRITE_SCOPE:-chengdu_zuuu}"
if ! [[ "$REFRESH_TIMEOUT_SEC" =~ ^[0-9]+$ ]]; then
  REFRESH_TIMEOUT_SEC="45"
fi
REFRESH_NICE="$(normalize_nice_level "${PM_MARKET_REFRESH_URGENT_NICE:-15}" 15)"

cleanup_locks() {
  weather_refresh_db_writer_release
  weather_scan_lane_release
}
trap cleanup_locks EXIT

# 查询距离结算<8小时的事件数量（临近结算加速）
URGENT_COUNT="$(sqlite3 data/archive.db "SELECT COUNT(*) FROM event_fact_latest WHERE event_date > datetime('now') AND (julianday(event_date) - julianday('now')) * 24 < 8;" 2>/dev/null || echo 0)"
URGENT_COUNT="${URGENT_COUNT%%$'\n'*}"
if ! [[ "${URGENT_COUNT}" =~ ^[0-9]+$ ]]; then
  URGENT_COUNT=0
fi

if [ "$URGENT_COUNT" -eq 0 ]; then
  # 没有紧急事件，跳过
  exit 0
fi

if ! weather_scan_lane_acquire "market_refresh_urgent" 0 "$LOG" "urgent_refresh"; then
  exit 0
fi

: "${PM_MARKET_REFRESH_DB_LOCK_ENABLED:=1}"
if [ "${PM_MARKET_REFRESH_DB_LOCK_ENABLED}" = "1" ]; then
  : "${PM_DB_WRITER_LOCK_WAIT_SEC:=15}"
  if ! weather_refresh_db_writer_acquire "market_refresh_urgent" "$PM_DB_WRITER_LOCK_WAIT_SEC" "$LOG" "urgent_refresh"; then
    exit 0
  fi
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') [urgent_refresh] urgent events: $URGENT_COUNT timeout=${REFRESH_TIMEOUT_SEC}s nice=${REFRESH_NICE}" >> "$LOG"

# 只刷新临近结算的事件（<8小时）
# 限制最多20个事件，25秒预算
# 增加超时和重试：5秒超时，3次重试
run_with_timeout "$REFRESH_TIMEOUT_SEC" "$LOG" nice -n "$REFRESH_NICE" "$PY" scan_weather_market.py \
  --scan-weather-today \
  --market-refresh-only \
  --market-refresh-max-outcomes 20 \
  --market-refresh-time-budget-sec 25 \
  --market-refresh-clob-timeout-sec 5 \
  --market-refresh-clob-retries 3 \
  --write-scope "${PM_ARCHIVE_WRITE_SCOPE}" \
  --no-auto-trade

exit_code=$?
echo "$(date '+%Y-%m-%d %H:%M:%S') [urgent_refresh] completed exit=$exit_code" >> "$LOG"
exit $exit_code

#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DST_DIR="${HOME}/Library/LaunchAgents"
USER_NAME="${PM_LAUNCHD_USER:-${USER}}"
UID_VALUE="$(id -u "${USER_NAME}")"

PLISTS=(
  com.xiong.weather.ui.plist
  com.weather.market_refresh_1m.plist
  com.weather.model_scan.plist
  com.weather.daily_scan.plist
  com.weather.auto_trade_watcher.plist
  com.weather.auto_trade_entry_1m.plist
  com.weather.trade_engine.plist
  com.weather.auto_trade_worker.plist
  com.weather.trade_sync_loop.plist
  com.weather.q_calibrated_writeback_hourly.plist
  com.weather.feedback_loop_hourly.plist
  com.weather.daily_trade_sms_summary.plist
)

for name in "${PLISTS[@]}"; do
  dest="${DST_DIR}/${name}"
  label="${name%.plist}"
  if [ -f "$dest" ]; then
    launchctl bootout "gui/${UID_VALUE}" "$dest" >/dev/null 2>&1 || true
    rm -f "$dest"
  fi
done

echo "Removed LaunchAgents for ${USER_NAME}."

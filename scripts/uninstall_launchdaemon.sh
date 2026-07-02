#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DST_DIR="/Library/LaunchDaemons"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run with sudo: sudo $0" >&2
  exit 1
fi

PLISTS=(
  com.xiong.weather.ui.plist
  com.weather.model_scan.plist
  com.weather.daily_scan.plist
  com.weather.chengdu_wu_backfill.plist
  com.weather.monitor.daemon.plist
  com.weather.auto_trade_watcher.plist
  com.weather.auto_trade_entry_1m.plist
  com.weather.trade_engine.plist
  com.weather.auto_trade_worker.plist
  com.weather.trade_sync_loop.plist
)

for name in "${PLISTS[@]}"; do
  dest="$DST_DIR/$name"
  if [ ! -f "$dest" ]; then
    continue
  fi
  label=""
  if /usr/bin/plutil -extract Label raw "$dest" >/dev/null 2>&1; then
    label="$(/usr/bin/plutil -extract Label raw "$dest" 2>/dev/null || true)"
  fi
  if [ -n "$label" ]; then
    launchctl bootout "system/${label}" >/dev/null 2>&1 || true
  else
    launchctl bootout system "$dest" >/dev/null 2>&1 || true
  fi
  rm -f "$dest"
done

echo "Removed LaunchDaemons."

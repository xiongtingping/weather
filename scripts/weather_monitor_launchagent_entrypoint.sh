#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UID_VALUE="$(id -u)"
SYSTEM_LABEL="system/com.weather.monitor.daemon"
GUI_DOMAIN="gui/${UID_VALUE}/com.weather.monitor.daemon"
STATUS="$ROOT/data/monitor_launch_domain_status.json"

mkdir -p "$ROOT/data"
while launchctl print "$SYSTEM_LABEL" 2>/dev/null | grep -Eq '^[[:space:]]+pid = [0-9]+$'; do
  printf '{"schema":"weather_monitor_launch_domain_v1","state":"waiting_for_legacy_system_exit","active_domain":"%s","target_domain":"%s","updated_at":"%s"}\n' \
    "$SYSTEM_LABEL" "$GUI_DOMAIN" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "${STATUS}.tmp.$$"
  mv "${STATUS}.tmp.$$" "$STATUS"
  sleep 2
done

printf '{"schema":"weather_monitor_launch_domain_v1","state":"user_domain_starting","active_domain":"%s","target_domain":"%s","updated_at":"%s"}\n' \
  "$GUI_DOMAIN" "$GUI_DOMAIN" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "${STATUS}.tmp.$$"
mv "${STATUS}.tmp.$$" "$STATUS"
export PM_MONITOR_LAUNCH_DOMAIN="$GUI_DOMAIN"
exec "$ROOT/scripts/polymarket_monitor_daemon.sh"

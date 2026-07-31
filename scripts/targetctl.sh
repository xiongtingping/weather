#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
UID_VALUE="$(id -u)"
DOMAIN="gui/${UID_VALUE}"
PAUSE="$ROOT/data/live_paused.flag"
LABELS=(
  com.weather.reported_trajectory_no
  com.weather.market_ws
  com.weather.clob_order_service
)

usage() {
  echo "usage: scripts/targetctl.sh <install|start|stop|restart|status>" >&2
  exit 2
}

assert_system_absent() {
  local label
  for label in "${LABELS[@]}"; do
    if /bin/launchctl print "system/${label}" >/dev/null 2>&1; then
      echo "system owner still loaded: ${label}" >&2
      return 1
    fi
  done
}

stop_user_services() {
  local label
  touch "$PAUSE"
  for label in "${LABELS[@]}"; do
    /bin/launchctl disable "${DOMAIN}/${label}" >/dev/null 2>&1 || true
    /bin/launchctl bootout "${DOMAIN}/${label}" >/dev/null 2>&1 || true
  done
  local deadline=$((SECONDS + 45))
  while [ "$SECONDS" -lt "$deadline" ]; do
    local present=0
    for label in "${LABELS[@]}"; do
      if /bin/launchctl print "${DOMAIN}/${label}" >/dev/null 2>&1; then
        present=1
        break
      fi
    done
    [ "$present" -eq 1 ] || return 0
    sleep 0.25
  done
  echo "user-domain target service did not stop" >&2
  return 1
}

status_services() {
  local label
  local state
  for label in "${LABELS[@]}"; do
    state="absent"
    if /bin/launchctl print "${DOMAIN}/${label}" >/dev/null 2>&1; then
      state="user"
    fi
    if /bin/launchctl print "system/${label}" >/dev/null 2>&1; then
      state="${state}+system"
    fi
    printf '%s=%s\n' "$label" "$state"
  done
  printf 'production_no_live=true\n'
  printf 'live_pause_present=%s\n' "$([ -f "$PAUSE" ] && echo true || echo false)"
}

case "${1:-}" in
  install|start)
    assert_system_absent
    exec /bin/bash "$ROOT/scripts/install_reported_trajectory_no_user_services.sh"
    ;;
  stop)
    stop_user_services
    ;;
  restart)
    assert_system_absent
    stop_user_services
    exec /bin/bash "$ROOT/scripts/install_reported_trajectory_no_user_services.sh"
    ;;
  status)
    status_services
    ;;
  *)
    usage
    ;;
esac

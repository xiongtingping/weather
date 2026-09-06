#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
UID_VALUE="$(id -u)"
DOMAIN="gui/${UID_VALUE}"
PAUSE="$ROOT/data/live_paused.flag"
LEGACY_LABEL="com.weather.reported_trajectory_no"
LABELS=(
  com.weather.weather_basket_yes
  com.weather.market_ws
  com.weather.clob_order_service
)
RETIRED_SYSTEM_LABELS=(
  com.weather.monitor.daemon
  com.weather.model_release_watcher
)

usage() {
  echo "usage: scripts/targetctl.sh <install|start|stop|restart|status>" >&2
  exit 2
}

assert_system_absent() {
  local label
  for label in "$LEGACY_LABEL" "${LABELS[@]}" "${RETIRED_SYSTEM_LABELS[@]}"; do
    if /bin/launchctl print "system/${label}" >/dev/null 2>&1; then
      echo "system owner still loaded: ${label}" >&2
      return 1
    fi
    if [ -e "/Library/LaunchDaemons/${label}.plist" ]; then
      echo "retired system plist still present: /Library/LaunchDaemons/${label}.plist" >&2
      return 1
    fi
  done
}

stop_user_services() {
  local label
  # This function used to create the pause file before stopping anything,
  # which reads as
  # prudent -- do not leave submission open while the services go down -- and
  # was the first link in the 2026-09-06 cascade.  Nothing here ever lowered
  # it again.  A restart that completed left submission shut, and a restart
  # that did NOT complete left it shut with the services down too: the stop
  # below times out non-zero, ``set -e`` skips the install that follows, and
  # the remedy a person then reaches for is the installer, which used to arm
  # the same flag a second time.  Measured that day: two services down, the
  # flag set at 12:07:51Z, and at 13:36:22Z EGLC's first complete 2026-09-08
  # basket -- three legs, 5.202 USDC, the 0.031 leg lifted to 33 shares by
  # charter 98 -- refused three times with clob_order_service_live_paused.
  #
  # The safety this was reaching for is real but belongs to the boundary that
  # already enforces it: the order service reads the flag per order and fails
  # closed on an unreadable one, and a stopped service submits nothing at all.
  # What the touch added was not safety, it was a switch thrown on the way
  # past that nobody could see.
  #
  # User ruling 2026-09-06 ("取消暂停旗标,我自己开关" / "拆"): the flag is the
  # operator's alone.  Stop reports it, sets it never.
  printf 'live_pause_present=%s\n' "$([ -f "$PAUSE" ] && echo true || echo false)"
  /bin/launchctl disable "${DOMAIN}/${LEGACY_LABEL}" >/dev/null 2>&1 || true
  /bin/launchctl bootout "${DOMAIN}/${LEGACY_LABEL}" >/dev/null 2>&1 || true
  /bin/rm -f "$HOME/Library/LaunchAgents/${LEGACY_LABEL}.plist"
  local legacy_deadline=$((SECONDS + 10))
  while /bin/launchctl print "${DOMAIN}/${LEGACY_LABEL}" >/dev/null 2>&1; do
    if [ "$SECONDS" -ge "$legacy_deadline" ]; then
      echo "retired user-domain owner remained loaded: ${LEGACY_LABEL}" >&2
      return 1
    fi
    sleep 0.25
  done
  if /bin/launchctl print "${DOMAIN}/${LEGACY_LABEL}" >/dev/null 2>&1; then
    echo "retired user-domain owner remained loaded: ${LEGACY_LABEL}" >&2
    return 1
  fi
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

ensure_clob_port_free() {
  # A prior user-domain crash can leave the node child orphaned under launchd's
  # PID 1 after bootout.  Do not let a stale listener create a second
  # read-only gateway or leave the LaunchAgent in an EADDRINUSE respawn loop.
  # Only an exact copy of this repository's clob service may be terminated;
  # any unrelated listener is a hard blocker.
  local pid command
  while read -r pid; do
    [ -n "$pid" ] || continue
    command="$(/bin/ps -p "$pid" -o command= 2>/dev/null || true)"
    if [[ "$command" != *"$ROOT/scripts/clob_order_service.mjs"* ]]; then
      echo "unexpected process owns CLOB port 8765: pid=${pid} command=${command}" >&2
      return 1
    fi
    /bin/kill "$pid" >/dev/null 2>&1 || true
  done < <(/usr/sbin/lsof -nP -t -iTCP:8765 -sTCP:LISTEN 2>/dev/null || true)
  local deadline=$((SECONDS + 10))
  while /usr/sbin/lsof -nP -t -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; do
    [ "$SECONDS" -lt "$deadline" ] || {
      echo "CLOB port 8765 remained occupied after stale exact-owner termination" >&2
      return 1
    }
    sleep 0.25
  done
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
  for label in "${RETIRED_SYSTEM_LABELS[@]}"; do
    if /bin/launchctl print "system/${label}" >/dev/null 2>&1; then
      printf '%s=system\n' "$label"
    elif [ -e "/Library/LaunchDaemons/${label}.plist" ]; then
      printf '%s=plist_residue\n' "$label"
    else
      printf '%s=absent\n' "$label"
    fi
  done
  printf 'production_no_live=true\n'
  printf 'live_pause_present=%s\n' "$([ -f "$PAUSE" ] && echo true || echo false)"
}

case "${1:-}" in
  install|start)
    assert_system_absent
    exec /bin/bash "$ROOT/scripts/install_weather_basket_yes_user_services.sh"
    ;;
  stop)
    stop_user_services
    ;;
restart)
    assert_system_absent
    stop_user_services
    ensure_clob_port_free
    # The installer reports the pause state again on the way out; this is the
    # last line before exec replaces the process, so the state is said on both
    # sides of the restart.
    exec /bin/bash "$ROOT/scripts/install_weather_basket_yes_user_services.sh"
    ;;
  status)
    status_services
    ;;
  *)
    usage
    ;;
esac

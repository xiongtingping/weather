#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
CONFIRM="${1:-}"
PAUSE="$ROOT/data/live_paused.flag"
LEGACY_LABEL="com.weather.reported_trajectory_no"
LABELS=(
  com.weather.weather_basket_yes
  com.weather.market_ws
  com.weather.clob_order_service
)
# These are retired root-owned producers.  They are deliberately distinct
# from the three user-domain production labels: leaving their plists on disk
# is unsafe even when they are not currently bootstrapped, because launchd
# can revive them after a restart or an accidental bootstrap.
RETIRED_SYSTEM_LABELS=(
  com.weather.monitor.daemon
  com.weather.model_release_watcher
)
SYSTEM_RETIRE_LABELS=(
  "$LEGACY_LABEL"
  "${LABELS[@]}"
  "${RETIRED_SYSTEM_LABELS[@]}"
)

if [ "$CONFIRM" != "--confirm-user-domain-production-no-live" ]; then
  echo "usage: $0 --confirm-user-domain-production-no-live" >&2
  exit 2
fi
if [ "$(id -u)" -eq 0 ]; then
  echo "run migration as the login user, not root" >&2
  exit 2
fi

mkdir -p "$ROOT/data" "$ROOT/data/logs"
touch "$PAUSE"

# Finish all user-domain preflight checks before touching the root-owned jobs.
for script in \
  "$ROOT/scripts/install_launchagent.sh" \
  "$ROOT/scripts/install_weather_basket_yes_user_services.sh" \
  "$ROOT/scripts/weather_basket_yes_daemon.sh" \
  "$ROOT/scripts/market_ws_listener.sh" \
  "$ROOT/scripts/clob_order_service.sh"; do
  /bin/bash -n "$script"
done
for label in "${LABELS[@]}"; do
  source_plist="$ROOT/launchd/${label}.plist"
  if [ ! -r "$source_plist" ]; then
    echo "user-domain source plist missing: ${source_plist}" >&2
    exit 4
  fi
  /usr/bin/plutil -lint "$source_plist" >/dev/null
done
if ! /bin/launchctl print "gui/$(id -u)" >/dev/null 2>&1; then
  echo "login user launchd domain unavailable" >&2
  exit 5
fi

system_retirement_required=0
for label in "${SYSTEM_RETIRE_LABELS[@]}"; do
  if /bin/launchctl print "system/${label}" >/dev/null 2>&1; then
    system_retirement_required=1
  fi
  if [ -e "/Library/LaunchDaemons/${label}.plist" ]; then
    system_retirement_required=1
  fi
done

if [ "$system_retirement_required" -eq 1 ]; then
  # Never prompt for a password. A cached/noninteractive authorization may
  # complete the one-time retirement; otherwise no launchd state is changed.
  if ! /usr/bin/sudo -n true >/dev/null 2>&1; then
    echo "one_time_system_retirement_requires_cached_admin_authorization" >&2
    echo "no password prompt was opened; no service state was changed" >&2
    exit 6
  fi
  for label in "${SYSTEM_RETIRE_LABELS[@]}"; do
    /usr/bin/sudo -n /bin/launchctl disable "system/${label}" \
      >/dev/null 2>&1 || true
    /usr/bin/sudo -n /bin/launchctl bootout "system/${label}" \
      >/dev/null 2>&1 || true
  done
  deadline=$((SECONDS + 60))
  while [ "$SECONDS" -lt "$deadline" ]; do
    remaining=0
    for label in "${SYSTEM_RETIRE_LABELS[@]}"; do
      if /bin/launchctl print "system/${label}" >/dev/null 2>&1; then
        remaining=1
        break
      fi
    done
    [ "$remaining" -eq 1 ] || break
    sleep 0.25
  done
  for label in "${SYSTEM_RETIRE_LABELS[@]}"; do
    if /bin/launchctl print "system/${label}" >/dev/null 2>&1; then
      echo "system owner did not stop: ${label}" >&2
      exit 7
    fi
    plist="/Library/LaunchDaemons/${label}.plist"
    if [ -e "$plist" ]; then
      /usr/bin/sudo -n /bin/rm -f "$plist"
    fi
    if [ -e "$plist" ]; then
      echo "system plist was not retired: ${plist}" >&2
      exit 8
    fi
  done
fi

exec /bin/bash "$ROOT/scripts/install_weather_basket_yes_user_services.sh"

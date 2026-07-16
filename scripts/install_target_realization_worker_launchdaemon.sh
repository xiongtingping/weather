#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/launchd/com.weather.target_realization_worker.plist"
DST="/Library/LaunchDaemons/com.weather.target_realization_worker.plist"
LABEL="com.weather.target_realization_worker"
USER_NAME="${PM_LAUNCHD_USER:-${SUDO_USER:-xiong}}"
GROUP_NAME="${PM_LAUNCHD_GROUP:-$(id -gn "$USER_NAME")}" || GROUP_NAME="staff"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run with sudo: sudo bash $0" >&2
  exit 1
fi

install -d -o "$USER_NAME" -g "$GROUP_NAME" "$ROOT/data" "$ROOT/data/logs"
install -o root -g wheel -m 644 "$SRC" "$DST"
/usr/bin/plutil -lint "$DST"
/bin/launchctl bootout system "$DST" >/dev/null 2>&1 || true
/bin/launchctl bootstrap system "$DST"
/bin/launchctl enable "system/$LABEL"
/bin/launchctl kickstart -k "system/$LABEL"
/bin/launchctl print "system/$LABEL"

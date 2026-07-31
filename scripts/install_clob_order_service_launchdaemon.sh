#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.weather.clob_order_service"

if [ "$(id -u)" -eq 0 ]; then
  echo "CLOB installer is user-domain only; run without root/sudo" >&2
  exit 2
fi
if /bin/launchctl print "system/${LABEL}" >/dev/null 2>&1; then
  echo "system owner still loaded: ${LABEL}; refusing dual-active" >&2
  exit 3
fi

mkdir -p "$ROOT/data"
touch "$ROOT/data/live_paused.flag"
PM_LAUNCHD_ONLY="$LABEL" PM_LAUNCHD_BOOTSTRAP=1 \
  exec /bin/bash "$ROOT/scripts/install_launchagent.sh"

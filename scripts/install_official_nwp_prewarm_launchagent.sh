#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.weather.official_nwp_prewarm"
RETIRED_LABEL="com.weather.v31_multimodel_prewarm"
PLIST_SOURCE="$ROOT/launchd/${LABEL}.plist"
PLIST_TARGET="$HOME/Library/LaunchAgents/${LABEL}.plist"
DOMAIN="gui/$(id -u)"

if [ ! -f "$PLIST_SOURCE" ]; then
  echo "missing plist: $PLIST_SOURCE" >&2
  exit 2
fi

plutil -lint "$PLIST_SOURCE" >/dev/null
mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/data/logs" "$ROOT/data/runtime/official_nwp_multimodel_zuuu_v1/prewarm"
launchctl bootout "$DOMAIN/$RETIRED_LABEL" >/dev/null 2>&1 || true
rm -f "$HOME/Library/LaunchAgents/${RETIRED_LABEL}.plist"
cp "$PLIST_SOURCE" "$PLIST_TARGET"
launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
launchctl bootstrap "$DOMAIN" "$PLIST_TARGET"
launchctl print "$DOMAIN/$LABEL" >/dev/null
echo "installed ${DOMAIN}/${LABEL}; assembly only; no network, archive.db, posterior, decision, queue, or order writes"

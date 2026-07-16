#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.weather.dynamic_three_leg_execution"
SOURCE="$ROOT/launchd/${LABEL}.plist"
TARGET="$HOME/Library/LaunchAgents/${LABEL}.plist"
DOMAIN="gui/$(id -u)"

python3 - "$SOURCE" <<'PY'
import plistlib
import sys
from pathlib import Path

path = Path(sys.argv[1])
value = plistlib.loads(path.read_bytes())
if value.get("Label") != "com.weather.dynamic_three_leg_execution":
    raise SystemExit("dynamic_three_leg_resident_label_invalid")
if value.get("Disabled") is not True or value.get("RunAtLoad") is not False:
    raise SystemExit("dynamic_three_leg_resident_must_install_default_disabled")
environment = value.get("EnvironmentVariables") or {}
required_zero = {
    "PM_DYNAMIC_THREE_LEG_RESIDENT_SUBMITTER_ENABLED",
    "PM_DYNAMIC_THREE_LEG_DECISION_RESIDENT_ENABLED",
    "PM_DYNAMIC_THREE_LEG_PRODUCTION_ENABLED",
    "PM_DYNAMIC_THREE_LEG_LIVE_SUBMIT_ENABLED",
    "PM_MARKET_ALPHA_TAKE_PROFIT_PRODUCTION_ENABLED",
    "PM_MARKET_ALPHA_TAKE_PROFIT_LIVE_SUBMIT_ENABLED",
}
invalid = sorted(key for key in required_zero if str(environment.get(key, "")) != "0")
if invalid:
    raise SystemExit("dynamic_three_leg_resident_live_flags_not_zero:" + ",".join(invalid))
PY

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/data/logs"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || \
  launchctl bootout "$DOMAIN" "$TARGET" 2>/dev/null || true
launchctl disable "$DOMAIN/$LABEL"
install -m 644 "$SOURCE" "$TARGET"

SOURCE_SHA="$(shasum -a 256 "$SOURCE" | awk '{print $1}')"
TARGET_SHA="$(shasum -a 256 "$TARGET" | awk '{print $1}')"
test "$SOURCE_SHA" = "$TARGET_SHA"
if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  echo "dynamic-three-leg resident unexpectedly loaded while installing disabled" >&2
  exit 3
fi

echo "dynamic-three-leg resident installed default-disabled; not loaded; live flags all zero; sha256=$TARGET_SHA"

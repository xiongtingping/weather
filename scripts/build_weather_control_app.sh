#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

SRC="$ROOT/scripts/WeatherControl.applescript"
OUT_DIR="$ROOT/dist"
APP_OUT="$OUT_DIR/Weather Control.app"

mkdir -p "$OUT_DIR"

if ! command -v osacompile >/dev/null 2>&1; then
  echo "error: osacompile not found (macOS only)" >&2
  exit 2
fi

if [ ! -f "$SRC" ]; then
  echo "error: missing $SRC" >&2
  exit 2
fi

rm -rf "$APP_OUT" 2>/dev/null || true
osacompile -o "$APP_OUT" "$SRC"

RES_DIR="$APP_OUT/Contents/Resources"
INFO_PLIST="$APP_OUT/Contents/Info.plist"
mkdir -p "$RES_DIR"

# Icon: prefer a distinct built-in system icon so it doesn't look identical to Weather.app.
# You can override by setting WEATHER_CONTROL_ICON to a .icns path.
ICON_SRC="${WEATHER_CONTROL_ICON:-/System/Library/CoreServices/CoreTypes.bundle/Contents/Resources/Actions.icns}"
if [ -f "$ICON_SRC" ]; then
  cp -f "$ICON_SRC" "$RES_DIR/AppIcon.icns"
  /usr/libexec/PlistBuddy -c "Set :CFBundleIconFile AppIcon" "$INFO_PLIST" >/dev/null 2>&1 \
    || /usr/libexec/PlistBuddy -c "Add :CFBundleIconFile string AppIcon" "$INFO_PLIST" >/dev/null 2>&1 \
    || true
fi

echo "built: $APP_OUT"
echo "tip: drag it into Dock. The actions run via Terminal and call scripts/weatherctl.sh"

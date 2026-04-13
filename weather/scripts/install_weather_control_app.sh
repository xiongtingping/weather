#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

"$ROOT/scripts/build_weather_control_app.sh"

APP_SRC="$ROOT/dist/Weather Control.app"
APP_DST_DIR="$HOME/Applications"
APP_DST="$APP_DST_DIR/Weather Control.app"

mkdir -p "$APP_DST_DIR"
rm -rf "$APP_DST" 2>/dev/null || true
cp -R "$APP_SRC" "$APP_DST"

echo "installed: $APP_DST"
echo "next: drag it into Dock (or run: open \"$APP_DST\")"

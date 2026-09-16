#!/bin/bash
# Hot update the board: redraw the page with the template on disk around the
# payload its last full render kept (user ruling 2026-09-16: 前端改为热更新).
#
# launchd runs this on every change to calibration_dashboard.py -- the file
# itself for an in-place write, and its directory for a save that replaces
# the file by rename, since a watch on the old vnode never sees that.  The
# directory also changes whenever any other script is saved, so the source's
# mtime and size are remembered and an unchanged source exits at once.
#
# A save that lands while a redraw is running is not lost: the loop redraws
# again until the source it drew matches the source on disk.  The redraw
# itself refuses a template whose script does not parse, so a save in the
# middle of an edit leaves the previous page up.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance dashboard_reinflate
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
SOURCE="scripts/calibration_dashboard.py"
STATE="data/dashboard_reinflate.state"
PAGE="tmp/cal_dashboard.html"

signature() { stat -f '%m:%z' "$SOURCE"; }

for _ in 1 2 3 4 5; do
  current="$(signature)"
  if [ -f "$STATE" ] && [ "$(cat "$STATE")" = "$current" ]; then
    exit 0
  fi
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] reinflate ($current)"
  "$PY" "$SOURCE" --reinflate "$PAGE" || true
  printf '%s' "$current" > "$STATE"
done

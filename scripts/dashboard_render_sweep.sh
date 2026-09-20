#!/bin/bash
# Render the dashboard on its own clock, outside the resident's writer queue.
#
# The page's freshness used to ride the hosted paper-ledger sweep, and the
# hosted queue is a single writer lane: when it grew past ten members
# (2026-08-28) the ledger round waited hours for a turn and the page aged
# with it -- three separate "上轮 N 小时前" alarms in two days were stale
# renders, not stalls.  A render is a reader: it takes no write lock and
# has no business queueing behind writers, so it gets its own timer.
#
# The dashboard's own no-clobber guard (write only when nothing newer
# landed while building) keeps this and the hosted render from fighting;
# whichever finishes with the fresher payload wins.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance dashboard_render
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
SOURCE="scripts/calibration_dashboard.py"
BEFORE="$(stat -f '%m:%z' "$SOURCE")"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] dashboard render (standalone reader)"
# Behind the resident, never beside it: the render runs on the machine the
# resident runs on, for a quarter of every hour, and the resident's decision
# cycles are what the page exists to show.  A niced render yields the cores
# to them and takes what is left.
nice -n 10 "$PY" scripts/calibration_dashboard.py --html-out tmp/cal_dashboard.html
# A render draws with the template it imported at start, and one of these
# takes twenty minutes to an hour and a half.  A template edit that lands
# while it runs is therefore overwritten when it writes the page -- and the
# hot-update watcher will not put it back, because WatchPaths fires on
# changes to the source and the source has not changed since.  On
# 2026-09-20 that reverted the board to the previous day's layout for as
# long as nobody happened to save the file again.
#
# So: the signature the render started with, against the one on disk now.
# Different means the page on disk was drawn with a template nobody has any
# more -- redraw it around the payload this render just produced.  Cheap
# (about a second) and a no-op on the common path.
if [ "$(stat -f '%m:%z' "$SOURCE")" != "$BEFORE" ]; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] template changed during the render; redrawing"
  "$PY" "$SOURCE" --reinflate tmp/cal_dashboard.html || true
fi
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] done"

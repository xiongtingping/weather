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
. "$PROJECT_DIR/scripts/lib/run_with_timeout.sh"
single_instance dashboard_render
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
SOURCE="scripts/calibration_dashboard.py"
# Never start while the store's WAL is already past the resident's
# maintenance line (its TARGET_SSOT_WARN_WAL_BYTES, 256 MiB).  A truncating
# checkpoint needs every reader off the WAL, and the writer's (re)open takes
# one with no wait at all (resource_bounds.checkpoint_target_wal_once_if_
# oversized), so a render begun now is the reader that keeps production out.
# 2026-09-24: the resident restarted at 01:24:49Z onto a 976 MB WAL and
# failed closed on target_database_wal_checkpoint_busy while the 01:14Z
# render was reading; launchd would have started the next one straight
# into the same wall.  A skipped render keeps the previous page, and the
# next interval is the retry -- the same bargain as the deadline below.
#
# 2026-10-15 (user ruling 「页面渲染零查库，各节全搬到后端」): two steps.  The
# backend compute run (``--compute-pages``) is the only one that reads the
# store, under every rule below; the page (``--html-out``) is then drawn
# from what it and the research run kept, reads no SSOT, and takes seconds.
# The page is drawn whether or not the compute finished, skipped or was
# killed: a slow read costs that section its freshness, never the page.
draw_page() {
  if run_with_timeout "${PM_DASHBOARD_PAGE_TIMEOUT_SEC:-300}" "" \
      "$PY" scripts/calibration_dashboard.py --html-out tmp/cal_dashboard.html; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] page drawn (read-only)"
  else
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] page draw failed (exit $?)"
  fi
}
# Wait a while for the resident to truncate it rather than skip the half
# hour outright (2026-10-17: three of six computes from 04:19Z were skipped on
# a WAL the resident truncated minutes later).
. "$PROJECT_DIR/scripts/lib/ssot_reader_turn.sh"
if ! ssot_wal_under "${PM_DASHBOARD_RENDER_WAL_WAIT_SEC:-300}"; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] compute skipped: $SSOT_READER_REASON; the writer needs it truncated first"
  draw_page
  exit 0
fi
BEFORE="$(stat -f '%m:%z' "$SOURCE")"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] dashboard compute (standalone reader)"
# Behind the resident, never beside it: the render runs on the machine the
# resident runs on, for a quarter of every hour, and the resident's decision
# cycles are what the page exists to show.  A niced render yields the cores
# to them and takes what is left.
#
# ... and the DISK, which ``nice`` does not touch (2026-09-23).  On an 8 GB
# machine with 14 GB in swap the store's file cache was down to ~740 MB, so
# every read went to disk; the render (0.9 GB resident, 45 min to 2 h a run
# from 01:34Z, i.e. running continuously) was 40-50% of the disk's traffic
# (233-403 MB/s with it, 168-197 MB/s paused), and the resident's cycle went
# from 70-130 s to 1-2 h -- one decision round an hour, P1 facts starved.
# ``taskpolicy -b`` puts the render in Darwin background priority: its CPU
# AND its disk I/O are throttled whenever anyone else wants them.
#
# ... which is also how a render can never finish (2026-09-23, user ruling
# 「加超时」).  Throttled behind a saturated disk, the 08:07Z render sat five
# hours in uninterruptible I/O wait (11 CPU-minutes in all) holding its read
# snapshot of the store, so the WAL could not be reset: it grew from its
# 64 MB limit to 14.9 GB, filled the disk to 285 MiB, tripped the resident's
# storage guard, and the fleet produced nothing from 11:49Z.  A reader that
# nothing waits on can still starve everything that writes.  So the render
# gets a deadline: 419 renders to that morning took a median 10.8 min and at
# most 21.5 min on a healthy day; the ones over 25 min were the starved
# ones.  The deadline sits five minutes under the 30-minute StartInterval so
# a run that is killed leaves a gap with no render reading, in which the
# WAL can restart.  A killed render keeps the previous page on disk; the
# next interval is the retry.  Exit 124 is the timeout, as run_with_timeout
# reports it.
#
# 2026-09-24 (user ruling 「能分区分页渲染吗？不要一次性，找生产的空隙」): the
# render is paged.  Each section reads only in a production gap (no cycle in
# flight, WAL under the resident's line), gives its read up if the WAL
# crosses the line, and is kept on disk the moment it is done; the run
# serves anything its budget did not reach from the last page and writes the
# page before this deadline.  It yields to production by schedule now, so it
# no longer runs at Darwin background priority: under ``taskpolicy -b`` on
# the saturated disk the 01:30Z run got 7 CPU-seconds in ten minutes and
# finished no section at all -- it held its reads longer, not shorter.  The
# utility I/O tier still gives way to the resident's default-tier I/O.
RENDER_TIMEOUT_SEC="${PM_DASHBOARD_RENDER_TIMEOUT_SEC:-1500}"
if run_with_timeout "$RENDER_TIMEOUT_SEC" "" \
    taskpolicy -d utility nice -n 10 "$PY" scripts/calibration_dashboard.py --compute-pages; then
  :
else
  rc=$?
  if [ "$rc" = "124" ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] compute killed after ${RENDER_TIMEOUT_SEC}s (timeout); the page is drawn from what it kept"
  else
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] compute failed (exit $rc)"
  fi
fi
draw_page
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

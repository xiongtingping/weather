#!/bin/bash
# The paper ledger's daily pass (user rulings 2026-09-24/25: 「账本对齐」,
# 退役旧账本那套手写裁判; 2026-09-26: 「纸面账本不要阻断生产。失败即跳过。」).
#
# scripts/paper_ledger_aligned.py decides every basket through production's
# own compile entries at production's own decision moments and simulates only
# the venue.  A pass walks every settled event day of the last
# PAPER_LEDGER_WINDOW_DAYS (default 3, ending two days back) that no pass has
# attempted, oldest first, each whole with its own paper account and the
# calibrated maker curves, stored as a run labelled ``daily_sweep``.  A day is
# attempted once: an error, the pass deadline, SIGTERM or a kill skips it with
# its reason and the pass goes on to the next.
#
# Scheduled by its own LaunchAgent (launchd/com.weather.paper_ledger_aligned
# .plist), never by the resident.  From 2026-09-25 the resident hosted it as a
# sweep: a sweeps-turn slot, one of the four its maintenance lanes share, held
# for the hour or more a day takes, and killed by every restart.  The pass
# needs no writer turn -- it reads the SSOT and the archive ``mode=ro`` with
# one short statement at a time and writes data/paper_ledger_aligned.db,
# which no other lane writes -- and between decisions it waits while a
# production cycle is overdue or the SSOT's WAL is past the resident's
# truncate line.  A start handed a writer lease is the resident's: it exits at
# once, so the resident never runs the ledger in its slot again.

set -euo pipefail

if [ -n "${PM_WRITER_LEASE_SEC:-}" ]; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] paper ledger: started by the resident in a writer slot;" \
       "the ledger runs as com.weather.paper_ledger_aligned, not here"
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance paper_ledger_aligned_sweep
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
WINDOW_DAYS="${PAPER_LEDGER_WINDOW_DAYS:-3}"
# The maker fill curves the walk fills its resting orders on (user ruling
# 2026-09-25: 「每周自动（样本外核对过线才换）」): once a week a candidate
# fitted before the last week's event days is scored against the curves in
# service on that week's real orders, and replaces them only when it is no
# worse on both proper scores.  Not due, or a week too thin to decide, is a
# no-op; never fatal -- the curves in service stand.
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] maker fill curves: weekly re-pin"
taskpolicy -d utility nice -n 10 "$PY" scripts/paper_ledger_fill_curves.py --weekly-repin \
  || echo "maker fill curve re-pin failed; the curves in service stand" >&2
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] paper ledger (production entries): the owed settled days of the last ${WINDOW_DAYS}"
# The render's tier (dashboard_render_sweep.sh): utility disk I/O gives way
# to the resident's default-tier reads.  2026-09-25: on a disk saturated by
# page-ins, each of the day's three unthrottled passes coincided with an
# overdue production cycle; not ``-b``, under which a reader on that disk
# holds its reads longer rather than shorter.
taskpolicy -d utility nice -n 10 "$PY" scripts/paper_ledger_aligned.py \
  --daily-pass --window-days "$WINDOW_DAYS" \
  --pass-deadline-sec "${PAPER_LEDGER_PASS_DEADLINE_SEC:-14400}" \
  --capital paper --opening-cash-usdc "${PAPER_LEDGER_OPENING_CASH_USDC:-120}" \
  --progress 0

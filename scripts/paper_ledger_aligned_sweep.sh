#!/bin/bash
# The paper ledger's daily pass (user rulings 2026-09-24/25: 「账本对齐」,
# 退役旧账本那套手写裁判).
#
# scripts/paper_ledger_aligned.py decides every basket through production's
# own compile entries at production's own decision moments and simulates only
# the venue.  Each pass walks ONE settled event day -- the oldest in the last
# PAPER_LEDGER_WINDOW_DAYS (default 3, ending two days back) that no daily pass
# has stored -- whole, with its own paper account and the calibrated maker
# curves, and records it as a run labelled ``daily_sweep``; while days are
# left it prints the resident's continue marker, so a backlog drains a day a
# pass and the steady state is a day a day.  Until 2026-09-25 every pass
# re-walked seven days: ~12,000 decisions at 1-5 s of SSOT reads each against
# a 90-minute slot, killed at 69 minutes with nothing stored (see
# ``daily_pass`` in paper_ledger_aligned.py).  A walk is deterministic -- the
# same day on the same store gives the same book -- so a late or repeated
# pass costs time, never correctness.
#
# Reads the SSOT and the archive ``mode=ro`` with one short statement at a
# time (no read stays open to pin the WAL); writes data/paper_ledger_aligned.db
# only, which no other lane writes.  The decision list is index seeks, a
# station-day's books are held only while the walk can need them
# (``BookStore``) and its older decisions only as the two fields still read
# (``ChainDay.remember``): a one-day walk peaked at 147 MB on 2026-09-25.

set -euo pipefail

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
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] paper ledger (production entries): one settled day of the last ${WINDOW_DAYS}"
# The render's tier (dashboard_render_sweep.sh): utility disk I/O gives way
# to the resident's default-tier reads.  2026-09-25: on a disk saturated by
# page-ins, each of the day's three unthrottled passes coincided with an
# overdue production cycle; not ``-b``, under which a reader on that disk
# holds its reads longer rather than shorter.
taskpolicy -d utility nice -n 10 "$PY" scripts/paper_ledger_aligned.py \
  --daily-pass --window-days "$WINDOW_DAYS" \
  --capital paper --opening-cash-usdc "${PAPER_LEDGER_OPENING_CASH_USDC:-120}" \
  --progress 0

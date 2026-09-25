#!/bin/bash
# The paper ledger's daily pass (user rulings 2026-09-24/25: 「账本对齐」,
# 退役旧账本那套手写裁判).
#
# scripts/paper_ledger_aligned.py decides every basket through production's
# own compile entries at production's own decision moments and simulates only
# the venue.  This pass re-walks the last PAPER_LEDGER_WINDOW_DAYS event days
# (default 7, up to yesterday) with paper capital and the calibrated maker
# curves, and records one run labelled ``daily_sweep``; readers take the
# newest such run.  A run is stateless and deterministic -- the same window
# on the same store gives the same book -- so a late or repeated pass costs
# time, never correctness.
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
WINDOW_DAYS="${PAPER_LEDGER_WINDOW_DAYS:-7}"
FROM="$("$PY" -c "from datetime import date, timedelta; print(date.today() - timedelta(days=int('$WINDOW_DAYS')))")"
TO="$("$PY" -c "from datetime import date, timedelta; print(date.today() - timedelta(days=1))")"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] paper ledger (production entries) ${FROM}..${TO}"
"$PY" scripts/paper_ledger_aligned.py \
  --from-event-date "$FROM" --to-event-date "$TO" \
  --capital paper --opening-cash-usdc "${PAPER_LEDGER_OPENING_CASH_USDC:-120}" \
  --label daily_sweep --progress 0

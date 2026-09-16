#!/bin/bash
# Take the day's reading of how far the paper ledger has drifted from the
# live book, and write it down.
#
# The rule-binding test already refuses a fork in the RULES, and on
# 2026-09-16 it was green while the two sides were buying different legs on
# the same station-day: the rules were shared and applied to different books.
# Nothing was watching that, so the divergence was three weeks old before
# anybody measured it, and there was no way afterwards to date it.  This is
# the instrument for the second failure -- outcomes rather than source.
#
# Cheap by construction: 0.7 seconds measured, three indexed reads over
# tables in the low thousands of rows, and a single small row out.  It does
# write SSOT, so it takes a writer slot, but one insert a day is not what
# contends for the lock -- and it waits (busy_timeout) rather than making
# production wait.
#
# Daily, because the readings aggregate over a fourteen-day window and do not
# move inside a day.  What they DO move with is a rule change: every one
# mints a new selection fingerprint and re-derives the book beneath it, which
# is why the fingerprint rides on every row.  A reading that moved with the
# fingerprint and one that moved on its own are different diagnoses, and the
# row is the only place that distinction survives.
#
# It never exits non-zero on its own finding.  A guard that failed the sweep
# when it found something would be indistinguishable in the logs from a guard
# that crashed, and the finding belongs on the board, not in an exit code.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance ledger_production_alignment_guard_sweep
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] ledger/production alignment guard"
"$PY" scripts/ledger_production_alignment_guard.py --apply

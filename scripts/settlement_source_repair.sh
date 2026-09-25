#!/bin/bash
# Charter 225 (八): the daily settlement-source repair pass (user 2026-09-25,
# 「加一条常规车道：每天把新结算的市场和真值对一遍，不一致就按结算源自动修补」).
#
# The charter 207 reconciliation run with --repair: each disagreement is
# re-settled from the market's own settlement evidence, captured outside any
# lock; each repair is one short BEGIN IMMEDIATE.  Bounded by --max-repairs,
# a free-disk floor and the process memory budget.  The three-hourly pass
# stays a reader.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance settlement_source_repair
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] settlement source repair"
nice -n 10 "$PY" scripts/settlement_truth_reconciliation.py --repair --days 10
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] done"

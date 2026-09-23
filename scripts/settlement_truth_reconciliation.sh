#!/bin/bash
# Charter 207: reconcile our settled buckets against what the market paid,
# every three hours.
#
# A reader, not a writer: Gamma over the network, the SSOT through a read-only
# connection held for a couple of seconds, and two files under data/.  It
# never takes a writer turn and never holds a read long enough to pin the WAL.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance settlement_truth_reconciliation
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] settlement truth reconciliation"
nice -n 10 "$PY" scripts/settlement_truth_reconciliation.py --days 10
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] done"

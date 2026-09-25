#!/bin/bash
# Daily IEM truth audit (user 2026-09-25): every settled station-day of the
# last ten days against IEM's copy of the same METAR/SPECI traffic.
#
# A reader, not a writer: IEM over the network, the SSOT through a read-only
# connection closed before the network work starts, and two files under
# data/.  It never takes a writer turn and never changes truth.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance iem_truth_audit
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] iem truth audit"
nice -n 10 "$PY" scripts/iem_truth_audit.py --days 10
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] done"

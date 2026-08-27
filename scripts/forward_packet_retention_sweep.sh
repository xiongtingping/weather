#!/bin/bash
# Retain each forward packet once, so a past P0 can be recompiled.
#
# Pure file work: it reads the inbox the collector writes and writes into an
# archive directory beside it.  It never touches the SSOT, so it cannot
# contend for the single write lock and cannot be what fail-closes a cycle.
#
# Content-keyed, so a pass over an unchanged inbox writes nothing.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance forward_packet_retention_sweep
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] forward packet retention sweep"
"$PY" scripts/forward_packet_retention_sweep.py

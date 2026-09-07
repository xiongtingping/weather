#!/bin/bash
# Retire market registry row copies past the retention window.
#
# The registry is rewritten whole on every refresh and nothing removed the
# previous copy: 1,176 copies by 2026-09-07, 2.58 GB, 270-320 MB a day, in
# a database 6.1 days from its ceiling.  The copies also cost every read --
# no query scopes to a snapshot, so one timezone lookup scanned 2,233 rows
# for one answer.
#
# Bounded per pass because one writer serves everything here.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance market_registry_retention_sweep
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] market registry retention"
"$PY" scripts/market_registry_retention_sweep.py
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] done"

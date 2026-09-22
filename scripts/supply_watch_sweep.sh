#!/bin/bash
# Charter 197: the supply watch, on the resident's sweep clock.
#
# 2026-09-21 16:04Z the proxy tunnel stopped carrying traffic and every
# upstream failed together; by 21:37Z no D+1 P0 was fresh anywhere, and the
# board stayed green until the morning because every light on it measured a
# lane moving rather than the thing it moves arriving.  This pass measures
# both directly -- one HEAD per upstream through the production proxy, and
# fresh served P0/P0-M per listed future date -- and writes one small JSON.
#
# No database write (SSOT opened mode=ro), so it holds no write domain and
# excludes no writer; seconds per pass, bounded by the 12 s probe timeout.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance supply_watch_sweep
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] supply watch"
"$PY" scripts/supply_watch.py

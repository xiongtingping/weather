#!/bin/bash
# Keep the delta-v2 execution authority servable: the fill batch and the
# policy generations every candidate resolves.
#
# Both carry validity windows the resolvers enforce, and until 2026-09-17
# both were minted with thirty days by scripts nothing ran on a clock: the
# fill batch fitted 08-22 would have ended 09-21 and the policies 09-27 and
# 09-30, each end refusing every candidate at every station.  The rules are
# in bayes_basket_target/delta_v2_authority_clock.py; this is the clock.
#
# Daily, two steps, in dependency order:
#   1. renew any served fill batch inside sixty days of its end (same
#      numbers, new year);
#   2. re-pin and renew the policies (a pin that went stale, or a window
#      inside sixty days of its end).
#
# The weekly refit from the maker shadow book is no longer a step.  The probe
# stopped writing on 2026-09-28 and the book left the machine on 09-30 (cold
# copy and receipt under weather_cold/maker_shadow_retired_20260930), so from
# 10-01 the refit failed every turn on "no such table: maker_shadow_order" and
# the lane read red while the renewal behind it kept the batch served.  The
# probe itself was retired on 2026-10-02 (user ruling 「去掉挂单探针采集」),
# so the book will not grow again: the fitter stays as the served batch's
# lineage and reads the frozen cold copy by hand, never on this clock.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance delta_v2_authority_sweep
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
RC=0

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] delta_v2 fill renewal"
"$PY" scripts/renew_delta_v2_fill_batch.py --commit || RC=$?

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] delta_v2 policy re-pin / renewal"
"$PY" scripts/repin_delta_v2_policy_to_current_fill.py --commit || RC=$?

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] delta_v2 authority sweep done rc=$RC"
exit "$RC"

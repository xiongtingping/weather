#!/bin/bash
# The D0 card's background reading, every ten minutes (user 2026-10-15
# 「后台计算，前端只读，保留历史数据」): bayes_basket_target/d0_readings.py
# writes data/board_readings/d0_latest.json and appends each changed
# station-day reading to data/board_readings/d0_history/<date>.jsonl.
#
# A reader, not a writer: the SSOT and archive read-only (two short
# connections), JAXA/GSMaP files for the hours not cached yet, one Open-Meteo
# call per station-day (lane d0_readings), files under data/ only.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
. "$PROJECT_DIR/scripts/lib/run_with_timeout.sh"
single_instance d0_readings
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] d0 readings"
run_with_timeout 540 "$PROJECT_DIR/data/logs/d0_readings.step.log" nice -n 10 "$PY" -m bayes_basket_target.d0_readings
# The member-trajectory Bayes estimate beside it (user 2026-10-16 「站点看板增加贝叶斯更新的
# 温度预估显示」): read-only, files under data/board_readings only.
run_with_timeout 240 "$PROJECT_DIR/data/logs/member_bayes_readings.step.log" nice -n 10 "$PY" -m bayes_basket_target.member_bayes_readings || true
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] done"

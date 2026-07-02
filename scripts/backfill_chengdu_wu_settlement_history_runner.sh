#!/bin/bash
set -euo pipefail

ROOT_DIR="/Users/xiong/weather"
LOG_DIR="${ROOT_DIR}/data/logs"
mkdir -p "${LOG_DIR}"
cd "${ROOT_DIR}"

REPORT="${PM_CHENGDU_WU_BACKFILL_REPORT:-${ROOT_DIR}/data/reports/backfill_chengdu_settlement_history_runner_latest.json}"
OUT_LOG="${ROOT_DIR}/data/logs/chengdu_wu_backfill.out.log"
RUN_TIMEOUT="${PM_CHENGDU_WU_BACKFILL_TIMEOUT_SEC:-${PM_CHENGDU_WU_BACKFILL_TIMEOUT:-900}}"

if [ -f "${ROOT_DIR}/scripts/lib/load_launchctl_env.sh" ]; then
  . "${ROOT_DIR}/scripts/lib/load_launchctl_env.sh"
  load_repo_env_file "${ROOT_DIR}/.env"
fi

CHENGDU_WU_CITY="chengdu"
CHENGDU_WU_STATION_ID="ZUUU"
CHENGDU_MODEL_HISTORY_LOC_KEY="30.5583,103.9460"
export PM_ARCHIVE_WRITE_SCOPE="chengdu_zuuu"

. "${ROOT_DIR}/scripts/lib/run_with_timeout.sh"
. "${ROOT_DIR}/scripts/lib/resolve_project_python.sh"
. "${ROOT_DIR}/scripts/lib/acquire_scan_lock.sh"

if ! acquire_scan_lock "chengdu_wu_backfill" 120 5; then
  echo "=== Chengdu WU backfill skipped: lock busy at $(date -Iseconds) ===" | tee -a "${OUT_LOG}"
  exit 0
fi

PY="$(resolve_project_python "${ROOT_DIR}" || true)"
if [ -z "${PY}" ] || [ ! -x "${PY}" ]; then
  echo "=== Chengdu WU backfill missing python runtime at $(date -Iseconds) ===" | tee -a "${OUT_LOG}"
  exit 1
fi

RUN_ARGS=(
  "$PY"
  "${ROOT_DIR}/scripts/backfill_chengdu_wu_settlement_history.py"
  "--json"
  "--days" "${PM_CHENGDU_WU_BACKFILL_DAYS:-0}"
  "--out" "${REPORT}"
  "--city" "${CHENGDU_WU_CITY}"
  "--station-id" "${CHENGDU_WU_STATION_ID}"
  "--market-scope" "${PM_CHENGDU_WU_MARKET_SCOPE:-highest_temperature}"
)

if [ "${PM_CHENGDU_WU_STRICT_HISTORY:-1}" = "1" ]; then
  RUN_ARGS+=("--strict-wu-history")
fi

if [ -n "${PM_CHENGDU_WU_INCLUDE_OPEN:-}" ] && [ "${PM_CHENGDU_WU_INCLUDE_OPEN}" = "1" ]; then
  RUN_ARGS+=("--include-open")
fi
if [ -n "${PM_CHENGDU_WU_NO_PREVIEW:-}" ] && [ "${PM_CHENGDU_WU_NO_PREVIEW}" = "1" ]; then
  RUN_ARGS+=("--no-preview")
fi
if [ -n "${PM_CHENGDU_WU_START_DATE:-}" ]; then
  RUN_ARGS+=("--start-date" "${PM_CHENGDU_WU_START_DATE}")
fi
if [ -n "${PM_CHENGDU_WU_END_DATE:-}" ]; then
  RUN_ARGS+=("--end-date" "${PM_CHENGDU_WU_END_DATE}")
fi
if [ "${PM_CHENGDU_WU_ALL_HISTORY:-1}" = "1" ]; then
  RUN_ARGS+=("--all-history")
fi
if [ -n "${PM_CHENGDU_WU_WU_BASE_URL:-}" ]; then
  RUN_ARGS+=("--wu-base-url" "${PM_CHENGDU_WU_WU_BASE_URL}")
fi
if [ -n "${PM_CHENGDU_WU_SCRAPER_MODE:-}" ]; then
  RUN_ARGS+=("--scraper-mode" "${PM_CHENGDU_WU_SCRAPER_MODE}")
fi
if [ -n "${PM_CHENGDU_WU_BACKFILL_MODE:-}" ]; then
  RUN_ARGS+=("--backfill-mode" "${PM_CHENGDU_WU_BACKFILL_MODE}")
fi
if [ -n "${PM_CHENGDU_WU_TIMEOUT:-}" ]; then
  RUN_ARGS+=("--timeout" "${PM_CHENGDU_WU_TIMEOUT}")
elif [ -n "${PM_CHENGDU_WU_BACKFILL_TIMEOUT:-}" ]; then
  RUN_ARGS+=("--timeout" "${PM_CHENGDU_WU_BACKFILL_TIMEOUT}")
fi
if [ "${PM_CHENGDU_WU_APPLY:-1}" = "1" ]; then
  RUN_ARGS+=("--apply")
  RUN_ARGS+=("--confirm-apply" "${PM_CHENGDU_WU_CONFIRM_APPLY:-APPLY_CHENGDU_HISTORY_SETTLEMENTS}")
fi

run_with_timeout "${RUN_TIMEOUT}" "${OUT_LOG}" \
  env PYTHONUNBUFFERED=1 "${RUN_ARGS[@]}"

MODEL_HISTORY_REPORT="${PM_CHENGDU_MODEL_HISTORY_REPORT:-${ROOT_DIR}/data/reports/chengdu_model_history_cache_backfill_latest.json}"
MODEL_HISTORY_ARGS=(
  "$PY"
  "${ROOT_DIR}/scripts/backfill_chengdu_model_history_cache.py"
  "--db" "${ROOT_DIR}/data/archive.db"
  "--out" "${MODEL_HISTORY_REPORT}"
  "--station-id" "${CHENGDU_WU_STATION_ID}"
  "--loc-key" "${CHENGDU_MODEL_HISTORY_LOC_KEY}"
  "--models" "${PM_CHENGDU_MODEL_HISTORY_MODELS:-gfs_seamless,icon_seamless,ecmwf_ifs025}"
  "--days" "${PM_CHENGDU_MODEL_HISTORY_DAYS:-0}"
)
if [ "${PM_CHENGDU_MODEL_HISTORY_APPLY:-1}" = "1" ]; then
  MODEL_HISTORY_ARGS+=("--apply")
  MODEL_HISTORY_ARGS+=("--confirm-apply" "${PM_CHENGDU_MODEL_HISTORY_CONFIRM_APPLY:-APPLY_CHENGDU_MODEL_HISTORY}")
fi
if [ "${PM_CHENGDU_MODEL_HISTORY_FORCE:-0}" = "1" ]; then
  MODEL_HISTORY_ARGS+=("--force")
fi
run_with_timeout "${PM_CHENGDU_MODEL_HISTORY_TIMEOUT_SEC:-300}" "${OUT_LOG}" \
  env PYTHONUNBUFFERED=1 "${MODEL_HISTORY_ARGS[@]}"

BIAS_REPORT="${PM_CHENGDU_WU_BIAS_REPORT:-${ROOT_DIR}/data/reports/chengdu_wu_model_bias_correction_latest.json}"
BIAS_ARGS=(
  "$PY"
  "${ROOT_DIR}/scripts/chengdu_wu_model_bias_correction.py"
  "--db" "${ROOT_DIR}/data/archive.db"
  "--out" "${BIAS_REPORT}"
  "--station-id" "${CHENGDU_WU_STATION_ID}"
  "--days" "${PM_CHENGDU_WU_BIAS_DAYS:-0}"
)
run_with_timeout "${PM_CHENGDU_WU_BIAS_TIMEOUT_SEC:-120}" "${OUT_LOG}" \
  env PYTHONUNBUFFERED=1 "${BIAS_ARGS[@]}"

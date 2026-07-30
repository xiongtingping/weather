#!/bin/bash
set -euo pipefail

ROOT="${AMSC_POLL_PROJECT_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
PYTHON_BIN="${PYTHON:-python3}"
OUT_DIR="${AMSC_ZUUU_OUT_DIR:-$ROOT/data/amsc_zuuu_metar_taf}"
RAW_ARCHIVE_DIR="${AMSC_ZUUU_RAW_ARCHIVE_DIR:-$ROOT/data/amsc_raw_history}"
PLAN_PATH="${AMSC_ZUUU_PLAN_PATH:-$OUT_DIR/write_plan_latest.json}"
STATUS_PATH="${AMSC_ZUUU_STATUS_PATH:-$OUT_DIR/status_latest.json}"
APPLY_RESULT_PATH="${AMSC_ZUUU_APPLY_RESULT_PATH:-$OUT_DIR/apply_result_latest.json}"
# The source observation commit is deliberately recorded separately from the
# later posterior-task hand-off. A weak-source outage must never erase, delay,
# or make ambiguous the durable hard-observation fact.
FACT_APPLY_RESULT_PATH="${AMSC_ZUUU_FACT_APPLY_RESULT_PATH:-$OUT_DIR/fact_apply_result_latest.json}"
REPORT_COUNT="${AMSC_ZUUU_REPORT_COUNT:-${AMSC_ZUUU_COUNT:-48}}"
TIMEOUT="${AMSC_ZUUU_TIMEOUT:-12}"
RELEASE_DELAY_SEC="${AMSC_ZUUU_RELEASE_DELAY_SEC:-${AMSC_ZUUU_DELAY_SEC:-30}}"
POLL_ATTEMPTS="${AMSC_ZUUU_POLL_ATTEMPTS:-40}"
POLL_INTERVAL_SEC="${AMSC_ZUUU_POLL_INTERVAL_SEC:-1}"
STOP_ON_APPLIED="${AMSC_ZUUU_STOP_ON_APPLIED:-1}"
APPLY_ENABLED="${AMSC_ZUUU_APPLY:-0}"
APPLY_DB="${AMSC_ZUUU_DB:-$ROOT/data/archive.db}"
APPLY_CONFIRM="${AMSC_ZUUU_APPLY_CONFIRM:-}"
OUTBOX_DIR="${AMSC_ZUUU_OUTBOX_DIR:-$OUT_DIR/outbox}"
PENDING_DIR="${AMSC_ZUUU_PENDING_DIR:-$OUT_DIR/pending_apply}"
# Standard applies are recovery/hand-off work. They must never be reused by
# Transaction A: a generic writer-queue wait can be tens of seconds.
APPLY_RETRIES="${AMSC_ZUUU_APPLY_RETRIES:-8}"
APPLY_RETRY_INTERVAL_SEC="${AMSC_ZUUU_APPLY_RETRY_INTERVAL_SEC:-2}"
# Transaction A gets exactly one bounded writer attempt. On contention it
# persists immutable evidence and returns; it never spends the 3-second SLO
# in repeated SQLite waits.
SOURCE_FACT_FAST_QUEUE_WAIT_SEC="${AMSC_ZUUU_SOURCE_FACT_FAST_QUEUE_WAIT_SEC:-0.20}"
SOURCE_FACT_FAST_DB_TIMEOUT_SEC="${AMSC_ZUUU_SOURCE_FACT_FAST_DB_TIMEOUT_SEC:-0.20}"
SOURCE_FACT_FAST_DB_BUSY_TIMEOUT_MS="${AMSC_ZUUU_SOURCE_FACT_FAST_DB_BUSY_TIMEOUT_MS:-100}"
# Deferred source facts are recovery evidence only. Each poll gives them a
# small bounded opportunity after the fresh source path, so recovery cannot
# starve the next scheduled METAR slot.
PENDING_RECOVERY_QUEUE_WAIT_SEC="${AMSC_ZUUU_PENDING_RECOVERY_QUEUE_WAIT_SEC:-1}"
PENDING_RECOVERY_DB_TIMEOUT_SEC="${AMSC_ZUUU_PENDING_RECOVERY_DB_TIMEOUT_SEC:-1}"
PENDING_RECOVERY_DB_BUSY_TIMEOUT_MS="${AMSC_ZUUU_PENDING_RECOVERY_DB_BUSY_TIMEOUT_MS:-500}"
PENDING_RECOVERY_RETRIES="${AMSC_ZUUU_PENDING_RECOVERY_RETRIES:-2}"
PENDING_RECOVERY_RETRY_INTERVAL_SEC="${AMSC_ZUUU_PENDING_RECOVERY_RETRY_INTERVAL_SEC:-1}"
PENDING_DRAIN_LIMIT="${AMSC_ZUUU_PENDING_DRAIN_LIMIT:-3}"
LOCK_DIR="$ROOT/data/amsc_zuuu_metar_taf.poll.lock"
# The selected mode is copied into every queued posterior task.  Do not turn
# this into a worker-only environment flag: source acquisition and posterior
# materialization must agree on the same immutable context policy.
OFFICIAL_NWP_CONTEXT_MODE="${AMSC_ZUUU_OFFICIAL_NWP_CONTEXT_MODE:-prewarmed_cache_only_v1}"
OFFICIAL_NWP_GENERATION_REF_PATH="${AMSC_ZUUU_OFFICIAL_NWP_GENERATION_REF_PATH:-$OUT_DIR/official_nwp_generation_ref.json}"
# Canonical capture and sequential Bayes are the production probability fast
# lane.  They run from the frozen fetch plan/raw archive before the legacy
# fact projection, which is downstream compatibility/diagnostic state only.
P2_CANONICAL_APPEND_ENABLED="${AMSC_ZUUU_P2_CANONICAL_APPEND_ENABLED:-1}"
P2_CANONICAL_RESULT_PATH="${AMSC_ZUUU_P2_CANONICAL_RESULT_PATH:-$OUT_DIR/p2_canonical_result_latest.json}"
P2_CANONICAL_CONFIRM="${AMSC_ZUUU_P2_CANONICAL_CONFIRM:-APPEND_STRICT_AMSC_P2_CANONICAL}"
P2_SHADOW_MIRROR_ENABLED="${AMSC_ZUUU_P2_SHADOW_MIRROR_ENABLED:-1}"
P2_SHADOW_DB="${AMSC_ZUUU_P2_SHADOW_DB:-$ROOT/data/shadow/p2_canonical_amsc_shadow.db}"
P2_SHADOW_RESULT_PATH="${AMSC_ZUUU_P2_SHADOW_RESULT_PATH:-$OUT_DIR/p2_shadow_mirror_result_latest.json}"
P2_SHADOW_CONFIRM="${AMSC_ZUUU_P2_SHADOW_CONFIRM:-INITIALIZE_AND_MIRROR_P2_ISOLATED_SHADOW}"
P2_SIDECAR_LOCK_DIR="${AMSC_ZUUU_P2_SIDECAR_LOCK_DIR:-$OUT_DIR/p2_canonical_sidecar.lock}"
P2_SIDECAR_LOG_PATH="${AMSC_ZUUU_P2_SIDECAR_LOG_PATH:-$ROOT/data/logs/amsc_zuuu_p2_canonical_sidecar.log}"
P2_PENDING_PROBABILITY_DIR="${AMSC_ZUUU_P2_PENDING_PROBABILITY_DIR:-$OUT_DIR/pending_probability}"
P2_PENDING_PROBABILITY_DRAIN_LIMIT="${AMSC_ZUUU_P2_PENDING_PROBABILITY_DRAIN_LIMIT:-3}"
PRODUCTION_PROBABILITY_UPDATE_ENABLED="${AMSC_ZUUU_PRODUCTION_PROBABILITY_UPDATE_ENABLED:-1}"
PRODUCTION_PROBABILITY_RESULT_PATH="${AMSC_ZUUU_PRODUCTION_PROBABILITY_RESULT_PATH:-$OUT_DIR/production_probability_result_latest.json}"
PRODUCTION_PROBABILITY_ACTIVATION_PATH="${AMSC_ZUUU_PRODUCTION_PROBABILITY_ACTIVATION_PATH:-$ROOT/config/production_probability_runtime_activation.json}"
TAF_FORECAST_REVISION_RESULT_PATH="${AMSC_ZUUU_TAF_FORECAST_REVISION_RESULT_PATH:-$OUT_DIR/taf_forecast_revision_result_latest.json}"
mkdir -p "$OUT_DIR" "$OUTBOX_DIR" "$PENDING_DIR" "$P2_PENDING_PROBABILITY_DIR" "$RAW_ARCHIVE_DIR" "$ROOT/data/logs"

if ! [[ "$REPORT_COUNT" =~ ^[0-9]+$ ]] || [ "$REPORT_COUNT" -le 0 ]; then
  echo "[amsc_zuuu] invalid report count=$REPORT_COUNT; using 48"
  REPORT_COUNT="48"
fi

if ! [[ "$POLL_ATTEMPTS" =~ ^[0-9]+$ ]] || [ "$POLL_ATTEMPTS" -le 0 ]; then
  echo "[amsc_zuuu] invalid AMSC_ZUUU_POLL_ATTEMPTS=$POLL_ATTEMPTS; using 40"
  POLL_ATTEMPTS="40"
fi

if ! [[ "$RELEASE_DELAY_SEC" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "[amsc_zuuu] invalid AMSC_ZUUU_RELEASE_DELAY_SEC=$RELEASE_DELAY_SEC; using 30"
  RELEASE_DELAY_SEC="30"
fi

if ! [[ "$POLL_INTERVAL_SEC" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "[amsc_zuuu] invalid AMSC_ZUUU_POLL_INTERVAL_SEC=$POLL_INTERVAL_SEC; using 1"
  POLL_INTERVAL_SEC="1"
fi

if ! [[ "$APPLY_RETRIES" =~ ^[0-9]+$ ]]; then
  echo "[amsc_zuuu] invalid AMSC_ZUUU_APPLY_RETRIES=$APPLY_RETRIES; using 8"
  APPLY_RETRIES="8"
fi

if ! [[ "$APPLY_RETRY_INTERVAL_SEC" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "[amsc_zuuu] invalid AMSC_ZUUU_APPLY_RETRY_INTERVAL_SEC=$APPLY_RETRY_INTERVAL_SEC; using 2"
  APPLY_RETRY_INTERVAL_SEC="2"
fi

for budget_value in SOURCE_FACT_FAST_QUEUE_WAIT_SEC SOURCE_FACT_FAST_DB_TIMEOUT_SEC PENDING_RECOVERY_QUEUE_WAIT_SEC PENDING_RECOVERY_DB_TIMEOUT_SEC PENDING_RECOVERY_RETRY_INTERVAL_SEC; do
  if ! [[ "${!budget_value}" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    echo "[amsc_zuuu] invalid ${budget_value}=${!budget_value}" >&2
    exit 2
  fi
done
for budget_value in SOURCE_FACT_FAST_DB_BUSY_TIMEOUT_MS PENDING_RECOVERY_DB_BUSY_TIMEOUT_MS PENDING_RECOVERY_RETRIES; do
  if ! [[ "${!budget_value}" =~ ^[0-9]+$ ]]; then
    echo "[amsc_zuuu] invalid ${budget_value}=${!budget_value}" >&2
    exit 2
  fi
done

if ! [[ "$PENDING_DRAIN_LIMIT" =~ ^[0-9]+$ ]]; then
  echo "[amsc_zuuu] invalid AMSC_ZUUU_PENDING_DRAIN_LIMIT=$PENDING_DRAIN_LIMIT; using 3"
  PENDING_DRAIN_LIMIT="3"
fi
if ! [[ "$P2_PENDING_PROBABILITY_DRAIN_LIMIT" =~ ^[0-9]+$ ]]; then
  echo "[amsc_zuuu] invalid AMSC_ZUUU_P2_PENDING_PROBABILITY_DRAIN_LIMIT=$P2_PENDING_PROBABILITY_DRAIN_LIMIT; using 3"
  P2_PENDING_PROBABILITY_DRAIN_LIMIT="3"
fi

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "[amsc_zuuu] skip: previous poll still running"
  exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

# Every :00/:30 boundary triggers acquisition.  The frozen 44-slot schedule
# controls only whether a matching regular METAR may enter the strong lane;
# it must never suppress TAF/TAF AMD/TAF COR discovery.
schedule_attempt_at="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
schedule_state="$($PYTHON_BIN - "$ROOT" "$schedule_attempt_at" <<'PY'
import json
import sys

root = sys.argv[1]
if root not in sys.path:
    sys.path.insert(0, root)
from amsc_poll_slot_runtime import assess_zuuu_regular_metar_schedule

print(json.dumps(assess_zuuu_regular_metar_schedule(sys.argv[2]), sort_keys=True))
PY
)"
POLL_SLOT_UTC="$(printf '%s' "$schedule_state" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin).get("expected_slot_at") or "")')"
scheduled_regular_slot="$(printf '%s' "$schedule_state" | "$PYTHON_BIN" -c 'import json,sys; print("true" if json.load(sys.stdin).get("scheduled_regular_metar_slot") else "false")')"
# A process can remain alive across several slots while a previous SQLite
# apply is blocked.  Freshness is therefore evaluated against each actual
# fetch attempt, never against the launcher process start time.
plan_matches_poll_slot() {
  local plan_path="$1"
  local attempt_started_at="$2"
  "$PYTHON_BIN" "$ROOT/amsc_poll_slot_runtime.py" \
    --plan "$plan_path" \
    --attempt-started-at "$attempt_started_at"
}

write_slot_stale_status() {
  local plan_path="$1"
  local status_path="$2"
  local attempt="$3"
  local expected_slot_at="$4"
  local retry_reason="$5"
  "$PYTHON_BIN" - "$plan_path" "$status_path" "$expected_slot_at" "$retry_reason" "$attempt" "$POLL_ATTEMPTS" "$REPORT_COUNT" "$POLL_INTERVAL_SEC" "$RELEASE_DELAY_SEC" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

plan_path = Path(sys.argv[1])
status_path = Path(sys.argv[2])
expected_slot_at = sys.argv[3]
retry_reason = sys.argv[4]
attempt = int(sys.argv[5])
attempts = int(sys.argv[6])
report_count = int(sys.argv[7])
poll_interval_sec = sys.argv[8]
release_delay_sec = sys.argv[9]
plan = json.loads(plan_path.read_text(encoding="utf-8"))
features = (plan.get("effective_payload") or {}).get("weather_likelihood_features") or {}
status = {
    "schema": "amsc_zuuu_metar_taf_poll_status_v1",
    "ok": False,
    "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "attempt": attempt,
    "attempts": attempts,
    "report_count": report_count,
    "poll_interval_sec": poll_interval_sec,
    "release_delay_sec": release_delay_sec,
    "source_slot_fresh": False,
    "source_slot_expected_at": expected_slot_at,
    "retry_reason": retry_reason,
    "latest_metar_at": features.get("zuuu_metar_raw_amsc_latest_at"),
    "latest_metar_local": features.get("zuuu_metar_raw_amsc_latest_local"),
    "latest_metar_temp_c": features.get("zuuu_metar_raw_amsc_latest_temp_c"),
    "plan_path": str(plan_path),
    "apply_enabled": False,
    "applied": False,
    "apply_pending": False,
    "skipped": True,
    "skip_reason": retry_reason,
}
status_path.parent.mkdir(parents=True, exist_ok=True)
tmp = status_path.with_name(f".{status_path.name}.tmp.{os.getpid()}")
tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.replace(tmp, status_path)
PY
}

persist_plan_artifacts() {
  local plan_path="$1"
  local attempt="$2"
  "$PYTHON_BIN" - "$plan_path" "$OUTBOX_DIR" "$PENDING_DIR" "$attempt" <<'PY'
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

plan_path = Path(sys.argv[1])
outbox_dir = Path(sys.argv[2])
pending_dir = Path(sys.argv[3])
attempt = str(sys.argv[4])
plan = json.loads(plan_path.read_text(encoding="utf-8"))
features = (plan.get("effective_payload") or {}).get("weather_likelihood_features") or {}
event_date = str(plan.get("event_date") or "unknown-date")
latest_at = str(features.get("zuuu_metar_raw_amsc_latest_at") or plan.get("fact_as_of") or "")
latest_local = str(features.get("zuuu_metar_raw_amsc_latest_local") or "")
latest_raw = str(features.get("zuuu_metar_raw_amsc_latest_raw") or "")
taf_raw = str(features.get("taf_forecast_peak_raw") or "")
signature = "|".join([event_date, latest_at, latest_local, latest_raw, taf_raw])
digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:20]
safe_latest = re.sub(r"[^0-9A-Za-z]+", "", latest_at or datetime.now(timezone.utc).isoformat())
stem = f"{event_date}_{safe_latest}_{digest}"
outbox_dir.mkdir(parents=True, exist_ok=True)
pending_dir.mkdir(parents=True, exist_ok=True)
outbox_path = outbox_dir / f"{stem}.json"
pending_path = pending_dir / f"{stem}.json"
payload = json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
if not outbox_path.exists():
    tmp = outbox_path.with_name(f".{outbox_path.name}.tmp.{os.getpid()}")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, outbox_path)
print(json.dumps({
    "outbox_path": str(outbox_path),
    "pending_path": str(pending_path),
    "signature_digest": digest,
    "latest_metar_at": latest_at,
    "attempt": attempt,
}, ensure_ascii=True, separators=(",", ":")))
PY
}

queue_pending_probability_plan() {
  local plan_path="$1"
  local result_path="$2"
  "$PYTHON_BIN" "$ROOT/scripts/amsc_probability_pending_queue.py" enqueue \
    --plan "$plan_path" \
    --result "$result_path" \
    --pending-dir "$P2_PENDING_PROBABILITY_DIR"
}

is_apply_sqlite_locked() {
  local result_path="$1"
  [ -f "$result_path" ] || return 1
  "$PYTHON_BIN" - "$result_path" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(1)
summary = payload.get("writer_apply_result_summary") or {}
needles = [
    payload.get("block_reason"),
    payload.get("error"),
    payload.get("write_error"),
    summary.get("block_reason"),
    summary.get("error"),
]
text = " ".join(str(x or "") for x in needles).lower()
if (
    "sqlite_error" in text
    or "database is locked" in text
    or "database table is locked" in text
    or "archive_db_write_queue_timeout" in text
    or "archive_db_fast_lane_deferred" in text
    or "db_writer.user.lock" in text
):
    raise SystemExit(0)
raise SystemExit(1)
PY
}

write_missing_apply_result() {
  local result_path="$1"
  local rc="$2"
  local label="$3"
  local output_path="${4:-}"
  "$PYTHON_BIN" - "$result_path" "$rc" "$label" "$output_path" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

result_path = Path(sys.argv[1])
rc = int(sys.argv[2])
label = sys.argv[3]
output_path = Path(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4] else None
output_tail = ""
if output_path and output_path.exists():
    try:
        output_tail = output_path.read_text(encoding="utf-8", errors="replace")[-6000:]
    except Exception:
        output_tail = ""
summary_error = "apply_result_missing_after_apply_failure"
if output_tail:
    summary_error = f"{summary_error}: {output_tail}"
payload = {
    "schema": "amsc_zuuu_apply_result_missing_after_failure_v1",
    "ok": False,
    "applied": False,
    "skipped": False,
    "error": "apply_result_missing_after_apply_failure",
    "apply_output_tail": output_tail,
    "apply_label": label,
    "apply_returncode": rc,
    "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "writer_apply_result_summary": {
        "ok": False,
        "error": summary_error,
        "returncode": rc,
    },
}
result_path.parent.mkdir(parents=True, exist_ok=True)
tmp = result_path.with_name(f".{result_path.name}.tmp.{os.getpid()}")
tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.replace(tmp, result_path)
PY
}

queue_pending_apply() {
  local plan_path="$1"
  local pending_path="$2"
  local result_path="$3"
  "$PYTHON_BIN" - "$plan_path" "$pending_path" "$result_path" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

plan_path = Path(sys.argv[1])
pending_path = Path(sys.argv[2])
result_path = Path(sys.argv[3])
pending_path.parent.mkdir(parents=True, exist_ok=True)
plan_payload = plan_path.read_text(encoding="utf-8")
plan = json.loads(plan_payload)
if not pending_path.exists():
    tmp = pending_path.with_name(f".{pending_path.name}.tmp.{os.getpid()}")
    tmp.write_text(plan_payload, encoding="utf-8")
    os.replace(tmp, pending_path)
try:
    result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
except Exception:
    result = {}
result.update({
    "apply_pending": True,
    "apply_pending_reason": "archive_db_fast_lane_deferred",
    "pending_plan_path": str(pending_path),
    "pending_queued_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "fetch_accepted": True,
    "fetch_acceptance_state": "fetch_ok_apply_pending",
})
receipt_path = pending_path.with_name(f"{pending_path.name}.fast_lane_deferred.json")
if not receipt_path.exists():
    features = ((plan.get("effective_payload") or {}).get("weather_likelihood_features") or {})
    receipt = {
        "schema": "amsc_source_fact_fast_lane_deferral_v1",
        "event_date": plan.get("event_date") or "",
        "slug": plan.get("slug") or "",
        "fact_as_of": plan.get("fact_as_of") or "",
        "metar_at": features.get("zuuu_metar_raw_amsc_latest_at") or "",
        "deferred_at": result["pending_queued_at"],
        "reason": "archive_db_fast_lane_deferred",
        "source_fact_t1_deadline_seconds": 3.0,
        "apply_result": {
            "block_reason": result.get("block_reason") or "",
            "error": result.get("error") or "",
            "archive_write_budget": result.get("archive_write_budget") or {},
        },
        "pending_plan_path": str(pending_path),
        "recovery_policy": "append_fact_only_no_late_posterior_handoff",
    }
    receipt_tmp = receipt_path.with_name(f".{receipt_path.name}.tmp.{os.getpid()}")
    receipt_tmp.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(receipt_tmp, receipt_path)
result["fast_lane_deferral_receipt_path"] = str(receipt_path)
tmp = result_path.with_name(f".{result_path.name}.tmp.{os.getpid()}")
tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.replace(tmp, result_path)
print(str(pending_path))
PY
  if [ -f "$OFFICIAL_NWP_GENERATION_REF_PATH" ] && [ ! -f "${pending_path}.generation_ref.json" ]; then
    cp "$OFFICIAL_NWP_GENERATION_REF_PATH" "${pending_path}.generation_ref.json"
  fi
}

apply_plan_with_short_retry() {
  local plan_path="$1"
  local result_path="$2"
  local label="$3"
  local generation_ref_path="${4:-$OFFICIAL_NWP_GENERATION_REF_PATH}"
  local no_enqueue_posterior="${5:-0}"
  local write_profile="${6:-standard}"
  local apply_try=1
  local apply_max=$((APPLY_RETRIES + 1))
  local retry_interval_sec="$APPLY_RETRY_INTERVAL_SEC"
  local rc=0
  local attempt_output="${result_path}.attempt.log"
  local generation_ref_json=""
  if [ -f "$generation_ref_path" ]; then
    generation_ref_json="$(<"$generation_ref_path")"
  fi
  local -a enqueue_args=()
  if [ "$no_enqueue_posterior" = "1" ]; then
    enqueue_args+=(--no-enqueue-posterior)
  fi
  local -a write_budget_args=()
  case "$write_profile" in
    source_fact_fast_lane)
      apply_max=1
      write_budget_args+=(
        --fast-lane
        --archive-write-queue-wait-sec "$SOURCE_FACT_FAST_QUEUE_WAIT_SEC"
        --db-timeout-sec "$SOURCE_FACT_FAST_DB_TIMEOUT_SEC"
        --db-busy-timeout-ms "$SOURCE_FACT_FAST_DB_BUSY_TIMEOUT_MS"
      )
      ;;
    pending_recovery)
      apply_max=$((PENDING_RECOVERY_RETRIES + 1))
      retry_interval_sec="$PENDING_RECOVERY_RETRY_INTERVAL_SEC"
      write_budget_args+=(
        --archive-write-queue-wait-sec "$PENDING_RECOVERY_QUEUE_WAIT_SEC"
        --db-timeout-sec "$PENDING_RECOVERY_DB_TIMEOUT_SEC"
        --db-busy-timeout-ms "$PENDING_RECOVERY_DB_BUSY_TIMEOUT_MS"
      )
      ;;
    standard)
      ;;
    *)
      echo "[amsc_zuuu] invalid apply write profile=${write_profile}" >&2
      return 2
      ;;
  esac
  rm -f "$result_path"
  while [ "$apply_try" -le "$apply_max" ]; do
    rm -f "$attempt_output"
    # Bash with `set -u` treats an empty declared array expansion as unbound
    # on some system shells.  The normal posterior hand-off intentionally has
    # no optional --no-enqueue-posterior argument, so expand it with nounset
    # temporarily disabled rather than turning an empty argument into a
    # malformed CLI token.
    set +u
    if "$PYTHON_BIN" "$ROOT/scripts/apply_amsc_zuuu_fact_snapshot_plan.py" \
      --plan "$plan_path" \
      --db "$APPLY_DB" \
      --apply \
      --confirm "$APPLY_CONFIRM" \
      --posterior-official-nwp-context-mode "$OFFICIAL_NWP_CONTEXT_MODE" \
      --posterior-official-nwp-generation-ref-json "$generation_ref_json" \
      "${enqueue_args[@]}" \
      "${write_budget_args[@]}" \
      --result-json "$result_path" >"$attempt_output" 2>&1; then
      set -u
      cat "$attempt_output"
      rm -f "$attempt_output"
      return 0
    fi
    set -u
    rc=$?
    cat "$attempt_output" >&2 || true
    if [ ! -f "$result_path" ]; then
      write_missing_apply_result "$result_path" "$rc" "$label" "$attempt_output"
    fi
    rm -f "$attempt_output"
    if ! is_apply_sqlite_locked "$result_path"; then
      return "$rc"
    fi
    if [ "$apply_try" -lt "$apply_max" ]; then
      echo "[amsc_zuuu] apply ${label} sqlite locked; retry ${apply_try}/$((apply_max - 1)) after ${retry_interval_sec}s"
      sleep "$retry_interval_sec"
    fi
    apply_try=$((apply_try + 1))
  done
  return "$rc"
}

run_probability_fast_lane_from_plan() {
  [ "$P2_CANONICAL_APPEND_ENABLED" = "1" ] || return 0
  [ "$PRODUCTION_PROBABILITY_UPDATE_ENABLED" = "1" ] || return 0
  local plan_path="${1:-$PLAN_PATH}"
  local result_path="${2:-$P2_CANONICAL_RESULT_PATH}"
  local p2_event_date=""
  p2_event_date="$($PYTHON_BIN - "$plan_path" <<'PY'
import json
import sys
from pathlib import Path

try:
    plan = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except Exception:
    plan = {}
print(str(plan.get("event_date") or "")[:10])
PY
)"
  if [ -z "$p2_event_date" ]; then
    echo "[amsc_zuuu] probability fast lane skipped: event date missing from fetch plan" >&2
    return 0
  fi
  if (
    probability_fast_lane_lock_pid_path="$P2_SIDECAR_LOCK_DIR/owner_pid"
    probability_fast_lane_lock_owner_pid="${BASHPID:-$$}"
    release_probability_fast_lane_lock() {
      local recorded_pid=""
      if [ -f "$probability_fast_lane_lock_pid_path" ]; then
        recorded_pid="$(tr -dc '0-9' <"$probability_fast_lane_lock_pid_path" 2>/dev/null || true)"
      fi
      if [ "$recorded_pid" = "$probability_fast_lane_lock_owner_pid" ]; then
        rm -f "$probability_fast_lane_lock_pid_path"
        rmdir "$P2_SIDECAR_LOCK_DIR" 2>/dev/null || true
      fi
    }
    acquire_probability_fast_lane_lock() {
      local owner_pid=""
      local active_probability_runner=""
      local stale_lock_dir=""
      if mkdir "$P2_SIDECAR_LOCK_DIR" 2>/dev/null; then
        printf '%s\n' "$probability_fast_lane_lock_owner_pid" >"$probability_fast_lane_lock_pid_path"
        return 0
      fi

      # A lock is valid only while its recorded owner is alive. Older builds
      # wrote no owner metadata, so also protect any currently running fast
      # lane process before recovering an ownerless directory.
      if [ ! -f "$probability_fast_lane_lock_pid_path" ]; then
        sleep 0.10
      fi
      if [ -f "$probability_fast_lane_lock_pid_path" ]; then
        owner_pid="$(tr -dc '0-9' <"$probability_fast_lane_lock_pid_path" 2>/dev/null || true)"
      fi
      if [ -n "$owner_pid" ] && kill -0 "$owner_pid" 2>/dev/null; then
        return 1
      fi
      active_probability_runner="$(pgrep -f "$ROOT/scripts/apply_amsc_probability_fast_lane_v6_from_plan.py" 2>/dev/null | head -1 || true)"
      if [ -n "$active_probability_runner" ]; then
        return 1
      fi

      stale_lock_dir="${P2_SIDECAR_LOCK_DIR}.stale.${probability_fast_lane_lock_owner_pid}"
      if ! mv "$P2_SIDECAR_LOCK_DIR" "$stale_lock_dir" 2>/dev/null; then
        return 1
      fi
      rm -f "$stale_lock_dir/owner_pid"
      if ! rmdir "$stale_lock_dir" 2>/dev/null; then
        echo "[amsc_zuuu] probability fast lane blocked: stale lock contains unexpected files path=${stale_lock_dir}" >&2
        return 1
      fi
      if ! mkdir "$P2_SIDECAR_LOCK_DIR" 2>/dev/null; then
        return 1
      fi
      printf '%s\n' "$probability_fast_lane_lock_owner_pid" >"$probability_fast_lane_lock_pid_path"
      echo "[amsc_zuuu] probability fast lane recovered stale lock event_date=${p2_event_date}"
      return 0
    }

    if ! acquire_probability_fast_lane_lock; then
      echo "[amsc_zuuu] probability fast lane deferred: previous runner still active event_date=${p2_event_date}"
      exit 0
    fi
    trap 'release_probability_fast_lane_lock' EXIT
    result_tmp="${result_path}.tmp.${probability_fast_lane_lock_owner_pid}"
    rm -f "$result_tmp"
    if "$PYTHON_BIN" "$ROOT/scripts/apply_amsc_probability_fast_lane_v6_from_plan.py" \
      --plan "$plan_path" \
      --db "$APPLY_DB" \
      --raw-archive-dir "$RAW_ARCHIVE_DIR" \
      --activation "$PRODUCTION_PROBABILITY_ACTIVATION_PATH" \
      --apply \
      --confirm APPLY_AMSC_PROBABILITY_FAST_LANE_V6 \
      >"$result_tmp" 2>&1; then
      mv "$result_tmp" "$result_path"
      echo "[amsc_zuuu] probability fast lane committed event_date=${p2_event_date} result=${result_path}"
    else
      probability_fast_lane_rc=$?
      mv "$result_tmp" "$result_path"
      pending_probability_path="$(queue_pending_probability_plan "$plan_path" "$result_path")"
      echo "[amsc_zuuu] warning: probability fast lane deferred; raw archive retained pending=${pending_probability_path} result=${result_path}" >&2
      exit "$probability_fast_lane_rc"
    fi

    # Shadow mirroring is explicitly downstream of T3 and can never consume
    # the live latency budget.
    if [ "$P2_SHADOW_MIRROR_ENABLED" = "1" ]; then
      p2_shadow_result_tmp="${P2_SHADOW_RESULT_PATH}.tmp.${probability_fast_lane_lock_owner_pid}"
      rm -f "$p2_shadow_result_tmp"
      local -a p2_shadow_args=(
        --source-db "$APPLY_DB"
        --shadow-db "$P2_SHADOW_DB"
        --raw-archive-dir "$RAW_ARCHIVE_DIR"
        --event-date "$p2_event_date"
        --apply
        --confirm "$P2_SHADOW_CONFIRM"
      )
      if [ ! -f "$P2_SHADOW_DB" ]; then
        p2_shadow_args+=(--initialize-isolated-shadow)
      fi
      if "$PYTHON_BIN" "$ROOT/scripts/mirror_p2_canonical_to_isolated_shadow.py" \
        "${p2_shadow_args[@]}" >"$p2_shadow_result_tmp" 2>&1; then
        mv "$p2_shadow_result_tmp" "$P2_SHADOW_RESULT_PATH"
        echo "[amsc_zuuu] P2 shadow mirror completed event_date=${p2_event_date} result=${P2_SHADOW_RESULT_PATH}"
      else
        mv "$p2_shadow_result_tmp" "$P2_SHADOW_RESULT_PATH"
        echo "[amsc_zuuu] warning: P2 shadow mirror blocked; production fast lane remains durable result=${P2_SHADOW_RESULT_PATH}" >&2
      fi
    fi
  ) >>"$P2_SIDECAR_LOG_PATH" 2>&1; then
    echo "[amsc_zuuu] probability fast lane completed event_date=${p2_event_date} log=${P2_SIDECAR_LOG_PATH}"
    return 0
  else
    echo "[amsc_zuuu] probability fast lane runner failed event_date=${p2_event_date} log=${P2_SIDECAR_LOG_PATH}" >&2
    return 1
  fi
}

drain_pending_probability_queue() {
  [ "$APPLY_ENABLED" = "1" ] || return 0
  [ "$P2_CANONICAL_APPEND_ENABLED" = "1" ] || return 0
  [ "$PRODUCTION_PROBABILITY_UPDATE_ENABLED" = "1" ] || return 0
  [ "$P2_PENDING_PROBABILITY_DRAIN_LIMIT" -gt 0 ] || return 0
  [ -d "$P2_PENDING_PROBABILITY_DIR" ] || return 0
  local current_event_date=""
  current_event_date="$(TZ=Asia/Shanghai date +%F)"
  # Recovery runs after the fresh event's T3.  First remove only plans whose
  # exact canonical event is already present in the active checkpoint
  # sequence; never replay an observation merely because a stale filesystem
  # receipt survived a manifest epoch refresh.
  if ! "$PYTHON_BIN" "$ROOT/scripts/amsc_probability_pending_queue.py" archive-consumed \
    --pending-dir "$P2_PENDING_PROBABILITY_DIR" \
    --db "$APPLY_DB" \
    --activation "$PRODUCTION_PROBABILITY_ACTIVATION_PATH" \
    --event-date "$current_event_date" \
    --apply \
    --confirm ARCHIVE_CONSUMED_AMSC_PROBABILITY_PLANS; then
    echo "[amsc_zuuu] pending consumed-plan archive unavailable; retain plans for bounded replay" >&2
  fi
  "$PYTHON_BIN" "$ROOT/scripts/amsc_probability_pending_queue.py" compact \
    --pending-dir "$P2_PENDING_PROBABILITY_DIR" \
    --apply \
    --confirm COMPACT_AMSC_PROBABILITY_PENDING_QUEUE
  local drained=0
  local pending_path=""
  local replay_result=""
  local replay_rc=0
  local expiry_state=""
  while IFS= read -r pending_path; do
    [ -n "$pending_path" ] || continue
    [ -f "$pending_path" ] || continue
    expiry_state="$($PYTHON_BIN "$ROOT/scripts/amsc_probability_pending_queue.py" expire-noncurrent \
      --plan "$pending_path" \
      --current-event-date "$current_event_date" \
      --apply \
      --confirm EXPIRE_NONCURRENT_AMSC_PROBABILITY_PLAN)"
    if printf '%s' "$expiry_state" | "$PYTHON_BIN" -c \
      'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("state") == "expired_noncurrent_physical_station_day" else 1)'; then
      echo "[amsc_zuuu] pending probability plan archived as noncurrent evidence-only plan=${pending_path}"
      continue
    fi
    [ "$drained" -lt "$P2_PENDING_PROBABILITY_DRAIN_LIMIT" ] || break
    drained=$((drained + 1))
    replay_result="${pending_path%.plan.json}.replay_result.json"
    echo "[amsc_zuuu] pending probability replay ${drained}/${P2_PENDING_PROBABILITY_DRAIN_LIMIT} plan=${pending_path}"
    set +e
    run_probability_fast_lane_from_plan "$pending_path" "$replay_result"
    replay_rc=$?
    set -e
    if [ "$replay_rc" != "0" ]; then
      echo "[amsc_zuuu] pending probability replay still blocked; keep plan=${pending_path}" >&2
      break
    fi
    "$PYTHON_BIN" - "$pending_path" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

plan_path = Path(sys.argv[1])
deferred_path = plan_path.with_suffix(".deferred.json")
recovered_path = plan_path.with_suffix(".recovered.json")
try:
    receipt = json.loads(deferred_path.read_text(encoding="utf-8"))
except Exception:
    receipt = {"schema": "amsc_probability_fast_lane_deferral_v1"}
receipt.update({
    "state": "recovered",
    "recovered_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    "pending_plan_path": str(plan_path),
})
temporary = recovered_path.with_name(f".{recovered_path.name}.tmp.{os.getpid()}")
temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.replace(temporary, recovered_path)
deferred_path.unlink(missing_ok=True)
PY
    rm -f "$pending_path" "$replay_result"
    echo "[amsc_zuuu] pending probability replay recovered plan=${pending_path}"
  done < <(find "$P2_PENDING_PROBABILITY_DIR" -maxdepth 1 -type f -name '*.plan.json' | sort)
}

drain_pending_apply_queue() {
  [ "$APPLY_ENABLED" = "1" ] || return 0
  [ "$PENDING_DRAIN_LIMIT" -gt 0 ] || return 0
  [ -d "$PENDING_DIR" ] || return 0
  local drained=0
  local pending_path=""
  local pending_result=""
  local pending_rc=0
  while IFS= read -r pending_path; do
    [ -n "$pending_path" ] || continue
    [ -f "$pending_path" ] || continue
    drained=$((drained + 1))
    pending_result="${pending_path}.apply_result.json"
    echo "[amsc_zuuu] pending apply drain ${drained}/${PENDING_DRAIN_LIMIT} plan=${pending_path}"
    set +e
    # A deferred source fact has no right to borrow a later weak forecast.
    # It is recovered as evidence only; posterior hand-off needs a generation
    # frozen at the original source event and otherwise remains fail-closed.
    apply_plan_with_short_retry "$pending_path" "$pending_result" "pending_source_fact" "${pending_path}.generation_ref.json" "1" "pending_recovery"
    pending_rc=$?
    set -e
    if [ "$pending_rc" = "0" ]; then
      rm -f "$pending_path" "${pending_path}.generation_ref.json" "$pending_result"
      echo "[amsc_zuuu] pending apply drained plan=${pending_path}"
      continue
    fi
    if is_apply_sqlite_locked "$pending_result"; then
      echo "[amsc_zuuu] pending apply still sqlite locked; keep plan=${pending_path}"
      break
    fi
    echo "[amsc_zuuu] pending apply failed rc=${pending_rc}; keep plan=${pending_path}" >&2
  done < <(find "$PENDING_DIR" -maxdepth 1 -type f -name '*.json' ! -name '*.generation_ref.json' ! -name '*.apply_result.json' ! -name '*.fast_lane_deferred.json' | sort | head -n "$PENDING_DRAIN_LIMIT")
}

release_wait_sec="$($PYTHON_BIN - "$POLL_SLOT_UTC" "$RELEASE_DELAY_SEC" <<'PY'
from datetime import datetime, timedelta, timezone
import sys

slot_text = str(sys.argv[1] or "").strip()
delay = float(sys.argv[2])
if not slot_text:
    print(max(0.0, delay))
else:
    slot = datetime.fromisoformat(slot_text.replace("Z", "+00:00"))
    if slot.tzinfo is None:
        raise SystemExit("poll_slot_utc_must_be_timezone_aware")
    release_at = slot.astimezone(timezone.utc) + timedelta(seconds=delay)
    print(max(0.0, (release_at - datetime.now(timezone.utc)).total_seconds()))
PY
)"
if "$PYTHON_BIN" - "$release_wait_sec" <<'PY'
import sys
raise SystemExit(0 if float(sys.argv[1]) > 0.0 else 1)
PY
then
  echo "[amsc_zuuu] waiting until absolute release ${POLL_SLOT_UTC}+${RELEASE_DELAY_SEC}s remaining=${release_wait_sec}s $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  sleep "$release_wait_sec"
fi

echo "[amsc_zuuu] poll window start $(date -u '+%Y-%m-%dT%H:%M:%SZ') attempts=${POLL_ATTEMPTS} interval=${POLL_INTERVAL_SEC}s report_count=${REPORT_COUNT}"

final_rc=1
applied_once=0
slot_completed=0

for attempt in $(seq 1 "$POLL_ATTEMPTS"); do
  taf_revision_attempted=0
  probability_rc=0
  # Per-attempt receipts must never inherit a prior slot's success identity.
  rm -f "$P2_CANONICAL_RESULT_PATH" "$P2_SHADOW_RESULT_PATH"
  attempt_started_at="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  echo "[amsc_zuuu] poll attempt ${attempt}/${POLL_ATTEMPTS} start ${attempt_started_at}"
  if "$PYTHON_BIN" "$ROOT/scripts/fetch_zuuu_metar_taf.py" \
    --count "$REPORT_COUNT" \
    --timeout "$TIMEOUT" \
    --archive-dir "$RAW_ARCHIVE_DIR" \
    --defer-daily-coverage \
    --write-plan-json \
    --enable-apply-contract \
    --output-json "$PLAN_PATH"; then
    slot_state="$(plan_matches_poll_slot "$PLAN_PATH" "$attempt_started_at")"
    slot_fresh="$(printf '%s' "$slot_state" | "$PYTHON_BIN" -c 'import json,sys; print("true" if json.load(sys.stdin).get("fresh") else "false")')"
    slot_reason="$(printf '%s' "$slot_state" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin).get("reason") or "poll_slot_state_invalid")')"
    # T1 -> T3 owns the first scheduling priority after a fresh AMSC report.
    # Half-hour/TAF classification, compatibility artifacts and shadow work
    # are deliberately downstream; none may make the strong observation wait
    # for the shared archive writer queue.
    # A delayed prior-slot METAR remains real hard evidence and must enter the
    # canonical/v6 chain exactly once.  It does not complete the current slot
    # and its expired execution deadline prevents a late order side effect.
    if [ "$APPLY_ENABLED" = "1" ] && { [ "$slot_fresh" = "true" ] || [ "$slot_reason" = "latest_metar_before_poll_slot" ]; }; then
      set +e
      run_probability_fast_lane_from_plan
      probability_rc=$?
      set -e
    fi
    # Coverage is a slow archive-wide audit derivative, never an input to the
    # live strong likelihood. Materialize it only after the fresh event's T3.
    if [ "$slot_fresh" = "true" ]; then
      p2_event_date="$($PYTHON_BIN - "$PLAN_PATH" <<'PY'
import json
import sys
from pathlib import Path
try:
    print(str(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")).get("event_date") or "")[:10])
except Exception:
    print("")
PY
)"
      if [ -n "$p2_event_date" ]; then
        if ! "$PYTHON_BIN" "$ROOT/scripts/materialize_amsc_daily_coverage_v2.py" \
          --archive-dir "$RAW_ARCHIVE_DIR" \
          --event-date "$p2_event_date" \
          --apply \
          --confirm MATERIALIZE_AMSC_DAILY_COVERAGE_V2 \
          >"$OUT_DIR/coverage_materialization_latest.json" 2>&1; then
          echo "[amsc_zuuu] warning: post-T3 daily coverage materialization failed; raw archive and posterior remain durable" >&2
        fi
      fi
    fi
    half_hour_state="$($PYTHON_BIN - "$ROOT" "$PLAN_PATH" "$attempt_started_at" <<'PY'
import json
import sys
root, path, started = sys.argv[1:]
if root not in sys.path:
    sys.path.insert(0, root)
from amsc_half_hour_trigger_runtime import classify_half_hour_fetch
with open(path, encoding="utf-8") as handle:
    print(json.dumps(classify_half_hour_fetch(json.load(handle), attempt_started_at=started), sort_keys=True))
PY
)"
    forecast_revision_count="$(printf '%s' "$half_hour_state" | "$PYTHON_BIN" -c 'import json,sys; print(int(json.load(sys.stdin).get("forecast_revision_candidate_count") or 0))')"
    if [ "$slot_fresh" != "true" ] && [ "$forecast_revision_count" = "0" ]; then
      if [ "$scheduled_regular_slot" != "true" ]; then
        "$PYTHON_BIN" - "$STATUS_PATH" "$half_hour_state" "$REPORT_COUNT" <<'PY'
import json, os, sys
from pathlib import Path
path = Path(sys.argv[1]); trigger = json.loads(sys.argv[2])
status = {
    "schema": "amsc_zuuu_metar_taf_poll_status_v1",
    "ok": True,
    "state": "half_hour_fetch_no_new_event",
    "source_slot_expected_at": trigger.get("trigger_slot_at") or "",
    "scheduled_regular_metar_slot": False,
    "forecast_revision_candidate_count": 0,
    "stale_metar_reconsumed": False,
    "report_count": int(sys.argv[3]),
    "writes_archive_db": False,
    "writes_posterior": False,
    "places_orders": False,
}
path.parent.mkdir(parents=True, exist_ok=True)
tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
tmp.write_text(json.dumps(status, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(path)
PY
        echo "[amsc_zuuu] half-hour fetch complete; no new METAR/TAF event ${half_hour_state}"
        slot_completed=1
        final_rc=0
        break
      fi
      # `latest_metar_before_poll_slot` is the normal stale-report reason;
      # a scheduled slot keeps its bounded retry window.
      expected_slot_at="$(printf '%s' "$slot_state" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin).get("expected_slot_at") or "")')"
      slot_retry_reason="$(printf '%s' "$slot_state" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin).get("reason") or "poll_slot_state_invalid")')"
      write_slot_stale_status "$PLAN_PATH" "$STATUS_PATH" "$attempt" "$expected_slot_at" "$slot_retry_reason"
      echo "[amsc_zuuu] poll attempt ${attempt}/${POLL_ATTEMPTS} stale source ${slot_state}; retrying"
      if [ "$attempt" -lt "$POLL_ATTEMPTS" ]; then
        sleep "$POLL_INTERVAL_SEC"
      fi
      continue
    fi
    artifact_state="$(persist_plan_artifacts "$PLAN_PATH" "$attempt" 2>/dev/null || printf '{}')"
    pending_plan_path="$(
      printf '%s' "$artifact_state" | "$PYTHON_BIN" -c 'import json,sys; d=json.loads(sys.stdin.read() or "{}"); print(d.get("pending_path") or "")' 2>/dev/null || true
    )"
    apply_rc=0
    apply_pending=0
    if [ "$APPLY_ENABLED" = "1" ]; then
      # The unique live probability mainline consumes the frozen source plan
      # and raw archive before reaching this downstream compatibility block.
      # Legacy event-fact projection is intentionally unable to delay T3.

      set +e
      # Compatibility/diagnostic fact projection.  It persists no probability
      # and the retired generic posterior queue remains disabled.
      apply_plan_with_short_retry "$PLAN_PATH" "$FACT_APPLY_RESULT_PATH" "current_source_fact" "" "1" "source_fact_fast_lane"
      apply_rc=$?
      set -e
      if [ "$apply_rc" != "0" ]; then
        if is_apply_sqlite_locked "$FACT_APPLY_RESULT_PATH" && [ -n "$pending_plan_path" ]; then
          queued_path="$(queue_pending_apply "$PLAN_PATH" "$pending_plan_path" "$FACT_APPLY_RESULT_PATH")"
          apply_pending=1
          apply_rc=0
          echo "[amsc_zuuu] apply current deferred pending=${queued_path}"
        fi
      else
        cp "$FACT_APPLY_RESULT_PATH" "$APPLY_RESULT_PATH"
      fi
      if [ "$forecast_revision_count" != "0" ] && [ "$apply_rc" = "0" ]; then
        taf_revision_attempted=1
        taf_revision_result_tmp="${TAF_FORECAST_REVISION_RESULT_PATH}.tmp.$$"
        rm -f "$taf_revision_result_tmp"
        set +e
        "$PYTHON_BIN" "$ROOT/scripts/apply_amsc_taf_forecast_revision_v6_from_plan.py" \
          --db "$APPLY_DB" --plan "$PLAN_PATH" \
          --activation "$PRODUCTION_PROBABILITY_ACTIVATION_PATH" > "$taf_revision_result_tmp"
        taf_revision_rc=$?
        set -e
        # A killed interpreter or an import-time failure can return before the
        # Python CLI emits its normal fail-closed JSON.  Never publish an empty
        # "latest" file: operators must be able to distinguish an archived TAF
        # revision from a revision that actually reached the weak posterior.
        if [ ! -s "$taf_revision_result_tmp" ]; then
          "$PYTHON_BIN" - "$taf_revision_result_tmp" "$taf_revision_rc" "$PLAN_PATH" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
payload = {
    "schema": "apply_amsc_taf_forecast_revision_from_plan_v1",
    "ok": False,
    "state": "blocked",
    "reason": "taf_forecast_revision_runner_returned_no_receipt",
    "runner_returncode": int(sys.argv[2]),
    "plan_path": sys.argv[3],
    "recorded_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    "source_archive_remains_authoritative": True,
    "writes_posterior": False,
    "places_orders": False,
}
temporary = path.with_name(f".{path.name}.receipt.{os.getpid()}")
temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
os.replace(temporary, path)
PY
        fi
        mv "$taf_revision_result_tmp" "$TAF_FORECAST_REVISION_RESULT_PATH"
        if [ "$taf_revision_rc" != "0" ]; then
          echo "[amsc_zuuu] TAF forecast revision posterior update blocked; source fact remains durable result=${TAF_FORECAST_REVISION_RESULT_PATH}" >&2
        else
          echo "[amsc_zuuu] TAF forecast revision posterior update complete result=${TAF_FORECAST_REVISION_RESULT_PATH}"
        fi
      fi
      # Recovery is outside the fresh event's T1->T3 budget.  A failed live
      # attempt is durable and receives a bounded retry after compatibility
      # fact projection releases its writer handle; it can never disappear
      # merely because one unrelated writer held the queue.
      drain_pending_probability_queue
    fi
    attempt_state="$("$PYTHON_BIN" - "$PLAN_PATH" "$STATUS_PATH" "$FACT_APPLY_RESULT_PATH" "$APPLY_RESULT_PATH" "$APPLY_ENABLED" "$apply_pending" "$attempt" "$POLL_ATTEMPTS" "$REPORT_COUNT" "$POLL_INTERVAL_SEC" "$RELEASE_DELAY_SEC" "$P2_CANONICAL_APPEND_ENABLED" "$P2_CANONICAL_RESULT_PATH" "$P2_SHADOW_MIRROR_ENABLED" "$P2_SHADOW_RESULT_PATH" "$half_hour_state" "$TAF_FORECAST_REVISION_RESULT_PATH" "$taf_revision_attempted" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

plan_path = Path(sys.argv[1])
status_path = Path(sys.argv[2])
source_apply_result_path = Path(sys.argv[3])
posterior_handoff_result_path = Path(sys.argv[4])
apply_enabled = sys.argv[5] == "1"
apply_pending_from_shell = sys.argv[6] == "1"
attempt = int(sys.argv[7])
attempts = int(sys.argv[8])
report_count = int(sys.argv[9])
poll_interval_sec = sys.argv[10]
release_delay_sec = sys.argv[11]
p2_canonical_enabled = sys.argv[12] == "1"
p2_result_path = Path(sys.argv[13])
p2_shadow_enabled = sys.argv[14] == "1"
p2_shadow_result_path = Path(sys.argv[15])
half_hour_trigger = json.loads(sys.argv[16])
taf_revision_result_path = Path(sys.argv[17])
taf_revision_attempted = sys.argv[18] == "1"
plan = json.loads(plan_path.read_text(encoding="utf-8"))
source_apply_result = {}
if apply_enabled and source_apply_result_path.exists():
    source_apply_result = json.loads(source_apply_result_path.read_text(encoding="utf-8"))
posterior_handoff_result = {}
if apply_enabled and posterior_handoff_result_path.exists():
    posterior_handoff_result = json.loads(posterior_handoff_result_path.read_text(encoding="utf-8"))
p2_capture = {}
if p2_canonical_enabled and p2_result_path.exists():
    try:
        p2_capture = json.loads(p2_result_path.read_text(encoding="utf-8"))
    except Exception:
        p2_capture = {
            "ok": False,
            "state": "invalid_result",
            "reason": "p2_canonical_result_json_invalid",
        }
p2_shadow = {}
if p2_shadow_enabled and p2_shadow_result_path.exists():
    try:
        p2_shadow = json.loads(p2_shadow_result_path.read_text(encoding="utf-8"))
    except Exception:
        p2_shadow = {
            "ok": False,
            "state": "invalid_result",
            "reason": "p2_shadow_mirror_result_json_invalid",
        }
taf_revision = {}
taf_revision_candidate_count = int(half_hour_trigger.get("forecast_revision_candidate_count") or 0)
if taf_revision_attempted and taf_revision_result_path.exists():
    try:
        taf_revision = json.loads(taf_revision_result_path.read_text(encoding="utf-8"))
    except Exception:
        taf_revision = {
            "ok": False,
            "state": "invalid_result",
            "reason": "taf_forecast_revision_result_json_invalid",
            "places_orders": False,
        }
p2_canonical = p2_capture.get("canonical") if isinstance(p2_capture.get("canonical"), dict) else {}
p2_canonical_plan = p2_canonical.get("plan") if isinstance(p2_canonical.get("plan"), dict) else {}
p2_probability = p2_capture.get("probability") if isinstance(p2_capture.get("probability"), dict) else {}
features = (plan.get("effective_payload") or {}).get("weather_likelihood_features") or {}
raw_history_archive = (plan.get("effective_payload") or {}).get("amsc_raw_history_archive") or {}
source_newly_applied = bool(source_apply_result.get("applied"))
source_duplicate = bool(source_apply_result.get("skipped")) and source_apply_result.get("skip_reason") == "duplicate_amsc_observation_signature"
source_fact_committed = source_newly_applied or source_duplicate
skipped = bool(source_apply_result.get("skipped"))
apply_pending = apply_pending_from_shell or bool(source_apply_result.get("apply_pending"))
status = {
    "schema": "amsc_zuuu_metar_taf_poll_status_v1",
    "ok": bool(plan.get("ok")) and (not apply_enabled or bool(source_apply_result.get("ok")) or apply_pending),
    "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "attempt": attempt,
    "attempts": attempts,
    "report_count": report_count,
    "poll_interval_sec": poll_interval_sec,
    "release_delay_sec": release_delay_sec,
    "plan_path": str(plan_path),
    "apply_enabled": apply_enabled,
    "source_fact_apply_result_path": str(source_apply_result_path) if apply_enabled else "",
    "source_fact_apply_result": source_apply_result,
    "source_fact_write_budget": source_apply_result.get("archive_write_budget") or {},
    "posterior_handoff_result_path": str(posterior_handoff_result_path) if apply_enabled else "",
    "posterior_handoff_result": posterior_handoff_result,
    "plan_schema": plan.get("schema"),
    "dry_run": bool(plan.get("dry_run")),
    "apply_supported": bool(plan.get("apply_supported")),
    "execution_mode": plan.get("execution_mode"),
    "write_latest": bool(plan.get("write_latest")),
    "slug": plan.get("slug"),
    "event_date": plan.get("event_date"),
    "fact_as_of": plan.get("fact_as_of"),
    "source_observation_id_count": len(plan.get("source_observation_ids") or []),
    "raw_history_archive": raw_history_archive,
    "latest_metar_at": features.get("zuuu_metar_raw_amsc_latest_at"),
    "latest_metar_local": features.get("zuuu_metar_raw_amsc_latest_local"),
    "latest_metar_temp_c": features.get("zuuu_metar_raw_amsc_latest_temp_c"),
    "taf_forecast_peak_c": features.get("taf_forecast_peak_c"),
    "half_hour_trigger": half_hour_trigger,
    "taf_forecast_revision": {
        "attempted": taf_revision_attempted,
        "candidate_count": taf_revision_candidate_count,
        "result_path": str(taf_revision_result_path) if taf_revision_attempted else "",
        "ok": taf_revision.get("ok") if taf_revision else None,
        "state": taf_revision.get("state") if taf_revision else "not_attempted",
        "reason": taf_revision.get("reason") if taf_revision else "",
        "trigger_source_revision_id": taf_revision.get("trigger_source_revision_id") if taf_revision else "",
        "new_taf_revision_candidate_count": taf_revision.get("new_taf_revision_candidate_count") if taf_revision else 0,
        "places_orders": False,
    },
    "applied": source_fact_committed,
    "source_fact_newly_applied": source_newly_applied,
    "source_fact_committed": source_fact_committed,
    "apply_pending": apply_pending,
    "apply_pending_reason": source_apply_result.get("apply_pending_reason") or "",
    "pending_plan_path": source_apply_result.get("pending_plan_path") or "",
    "skipped": skipped,
    "skip_reason": source_apply_result.get("skip_reason") or "",
    "apply_block_reason": source_apply_result.get("block_reason") or ((source_apply_result.get("writer_apply_result_summary") or {}).get("block_reason") or ""),
    "apply_error": source_apply_result.get("error") or ((source_apply_result.get("writer_apply_result_summary") or {}).get("error") or ""),
    "p2_canonical_capture": {
        "enabled": p2_canonical_enabled,
        "result_path": str(p2_result_path) if p2_canonical_enabled else "",
        "ok": p2_capture.get("ok") if p2_capture else None,
        "state": p2_capture.get("state") if p2_capture else "not_attempted",
        "event_date": p2_canonical_plan.get("event_date") if p2_canonical_plan else plan.get("event_date"),
        "target_identity": p2_canonical_plan.get("target_identity") if p2_canonical_plan else "",
        "proposal_summary": p2_canonical_plan.get("proposal_summary") if p2_canonical_plan else {},
        "writes": p2_canonical.get("writes") if p2_canonical else {},
        "probability_state": p2_probability.get("state") if p2_probability else "not_attempted",
        "fast_lane": p2_capture.get("fast_lane") if p2_capture else {},
        "blockers": p2_capture.get("blockers") if p2_capture else [],
        "shadow_mirror": {
            "enabled": p2_shadow_enabled,
            "result_path": str(p2_shadow_result_path) if p2_shadow_enabled else "",
            "ok": p2_shadow.get("ok") if p2_shadow else None,
            "state": p2_shadow.get("state") if p2_shadow else "not_attempted",
            "writes": p2_shadow.get("writes") if p2_shadow else {},
            "blockers": p2_shadow.get("blockers") if p2_shadow else [],
            "boundary": "isolated_shadow_canonical_only_not_transaction_b",
        },
        "boundary": "source_observation_then_canonical_then_unique_probability_before_legacy_fact_no_order",
    },
    "guards": plan.get("guards"),
    "db_write_guard": plan.get("db_write_guard"),
    "sync_chain": {
        "source_observation_fast_append": "apply_amsc_probability_fast_lane_v6_from_plan.py",
        "canonical_event_append": "production_amsc_archive_probability_ingress.py",
        "posterior_materialization": "production_sequential_bayes_transaction_b_before_legacy_fact",
        "legacy_fact_projection": "apply_amsc_zuuu_fact_snapshot_plan.py_after_t3",
        "ui_refresh": "event_fact_latest_sequential_temperature_posterior_ssot_v2",
    },
}
tmp = status_path.with_name(f".{status_path.name}.tmp.{os.getpid()}")
status_path.parent.mkdir(parents=True, exist_ok=True)
tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.replace(tmp, status_path)
print(json.dumps({
    "ok": status["ok"],
    "applied": source_fact_committed,
    "source_fact_newly_applied": source_newly_applied,
    "apply_pending": apply_pending,
    "skipped": skipped,
    "skip_reason": status["skip_reason"],
    "p2_canonical_state": status["p2_canonical_capture"]["state"],
    "p2_shadow_mirror_state": status["p2_canonical_capture"]["shadow_mirror"]["state"],
    "taf_forecast_revision_state": status["taf_forecast_revision"]["state"],
    "latest_metar_at": status["latest_metar_at"],
}, ensure_ascii=True, separators=(",", ":")))
PY
)"
    final_rc=0
    echo "[amsc_zuuu] poll attempt ${attempt}/${POLL_ATTEMPTS} ok ${attempt_state}"
    if [ "$slot_fresh" = "true" ] || [ "$scheduled_regular_slot" != "true" ]; then
      slot_completed=1
    fi
    # Catch-up application and TAF revision processing do not satisfy a
    # scheduled :00/:30 METAR slot.  Continue until the exact report arrives.
    if [ "$scheduled_regular_slot" = "true" ] && [ "$slot_fresh" != "true" ]; then
      if [ "$attempt" -lt "$POLL_ATTEMPTS" ]; then
        sleep "$POLL_INTERVAL_SEC"
      fi
      continue
    fi
    if [ "$STOP_ON_APPLIED" = "1" ] && printf '%s' "$attempt_state" | "$PYTHON_BIN" -c 'import json,sys; d=json.load(sys.stdin); raise SystemExit(0 if d.get("applied") else 1)' >/dev/null 2>&1; then
      applied_once=1
      break
    fi
    if [ "$STOP_ON_APPLIED" = "1" ] && printf '%s' "$attempt_state" | "$PYTHON_BIN" -c 'import json,sys; d=json.load(sys.stdin); raise SystemExit(0 if d.get("apply_pending") else 1)' >/dev/null 2>&1; then
      break
    fi
    if [ "$apply_rc" != "0" ]; then
      final_rc="$apply_rc"
    fi
  else
    rc=$?
    final_rc="$rc"
    "$PYTHON_BIN" - "$STATUS_PATH" "$rc" "$attempt" "$POLL_ATTEMPTS" "$REPORT_COUNT" "$POLL_INTERVAL_SEC" "$RELEASE_DELAY_SEC" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

status_path = Path(sys.argv[1])
rc = int(sys.argv[2])
attempt = int(sys.argv[3])
attempts = int(sys.argv[4])
report_count = int(sys.argv[5])
poll_interval_sec = sys.argv[6]
release_delay_sec = sys.argv[7]
status = {
    "schema": "amsc_zuuu_metar_taf_poll_status_v1",
    "ok": False,
    "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "attempt": attempt,
    "attempts": attempts,
    "report_count": report_count,
    "poll_interval_sec": poll_interval_sec,
    "release_delay_sec": release_delay_sec,
    "exit_code": rc,
    "dry_run": True,
    "apply_supported": True,
    "executes_sql": False,
}
tmp = status_path.with_name(f".{status_path.name}.tmp.{os.getpid()}")
status_path.parent.mkdir(parents=True, exist_ok=True)
tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.replace(tmp, status_path)
PY
    echo "[amsc_zuuu] poll attempt ${attempt}/${POLL_ATTEMPTS} failed rc=$rc $(date -u '+%Y-%m-%dT%H:%M:%SZ')" >&2
  fi

  if [ "$attempt" -lt "$POLL_ATTEMPTS" ]; then
    sleep "$POLL_INTERVAL_SEC"
  fi
done

if [ "$scheduled_regular_slot" = "true" ] && [ "$slot_completed" != "1" ]; then
  final_rc=1
fi

if [ "$final_rc" = "0" ]; then
  # Recovery is deliberately after the complete fresh-slot path.  It can only
  # append an older hard fact; it cannot attach a later weak generation or
  # create a late posterior/order action.
  drain_pending_apply_queue
  echo "[amsc_zuuu] poll window ok $(date -u '+%Y-%m-%dT%H:%M:%SZ') applied=${applied_once} plan=$PLAN_PATH"
  exit 0
fi

echo "[amsc_zuuu] poll window failed rc=$final_rc $(date -u '+%Y-%m-%dT%H:%M:%SZ')" >&2
exit "$final_rc"

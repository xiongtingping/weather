#!/bin/bash
set -euo pipefail

# Narrow target-only maintenance window:
# target WAL -> probability-evidence DDL -> RKPK v3 no-live artifact packet.
# market_ws and clob_order_service must remain on their original PIDs.

ROOT="/Users/xiong/weather"
TARGET_LABEL="com.weather.reported_trajectory_no"
MARKET_WS_LABEL="com.weather.market_ws"
CLOB_LABEL="com.weather.clob_order_service"
TARGET_PLIST="/Library/LaunchDaemons/${TARGET_LABEL}.plist"
TARGET_DB="$ROOT/data/bayes_basket_target_ssot.db"
TARGET_PAUSE="$ROOT/data/bayes_basket_target_migration_paused.flag"
LIVE_PAUSE="$ROOT/data/live_paused.flag"
PACKET="$ROOT/output/audits/bayes_basket_target_no_live_vertical_slice_RKPK_v3.json"
FORMAL_PROBABILITY_EVIDENCE_PACKET="$ROOT/output/audits/bayes_basket_probability_evidence_RKPK_formal_packet.json"
CANDIDATE_PROBABILITY_EVIDENCE_PACKET="$ROOT/output/audits/bayes_basket_target_probability_evidence_rkpk_v1/metar_trajectory_candidate_packet.json"
ACTIVATION="$ROOT/config/trading/bayes_basket_target_production_no_live_activation_v1.json"
PY="$ROOT/.venv/bin/python"
STAMP="$(/bin/date -u +%Y%m%dT%H%M%SZ)"
AUDIT_DIR="$ROOT/output/audits"
QUIESCENCE="$AUDIT_DIR/bayes_basket_target_writer_quiescence_${STAMP}.json"
WAL_RECEIPT="$AUDIT_DIR/bayes_basket_target_wal_checkpoint_${STAMP}.json"
BACKUP="$ROOT/data/bayes_basket_target_ssot.pre_probability_evidence_${STAMP}.backup.db"
CUTOVER_RECEIPT="$AUDIT_DIR/bayes_basket_target_probability_evidence_cutover_${STAMP}.json"
FINAL_READINESS="$AUDIT_DIR/bayes_basket_target_probability_evidence_readiness_${STAMP}.json"
RESIDENT_ACCEPTANCE="$AUDIT_DIR/bayes_basket_target_probability_resident_two_rounds_${STAMP}.json"
RESIDENT_VERIFIED="$AUDIT_DIR/bayes_basket_target_probability_resident_two_rounds_verified_${STAMP}.json"
NON_RKPK_BEFORE="$AUDIT_DIR/bayes_basket_target_probability_non_rkpk_before_${STAMP}.json"
NON_RKPK_AFTER="$AUDIT_DIR/bayes_basket_target_probability_non_rkpk_after_${STAMP}.json"
DAILY_ACCEPTANCE="$AUDIT_DIR/bayes_basket_target_daily_no_live_${STAMP}.json"
SETTLEMENT_VERSION="$AUDIT_DIR/bayes_basket_target_settlement_version_${STAMP}.json"
FAILURE_RECEIPT="$AUDIT_DIR/bayes_basket_target_probability_evidence_failure_${STAMP}.json"
RETENTION_RECEIPT="$AUDIT_DIR/bayes_basket_target_backup_retention_${STAMP}.json"
RUN_USER="${SUDO_USER:-xiong}"
if [ "$RUN_USER" = "root" ]; then
  RUN_USER="xiong"
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "必须以 root 运行：cd '$ROOT' && sudo bash scripts/cutover_bayes_basket_target_probability_evidence_no_live.sh --confirm-target-probability-evidence-production-no-live" >&2
  exit 1
fi
if [ "${1:-}" != "--confirm-target-probability-evidence-production-no-live" ]; then
  echo "explicit_target_probability_evidence_confirmation_required" >&2
  exit 2
fi
for required in \
  "$PY" "$TARGET_PLIST" "$TARGET_DB" "$PACKET" "$ACTIVATION" \
  "$ROOT/scripts/restore_bayes_basket_target_backup.py"; do
  if [ ! -f "$required" ] || [ -L "$required" ]; then
    echo "required_regular_path_invalid=$required" >&2
    exit 1
  fi
done

service_pid() {
  /bin/launchctl print "system/$1" 2>/dev/null \
    | /usr/bin/awk '/^[[:space:]]*pid = / {print $3; exit}'
}

restart_target() {
  if ! /bin/launchctl print "system/$TARGET_LABEL" >/dev/null 2>&1; then
    /bin/launchctl bootstrap system "$TARGET_PLIST"
  fi
  /bin/launchctl kickstart -k "system/$TARGET_LABEL" >/dev/null
}

wait_target_stopped() {
  local deadline=$((SECONDS + 30))
  while /bin/launchctl print "system/$TARGET_LABEL" >/dev/null 2>&1; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      return 1
    fi
    /bin/sleep 0.25
  done
}

wait_target_started() {
  local deadline=$((SECONDS + 30))
  local pid=""
  while [ -z "$pid" ]; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      return 1
    fi
    pid="$(service_pid "$TARGET_LABEL")"
    [ -n "$pid" ] || /bin/sleep 0.25
  done
  printf '%s\n' "$pid"
}

restore_backup() {
  sudo -u "$RUN_USER" "$PY" \
    "$ROOT/scripts/restore_bayes_basket_target_backup.py" \
    --backup-db "$BACKUP" \
    --db "$TARGET_DB"
}

write_failure_receipt() {
  local exit_code="$1"
  local reason="$2"
  local target_pid
  target_pid="$(service_pid "$TARGET_LABEL")"
  sudo -u "$RUN_USER" "$PY" - \
    "$FAILURE_RECEIPT" "$TARGET_DB" "$BACKUP" "$exit_code" "$reason" \
    "$target_pid" "$protected_service_drift" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

(
    output_raw,
    database,
    backup,
    exit_code,
    reason,
    target_pid,
    protected_service_drift,
) = sys.argv[1:]
output = Path(output_raw).resolve()
payload = {
    "schema": "bayes_basket_target_probability_evidence_failure_v1",
    "state": "BLOCKED_FAIL_CLOSED",
    "failed_at_utc": datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z"),
    "reason": reason,
    "exit_code": int(exit_code),
    "database": str(Path(database).resolve()),
    "backup_path": str(Path(backup).resolve()),
    "backup_retained": Path(backup).is_file(),
    "target_writer_running": bool(target_pid),
    "target_writer_pid": int(target_pid) if target_pid else None,
    "protected_service_drift": protected_service_drift == "1",
    "live_pause_retained": True,
    "calls_order_gateway": False,
    "live_submit_authorized": False,
    "orders_submitted": 0,
}
encoded = (
    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
).encode("utf-8")
if len(encoded) > 16 * 1024:
    raise SystemExit("target_probability_evidence_failure_receipt_too_large")
output.parent.mkdir(parents=True, exist_ok=True)
temporary = output.with_name(output.name + f".tmp.{os.getpid()}")
temporary.write_bytes(encoded)
os.replace(temporary, output)
PY
}

success=0
target_stopped=0
cutover_started=0
protected_service_drift=0
failure_reason="unhandled_cutover_failure"
cleanup() {
  local code=$?
  if [ "$success" -eq 1 ]; then
    exit "$code"
  fi
  /usr/bin/touch "$LIVE_PAUSE"
  if [ "$target_stopped" -eq 1 ]; then
    /bin/launchctl bootout "system/$TARGET_LABEL" >/dev/null 2>&1 || true
    if ! wait_target_stopped; then
      failure_reason="rollback_writer_not_stopped"
      echo "target_probability_evidence_rollback_writer_not_stopped" >&2
      write_failure_receipt "$code" "$failure_reason" || true
      exit "$code"
    fi
    if [ "$cutover_started" -eq 1 ] && [ -f "$BACKUP" ]; then
      if restore_backup; then
        if [ "$protected_service_drift" -eq 1 ]; then
          failure_reason="shared_service_drift_after_restore"
          echo "target_probability_evidence_shared_service_drift_writer_left_stopped" >&2
        elif restart_target && wait_target_started >/dev/null; then
          :
        else
          failure_reason="rollback_restart_failed"
          /bin/launchctl bootout "system/$TARGET_LABEL" >/dev/null 2>&1 || true
          wait_target_stopped || true
          echo "target_probability_evidence_rollback_restart_failed_writer_left_stopped" >&2
        fi
      else
        failure_reason="rollback_restore_failed"
        echo "target_probability_evidence_rollback_failed_writer_left_stopped" >&2
      fi
    else
      if [ "$protected_service_drift" -eq 1 ]; then
        failure_reason="shared_service_drift_without_restore"
        echo "target_probability_evidence_shared_service_drift_writer_left_stopped" >&2
      elif restart_target && wait_target_started >/dev/null; then
        :
      else
        failure_reason="restart_failed"
        /bin/launchctl bootout "system/$TARGET_LABEL" >/dev/null 2>&1 || true
        wait_target_stopped || true
        echo "target_probability_evidence_restart_failed_writer_left_stopped" >&2
      fi
    fi
  fi
  write_failure_receipt "$code" "$failure_reason" || true
  exit "$code"
}
trap cleanup EXIT

/usr/bin/touch "$LIVE_PAUSE"
market_ws_pid_before="$(service_pid "$MARKET_WS_LABEL")"
clob_pid_before="$(service_pid "$CLOB_LABEL")"
target_pid_before="$(service_pid "$TARGET_LABEL")"
if [ -z "$market_ws_pid_before" ] \
  || [ -z "$clob_pid_before" ] \
  || [ -z "$target_pid_before" ]; then
  failure_reason="required_system_service_pid_missing"
  echo "required_system_service_pid_missing" >&2
  exit 1
fi

/bin/launchctl bootout "system/$TARGET_LABEL"
target_stopped=1
if ! wait_target_stopped; then
  failure_reason="target_writer_did_not_stop"
  echo "target_writer_did_not_stop" >&2
  exit 1
fi

sudo -u "$RUN_USER" "$PY" \
  "$ROOT/scripts/probe_bayes_basket_target_writer_quiescence.py" \
  --db "$TARGET_DB" --output "$QUIESCENCE"
sudo -u "$RUN_USER" "$PY" \
  "$ROOT/scripts/checkpoint_bayes_basket_target_wal.py" \
  --db "$TARGET_DB" \
  --quiescence-receipt "$QUIESCENCE" \
  --output "$WAL_RECEIPT"
sudo -u "$RUN_USER" "$PY" \
  "$ROOT/scripts/probe_bayes_basket_target_writer_quiescence.py" \
  --db "$TARGET_DB" --output "$QUIESCENCE"

cutover_started=1
transaction_args=(
  "$ROOT/scripts/cutover_bayes_basket_target_ssot_v4_transaction_fenced.py"
  --db "$TARGET_DB"
  --packet "$PACKET"
  --activation "$ACTIVATION"
  --pause-file "$TARGET_PAUSE"
  --backup-db "$BACKUP"
  --receipt "$CUTOVER_RECEIPT"
  --target-writer-stopped
  --confirm-transaction-fenced-cutover
)
probability_evidence_packet=""
for candidate_packet in \
  "$FORMAL_PROBABILITY_EVIDENCE_PACKET" \
  "$CANDIDATE_PROBABILITY_EVIDENCE_PACKET"; do
  if [ ! -e "$candidate_packet" ]; then
    continue
  fi
  if [ ! -f "$candidate_packet" ] \
    || [ -L "$candidate_packet" ]; then
    failure_reason="probability_evidence_packet_path_invalid"
    echo "probability_evidence_packet_path_invalid" >&2
    exit 1
  fi
  probability_evidence_packet="$candidate_packet"
  break
done
if [ -n "$probability_evidence_packet" ]; then
  transaction_args+=(
    --probability-evidence-packet "$probability_evidence_packet"
    --expected-probability-station-id RKPK
  )
  if [ "$probability_evidence_packet" = \
    "$CANDIDATE_PROBABILITY_EVIDENCE_PACKET" ]; then
    transaction_args+=(--require-no-probability-activations)
  fi
fi
sudo -u "$RUN_USER" "$PY" "${transaction_args[@]}"

if [ "$(service_pid "$MARKET_WS_LABEL")" != "$market_ws_pid_before" ]; then
  protected_service_drift=1
  failure_reason="market_ws_pid_changed_before_target_restart"
  echo "market_ws_pid_changed_before_target_restart" >&2
  exit 1
fi
if [ "$(service_pid "$CLOB_LABEL")" != "$clob_pid_before" ]; then
  protected_service_drift=1
  failure_reason="clob_order_service_pid_changed_before_target_restart"
  echo "clob_order_service_pid_changed_before_target_restart" >&2
  exit 1
fi
restart_target
if ! reported_pid_after="$(wait_target_started)"; then
  failure_reason="target_writer_did_not_restart"
  echo "target_writer_did_not_restart" >&2
  exit 1
fi
if [ "$reported_pid_after" = "$target_pid_before" ]; then
  failure_reason="target_writer_pid_did_not_change"
  echo "target_writer_pid_did_not_change" >&2
  exit 1
fi
if [ "$(service_pid "$MARKET_WS_LABEL")" != "$market_ws_pid_before" ]; then
  protected_service_drift=1
  failure_reason="market_ws_pid_changed"
  echo "market_ws_pid_changed" >&2
  exit 1
fi
if [ "$(service_pid "$CLOB_LABEL")" != "$clob_pid_before" ]; then
  protected_service_drift=1
  failure_reason="clob_order_service_pid_changed"
  echo "clob_order_service_pid_changed" >&2
  exit 1
fi

sudo -u "$RUN_USER" "$PY" \
  "$ROOT/scripts/bayes_basket_target_production_readiness.py" \
  --station-id RKPK \
  --output "$FINAL_READINESS" || true
sudo -u "$RUN_USER" "$PY" - \
  "$FINAL_READINESS" "$target_pid_before" "$reported_pid_after" \
  "$market_ws_pid_before" "$(service_pid "$MARKET_WS_LABEL")" \
  "$clob_pid_before" "$(service_pid "$CLOB_LABEL")" \
  "$probability_evidence_packet" <<'PY'
import json
import sqlite3
import sys
from pathlib import Path

(
    readiness_raw,
    target_pid_before,
    target_pid_after,
    market_ws_pid_before,
    market_ws_pid_after,
    clob_pid_before,
    clob_pid_after,
    probability_evidence_packet_raw,
) = sys.argv[1:]
receipt = json.loads(Path(readiness_raw).read_text(encoding="utf-8"))
boundary = receipt.get("boundary") or {}
migration = receipt.get("migration") or {}
schema = migration.get("production_ssot_schema") or {}
vector = migration.get("blocked_execution_vector_lineage") or {}
resources = receipt.get("resource_budget") or {}
ws = receipt.get("official_ws_scope_coverage") or {}
target_inputs = receipt.get("target_inputs") or {}
exact = target_inputs.get("exact_scope_readiness") or {}
expected_tables = {
    "basket_probability_evidence_artifact",
    "basket_probability_evidence_promotion",
    "basket_probability_evidence_activation",
    "basket_probability_update_proof",
    "basket_probability_bridge_history",
    "basket_probability_bridge_latest",
    "basket_probability_bridge_bucket_probability",
}
database = Path(str(receipt.get("database") or "")).resolve()
with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
    present = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    missing_probability_tables = sorted(expected_tables - present)
    probability_packet_materialized = True
    if probability_evidence_packet_raw:
        packet_path = Path(probability_evidence_packet_raw).resolve()
        if (
            not packet_path.is_file()
            or packet_path.is_symlink()
            or packet_path.stat().st_size > 8 * 1024 * 1024
        ):
            raise SystemExit(
                "target_probability_evidence_post_packet_path_invalid"
            )
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        expected = {
            "basket_probability_evidence_artifact": (
                "artifact_id",
                tuple(
                    str(row["artifact_id"])
                    for row in packet.get("artifacts") or ()
                ),
            ),
            "basket_probability_evidence_promotion": (
                "promotion_id",
                tuple(
                    str(row["promotion_id"])
                    for row in packet.get("promotions") or ()
                ),
            ),
            "basket_probability_evidence_activation": (
                "activation_id",
                tuple(
                    str(row["activation_id"])
                    for row in packet.get("activations") or ()
                ),
            ),
        }
        for table, (column, identities) in expected.items():
            if not identities:
                continue
            placeholders = ",".join("?" for _ in identities)
            found = int(conn.execute(
                f"SELECT COUNT(*) FROM {table} "
                f"WHERE {column} IN ({placeholders})",
                identities,
            ).fetchone()[0])
            if found != len(set(identities)):
                probability_packet_materialized = False
        packet_artifact_ids = expected[
            "basket_probability_evidence_artifact"
        ][1]
        packet_activation_ids = expected[
            "basket_probability_evidence_activation"
        ][1]
        if packet_artifact_ids:
            placeholders = ",".join("?" for _ in packet_artifact_ids)
            bound_activation_count = int(conn.execute(
                "SELECT COUNT(*) FROM "
                "basket_probability_evidence_activation "
                f"WHERE artifact_id IN ({placeholders})",
                packet_artifact_ids,
            ).fetchone()[0])
            if bound_activation_count != len(set(packet_activation_ids)):
                probability_packet_materialized = False
max_wal_bytes = int(
    (resources.get("limits") or {}).get("max_wal_bytes") or 0
)
if (
    target_pid_before == target_pid_after
    or market_ws_pid_before != market_ws_pid_after
    or clob_pid_before != clob_pid_after
    or schema.get("ok") is not True
    or schema.get("missing_tables") != []
    or missing_probability_tables
    or vector.get("ok") is not True
    or resources.get("ok") is not True
    or max_wal_bytes <= 0
    or int(resources.get("wal_bytes") or 0) > max_wal_bytes
    or target_inputs.get("ready") is not True
    or exact.get("ready") is not True
    or int(exact.get("exact_scope_count") or 0) < 1
    or int(exact.get("blocked_scope_count") or 0) != 0
    or probability_packet_materialized is not True
    or ws.get("state") != "loaded"
    or int(ws.get("incomplete_scope_count") or 0) != 0
    or boundary.get("calls_order_gateway") is not False
    or boundary.get("live_submit_authorized") is not False
    or boundary.get("places_orders") is not False
    or int(boundary.get("orders_submitted") or 0) != 0
    or boundary.get("execution_mode") != "production_no_live"
):
    raise SystemExit("target_probability_evidence_post_readiness_invalid")
PY

snapshot_probability_scope() {
  local output="$1"
  sudo -u "$RUN_USER" "$PY" - "$TARGET_DB" "$output" <<'PY'
import sys
from pathlib import Path

from bayes_basket_target.resource_bounds import write_bounded_json
from scripts.run_bayes_basket_target_post_migration_no_live import (
    probability_scope_snapshot,
)

payload = probability_scope_snapshot(Path(sys.argv[1]), station_id="RKPK")
write_bounded_json(Path(sys.argv[2]).resolve(), payload, limit=1024 * 1024)
PY
}

assert_user_domain_quiescent() {
  sudo -u "$RUN_USER" "$PY" - <<'PY'
import os
import subprocess

uid = os.getuid()
labels = (
    "com.weather.reported_trajectory_no",
    "com.weather.market_ws",
    "com.weather.clob_order_service",
)
for label in labels:
    result = subprocess.run(
        ["launchctl", "print", f"gui/{uid}/{label}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode == 0:
        raise SystemExit(f"resident_core_service_loaded_user_domain:{label}")
PY
}

# The resident helper captures two consecutive service-owned rounds.  The
# outer snapshots cover every probability/evidence/bridge table, including
# tables not present in the legacy helper's narrower station list.
snapshot_probability_scope "$NON_RKPK_BEFORE"
assert_user_domain_quiescent
sudo -u "$RUN_USER" "$PY" - "$TARGET_DB" "$RESIDENT_ACCEPTANCE" <<'PY'
import resource
import subprocess
import sys
import time
from pathlib import Path

from bayes_basket_target.resource_bounds import write_bounded_json
from scripts.accept_bayes_basket_target_probability_resident_two_rounds import (
    capture_round,
    compare_rounds,
)


def service_rss_bytes(services):
    result = {}
    for label, pid in services.items():
        completed = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        raw = completed.stdout.strip()
        if completed.returncode != 0 or not raw.isdigit():
            raise SystemExit(f"resident_service_rss_probe_failed:{label}")
        result[label] = int(raw) * 1024
    return result


def bounded_round(db):
    receipt = capture_round(db, station_id="RKPK")
    resources = dict(receipt.get("resources") or {})
    resources["resident_service_rss_bytes"] = service_rss_bytes(
        receipt.get("services") or {}
    )
    raw_rss = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    resources["acceptance_process_rss_bytes"] = (
        raw_rss if sys.platform == "darwin" else raw_rss * 1024
    )
    receipt["resources"] = resources
    return receipt


first = bounded_round(Path(sys.argv[1]).resolve())
time.sleep(15)
second = bounded_round(Path(sys.argv[1]).resolve())
result = compare_rounds(first, second)
result["rounds"] = [first, second]
write_bounded_json(Path(sys.argv[2]).resolve(), result, limit=4 * 1024 * 1024)
if result.get("state") != "PASS":
    raise SystemExit(
        "resident_two_round_capture_failed:"
        + ",".join(result.get("blockers") or [])
    )
PY
snapshot_probability_scope "$NON_RKPK_AFTER"
assert_user_domain_quiescent
sudo -u "$RUN_USER" "$PY" - \
  "$RESIDENT_ACCEPTANCE" "$NON_RKPK_BEFORE" "$NON_RKPK_AFTER" \
  "$RESIDENT_VERIFIED" <<'PY'
import json
import sys
from pathlib import Path

from bayes_basket_target.resource_bounds import write_bounded_json
from scripts.run_bayes_basket_target_post_migration_no_live import (
    verify_two_round_resident_receipts,
)

acceptance_path = Path(sys.argv[1]).resolve()
before_path = Path(sys.argv[2]).resolve()
after_path = Path(sys.argv[3]).resolve()
verified_path = Path(sys.argv[4]).resolve()
acceptance = json.loads(acceptance_path.read_text(encoding="utf-8"))
before = json.loads(before_path.read_text(encoding="utf-8"))
after = json.loads(after_path.read_text(encoding="utf-8"))
rounds = acceptance.get("rounds")
if not isinstance(rounds, list) or len(rounds) != 2:
    raise SystemExit("resident_acceptance_two_round_receipts_missing")
verified = verify_two_round_resident_receipts(
    rounds[0],
    rounds[1],
    before_snapshot=before,
    after_snapshot=after,
    station_id="RKPK",
)
verified["source_acceptance"] = str(acceptance_path)
verified["source_acceptance_content_sha256"] = acceptance.get(
    "content_sha256"
)
write_bounded_json(verified_path, verified, limit=4 * 1024 * 1024)
if verified.get("state") != "PASS":
    raise SystemExit(
        "resident_acceptance_verification_failed:"
        + ",".join(verified.get("blockers") or [])
    )
PY

as_of_utc="$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ)"
sudo -u "$RUN_USER" "$PY" \
  "$ROOT/scripts/accept_bayes_basket_target_daily_no_live.py" \
  --db "$TARGET_DB" \
  --station-id RKPK \
  --as-of-utc "$as_of_utc" \
  --raw-root "$ROOT/data/research/bayes_basket_target/model_arrivals" \
  --output "$DAILY_ACCEPTANCE"
sudo -u "$RUN_USER" "$PY" \
  "$ROOT/scripts/audit_bayes_basket_target_settlement_version.py" \
  --db "$TARGET_DB" \
  --as-of-utc "$as_of_utc" \
  --station-id RKPK \
  --latest-labeled-event-date \
  --output "$SETTLEMENT_VERSION"

success=1
trap - EXIT
"$PY" "$ROOT/scripts/prune_bayes_basket_target_maintenance_artifacts.py" \
  --apply \
  --confirm APPLY_BOUNDED_MAINTENANCE_RETENTION \
  --receipt "$RETENTION_RECEIPT"
echo "target_probability_evidence_no_live_cutover_ok reported_pid=${reported_pid_after} market_ws_pid=${market_ws_pid_before} clob_pid=${clob_pid_before} receipt=${CUTOVER_RECEIPT} resident=${RESIDENT_VERIFIED} daily=${DAILY_ACCEPTANCE} settlement_version=${SETTLEMENT_VERSION}"

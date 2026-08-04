#!/bin/bash
set -euo pipefail

# Narrow target-only maintenance window:
# target WAL -> probability-evidence DDL -> canonical no-live resident proof.
# market_ws and clob_order_service must remain on their original PIDs.

ROOT="/Users/xiong/weather"
cd "$ROOT"
TARGET_LABEL="com.weather.weather_basket_yes"
MARKET_WS_LABEL="com.weather.market_ws"
CLOB_LABEL="com.weather.clob_order_service"
UID_VALUE="$(id -u)"
SERVICE_DOMAIN="gui/${UID_VALUE}"
TARGET_PLIST="$HOME/Library/LaunchAgents/${TARGET_LABEL}.plist"
TARGET_DB="$ROOT/data/bayes_basket_target_ssot.db"
TARGET_PAUSE="$ROOT/data/bayes_basket_target_migration_paused.flag"
LIVE_PAUSE="$ROOT/data/live_paused.flag"
# The transaction-fenced helper still consumes its historical schema
# bootstrap packet. It is not the production station scope; the resident,
# readiness, snapshots and two-round acceptance all use CANONICAL_STATIONS.
SCHEMA_BOOTSTRAP_PACKET="$ROOT/output/audits/bayes_basket_target_no_live_vertical_slice_RKPK_v3.json"
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
RESIDENT_NWP_ACCEPTANCE="$AUDIT_DIR/bayes_basket_target_canonical_nwp_scheduler_${STAMP}.json"
OUTSIDE_SCOPE_BEFORE="$AUDIT_DIR/bayes_basket_target_probability_outside_scope_before_${STAMP}.json"
OUTSIDE_SCOPE_AFTER="$AUDIT_DIR/bayes_basket_target_probability_outside_scope_after_${STAMP}.json"
DAILY_ACCEPTANCE="$AUDIT_DIR/bayes_basket_target_daily_no_live_${STAMP}.json"
SETTLEMENT_VERSION="$AUDIT_DIR/bayes_basket_target_settlement_version_${STAMP}.json"
FAILURE_RECEIPT="$AUDIT_DIR/bayes_basket_target_probability_evidence_failure_${STAMP}.json"
RETENTION_RECEIPT="$AUDIT_DIR/bayes_basket_target_backup_retention_${STAMP}.json"
ATOMIC_STAGE_DIR="$AUDIT_DIR/bayes_basket_target_m9_06_stages_${STAMP}"
ATOMIC_RECEIPT="$AUDIT_DIR/bayes_basket_target_m9_06_atomic_cutover_${STAMP}.json"
ATOMIC_LATEST="$AUDIT_DIR/bayes_basket_target_m9_06_atomic_cutover_latest.json"
if [ "$(id -u)" -eq 0 ]; then
  echo "user-domain cutover must run as the login user, not root" >&2
  exit 1
fi
if [ "${1:-}" != "--confirm-target-probability-evidence-production-no-live" ]; then
  echo "explicit_target_probability_evidence_confirmation_required" >&2
  exit 2
fi
if [ ! -x "$PY" ]; then
  echo "required_python_not_executable=$PY" >&2
  exit 1
fi
for required in \
  "$TARGET_PLIST" "$TARGET_DB" "$SCHEMA_BOOTSTRAP_PACKET" "$ACTIVATION" \
  "$ROOT/scripts/restore_bayes_basket_target_backup.py"; do
  if [ ! -f "$required" ] || [ -L "$required" ]; then
    echo "required_regular_path_invalid=$required" >&2
    exit 1
  fi
done
for label in "$TARGET_LABEL" "$MARKET_WS_LABEL" "$CLOB_LABEL"; do
  if /bin/launchctl print "system/${label}" >/dev/null 2>&1; then
    echo "system_domain_owner_forbidden=${label}" >&2
    exit 1
  fi
  if ! /bin/launchctl print "${SERVICE_DOMAIN}/${label}" >/dev/null 2>&1; then
    echo "user_domain_owner_missing=${label}" >&2
    exit 1
  fi
done

CANONICAL_STATION_ALLOWLIST="$(
  "$PY" - <<'PY'
from bayes_basket_target.station_registry import coordinate_registry_manifest

stations = coordinate_registry_manifest()["station_ids"]
if len(stations) != 45 or len(set(stations)) != 45:
    raise SystemExit("canonical_station_registry_invalid")
print(",".join(stations))
PY
)"
IFS=',' read -r -a CANONICAL_STATIONS <<< "$CANONICAL_STATION_ALLOWLIST"
READINESS_STATION_ARGS=()
SETTLEMENT_STATION_ARGS=()
for station in "${CANONICAL_STATIONS[@]}"; do
  READINESS_STATION_ARGS+=(--station-id "$station")
  SETTLEMENT_STATION_ARGS+=(--station-id "$station")
done

service_pid() {
  /bin/launchctl print "${SERVICE_DOMAIN}/$1" 2>/dev/null \
    | /usr/bin/awk '/^[[:space:]]*pid = / {print $3; exit}'
}

restart_target() {
  if ! /bin/launchctl print "${SERVICE_DOMAIN}/$TARGET_LABEL" >/dev/null 2>&1; then
    /bin/launchctl bootstrap "$SERVICE_DOMAIN" "$TARGET_PLIST"
  fi
  /bin/launchctl kickstart -k "${SERVICE_DOMAIN}/$TARGET_LABEL" >/dev/null
}

wait_target_stopped() {
  local deadline=$((SECONDS + 30))
  while /bin/launchctl print "${SERVICE_DOMAIN}/$TARGET_LABEL" >/dev/null 2>&1; do
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
  "$PY" \
    "$ROOT/scripts/restore_bayes_basket_target_backup.py" \
    --backup-db "$BACKUP" \
    --db "$TARGET_DB"
}

write_failure_receipt() {
  local exit_code="$1"
  local reason="$2"
  local target_pid
  target_pid="$(service_pid "$TARGET_LABEL")"
  "$PY" - \
    "$FAILURE_RECEIPT" "$TARGET_DB" "$BACKUP" "$exit_code" "$reason" \
    "$target_pid" "$protected_service_drift" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from bayes_basket_target.atomic_cutover_receipt import content_sha256

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
    "legacy_fallback_used": False,
}
payload["content_sha256"] = content_sha256(payload)
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
    /bin/launchctl bootout "${SERVICE_DOMAIN}/$TARGET_LABEL" >/dev/null 2>&1 || true
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
          /bin/launchctl bootout "${SERVICE_DOMAIN}/$TARGET_LABEL" >/dev/null 2>&1 || true
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
        /bin/launchctl bootout "${SERVICE_DOMAIN}/$TARGET_LABEL" >/dev/null 2>&1 || true
        wait_target_stopped || true
        echo "target_probability_evidence_restart_failed_writer_left_stopped" >&2
      fi
    fi
  fi
  write_failure_receipt "$code" "$failure_reason" || true
  exit "$code"
}
trap cleanup EXIT

current_market_ws_pid="$(service_pid "$MARKET_WS_LABEL" || true)"
current_clob_pid="$(service_pid "$CLOB_LABEL" || true)"
current_target_pid="$(service_pid "$TARGET_LABEL" || true)"
replay_probe="$({
  "$PY" - \
    "$ATOMIC_LATEST" "$ATOMIC_RECEIPT" "$TARGET_DB" \
    "$CANONICAL_STATION_ALLOWLIST" "$ACTIVATION" \
    "$SCHEMA_BOOTSTRAP_PACKET" "$current_market_ws_pid" "$current_clob_pid" \
    "$current_target_pid" "$LIVE_PAUSE" \
    "$ROOT/data/weather_basket_yes_status.json" <<'PY'
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

from bayes_basket_target.atomic_cutover_receipt import (
    build_idempotent_replay_receipt,
    build_release_identity,
    content_sha256,
    validate_cutover_receipt,
    write_receipt_if_changed,
)
from scripts.accept_bayes_basket_target_canonical_nwp_scheduler import (
    wait_for_acceptance,
)

latest_raw, output_raw, database_raw, station_csv, activation_raw, bootstrap_raw, market_pid, clob_pid, target_pid, live_pause_raw, status_raw = sys.argv[1:]
latest = Path(latest_raw).expanduser().resolve()
database = Path(database_raw).expanduser().resolve()
live_pause = Path(live_pause_raw).expanduser()
if (
    not latest.is_file()
    or latest.is_symlink()
    or not database.is_file()
    or not market_pid
    or not clob_pid
    or not target_pid
    or live_pause.is_symlink()
    or not live_pause.is_file()
):
    print("NOT_APPLICABLE")
    raise SystemExit(0)
try:
    previous = json.loads(latest.read_text(encoding="utf-8"))
    if not isinstance(previous, dict):
        raise ValueError("latest_receipt_mapping_required")
    activation_sha = hashlib.sha256(Path(activation_raw).read_bytes()).hexdigest()
    bootstrap_sha = hashlib.sha256(Path(bootstrap_raw).read_bytes()).hexdigest()
    schema_sha = content_sha256({
        "schema": "bayes_basket_target_probability_evidence_schema_v1",
        "tables": [
            "basket_probability_evidence_artifact",
            "basket_probability_evidence_promotion",
            "basket_probability_evidence_activation",
            "basket_probability_update_proof",
            "basket_probability_bridge_history",
            "basket_probability_bridge_latest",
            "basket_probability_bridge_bucket_probability",
        ],
        "execution_mode": "production_no_live",
    })
    release = build_release_identity(
        station_ids=tuple(value for value in station_csv.split(",") if value),
        schema_sha256=schema_sha,
        activation_sha256=activation_sha,
        artifact_sha256=bootstrap_sha,
    )
    if previous.get("protected_service_pids_after") != {
        "com.weather.market_ws": int(market_pid),
        "com.weather.clob_order_service": int(clob_pid),
    }:
        print("NOT_APPLICABLE")
        raise SystemExit(0)
    scheduler = wait_for_acceptance(
        Path(status_raw).expanduser().resolve(),
        wait_seconds=0,
    )
    if scheduler.get("state") != "PASS":
        print("NOT_APPLICABLE")
        raise SystemExit(0)
    required_tables = {
        "basket_probability_evidence_artifact",
        "basket_probability_evidence_promotion",
        "basket_probability_evidence_activation",
        "basket_probability_update_proof",
        "basket_probability_bridge_history",
        "basket_probability_bridge_latest",
        "basket_probability_bridge_bucket_probability",
    }
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        connection.execute("PRAGMA query_only=ON")
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    if not quick_check or quick_check[0] != "ok" or not required_tables.issubset(tables):
        print("NOT_APPLICABLE")
        raise SystemExit(0)
    stat = database.stat()
    current_fingerprint = {
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }
    previous_fingerprint = (
        previous.get("target_database_fingerprint_after") or {}
    )
    if (
        int(previous_fingerprint.get("device") or -1)
        != current_fingerprint["device"]
        or int(previous_fingerprint.get("inode") or -1)
        != current_fingerprint["inode"]
    ):
        print("NOT_APPLICABLE")
        raise SystemExit(0)
    replay = build_idempotent_replay_receipt(
        previous,
        station_ids=tuple(value for value in station_csv.split(",") if value),
        release_sha256=release["release_sha256"],
        target_database_fingerprint_after=current_fingerprint,
        target_service_pid_after=int(target_pid),
    )
    validate_cutover_receipt(replay)
except Exception:
    print("NOT_APPLICABLE")
    raise SystemExit(0)
write_receipt_if_changed(Path(output_raw), replay)
print(json.dumps(replay, sort_keys=True))
PY
} 2>/dev/null
)"
if [[ "$replay_probe" == *'"state": "NOOP_IDEMPOTENT"'* ]]; then
  success=1
  trap - EXIT
  echo "target_probability_evidence_no_live_cutover_noop state=NOOP_IDEMPOTENT writes=0 atomic_receipt=${ATOMIC_RECEIPT} latest=${ATOMIC_LATEST}"
  exit 0
fi

/usr/bin/touch "$LIVE_PAUSE"
market_ws_pid_before="$(service_pid "$MARKET_WS_LABEL")"
clob_pid_before="$(service_pid "$CLOB_LABEL")"
target_pid_before="$(service_pid "$TARGET_LABEL")"
if [ -z "$market_ws_pid_before" ] \
  || [ -z "$clob_pid_before" ] \
  || [ -z "$target_pid_before" ]; then
  failure_reason="required_user_service_pid_missing"
  echo "required_user_service_pid_missing" >&2
  exit 1
fi

/bin/launchctl bootout "${SERVICE_DOMAIN}/$TARGET_LABEL"
target_stopped=1
if ! wait_target_stopped; then
  failure_reason="target_writer_did_not_stop"
  echo "target_writer_did_not_stop" >&2
  exit 1
fi

"$PY" \
  "$ROOT/scripts/probe_bayes_basket_target_writer_quiescence.py" \
  --db "$TARGET_DB" --output "$QUIESCENCE"
"$PY" \
  "$ROOT/scripts/checkpoint_bayes_basket_target_wal.py" \
  --db "$TARGET_DB" \
  --quiescence-receipt "$QUIESCENCE" \
  --output "$WAL_RECEIPT"
"$PY" \
  "$ROOT/scripts/probe_bayes_basket_target_writer_quiescence.py" \
  --db "$TARGET_DB" --output "$QUIESCENCE"

cutover_started=1
transaction_args=(
  "$ROOT/scripts/cutover_bayes_basket_target_ssot_v4_transaction_fenced.py"
  --db "$TARGET_DB"
  --packet "$SCHEMA_BOOTSTRAP_PACKET"
  --activation "$ACTIVATION"
  --pause-file "$TARGET_PAUSE"
  --backup-db "$BACKUP"
  --receipt "$CUTOVER_RECEIPT"
  --target-writer-stopped
  --confirm-transaction-fenced-cutover
)
probability_evidence_packet=""
"$PY" "${transaction_args[@]}"

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

"$PY" \
  "$ROOT/scripts/accept_bayes_basket_target_canonical_nwp_scheduler.py" \
  --status "$ROOT/data/weather_basket_yes_status.json" \
  --wait-seconds 120 \
  --output "$RESIDENT_NWP_ACCEPTANCE"

"$PY" \
  "$ROOT/scripts/bayes_basket_target_production_readiness.py" \
  "${READINESS_STATION_ARGS[@]}" \
  --output "$FINAL_READINESS" || true
"$PY" - \
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
probability_stage_counts = target_inputs.get("probability_stage_counts") or {}
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
    # Exact current-day 45-station readiness and historical OOS are downstream
    # materialization/promotion states.  They must never roll back a valid
    # production_no_live schema cutover.  Require the durable chain inputs and
    # at least one resident-produced probability row at every stage instead.
    or receipt.get("unfinished_target_inputs") != []
    or target_inputs.get("market_registry_materialized") is not True
    or target_inputs.get("probability_materialized") is not True
    or int(probability_stage_counts.get("P0") or 0) < 1
    or int(probability_stage_counts.get("P0-M") or 0) < 1
    or int(probability_stage_counts.get("P1") or 0) < 1
    or target_inputs.get("official_ws_materialized") is not True
    or target_inputs.get("q0_inventory_materialized") is not True
    or target_inputs.get("order_state_materialized") is not True
    or target_inputs.get("p0_preselection_materialized") is not True
    or target_inputs.get("station_policy_authorized") is not True
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
  "$PY" - "$TARGET_DB" "$output" <<'PY'
import sys
from pathlib import Path

from bayes_basket_target.resource_bounds import write_bounded_json
from scripts.run_bayes_basket_target_post_migration_no_live import (
    probability_scope_snapshot,
)

payload = probability_scope_snapshot(Path(sys.argv[1]))
write_bounded_json(Path(sys.argv[2]).resolve(), payload, limit=1024 * 1024)
PY
}

assert_user_domain_resident() {
  "$PY" - <<'PY'
import os
import subprocess

uid = os.getuid()
labels = (
    "com.weather.weather_basket_yes",
    "com.weather.market_ws",
    "com.weather.clob_order_service",
)
for label in labels:
    system = subprocess.run(
        ["launchctl", "print", f"system/{label}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if system.returncode == 0:
        raise SystemExit(f"forbidden_system_service_residue:{label}")
    user = subprocess.run(
        ["launchctl", "print", f"gui/{uid}/{label}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if user.returncode != 0:
        raise SystemExit(f"resident_core_service_missing_user_domain:{label}")
    pids = [
        line.strip().split("=", 1)[1].strip()
        for line in user.stdout.splitlines()
        if line.strip().startswith("pid = ")
    ]
    if len(pids) != 1 or not pids[0].isdigit() or int(pids[0]) <= 0:
        raise SystemExit(f"resident_core_service_pid_invalid:{label}")
PY
}

# The resident helper captures two consecutive service-owned rounds. The
# outer snapshots prove that rows outside the canonical/explicit scope do not
# change while all in-scope stations may advance.
snapshot_probability_scope "$OUTSIDE_SCOPE_BEFORE"
assert_user_domain_resident
"$PY" - "$TARGET_DB" "$RESIDENT_ACCEPTANCE" <<'PY'
import resource
import subprocess
import sys
import time
from pathlib import Path

from bayes_basket_target.resource_bounds import write_bounded_json
from scripts.accept_bayes_basket_target_probability_resident_two_rounds import (
    capture_migration_round,
    compare_migration_rounds,
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
    receipt = capture_migration_round(db)
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
result = compare_migration_rounds(first, second)
result["rounds"] = [first, second]
write_bounded_json(Path(sys.argv[2]).resolve(), result, limit=4 * 1024 * 1024)
if result.get("state") != "PASS":
    raise SystemExit(
        "resident_two_round_capture_failed:"
        + ",".join(result.get("blockers") or [])
    )
PY
snapshot_probability_scope "$OUTSIDE_SCOPE_AFTER"
assert_user_domain_resident
"$PY" - \
  "$RESIDENT_ACCEPTANCE" "$OUTSIDE_SCOPE_BEFORE" "$OUTSIDE_SCOPE_AFTER" \
  "$RESIDENT_VERIFIED" <<'PY'
import json
import sys
from pathlib import Path

from bayes_basket_target.resource_bounds import write_bounded_json
from scripts.run_bayes_basket_target_post_migration_no_live import (
    verify_migration_two_round_resident_receipts,
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
verified = verify_migration_two_round_resident_receipts(
    rounds[0],
    rounds[1],
    before_snapshot=before,
    after_snapshot=after,
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
"$PY" - \
  "$RESIDENT_VERIFIED" "$DAILY_ACCEPTANCE" "$TARGET_DB" "$as_of_utc" \
  "$ROOT/data/research/bayes_basket_target/model_arrivals" <<'PY'
import json
import subprocess
import sys
from pathlib import Path

from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.resource_bounds import write_bounded_json

receipt_raw, output_raw, database, as_of_utc, raw_root = sys.argv[1:]
receipt = json.loads(Path(receipt_raw).read_text(encoding="utf-8"))
output = Path(output_raw)
station_outputs = {}
materialization_pending = {}
for station in receipt.get("started_station_ids") or []:
    station_output = output.with_name(
        f"{output.stem}_{station}{output.suffix}"
    )
    completed = subprocess.run(
        [
            sys.executable,
            "/Users/xiong/weather/scripts/"
            "accept_bayes_basket_target_daily_no_live.py",
            "--db",
            database,
            "--station-id",
            str(station),
            "--as-of-utc",
            as_of_utc,
            "--raw-root",
            raw_root,
            "--output",
            str(station_output),
        ],
        check=False,
        timeout=120,
    )
    if completed.returncode not in (0, 2):
        raise SystemExit(
            f"daily_no_live_probe_failed:{station}:"
            f"{completed.returncode}"
        )
    station_receipt = json.loads(
        station_output.read_text(encoding="utf-8")
    )
    checks = station_receipt.get("checks") or {}
    services = station_receipt.get("services") or {}
    resources = station_receipt.get("resources") or {}
    safety_checks = (
        station_receipt.get("production_no_live") is True
        and station_receipt.get("read_only") is True
        and station_receipt.get("calls_order_gateway") is False
        and station_receipt.get("live_submit_authorized") is False
        and int(station_receipt.get("orders_submitted") or 0) == 0
        and resources.get("ok") is True
        and services.get("state") == "PASS"
        and services.get("domain") == "user"
        and services.get("exactly_three") is True
        and (checks.get("activation") or {}).get("state") == "PASS"
        and (checks.get("atomic_ledger") or {}).get("state") == "PASS"
        and (checks.get("execution_boundary") or {}).get("state")
        == "PASS"
        and (checks.get("training") or {}).get("state") == "PASS"
        and checks.get("quick_check") == "ok"
    )
    if not safety_checks:
        raise SystemExit(
            f"daily_no_live_safety_boundary_invalid:{station}"
        )
    materialization_pending[str(station)] = list(
        station_receipt.get("blockers") or []
    )
    station_outputs[str(station)] = str(station_output)
identity = {
    "schema": "bayes_basket_target_daily_no_live_canonical_index_v2",
    "state": "PASS",
    "station_scope": receipt.get("station_scope"),
    "started_station_ids": receipt.get("started_station_ids") or [],
    "not_started_station_ids": (
        receipt.get("not_started_station_ids") or []
    ),
    "station_outputs": station_outputs,
    "materialization_pending_blockers": materialization_pending,
    "historical_oos_boundary": {
        "future_data_counts_as_oos": False,
        "future_data_blocks_production_no_live": False,
        "future_data_blocks_formal_promotion_or_live": False,
        "historical_strict_oos_required_for_formal_promotion_or_live": True,
    },
    "production_no_live": True,
    "calls_order_gateway": False,
    "live_submit_authorized": False,
    "orders_submitted": 0,
}
write_bounded_json(
    output,
    {**identity, "content_sha256": content_sha256(identity)},
)
PY
"$PY" \
  "$ROOT/scripts/audit_bayes_basket_target_settlement_version.py" \
  --db "$TARGET_DB" \
  --as-of-utc "$as_of_utc" \
  "${SETTLEMENT_STATION_ARGS[@]}" \
  --latest-labeled-event-date \
  --output "$SETTLEMENT_VERSION"

"$PY" "$ROOT/scripts/prune_bayes_basket_target_maintenance_artifacts.py" \
  --apply \
  --confirm APPLY_BOUNDED_MAINTENANCE_RETENTION \
  --receipt "$RETENTION_RECEIPT"

# Seal every maintenance stage, delete only the bounded recovery image after
# all no-live checks have passed, and write one final content-addressed M9-06
# receipt.  Until this block returns successfully, the EXIT trap can restore
# the pre-cutover database and restart only the target writer.
"$PY" - \
  "$ATOMIC_STAGE_DIR" "$ATOMIC_RECEIPT" "$ATOMIC_LATEST" "$TARGET_DB" \
  "$QUIESCENCE" "$WAL_RECEIPT" "$CUTOVER_RECEIPT" \
  "$FINAL_READINESS" "$RESIDENT_ACCEPTANCE" "$RESIDENT_VERIFIED" \
  "$DAILY_ACCEPTANCE" "$SETTLEMENT_VERSION" "$RETENTION_RECEIPT" \
  "$RESIDENT_NWP_ACCEPTANCE" \
  "$BACKUP" "$ACTIVATION" "$SCHEMA_BOOTSTRAP_PACKET" \
  "$CANONICAL_STATION_ALLOWLIST" \
  "$market_ws_pid_before" "$(service_pid "$MARKET_WS_LABEL")" \
  "$clob_pid_before" "$(service_pid "$CLOB_LABEL")" \
  "$target_pid_before" "$reported_pid_after" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

from bayes_basket_target.atomic_cutover_receipt import (
    build_cutover_receipt,
    build_release_identity,
    content_sha256,
    recovery_file_identity,
    seal_stage,
    source_stage_receipt,
    write_receipt_if_changed,
)

(
    stage_dir_raw,
    atomic_receipt_raw,
    atomic_latest_raw,
    database_raw,
    quiescence_raw,
    wal_raw,
    cutover_raw,
    readiness_raw,
    acceptance_raw,
    verified_raw,
    daily_raw,
    settlement_raw,
    retention_raw,
    nwp_acceptance_raw,
    backup_raw,
    activation_raw,
    bootstrap_raw,
    station_csv,
    market_before,
    market_after,
    clob_before,
    clob_after,
    target_before,
    target_after,
) = sys.argv[1:]

stage_dir = Path(stage_dir_raw).resolve()
database = Path(database_raw).resolve()
stat = database.stat()
database_fingerprint = {
    "device": int(stat.st_dev),
    "inode": int(stat.st_ino),
    "size": int(stat.st_size),
    "mtime_ns": int(stat.st_mtime_ns),
}
stage_dir.mkdir(parents=True, exist_ok=True)
stages = {}


def save_stage(name, receipt):
    stages[name] = receipt
    write_receipt_if_changed(
        stage_dir / f"{name}.json",
        receipt,
    )


def source(name, path):
    save_stage(name, source_stage_receipt(name, path))


def bounded_source_identity(label, path):
    candidate = Path(path).resolve()
    if candidate.is_symlink() or not candidate.is_file():
        raise SystemExit(f"m9_06_postcheck_source_invalid:{label}")
    raw = candidate.read_bytes()
    if len(raw) > 16 * 1024 * 1024:
        raise SystemExit(f"m9_06_postcheck_source_too_large:{label}")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"m9_06_postcheck_source_not_mapping:{label}")
    return {
        "path": str(candidate),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "schema": str(payload.get("schema") or ""),
        "state": str(payload.get("state") or ""),
    }


station_ids = tuple(
    sorted(value.strip().upper() for value in station_csv.split(",") if value.strip())
)
if len(station_ids) != 45 or len(set(station_ids)) != 45:
    raise SystemExit("m9_06_canonical_station_scope_invalid")

with open(activation_raw, "rb") as handle:
    activation_sha = hashlib.sha256(handle.read()).hexdigest()
with open(bootstrap_raw, "rb") as handle:
    bootstrap_sha = hashlib.sha256(handle.read()).hexdigest()

save_stage(
    "preflight",
    seal_stage(
        "preflight",
        {
            "schema": "bayes_basket_target_m9_06_preflight_v1",
            "state": "PASS",
            "station_scope": {"mode": "canonical_allowlist", "station_ids": list(station_ids)},
            "activation_sha256": activation_sha,
            "schema_bootstrap_sha256": bootstrap_sha,
            "legacy_fallback_used": False,
            "production_no_live": True,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        },
    ),
)
source("writer_quiescence", quiescence_raw)
source("wal_checkpoint", wal_raw)
cutover_source = bounded_source_identity(
    "schema_activation_replace",
    cutover_raw,
)
cutover_payload = json.loads(
    Path(cutover_raw).read_text(encoding="utf-8")
)
cutover_after = (
    (cutover_payload.get("migration") or {}).get("after") or {}
)
cutover_schema = cutover_after.get("production_ssot_schema") or {}
if (
    cutover_payload.get("state") != "MATERIALIZED"
    or cutover_payload.get("required_domain") != "user"
    or not str(cutover_payload.get("service_domain") or "").startswith(
        "gui/"
    )
    or cutover_payload.get("execution_mode") != "production_no_live"
    or cutover_payload.get("calls_order_gateway") is not False
    or cutover_payload.get("live_submit_authorized") is not False
    or int(cutover_payload.get("orders_submitted") or 0) != 0
    or cutover_schema.get("ok") is not True
    or cutover_schema.get("missing_tables") != []
):
    raise SystemExit("m9_06_schema_activation_source_invalid")
save_stage(
    "schema_activation_replace",
    seal_stage(
        "schema_activation_replace",
        {
            "schema": "bayes_basket_target_m9_06_schema_activation_v1",
            "state": "PASS",
            "source_receipt": cutover_source,
            "target_database_mutated": True,
            "transaction_fenced": True,
            "required_domain": "user",
            "service_domain": cutover_payload.get("service_domain"),
            "production_schema_ok": True,
            "missing_tables": [],
            "production_no_live": True,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        },
        source_path=cutover_source["path"],
        source_sha256=cutover_source["sha256"],
    ),
)

save_stage(
    "resident_restart",
    seal_stage(
        "resident_restart",
        {
            "schema": "bayes_basket_target_m9_06_resident_restart_v1",
            "state": "PASS",
            "target_writer_pid_before": int(target_before),
            "target_writer_pid_after": int(target_after),
            "target_writer_restarted": int(target_before) != int(target_after),
            "canonical_nwp_scheduler": bounded_source_identity(
                "canonical_nwp_scheduler",
                nwp_acceptance_raw,
            ),
            "production_no_live": True,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        },
    ),
)
save_stage(
    "protected_pid_postcheck",
    seal_stage(
        "protected_pid_postcheck",
        {
            "schema": "bayes_basket_target_m9_06_protected_pid_postcheck_v1",
            "state": "PASS",
            "protected_service_pids_before": {
                "com.weather.market_ws": int(market_before),
                "com.weather.clob_order_service": int(clob_before),
            },
            "protected_service_pids_after": {
                "com.weather.market_ws": int(market_after),
                "com.weather.clob_order_service": int(clob_after),
            },
            "pid_stability": int(market_before) == int(market_after)
            and int(clob_before) == int(clob_after),
            "postcheck_receipts": {
                label: bounded_source_identity(label, path)
                for label, path in (
                    ("readiness", readiness_raw),
                    ("resident_verified", verified_raw),
                    ("daily", daily_raw),
                    ("settlement_version", settlement_raw),
                )
            },
            "production_no_live": True,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        },
    ),
)

acceptance = json.loads(Path(acceptance_raw).read_text(encoding="utf-8"))
rounds = acceptance.get("rounds")
if not isinstance(rounds, list) or len(rounds) != 2:
    raise SystemExit("m9_06_two_round_receipts_missing")
for index, round_payload in enumerate(rounds, start=1):
    round_resources = round_payload.get("resources") or {}
    save_stage(
        f"resident_acceptance_round_{index}",
        seal_stage(
            f"resident_acceptance_round_{index}",
            {
                "schema": "bayes_basket_target_m9_06_resident_round_v1",
                "state": round_payload.get("state"),
                "source_content_sha256": content_sha256(round_payload),
                "source_round_sha256": round_payload.get(
                    "round_sha256"
                ),
                "station_scope": round_payload.get("station_scope"),
                "started_station_ids": round_payload.get(
                    "started_station_ids"
                ) or [],
                "candidate_accumulation_station_ids": round_payload.get(
                    "candidate_accumulation_station_ids"
                ) or [],
                "services": round_payload.get("services") or {},
                "resources": {
                    "database_bytes": int(
                        round_resources.get("database_bytes") or 0
                    ),
                    "wal_bytes": int(
                        round_resources.get("wal_bytes") or 0
                    ),
                    "free_bytes": int(
                        round_resources.get("free_bytes") or 0
                    ),
                    "resident_service_rss_bytes": dict(
                        round_resources.get(
                            "resident_service_rss_bytes"
                        ) or {}
                    ),
                },
                "materialization_pending_blockers": round_payload.get(
                    "materialization_pending_blockers"
                ) or [],
                "production_no_live": True,
                "calls_order_gateway": False,
                "live_submit_authorized": False,
                "orders_submitted": 0,
            },
        ),
    )
release = build_release_identity(
    station_ids=station_ids,
    schema_sha256=content_sha256({
        "schema": "bayes_basket_target_probability_evidence_schema_v1",
        "tables": [
            "basket_probability_evidence_artifact",
            "basket_probability_evidence_promotion",
            "basket_probability_evidence_activation",
            "basket_probability_update_proof",
            "basket_probability_bridge_history",
            "basket_probability_bridge_latest",
            "basket_probability_bridge_bucket_probability",
        ],
        "execution_mode": "production_no_live",
    }),
    activation_sha256=activation_sha,
    artifact_sha256=bootstrap_sha,
)
recovery_before = Path(backup_raw)
recovery_claim = recovery_file_identity(recovery_before)
retention_sha = content_sha256(
    json.loads(Path(retention_raw).read_text(encoding="utf-8"))
)
recovery_claim_payload = {
    "schema": "bayes_basket_target_m9_06_recovery_cleanup_v1",
    "state": "READY_TO_CLEAN",
    "recovery": recovery_claim,
    "retention_receipt_sha256": retention_sha,
    "production_no_live": True,
    "calls_order_gateway": False,
    "live_submit_authorized": False,
    "orders_submitted": 0,
}
stages["recovery_cleanup"] = seal_stage(
    "recovery_cleanup",
    recovery_claim_payload,
)
final = build_cutover_receipt(
    station_ids=station_ids,
    stage_receipts=stages,
    release=release,
    protected_pids_before={
        "com.weather.market_ws": int(market_before),
        "com.weather.clob_order_service": int(clob_before),
    },
    protected_pids_after={
        "com.weather.market_ws": int(market_after),
        "com.weather.clob_order_service": int(clob_after),
    },
    target_pid_before=int(target_before),
    target_pid_after=int(target_after),
    recovery=recovery_claim,
    target_database_fingerprint=database_fingerprint,
)
write_receipt_if_changed(Path(atomic_receipt_raw), final)
write_receipt_if_changed(Path(atomic_latest_raw), final)
print(json.dumps({
    "schema": final["schema"],
    "state": final["state"],
    "content_sha256": final["content_sha256"],
    "stage_count": len(stages),
    "recovery_deleted": recovery_claim["deleted_after_success"],
    "writes": final["writes"],
}, sort_keys=True))
PY

# The database and its rollback receipt are now durable.  Disarm rollback
# before deleting the bounded recovery image: cleanup failure may leave one
# harmless backup, but can never leave a changed database without a receipt.
success=1
trap - EXIT

"$PY" - \
  "$BACKUP" "$ATOMIC_RECEIPT" "$ATOMIC_LATEST" \
  "$ATOMIC_STAGE_DIR/recovery_cleanup.json" <<'PY'
import json
import sys
from pathlib import Path

from bayes_basket_target.atomic_cutover_receipt import (
    delete_recovery_after_success,
    finalize_recovery_cleanup_receipt,
    seal_stage,
    validate_cutover_receipt,
    write_receipt_if_changed,
)

backup_raw, receipt_raw, latest_raw, stage_raw = sys.argv[1:]
receipt_path = Path(receipt_raw).resolve()
latest_path = Path(latest_raw).resolve()
previous = json.loads(receipt_path.read_text(encoding="utf-8"))
validate_cutover_receipt(previous)
recovery_before = previous["recovery"]
recovery = delete_recovery_after_success(
    Path(backup_raw).resolve(),
    expected_sha256=str(recovery_before["sha256"]),
)
cleanup_stage = seal_stage(
    "recovery_cleanup",
    {
        "schema": "bayes_basket_target_m9_06_recovery_cleanup_v1",
        "state": "CLEANED",
        "previous_receipt_sha256": previous["content_sha256"],
        "recovery": recovery,
        "production_no_live": True,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    },
)
final = finalize_recovery_cleanup_receipt(
    previous,
    recovery=recovery,
    recovery_stage_receipt=cleanup_stage,
)
write_receipt_if_changed(Path(stage_raw), cleanup_stage)
write_receipt_if_changed(receipt_path, final)
write_receipt_if_changed(latest_path, final)
print(json.dumps({
    "schema": final["schema"],
    "state": final["state"],
    "content_sha256": final["content_sha256"],
    "recovery_deleted": recovery["deleted_after_success"],
}, sort_keys=True))
PY

echo "target_probability_evidence_no_live_cutover_ok reported_pid=${reported_pid_after} market_ws_pid=${market_ws_pid_before} clob_pid=${clob_pid_before} receipt=${CUTOVER_RECEIPT} atomic_receipt=${ATOMIC_RECEIPT} resident=${RESIDENT_VERIFIED} daily=${DAILY_ACCEPTANCE} settlement_version=${SETTLEMENT_VERSION}"

#!/bin/bash
set -euo pipefail

# Narrow maintenance-window cutover for the isolated Bayes Basket target DB.
# It stops only the target writer. market_ws and clob_order_service must stay
# on the same PIDs, and every live/order boundary remains closed.

ROOT="/Users/xiong/weather"
TARGET_LABEL="com.weather.reported_trajectory_no"
MARKET_WS_LABEL="com.weather.market_ws"
CLOB_LABEL="com.weather.clob_order_service"
TARGET_PLIST="/Library/LaunchDaemons/${TARGET_LABEL}.plist"
TARGET_DB="$ROOT/data/bayes_basket_target_ssot.db"
FORWARD_STORE_ROOT="$ROOT/data/runtime/bayes_basket_target_cash_phase"
FORWARD_STORE="$FORWARD_STORE_ROOT/forward_observations.sqlite3"
# JSON is an interchange format only.  This prefix is the sole location in
# which an explicitly requested offline export may be named; the resident
# writer is never allowed to use it as a store or a second write path.
OFFLINE_EXPORT_ROOT="$ROOT/output/audits/bayes_basket_target/offline_export"
TARGET_PAUSE="$ROOT/data/bayes_basket_target_migration_paused.flag"
CUTOVER_JOURNAL="$ROOT/data/bayes_basket_target_ssot_v4_cutover_journal.json"
LIVE_PAUSE="$ROOT/data/live_paused.flag"
ACTIVATION="$ROOT/config/trading/bayes_basket_target_production_no_live_activation_v1.json"
ARTIFACT_PACKET="$ROOT/output/audits/bayes_basket_target_no_live_vertical_slice_RKPK_v3.json"
AUDIT_DIR="$ROOT/output/audits"
RUN_USER="${SUDO_USER:-xiong}"
if [ "$RUN_USER" = "root" ]; then
  RUN_USER="xiong"
fi
PY="$ROOT/.venv/bin/python"
STAMP="$(/bin/date -u +%Y%m%dT%H%M%SZ)"
FORWARD_STORE_BACKUP="$FORWARD_STORE_ROOT/forward_observations.pre_cutover_${STAMP}.backup.sqlite3"
TARGET_PAUSE_BACKUP="$ROOT/data/bayes_basket_target_migration_paused.pre_cutover_${STAMP}.backup.flag"
QUIESCENCE="$AUDIT_DIR/bayes_basket_target_writer_quiescence_${STAMP}.json"
TARGET_WAL_CHECKPOINT="$AUDIT_DIR/bayes_basket_target_wal_checkpoint_${STAMP}.json"
SCHEMA_BACKUP="$ROOT/data/bayes_basket_target_ssot.pre_v4_${STAMP}.backup.db"
ACTIVATION_BACKUP="$ROOT/data/bayes_basket_target_ssot.pre_activation_${STAMP}.backup.db"
MIGRATION_RECEIPT="$AUDIT_DIR/bayes_basket_target_ssot_v4_migration_${STAMP}.json"
ARTIFACT_INGRESS_RECEIPT="$AUDIT_DIR/bayes_basket_target_artifact_ingress_${STAMP}.json"
ACTIVATION_RECEIPT="$AUDIT_DIR/bayes_basket_target_no_live_activation_${STAMP}.json"
CUTOVER_RECEIPT="$AUDIT_DIR/bayes_basket_target_ssot_v4_cutover_${STAMP}.json"
CUTOVER_FAILURE_RECEIPT="$AUDIT_DIR/bayes_basket_target_ssot_v4_cutover_failed_${STAMP}.json"
RETENTION_RECEIPT="$AUDIT_DIR/bayes_basket_target_maintenance_retention_${STAMP}.json"
FAILURE_RETENTION_RECEIPT="$AUDIT_DIR/bayes_basket_target_maintenance_retention_failure_${STAMP}.json"
FORWARD_PRECHECK="$AUDIT_DIR/bayes_basket_target_forward_store_precheck_${STAMP}.json"
FORWARD_INIT_RECEIPT="$AUDIT_DIR/bayes_basket_target_forward_store_initialize_${STAMP}.json"
FORWARD_INIT_CHECK="$AUDIT_DIR/bayes_basket_target_forward_store_initialized_${STAMP}.json"
FORWARD_ROUND_1="$AUDIT_DIR/bayes_basket_target_forward_store_round1_${STAMP}.json"
FORWARD_ROUND_2="$AUDIT_DIR/bayes_basket_target_forward_store_round2_${STAMP}.json"
HARD_NO_BACKUP="$ROOT/data/hard_no_decisions.pre_timezone_${STAMP}.backup.db"
HARD_NO_RECEIPT="$AUDIT_DIR/reported_trajectory_hard_no_timezone_${STAMP}.json"
MAX_FORWARD_STORE_BYTES=67108864
MAX_FORWARD_WAL_BYTES=16777216
MIN_FORWARD_FREE_BYTES=536870912
success=0
target_stopped=0
forward_store_initialized=0
hard_no_timezone_migrated=0
ssot_schema_migrated=0
no_live_activation_applied=0
FORWARD_STORE_PREEXISTED=0
TARGET_PAUSE_PREEXISTED=0

if [ "$(id -u)" -ne 0 ]; then
  echo "必须以 root 运行：cd '$ROOT' && sudo bash scripts/cutover_bayes_basket_target_ssot_v4_no_live.sh" >&2
  exit 1
fi

for required in "$PY" "$TARGET_PLIST" "$TARGET_DB" "$ACTIVATION" "$ARTIFACT_PACKET"; do
  if [ ! -e "$required" ]; then
    echo "required_path_missing=$required" >&2
    exit 1
  fi
done

service_pid() {
  /bin/launchctl print "system/$1" 2>/dev/null \
    | /usr/bin/awk '/^[[:space:]]*pid = / {print $3; exit}'
}

assert_target_runtime_wiring() {
  local launch_dump
  launch_dump="$(/bin/launchctl print "system/$TARGET_LABEL" 2>/dev/null)" || {
    echo "target_runtime_probe_unavailable" >&2
    return 1
  }
  "$PY" - "$launch_dump" "$FORWARD_STORE" "$OFFLINE_EXPORT_ROOT" <<'PY'
import sys

dump = sys.argv[1]
expected_store = sys.argv[2]
offline_export_root = sys.argv[3]
store_override_seen = False
writer_enabled = False
no_live_mode = False
for raw_line in dump.splitlines():
    line = raw_line.strip()
    if "PM_TARGET_PRODUCTION_WRITER" in line and "=> 1" in line:
        writer_enabled = True
    if (
        "PM_TARGET_EXECUTION_MODE" in line
        and "=> production_no_live" in line
    ):
        no_live_mode = True
    if "PM_TARGET_CASH_PHASE_FORWARD_STORE" in line:
        store_override_seen = True
        if expected_store not in line or ".json" in line.lower():
            raise SystemExit("resident_forward_store_override_not_v2_sqlite")
    if any(marker in line for marker in (
        "PM_TARGET_CASH_PHASE_FORWARD_OBSERVATIONS",
        "PM_TARGET_FORWARD_OBSERVATION_PATH",
    )) and ".json" in line.lower() and offline_export_root not in line:
        raise SystemExit("legacy_resident_json_forward_store_forbidden")

# An absent override means the worker's compiled default must be used.  If an
# override is present, it is accepted only when it names the exact same path.
if store_override_seen and expected_store not in dump:
    raise SystemExit("resident_forward_store_default_mismatch")
if not writer_enabled:
    raise SystemExit("resident_target_writer_not_persistently_enabled")
if not no_live_mode:
    raise SystemExit("resident_target_execution_mode_not_no_live")
PY
}

initialize_forward_store() {
  sudo -u "$RUN_USER" "$PY" - \
    "$ROOT" "$FORWARD_STORE_ROOT" "$FORWARD_STORE" "$FORWARD_INIT_RECEIPT" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

root = Path(sys.argv[1]).expanduser().resolve()
store_root = Path(sys.argv[2]).expanduser().resolve()
store = Path(sys.argv[3]).expanduser().resolve()
receipt_path = Path(sys.argv[4]).expanduser().resolve()
sys.path.insert(0, str(root))

try:
    from bayes_basket_target.forward_observation_store import (
        initialize_forward_observation_store,
    )
except (ImportError, AttributeError) as exc:
    raise SystemExit("forward_store_initializer_api_unavailable") from exc

initialized_at_utc = (
    datetime.now(timezone.utc)
    .isoformat(timespec="milliseconds")
    .replace("+00:00", "Z")
)
result = initialize_forward_observation_store(
    store,
    expected_generation=0,
    initialized_at_utc=initialized_at_utc,
    allowed_root=store_root,
)

if result is None:
    result = {"state": "initialized"}
if not isinstance(result, dict):
    result = {"state": "initialized", "result": repr(result)}
for key, expected in (
    ("execution_mode", "production_no_live"),
    ("live_submit_authorized", False),
    ("authorizes_order", False),
):
    if key in result and result.get(key) != expected:
        raise SystemExit("forward_store_initializer_live_boundary_invalid")
if "orders_submitted" in result and int(result.get("orders_submitted") or 0) != 0:
    raise SystemExit("forward_store_initializer_orders_nonzero")
result.update({
    "api": "initialize_forward_observation_store",
    "store_path": str(store),
    "store_root": str(store_root),
    "execution_mode": "production_no_live",
    "live_submit_authorized": False,
    "authorizes_order": False,
    "orders_submitted": 0,
})
raw = (json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
if len(raw) > 16 * 1024 * 1024:
    raise SystemExit("forward_store_initializer_receipt_bytes_exceeded")
receipt_path.parent.mkdir(parents=True, exist_ok=True)
receipt_path.write_bytes(raw)
PY
  forward_store_initialized=1
}

probe_forward_store() {
  local require_existing="$1"
  local ensure_empty="$2"
  local output_path="$3"
  sudo -u "$RUN_USER" "$PY" - \
    "$FORWARD_STORE_ROOT" "$FORWARD_STORE" "$ROOT" \
    "$require_existing" "$ensure_empty" "$output_path" \
    "$MAX_FORWARD_STORE_BYTES" "$MAX_FORWARD_WAL_BYTES" \
    "$MIN_FORWARD_FREE_BYTES" <<'PY'
import json
import shutil
import sqlite3
import sys
from pathlib import Path

root = Path(sys.argv[1]).expanduser()
store = Path(sys.argv[2]).expanduser()
project_root = Path(sys.argv[3]).expanduser().resolve()
require_existing = sys.argv[4] == "1"
ensure_empty = sys.argv[5] == "1"
output = Path(sys.argv[6]).expanduser().resolve()
max_db = int(sys.argv[7])
max_wal = int(sys.argv[8])
min_free = int(sys.argv[9])

sys.path.insert(0, str(project_root))
from bayes_basket_target.forward_observation_store import (
    STORE_SCHEMA,
    STORE_SCHEMA_VERSION,
)
from bayes_basket_target.resource_bounds import assert_process_memory_budget


def fail(code: str) -> None:
    raise SystemExit(code)


def reject_symlink_components(path: Path) -> None:
    current = path
    while True:
        if current.exists() and current.is_symlink():
            fail("forward_store_path_component_symlink_forbidden")
        if current.parent == current:
            return
        current = current.parent


if not root.is_absolute() or root != project_root / "data" / "runtime" / "bayes_basket_target_cash_phase":
    fail("forward_store_root_not_default_target_local_root")
if store != root / "forward_observations.sqlite3":
    fail("forward_store_path_not_default_target_local_path")
reject_symlink_components(root)
reject_symlink_components(store)
if root.exists() and not root.is_dir():
    fail("forward_store_root_not_directory")
root_missing_before_maintenance = not root.exists()
if root_missing_before_maintenance and ensure_empty:
    root.mkdir(parents=True, exist_ok=False)
if root.is_symlink() or (not root_missing_before_maintenance and not root.is_dir()):
    fail("forward_store_root_invalid_after_probe")

# Resource probes are mandatory.  An unavailable memory or disk probe raises
# and therefore closes the maintenance window before any target mutation.
rss_bytes = int(assert_process_memory_budget())
probe_root = root
while not probe_root.exists() and probe_root != probe_root.parent:
    probe_root = probe_root.parent
try:
    free_bytes = int(shutil.disk_usage(probe_root).free)
except (OSError, ValueError, TypeError) as exc:
    raise SystemExit("forward_store_disk_probe_unavailable") from exc
if free_bytes < min_free:
    fail("forward_store_min_free_space_guard")

if root_missing_before_maintenance and not ensure_empty:
    payload = {
        "state": "root_missing_before_maintenance",
        "schema": STORE_SCHEMA,
        "schema_version": STORE_SCHEMA_VERSION,
        "database_path": str(store),
        "database_bytes": 0,
        "wal_bytes": 0,
        "free_bytes": free_bytes,
        "process_rss_bytes": rss_bytes,
        "created_empty_store": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    raise SystemExit(0)

if store.exists() and not store.is_file():
    fail("forward_store_not_regular_file")
if store.is_symlink():
    fail("forward_store_symlink_forbidden")
if not store.exists():
    if require_existing or ensure_empty:
        fail("forward_store_missing_before_initializer_or_restart")
    payload = {
        "state": "missing_before_writer_start",
        "schema": STORE_SCHEMA,
        "schema_version": STORE_SCHEMA_VERSION,
        "database_path": str(store),
        "database_bytes": 0,
        "wal_bytes": 0,
        "free_bytes": free_bytes,
        "process_rss_bytes": rss_bytes,
        "created_empty_store": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    raise SystemExit(0)

wal = store.with_name(store.name + "-wal")
shm = store.with_name(store.name + "-shm")
for sidecar in (wal, shm):
    if sidecar.is_symlink():
        fail("forward_store_sidecar_symlink_forbidden")
db_bytes = int(store.stat().st_size)
wal_bytes = int(wal.stat().st_size) if wal.is_file() else 0
shm_bytes = int(shm.stat().st_size) if shm.is_file() else 0
if db_bytes > max_db:
    fail("forward_store_database_bytes_exceeded")
if wal_bytes > max_wal:
    fail("forward_store_wal_bytes_exceeded")
if db_bytes + wal_bytes + shm_bytes > max_db + max_wal:
    fail("forward_store_sidecars_resource_budget_exceeded")

required_columns = {
    "forward_store_meta": {
        "singleton", "schema_name", "schema_version", "generation",
        "last_source_rowid", "content_sha256", "updated_at_utc",
    },
    "forward_observation": {
        "observation_key", "decision_id", "observation_sha256", "observation_json",
        "state", "materialized_at_utc",
    },
    "forward_ingest_state": {
        "decision_id", "source_decision_sha256", "source_rowid", "state",
        "error_code", "first_seen_at_utc", "last_seen_at_utc",
    },
    "forward_settlement_label": {
        "observation_key", "revision", "winning_bucket", "settlement_timezone",
        "label_sha256",
    },
    "forward_label_ingest_state": {
        "source_label_rowid", "source_label_id",
        "source_label_content_sha256", "materialized_at_utc",
    },
    "forward_retention_tombstone": {
        "observation_key", "observation_sha256", "purged_at_utc",
        "purge_receipt_sha256",
    },
}
with sqlite3.connect(f"file:{store}?mode=ro", uri=True, timeout=5.0) as conn:
    conn.execute("PRAGMA query_only=ON")
    if [str(row[0]) for row in conn.execute("PRAGMA quick_check")] != ["ok"]:
        fail("forward_store_sqlite_quick_check_failed")
    tables = {
        str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    missing_tables = sorted(set(required_columns) - tables)
    if missing_tables:
        fail("forward_store_schema_tables_missing:" + ",".join(missing_tables))
    for table, expected in required_columns.items():
        columns = {
            str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')
        }
        if not expected.issubset(columns):
            fail("forward_store_schema_columns_missing:" + table)
    meta = conn.execute(
        "SELECT schema_name,schema_version,generation,last_source_rowid,"
        "content_sha256,updated_at_utc FROM forward_store_meta WHERE singleton=1"
    ).fetchone()
    if meta is None:
        fail("forward_store_meta_missing")
    if str(meta[0]) != STORE_SCHEMA or int(meta[1]) != STORE_SCHEMA_VERSION:
        fail("forward_store_schema_version_mismatch")
    identity = {
        "schema_name": str(meta[0]),
        "schema_version": int(meta[1]),
        "generation": int(meta[2]),
        "last_source_rowid": int(meta[3]),
        "updated_at_utc": str(meta[5]),
    }
    if str(meta[4]) != content_sha256(identity):
        fail("forward_store_meta_content_hash_mismatch")
    observation_count, unique_decisions, unique_keys, unique_hashes = conn.execute(
        "SELECT COUNT(*),COUNT(DISTINCT decision_id),COUNT(DISTINCT observation_key),"
        "COUNT(DISTINCT observation_sha256) FROM forward_observation"
    ).fetchone()
    duplicate_decision_ids = int(observation_count) - int(unique_decisions)
    duplicate_keys = int(observation_count) - int(unique_keys)
    duplicate_hashes = int(observation_count) - int(unique_hashes)
    quarantine_count = int(conn.execute(
        "SELECT COUNT(*) FROM forward_ingest_state WHERE state='QUARANTINED'"
    ).fetchone()[0])
    last_source_label_rowid = int(conn.execute(
        "SELECT COALESCE(MAX(source_label_rowid),0) "
        "FROM forward_label_ingest_state"
    ).fetchone()[0])
    if duplicate_decision_ids or duplicate_keys or duplicate_hashes:
        fail("forward_store_duplicate_identity_detected")

payload = {
    "state": "ready",
    "schema": STORE_SCHEMA,
    "schema_version": STORE_SCHEMA_VERSION,
    "database_path": str(store),
    "database_bytes": db_bytes,
    "wal_bytes": wal_bytes,
    "shm_bytes": shm_bytes,
    "free_bytes": free_bytes,
    "process_rss_bytes": rss_bytes,
    "generation": int(meta[2]),
    "last_source_rowid": int(meta[3]),
    "last_source_label_rowid": last_source_label_rowid,
    "observation_count": int(observation_count),
    "quarantine_count": quarantine_count,
    "duplicate_decision_ids": duplicate_decision_ids,
    "duplicate_observation_keys": duplicate_keys,
    "duplicate_observation_hashes": duplicate_hashes,
    "execution_mode": "production_no_live",
    "live_submit_authorized": False,
    "authorizes_order": False,
    "orders_submitted": 0,
    "created_empty_store": False,
    "limits": {
        "max_database_bytes": max_db,
        "max_wal_bytes": max_wal,
        "minimum_free_bytes": min_free,
    },
}
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
PY
}

assert_forward_store_rounds() {
  sudo -u "$RUN_USER" "$PY" - "$FORWARD_ROUND_1" "$FORWARD_ROUND_2" <<'PY'
import json
import sys
from pathlib import Path

first = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
second = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
for item in (first, second):
    if item.get("state") != "ready":
        raise SystemExit("forward_store_round_not_ready")
    if item.get("execution_mode") != "production_no_live":
        raise SystemExit("forward_store_round_execution_mode_invalid")
    if item.get("live_submit_authorized") is not False or item.get("authorizes_order") is not False:
        raise SystemExit("forward_store_round_live_boundary_open")
    if int(item.get("orders_submitted") or 0) != 0:
        raise SystemExit("forward_store_round_orders_nonzero")
    if any(int(item.get(key) or 0) != 0 for key in (
        "duplicate_decision_ids", "duplicate_observation_keys", "duplicate_observation_hashes",
    )):
        raise SystemExit("forward_store_round_duplicate_identity")
if int(second["last_source_rowid"]) < int(first["last_source_rowid"]):
    raise SystemExit("forward_store_source_cursor_regressed")
if int(second["last_source_label_rowid"]) < int(first["last_source_label_rowid"]):
    raise SystemExit("forward_store_label_cursor_regressed")
if int(second["generation"]) < int(first["generation"]):
    raise SystemExit("forward_store_generation_regressed")
if int(second["observation_count"]) < int(first["observation_count"]):
    raise SystemExit("forward_store_observation_count_regressed")
if (
    int(second["last_source_rowid"]) != int(first["last_source_rowid"])
    and int(second["generation"]) == int(first["generation"])
):
    raise SystemExit("forward_store_cursor_changed_without_generation")
if (
    int(second["observation_count"]) == int(first["observation_count"])
    and int(second["last_source_rowid"]) == int(first["last_source_rowid"])
    and int(second["last_source_label_rowid"]) == int(first["last_source_label_rowid"])
    and int(second["generation"]) != int(first["generation"])
):
    raise SystemExit("forward_store_generation_changed_without_progress")
PY
}

backup_forward_store_for_rollback() {
  if [ ! -e "$FORWARD_STORE" ]; then
    FORWARD_STORE_PREEXISTED=0
    return 0
  fi
  FORWARD_STORE_PREEXISTED=1
  sudo -u "$RUN_USER" "$PY" - \
    "$FORWARD_STORE" "$FORWARD_STORE_BACKUP" \
    "$MAX_FORWARD_STORE_BYTES" "$MAX_FORWARD_WAL_BYTES" \
    "$MIN_FORWARD_FREE_BYTES" <<'PY'
import shutil
import sqlite3
import sys
from pathlib import Path

source_input = Path(sys.argv[1]).expanduser()
backup_input = Path(sys.argv[2]).expanduser()
max_db = int(sys.argv[3])
max_wal = int(sys.argv[4])
min_free = int(sys.argv[5])
if source_input.is_symlink() or backup_input.is_symlink():
    raise SystemExit("forward_store_rollback_backup_symlink_forbidden")
source = source_input.resolve()
backup = backup_input.resolve()
if not source.is_file() or backup.exists():
    raise SystemExit("forward_store_rollback_backup_path_invalid")
if backup.parent != source.parent:
    raise SystemExit("forward_store_rollback_backup_parent_invalid")
source_bytes = int(source.stat().st_size)
wal = source.with_name(source.name + "-wal")
wal_bytes = int(wal.stat().st_size) if wal.is_file() else 0
if source_bytes > max_db or wal_bytes > max_wal:
    raise SystemExit("forward_store_rollback_backup_resource_limit")
if int(shutil.disk_usage(source.parent).free) < min_free + source_bytes + wal_bytes:
    raise SystemExit("forward_store_rollback_backup_free_space_insufficient")
try:
    with sqlite3.connect(
        f"file:{source}?mode=ro", uri=True, timeout=30.0
    ) as source_conn:
        if [str(row[0]) for row in source_conn.execute("PRAGMA quick_check")] != ["ok"]:
            raise SystemExit("forward_store_rollback_source_quick_check_failed")
        with sqlite3.connect(backup, timeout=30.0) as backup_conn:
            source_conn.backup(backup_conn)
            backup_conn.commit()
            if [str(row[0]) for row in backup_conn.execute("PRAGMA quick_check")] != ["ok"]:
                raise SystemExit("forward_store_rollback_backup_quick_check_failed")
    if int(backup.stat().st_size) > max_db:
        raise SystemExit("forward_store_rollback_backup_bytes_exceeded")
except BaseException:
    backup.unlink(missing_ok=True)
    raise
PY
}

restore_sqlite_database() {
  local recovery_image="$1"
  local destination="$2"
  local max_bytes="$3"
  sudo -u "$RUN_USER" "$PY" - \
    "$recovery_image" "$destination" "$max_bytes" <<'PY'
import os
import sqlite3
import sys
from pathlib import Path

source_input = Path(sys.argv[1]).expanduser()
destination_input = Path(sys.argv[2]).expanduser()
max_bytes = int(sys.argv[3])
if source_input.is_symlink() or destination_input.is_symlink():
    raise SystemExit("rollback_sqlite_symlink_forbidden")
source = source_input.resolve()
destination = destination_input.resolve()
if not source.is_file() or int(source.stat().st_size) > max_bytes:
    raise SystemExit("rollback_sqlite_recovery_image_invalid")
if source.parent != destination.parent:
    raise SystemExit("rollback_sqlite_cross_directory_forbidden")
with sqlite3.connect(
    f"file:{source}?mode=ro", uri=True, timeout=60.0
) as source_conn:
    if [str(row[0]) for row in source_conn.execute("PRAGMA quick_check")] != ["ok"]:
        raise SystemExit("rollback_sqlite_source_quick_check_failed")
    with sqlite3.connect(destination, timeout=60.0) as destination_conn:
        source_conn.backup(destination_conn)
        destination_conn.commit()
        busy, log_frames, checkpointed_frames = destination_conn.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone()
        if int(busy) != 0 or int(log_frames) != int(checkpointed_frames):
            raise SystemExit("rollback_sqlite_checkpoint_failed")
        if [str(row[0]) for row in destination_conn.execute("PRAGMA quick_check")] != ["ok"]:
            raise SystemExit("rollback_sqlite_destination_quick_check_failed")
for suffix in ("-wal", "-shm"):
    sidecar = destination.with_name(destination.name + suffix)
    if sidecar.is_symlink():
        raise SystemExit("rollback_sqlite_sidecar_symlink_forbidden")
    sidecar.unlink(missing_ok=True)
with sqlite3.connect(
    f"file:{destination}?mode=ro", uri=True, timeout=30.0
) as verify_conn:
    if [str(row[0]) for row in verify_conn.execute("PRAGMA quick_check")] != ["ok"]:
        raise SystemExit("rollback_sqlite_final_quick_check_failed")
PY
}

restore_hard_no_timezone_table() {
  local recovery_image="$1"
  sudo -u "$RUN_USER" "$PY" - \
    "$ROOT/data/archive.db" "$recovery_image" <<'PY'
import sqlite3
import sys
from pathlib import Path

database_input = Path(sys.argv[1]).expanduser()
backup_input = Path(sys.argv[2]).expanduser()
if database_input.is_symlink() or backup_input.is_symlink():
    raise SystemExit("hard_no_rollback_symlink_forbidden")
database = database_input.resolve()
backup = backup_input.resolve()
if not backup.is_file():
    raise SystemExit("hard_no_rollback_backup_missing")
with sqlite3.connect(
    f"file:{backup}?mode=ro", uri=True, timeout=30.0
) as backup_conn:
    if [str(row[0]) for row in backup_conn.execute("PRAGMA quick_check")] != ["ok"]:
        raise SystemExit("hard_no_rollback_backup_quick_check_failed")
    expected_rows = int(
        backup_conn.execute(
            "SELECT value FROM backup_metadata WHERE key='row_count'"
        ).fetchone()[0]
    )
    backup_rows = int(
        backup_conn.execute(
            "SELECT COUNT(*) FROM auto_trade_decisions_backup"
        ).fetchone()[0]
    )
if backup_rows != expected_rows:
    raise SystemExit("hard_no_rollback_backup_row_count_mismatch")
archive = "auto_trade_decisions_legacy_no_settlement_timezone_v1"
with sqlite3.connect(database, timeout=60.0, isolation_level=None) as conn:
    conn.execute("PRAGMA busy_timeout=60000")
    names = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "auto_trade_decisions" not in names:
        raise SystemExit("hard_no_rollback_schema_state_invalid")
    current_columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(auto_trade_decisions)")
    }
    if archive not in names and "settlement_timezone" not in current_columns:
        raise SystemExit(0)
    if archive not in names:
        raise SystemExit("hard_no_rollback_archive_missing")
    if "settlement_timezone" not in current_columns:
        raise SystemExit("hard_no_rollback_current_table_not_timezone_v2")
    archived_rows = int(
        conn.execute(f'SELECT COUNT(*) FROM "{archive}"').fetchone()[0]
    )
    if archived_rows != expected_rows:
        raise SystemExit("hard_no_rollback_archive_row_count_mismatch")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DROP TABLE auto_trade_decisions")
        conn.execute(
            f'ALTER TABLE "{archive}" RENAME TO auto_trade_decisions'
        )
        restored_columns = tuple(
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(auto_trade_decisions)"
            )
        )
        if "settlement_timezone" in restored_columns:
            raise SystemExit("hard_no_rollback_old_schema_not_restored")
        if int(
            conn.execute(
                "SELECT COUNT(*) FROM auto_trade_decisions"
            ).fetchone()[0]
        ) != expected_rows:
            raise SystemExit("hard_no_rollback_restored_row_count_mismatch")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
with sqlite3.connect(
    f"file:{database}?mode=ro", uri=True, timeout=30.0
) as verify_conn:
    if [
        str(row[0])
        for row in verify_conn.execute(
            "PRAGMA quick_check('auto_trade_decisions')"
        )
    ] != ["ok"]:
        raise SystemExit("hard_no_rollback_final_quick_check_failed")
PY
}

restore_target_pause_state() {
  local pause_preexisted="$1"
  local pause_backup="$2"
  if [ "$pause_preexisted" -eq 1 ]; then
    if [ ! -f "$pause_backup" ] || [ -L "$pause_backup" ]; then
      return 1
    fi
    /bin/cp -p "$pause_backup" "$TARGET_PAUSE"
  else
    /bin/rm -f "$TARGET_PAUSE"
  fi
}

write_cutover_journal() {
  local journal_state="$1"
  sudo -u "$RUN_USER" "$PY" - \
    "$ROOT" "$CUTOVER_JOURNAL" "$journal_state" "$STAMP" \
    "$FORWARD_STORE" "$FORWARD_STORE_BACKUP" \
    "$FORWARD_STORE_PREEXISTED" "$TARGET_PAUSE" \
    "$TARGET_PAUSE_BACKUP" "$TARGET_PAUSE_PREEXISTED" \
    "$TARGET_DB" "$SCHEMA_BACKUP" "$ACTIVATION_BACKUP" \
    "$HARD_NO_BACKUP" "$forward_store_initialized" \
    "$hard_no_timezone_migrated" "$ssot_schema_migrated" \
    "$no_live_activation_applied" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
from bayes_basket_target.resource_bounds import write_bounded_json

write_bounded_json(
    Path(sys.argv[2]),
    {
        "schema": "bayes_basket_target_ssot_v4_cutover_journal_v1",
        "state": sys.argv[3],
        "stamp": sys.argv[4],
        "forward_store": sys.argv[5],
        "forward_store_backup": sys.argv[6],
        "forward_store_preexisted": sys.argv[7] == "1",
        "target_pause": sys.argv[8],
        "target_pause_backup": sys.argv[9],
        "target_pause_preexisted": sys.argv[10] == "1",
        "target_database": sys.argv[11],
        "schema_backup": sys.argv[12],
        "activation_backup": sys.argv[13],
        "hard_no_backup": sys.argv[14],
        "committed_stages": {
            "forward_store_initialized": sys.argv[15] == "1",
            "hard_no_timezone_migrated": sys.argv[16] == "1",
            "ssot_schema_migrated": sys.argv[17] == "1",
            "no_live_activation_applied": sys.argv[18] == "1",
        },
        "execution_mode": "production_no_live",
        "live_submit_authorized": False,
        "authorizes_order": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    },
)
PY
}

write_cutover_failure_receipt() {
  local failure_state="$1"
  local rollback_state="$2"
  sudo -u "$RUN_USER" "$PY" - \
    "$ROOT" "$CUTOVER_FAILURE_RECEIPT" "$failure_state" \
    "$rollback_state" "$forward_store_initialized" \
    "$hard_no_timezone_migrated" "$ssot_schema_migrated" \
    "$no_live_activation_applied" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
from bayes_basket_target.resource_bounds import write_bounded_json

write_bounded_json(
    Path(sys.argv[2]),
    {
        "schema": "bayes_basket_target_ssot_v4_cutover_failure_v1",
        "state": sys.argv[3],
        "rollback_state": sys.argv[4],
        "committed_stages": {
            "forward_store_initialized": sys.argv[5] == "1",
            "hard_no_timezone_migrated": sys.argv[6] == "1",
            "ssot_schema_migrated": sys.argv[7] == "1",
            "no_live_activation_applied": sys.argv[8] == "1",
        },
        "execution_mode": "production_no_live",
        "live_submit_authorized": False,
        "authorizes_order": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    },
)
PY
}

apply_bounded_maintenance_retention() {
  local receipt="$1"
  sudo -u "$RUN_USER" "$PY" \
    "$ROOT/scripts/prune_bayes_basket_target_maintenance_artifacts.py" \
    --apply \
    --confirm APPLY_BOUNDED_MAINTENANCE_RETENTION \
    --receipt "$receipt"
}

recover_stale_cutover_journal() {
  if [ ! -e "$CUTOVER_JOURNAL" ]; then
    return 0
  fi
  if [ -L "$CUTOVER_JOURNAL" ] || [ ! -f "$CUTOVER_JOURNAL" ]; then
    echo "stale_cutover_journal_path_invalid" >&2
    return 1
  fi
  local values
  values="$(sudo -u "$RUN_USER" "$PY" - \
    "$ROOT" "$CUTOVER_JOURNAL" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
from bayes_basket_target.resource_bounds import load_bounded_json

journal = load_bounded_json(Path(sys.argv[2]))
if (
    not isinstance(journal, dict)
    or journal.get("schema")
    != "bayes_basket_target_ssot_v4_cutover_journal_v1"
    or journal.get("execution_mode") != "production_no_live"
    or journal.get("live_submit_authorized") is not False
    or journal.get("authorizes_order") is not False
    or journal.get("calls_order_gateway") is not False
    or int(journal.get("orders_submitted") or 0) != 0
):
    raise SystemExit("stale_cutover_journal_contract_invalid")
data = root / "data"
runtime_root = data / "runtime" / "bayes_basket_target_cash_phase"
expected = {
    "forward_store": runtime_root / "forward_observations.sqlite3",
    "target_pause": data / "bayes_basket_target_migration_paused.flag",
    "target_database": data / "bayes_basket_target_ssot.db",
}
for key, path in expected.items():
    if Path(str(journal.get(key) or "")).resolve() != path.resolve():
        raise SystemExit("stale_cutover_journal_path_scope_invalid:" + key)
paths = {
    "forward_store_backup": runtime_root,
    "target_pause_backup": data,
    "schema_backup": data,
    "activation_backup": data,
    "hard_no_backup": data,
}
for key, parent in paths.items():
    path = Path(str(journal.get(key) or ""))
    if path.resolve().parent != parent.resolve():
        raise SystemExit("stale_cutover_journal_backup_scope_invalid:" + key)
    if "\n" in str(path) or "\t" in str(path):
        raise SystemExit("stale_cutover_journal_path_control_character")
print(journal["forward_store_backup"])
print("1" if journal.get("forward_store_preexisted") else "0")
print(journal["target_pause_backup"])
print("1" if journal.get("target_pause_preexisted") else "0")
print(journal["schema_backup"])
print(journal["activation_backup"])
print(journal["hard_no_backup"])
PY
  )" || return 1
  local stale_forward_backup
  local stale_forward_preexisted
  local stale_pause_backup
  local stale_pause_preexisted
  local stale_schema_backup
  local stale_activation_backup
  local stale_hard_no_backup
  stale_forward_backup="$(/usr/bin/printf '%s\n' "$values" | /usr/bin/sed -n '1p')"
  stale_forward_preexisted="$(/usr/bin/printf '%s\n' "$values" | /usr/bin/sed -n '2p')"
  stale_pause_backup="$(/usr/bin/printf '%s\n' "$values" | /usr/bin/sed -n '3p')"
  stale_pause_preexisted="$(/usr/bin/printf '%s\n' "$values" | /usr/bin/sed -n '4p')"
  stale_schema_backup="$(/usr/bin/printf '%s\n' "$values" | /usr/bin/sed -n '5p')"
  stale_activation_backup="$(/usr/bin/printf '%s\n' "$values" | /usr/bin/sed -n '6p')"
  stale_hard_no_backup="$(/usr/bin/printf '%s\n' "$values" | /usr/bin/sed -n '7p')"

  /usr/bin/touch "$LIVE_PAUSE"
  /bin/launchctl bootout "system/$TARGET_LABEL" >/dev/null 2>&1 || true
  local recover_failed=0
  set +e
  if [ -f "$stale_schema_backup" ]; then
    restore_sqlite_database \
      "$stale_schema_backup" "$TARGET_DB" 268435456 \
      || recover_failed=1
  elif [ -f "$stale_activation_backup" ]; then
    restore_sqlite_database \
      "$stale_activation_backup" "$TARGET_DB" 268435456 \
      || recover_failed=1
  fi
  if [ -f "$stale_hard_no_backup" ]; then
    restore_hard_no_timezone_table "$stale_hard_no_backup" \
      || recover_failed=1
  fi
  if [ "$stale_forward_preexisted" -eq 1 ]; then
    restore_sqlite_database \
      "$stale_forward_backup" "$FORWARD_STORE" \
      "$MAX_FORWARD_STORE_BYTES" \
      || recover_failed=1
  else
    /bin/rm -f \
      "$FORWARD_STORE" "$FORWARD_STORE-wal" "$FORWARD_STORE-shm" \
      || recover_failed=1
  fi
  restore_target_pause_state \
    "$stale_pause_preexisted" "$stale_pause_backup" \
    || recover_failed=1
  if [ "$recover_failed" -eq 0 ]; then
    restart_target_writer >/dev/null 2>&1 || recover_failed=1
  fi
  if [ "$recover_failed" -eq 0 ]; then
    /bin/rm -f \
      "$stale_forward_backup" "$stale_pause_backup" "$CUTOVER_JOURNAL"
  else
    /usr/bin/printf '%s\n' \
      "bayes-basket-target-m5-migration-v1" > "$TARGET_PAUSE"
    /usr/sbin/chown "$RUN_USER":staff "$TARGET_PAUSE" >/dev/null 2>&1 || true
  fi
  set -e
  if [ "$recover_failed" -ne 0 ]; then
    echo "stale_cutover_journal_recovery_incomplete" >&2
    return 1
  fi
  echo "stale_cutover_journal_recovered_verified; live pause retained" >&2
}

restart_target_writer() {
  if ! /bin/launchctl print "system/$TARGET_LABEL" >/dev/null 2>&1; then
    /bin/launchctl enable "system/$TARGET_LABEL"
    /bin/launchctl bootstrap system "$TARGET_PLIST"
  fi
  /bin/launchctl kickstart -k "system/$TARGET_LABEL" >/dev/null
}

cleanup() {
  if [ "$success" -eq 1 ]; then
    return
  fi
  /usr/bin/touch "$LIVE_PAUSE"
  if [ "$target_stopped" -eq 0 ]; then
    write_cutover_failure_receipt \
      "aborted_before_writer_stop" "not_required" \
      >/dev/null 2>&1 || true
    /bin/rm -f "$FORWARD_STORE_BACKUP" "$TARGET_PAUSE_BACKUP"
    echo "target_ssot_v4_cutover_aborted_before_writer_stop; no target service mutation occurred; live pause retained" >&2
    return
  fi
  # Stop a writer that may have been restarted for post-cutover verification.
  /bin/launchctl bootout "system/$TARGET_LABEL" >/dev/null 2>&1 || true
  target_stopped=1
  local rollback_failed=0
  local rollback_verified=0
  local target_quick_check="not_required"
  local hard_no_quick_check="not_required"
  set +e

  # Reverse committed database stages.  SCHEMA_BACKUP is the original target
  # image, so it also reverses a later activation CAS in one bounded restore.
  if [ "$ssot_schema_migrated" -eq 1 ]; then
    restore_sqlite_database \
      "$SCHEMA_BACKUP" "$TARGET_DB" 268435456 \
      && target_quick_check="ok" \
      || rollback_failed=1
  elif [ "$no_live_activation_applied" -eq 1 ]; then
    restore_sqlite_database \
      "$ACTIVATION_BACKUP" "$TARGET_DB" 268435456 \
      && target_quick_check="ok" \
      || rollback_failed=1
  fi
  if [ "$hard_no_timezone_migrated" -eq 1 ]; then
    restore_hard_no_timezone_table "$HARD_NO_BACKUP" \
      && hard_no_quick_check="ok" \
      || rollback_failed=1
  fi
  if [ "$forward_store_initialized" -eq 1 ]; then
    if [ "$FORWARD_STORE_PREEXISTED" -eq 1 ]; then
      restore_sqlite_database \
        "$FORWARD_STORE_BACKUP" "$FORWARD_STORE" \
        "$MAX_FORWARD_STORE_BYTES" \
        || rollback_failed=1
    else
      /bin/rm -f \
        "$FORWARD_STORE" "$FORWARD_STORE-wal" "$FORWARD_STORE-shm" \
        || rollback_failed=1
    fi
  fi
  restore_target_pause_state \
    "$TARGET_PAUSE_PREEXISTED" "$TARGET_PAUSE_BACKUP" \
    || rollback_failed=1

  # Recovery images are independently quick-checked by each restore helper.
  if [ "$rollback_failed" -eq 0 ] \
    && [ "$target_quick_check" != "failed" ] \
    && [ "$hard_no_quick_check" != "failed" ]; then
    rollback_verified=1
  fi
  if [ "$rollback_verified" -eq 1 ]; then
    write_cutover_failure_receipt \
      "failed_after_mutation" "verified" \
      || rollback_failed=1
  else
    write_cutover_failure_receipt \
      "failed_after_mutation" "incomplete" \
      >/dev/null 2>&1 || true
  fi
  if [ "$rollback_verified" -eq 1 ] \
    && [ "$rollback_failed" -eq 0 ]; then
    restart_target_writer >/dev/null 2>&1 || rollback_failed=1
  fi
  if [ "$rollback_verified" -eq 1 ] \
    && [ "$rollback_failed" -eq 0 ]; then
    apply_bounded_maintenance_retention \
      "$FAILURE_RETENTION_RECEIPT" \
      || rollback_failed=1
  fi
  if [ "$rollback_failed" -ne 0 ]; then
    /usr/bin/printf '%s\n' \
      "bayes-basket-target-m5-migration-v1" > "$TARGET_PAUSE"
    /usr/sbin/chown "$RUN_USER":staff "$TARGET_PAUSE" >/dev/null 2>&1 || true
  fi
  if [ "$rollback_failed" -eq 0 ]; then
    /bin/rm -f \
      "$FORWARD_STORE_BACKUP" "$TARGET_PAUSE_BACKUP" "$CUTOVER_JOURNAL"
  fi
  set -e
  if [ "$rollback_failed" -eq 0 ]; then
    echo "target_ssot_v4_cutover_rolled_back_verified; target writer restarted; live pause retained" >&2
  else
    echo "target_ssot_v4_cutover_rollback_incomplete; target writer remains stopped; migration pause and live pause retained" >&2
  fi
}
recover_stale_cutover_journal
trap cleanup EXIT

/usr/bin/touch "$LIVE_PAUSE"
/bin/mkdir -p "$AUDIT_DIR"

# Read-only preflight: an absent store is acceptable before the maintenance
# transaction (including zero eligible rows), but a present store must already
# be the real v2 SQLite object.  No initializer is called in this phase.
assert_target_runtime_wiring
probe_forward_store 0 0 "$FORWARD_PRECHECK"

# Prove the file authority is target-only production_no_live before stopping
# anything. The Python loader also validates its self hash.
sudo -u "$RUN_USER" "$PY" - "$ROOT" "$ACTIVATION" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from bayes_basket_target.activation import load_target_activation

activation = load_target_activation(Path(sys.argv[2]))
false_fields = (
    "live_decision_write_authorized",
    "live_order_authorized",
    "live_submit_authorized",
    "production_mutation",
    "places_orders",
    "writes_production_db",
    "calls_order_gateway",
    "network_access",
    "authorizes_order",
)
if activation.get("execution_mode") != "production_no_live":
    raise SystemExit("activation_not_production_no_live")
if activation.get("target_only") is not True:
    raise SystemExit("activation_not_target_only")
if any(activation.get(key) is not False for key in false_fields):
    raise SystemExit("activation_live_boundary_open")
PY

market_ws_pid_before="$(service_pid "$MARKET_WS_LABEL")"
clob_pid_before="$(service_pid "$CLOB_LABEL")"
target_pid_before="$(service_pid "$TARGET_LABEL")"
if [ -z "$market_ws_pid_before" ] || [ -z "$clob_pid_before" ] || [ -z "$target_pid_before" ]; then
  echo "required_system_service_not_running" >&2
  exit 1
fi

if [ -e "$TARGET_PAUSE" ]; then
  if [ -L "$TARGET_PAUSE" ] || [ ! -f "$TARGET_PAUSE" ]; then
    echo "target_pause_preexisting_path_invalid" >&2
    exit 1
  fi
  if [ "$(stat -f '%z' "$TARGET_PAUSE")" -gt 4096 ]; then
    echo "target_pause_preexisting_bytes_exceeded" >&2
    exit 1
  fi
  TARGET_PAUSE_PREEXISTED=1
  /bin/cp -p "$TARGET_PAUSE" "$TARGET_PAUSE_BACKUP"
fi

/usr/bin/printf '%s\n' "bayes-basket-target-m5-migration-v1" > "$TARGET_PAUSE"
/usr/sbin/chown "$RUN_USER":staff "$TARGET_PAUSE"

# Only the isolated target writer is paused. The official WS listener and the
# read-only gateway are deliberately not restarted or reconfigured here.
/bin/launchctl bootout "system/$TARGET_LABEL"
target_stopped=1
deadline=$((SECONDS + 30))
while /bin/launchctl print "system/$TARGET_LABEL" >/dev/null 2>&1; do
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "target_writer_did_not_stop" >&2
    exit 1
  fi
  /bin/sleep 0.25
done

sudo -u "$RUN_USER" "$PY" \
  "$ROOT/scripts/probe_bayes_basket_target_writer_quiescence.py" \
  --db "$TARGET_DB" \
  --output "$QUIESCENCE"

# The resource gate rejects an oversized target WAL before migration.  Only
# after the target writer is absent and its exact database identity is attested
# may committed frames be checkpointed.  The checkpoint changes that identity,
# so immediately replace the pre-checkpoint receipt with a fresh one before
# any DDL or backup consumes it.
sudo -u "$RUN_USER" "$PY" \
  "$ROOT/scripts/checkpoint_bayes_basket_target_wal.py" \
  --db "$TARGET_DB" \
  --quiescence-receipt "$QUIESCENCE" \
  --output "$TARGET_WAL_CHECKPOINT"
sudo -u "$RUN_USER" "$PY" \
  "$ROOT/scripts/probe_bayes_basket_target_writer_quiescence.py" \
  --db "$TARGET_DB" \
  --output "$QUIESCENCE"
write_cutover_journal "target_wal_checkpoint_complete"

# The writer is now quiescent.  Initialize through the storage module's sole
# API, even when no eligible row exists; the script never copies the store DDL.
backup_forward_store_for_rollback
write_cutover_journal "prepared_before_forward_store_initializer"
initialize_forward_store
probe_forward_store 1 0 "$FORWARD_INIT_CHECK"
write_cutover_journal "forward_store_initialized"

# The stopped resident releases its archive readers/writers. Perform a
# nonblocking passive checkpoint before target mutation; never truncate,
# vacuum, delete, or replace the protected archive database.
sudo -u "$RUN_USER" "$PY" - "$ROOT/data/archive.db" <<'PY'
import json
import sqlite3
import sys
from pathlib import Path

db = Path(sys.argv[1]).resolve()
with sqlite3.connect(db, timeout=5.0, isolation_level=None) as conn:
    busy, log_frames, checkpointed_frames = conn.execute(
        "PRAGMA wal_checkpoint(PASSIVE)"
    ).fetchone()
wal = Path(str(db) + "-wal")
wal_bytes = int(wal.stat().st_size) if wal.exists() else 0
result = {
    "mode": "PASSIVE",
    "busy": int(busy),
    "log_frames": int(log_frames),
    "checkpointed_frames": int(checkpointed_frames),
    "wal_bytes": wal_bytes,
    "max_wal_bytes": 536870912,
}
print(json.dumps(result, sort_keys=True))
if wal_bytes > result["max_wal_bytes"]:
    raise SystemExit("archive_wal_remains_over_bounded_limit")
PY

sudo -u "$RUN_USER" "$PY" \
  "$ROOT/scripts/migrate_reported_trajectory_hard_no_timezone.py" \
  --db "$ROOT/data/archive.db" \
  --backup-db "$HARD_NO_BACKUP" \
  --confirm MIGRATE_HARD_NO_TIMEZONE_NO_LIVE \
  --receipt "$HARD_NO_RECEIPT"
if [ -f "$HARD_NO_BACKUP" ]; then
  hard_no_timezone_migrated=1
fi
write_cutover_journal "hard_no_timezone_stage_complete"

sudo -u "$RUN_USER" "$PY" \
  "$ROOT/scripts/bayes_basket_target_production_ssot_apply.py" \
  --db "$TARGET_DB" \
  --confirm-target-ssot-migration \
  --writer-quiescence-receipt "$QUIESCENCE" \
  --backup-db "$SCHEMA_BACKUP" \
  --pause-file "$TARGET_PAUSE" \
  --receipt "$MIGRATION_RECEIPT"
if [ -f "$SCHEMA_BACKUP" ]; then
  ssot_schema_migrated=1
fi
write_cutover_journal "ssot_schema_stage_complete"

# Materialize only the frozen RKPK vertical-slice candidate authority while the
# target writer remains quiescent.  This is production_no_live evidence
# authority, not a promotion and not an order authorization.  The packet is
# self-hashed, ingress is append-only/idempotent, and the pre-schema backup
# reverses this substage together with the schema if any later gate fails.
sudo -u "$RUN_USER" "$PY" \
  "$ROOT/scripts/ingest_bayes_basket_target_artifacts.py" \
  --db "$TARGET_DB" \
  --packet "$ARTIFACT_PACKET" \
  --apply \
  --confirm INGEST_BAYES_BASKET_TARGET_ARTIFACTS \
  --require-packet-sha256 \
  --receipt "$ARTIFACT_INGRESS_RECEIPT"
write_cutover_journal "candidate_artifact_ingress_complete"

# Schema and activation are separate compare-and-swap authorities. Align the
# DB row with the current validated no-live contract while the writer remains
# quiescent; this creates a second recovery image of the migrated schema.
sudo -u "$RUN_USER" "$PY" \
  "$ROOT/scripts/bayes_basket_target_no_live_activation_apply.py" \
  --db "$TARGET_DB" \
  --activation "$ACTIVATION" \
  --backup-db "$ACTIVATION_BACKUP" \
  --receipt "$ACTIVATION_RECEIPT"
no_live_activation_applied=1
write_cutover_journal "no_live_activation_stage_complete"

sudo -u "$RUN_USER" "$PY" - \
  "$ROOT" "$TARGET_DB" "$ACTIVATION" "$MIGRATION_RECEIPT" \
  "$ACTIVATION_RECEIPT" "$ARTIFACT_INGRESS_RECEIPT" "$CUTOVER_RECEIPT" \
  "$market_ws_pid_before" "$clob_pid_before" <<'PY'
import json
import sqlite3
import sys
from pathlib import Path

root = Path(sys.argv[1])
db = Path(sys.argv[2]).resolve()
activation_path = Path(sys.argv[3]).resolve()
migration_receipt_path = Path(sys.argv[4]).resolve()
activation_receipt_path = Path(sys.argv[5]).resolve()
artifact_ingress_receipt_path = Path(sys.argv[6]).resolve()
cutover_receipt_path = Path(sys.argv[7]).resolve()
market_ws_pid = int(sys.argv[8])
clob_pid = int(sys.argv[9])
sys.path.insert(0, str(root))

from bayes_basket_target.activation import load_target_activation
from bayes_basket_target.execution_vector_repair import (
    inspect_legacy_blocked_execution_vector_repair,
)
from bayes_basket_target.ssot import production_ssot_schema_status

activation = load_target_activation(activation_path)
migration = json.loads(migration_receipt_path.read_text(encoding="utf-8"))
activation_receipt = json.loads(activation_receipt_path.read_text(encoding="utf-8"))
artifact_ingress = json.loads(
    artifact_ingress_receipt_path.read_text(encoding="utf-8")
)
with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
    conn.execute("PRAGMA query_only=ON")
    status = production_ssot_schema_status(conn)
    row = conn.execute(
        "SELECT generation,state,strategy_id,activation_sha256,activation_json "
        "FROM basket_strategy_activation_state WHERE singleton=1"
    ).fetchone()
    if row is None:
        raise SystemExit("activation_row_missing")
    counts = {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (
            "basket_target_order_intents",
            "basket_target_capital_reservations",
            "basket_target_order_events",
            "basket_target_order_reconciliations",
        )
    }
    authorizing = int(
        conn.execute(
            "SELECT COUNT(*) FROM auto_trade_decisions "
            "WHERE will_order<>0 OR decided_state IN "
            "('submitting','submitted','reconcile_required')"
        ).fetchone()[0]
    )
    execution_vector_lineage = (
        inspect_legacy_blocked_execution_vector_repair(conn)
    )
    execution_vector_lineage.pop("_candidates", None)

if status.get("ok") is not True or status.get("ledger_schema_ok") is not True:
    raise SystemExit("target_ssot_v4_schema_not_ready")
if execution_vector_lineage.get("ok") is not True:
    raise SystemExit("blocked_execution_vector_lineage_incomplete")
if str(row[1]) != "enabled" or str(row[2]) != str(activation["strategy_id"]):
    raise SystemExit("activation_owner_or_state_mismatch")
if str(row[3]) != str(activation["activation_contract_sha256"]):
    raise SystemExit("activation_contract_hash_mismatch")
payload = json.loads(str(row[4]))
if payload.get("execution_mode") != "production_no_live":
    raise SystemExit("database_activation_not_production_no_live")
if any(counts.values()) or authorizing:
    raise SystemExit("target_execution_residue_nonzero")
if migration.get("writes_orders") is not False:
    raise SystemExit("migration_order_boundary_invalid")
if migration.get("execution_vector_repair", {}).get("after", {}).get("ok") is not True:
    raise SystemExit("migration_execution_vector_repair_invalid")
if activation_receipt.get("orders_submitted") != 0:
    raise SystemExit("activation_order_boundary_invalid")
if int(row[0]) != int(activation_receipt.get("generation") or -1):
    raise SystemExit("activation_generation_receipt_mismatch")
if artifact_ingress.get("state") != "COMMITTED":
    raise SystemExit("candidate_artifact_ingress_not_committed")
if artifact_ingress.get("policy_authority_scope") != "PRODUCTION_NO_LIVE_CANDIDATE":
    raise SystemExit("candidate_artifact_authority_scope_invalid")
if artifact_ingress.get("station_ids") != ["RKPK"]:
    raise SystemExit("candidate_artifact_station_identity_invalid")
if int(artifact_ingress.get("station_count") or 0) != 1:
    raise SystemExit("candidate_artifact_station_scope_incomplete")
ingress_expected = artifact_ingress.get("packet_expected_counts") or {}
ingress_bound = artifact_ingress.get("packet_binding_counts") or {}
if (
    int(ingress_expected.get("cash_phase_policies") or 0) != 1
    or int(ingress_expected.get("cash_phase_authority_manifests") or 0) != 1
    or int(ingress_bound.get("cash_phase_policies") or 0) != 1
    or int(ingress_bound.get("cash_phase_authority_manifests") or 0) != 1
):
    raise SystemExit("candidate_cash_authority_ingress_incomplete")
for key in (
    "calibration_artifacts",
    "ensemble_selection_evidence",
    "ensemble_artifacts",
    "calibration_activations",
):
    expected_count = int(ingress_expected.get(key) or 0)
    bound_count = int(ingress_bound.get(key) or 0)
    if expected_count <= 0 or bound_count != expected_count:
        raise SystemExit("candidate_calibration_authority_ingress_incomplete:" + key)
if (
    artifact_ingress.get("execution_mode") != "production_no_live"
    or artifact_ingress.get("live_submit_authorized") is not False
    or artifact_ingress.get("authorizes_order") is not False
    or artifact_ingress.get("calls_order_gateway") is not False
    or int(artifact_ingress.get("orders_submitted") or 0) != 0
):
    raise SystemExit("candidate_artifact_ingress_boundary_invalid")

receipt = {
    "schema": "bayes_basket_target_ssot_v4_cutover_receipt_v1",
    "state": "database_verified_before_writer_restart",
    "database": str(db),
    "generation": int(row[0]),
    "activation_contract_sha256": str(row[3]),
    "execution_mode": "production_no_live",
    "live_submit_authorized": False,
    "calls_order_gateway": False,
    "orders_submitted": 0,
    "execution_residue_counts": counts,
    "authorizing_decision_count": authorizing,
    "production_ssot_schema_ok": True,
    "ledger_schema_ok": True,
    "blocked_execution_vector_lineage": execution_vector_lineage,
    "execution_vector_repair": migration.get("execution_vector_repair"),
    "market_ws_pid_before": market_ws_pid,
    "clob_order_service_pid_before": clob_pid,
    "migration_receipt": str(migration_receipt_path),
    "artifact_ingress_receipt": str(artifact_ingress_receipt_path),
    "artifact_ingress": artifact_ingress,
    "activation_receipt": str(activation_receipt_path),
}
cutover_receipt_path.write_text(
    json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY

restart_target_writer
deadline=$((SECONDS + 30))
reported_pid_after=""
while [ "$SECONDS" -lt "$deadline" ]; do
  reported_pid_after="$(service_pid "$TARGET_LABEL")"
  if [ -n "$reported_pid_after" ]; then
    break
  fi
  /bin/sleep 0.25
done
if [ -z "$reported_pid_after" ]; then
  echo "target_writer_did_not_restart" >&2
  exit 1
fi

assert_target_runtime_wiring

# The resident worker runs production every 15 seconds.  Give it one full
# scheduled interval before the first read and another before the second;
# both rounds are read-only confirmations of the worker's own replay path.
/bin/sleep 16
probe_forward_store 1 0 "$FORWARD_ROUND_1"
/bin/sleep 16
probe_forward_store 1 0 "$FORWARD_ROUND_2"
assert_forward_store_rounds

market_ws_pid_after="$(service_pid "$MARKET_WS_LABEL")"
clob_pid_after="$(service_pid "$CLOB_LABEL")"
if [ "$market_ws_pid_after" != "$market_ws_pid_before" ]; then
  echo "market_ws_pid_changed" >&2
  exit 1
fi
if [ "$clob_pid_after" != "$clob_pid_before" ]; then
  echo "clob_order_service_pid_changed" >&2
  exit 1
fi

health_payload="$(/usr/bin/curl -fsS --max-time 3 http://127.0.0.1:8765/health)"
printf '%s' "$health_payload" | "$PY" -c '
import json,sys
x=json.load(sys.stdin)
ok=(
    x.get("ready") is True
    and x.get("read_only") is True
    and x.get("live_pause_present") is True
    and x.get("order_submission_enabled") is False
    and x.get("authorizes_order") is False
    and x.get("live_submit_authorized") is False
    and int(x.get("idempotency_unresolved_count") or 0) == 0
)
raise SystemExit(0 if ok else 1)
'

# A bootstrap reloads the installed plist. Require the new Clash Verge port in
# the resident service metadata; the writer also reloads .env at process start.
if ! /bin/launchctl print "system/$TARGET_LABEL" \
  | /usr/bin/grep -q '127.0.0.1:7897'; then
  echo "target_writer_proxy_7897_not_loaded" >&2
  exit 1
fi

sudo -u "$RUN_USER" "$PY" - "$CUTOVER_RECEIPT" \
  "$reported_pid_after" "$market_ws_pid_after" "$clob_pid_after" \
  "$FORWARD_PRECHECK" "$FORWARD_INIT_RECEIPT" "$FORWARD_INIT_CHECK" \
  "$FORWARD_ROUND_1" "$FORWARD_ROUND_2" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1]).resolve()
payload = json.loads(path.read_text(encoding="utf-8"))
precheck = json.loads(Path(sys.argv[5]).read_text(encoding="utf-8"))
initializer = json.loads(Path(sys.argv[6]).read_text(encoding="utf-8"))
initialized = json.loads(Path(sys.argv[7]).read_text(encoding="utf-8"))
round1 = json.loads(Path(sys.argv[8]).read_text(encoding="utf-8"))
round2 = json.loads(Path(sys.argv[9]).read_text(encoding="utf-8"))
payload.update({
    "state": "passed",
    "reported_trajectory_no_pid_after": int(sys.argv[2]),
    "market_ws_pid_after": int(sys.argv[3]),
    "clob_order_service_pid_after": int(sys.argv[4]),
    "gateway_read_only": True,
    "gateway_order_submission_enabled": False,
    "authorizes_order": False,
    "live_submit_authorized": False,
    "live_pause_present": True,
    "proxy_port": 7897,
    "forward_store_precheck_receipt": str(Path(sys.argv[5]).resolve()),
    "forward_store_initializer_receipt": str(Path(sys.argv[6]).resolve()),
    "forward_store_initialized_receipt": str(Path(sys.argv[7]).resolve()),
    "forward_store_round1_receipt": str(Path(sys.argv[8]).resolve()),
    "forward_store_round2_receipt": str(Path(sys.argv[9]).resolve()),
    "forward_store_rounds": {
        "round1": round1,
        "round2": round2,
        "cursor_non_decreasing": int(round2["last_source_rowid"]) >= int(round1["last_source_rowid"]),
        "generation_non_decreasing": int(round2["generation"]) >= int(round1["generation"]),
        "second_round_duplicate_inserts": 0,
    },
    "forward_store_initializer_api": initializer.get("api"),
    "forward_store_initialized_state": initialized.get("state"),
    "forward_store_precheck_state": precheck.get("state"),
})
path.write_text(
    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY

apply_bounded_maintenance_retention "$RETENTION_RECEIPT"
restore_target_pause_state "$TARGET_PAUSE_PREEXISTED" "$TARGET_PAUSE_BACKUP"
/bin/rm -f \
  "$FORWARD_STORE_BACKUP" "$TARGET_PAUSE_BACKUP" "$CUTOVER_JOURNAL"
success=1
trap - EXIT
generation="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["generation"])' "$CUTOVER_RECEIPT")"
echo "target_ssot_v4_cutover_ok receipt=$CUTOVER_RECEIPT generation=$generation reported_pid=$reported_pid_after market_ws_pid=$market_ws_pid_after clob_pid=$clob_pid_after"

"""Atomic V6-to-V7 migration for canonical republisher OOS artifacts."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .canonical_ssot_schema import (
    EXPECTED_COLUMNS,
    V6_TRIGGERS,
    V7_TRIGGERS,
    inspect_canonical_ssot_schema,
)
from .canonical_ssot_v6_migration import (
    _assert_disk_capacity,
    _assert_no_incoming_foreign_keys,
    _row_fingerprint,
    _trigger_snapshot,
)
from .contracts import content_sha256
from .migration_quiescence import validate_writer_quiescence_receipt


SCHEMA = "bayes_basket_target_canonical_ssot_fact_v7_migration_receipt_v1"
CONFIRMATION = "migrate-canonical-ssot-fact-v7-production-no-live"
TABLE = "canonical_ssot_fact"
NEW_TABLE = "canonical_ssot_fact_v7_new"
BACKUP_TABLE = "canonical_ssot_fact_v6_replaced"
SOURCE_TRIGGERS = V6_TRIGGERS
TARGET_TRIGGERS = V7_TRIGGERS
RETIRED_TRIGGERS = (
    "canonical_ssot_fact_v6_replaced_no_update",
    "canonical_ssot_fact_v6_replaced_no_delete",
)
_FAULT_STAGES = {"after_create", "after_copy", "after_swap", "before_commit"}

TARGET_TABLE_DDL = """
CREATE TABLE canonical_ssot_fact (
  fact_id TEXT PRIMARY KEY,
  fact_type TEXT NOT NULL CHECK(fact_type IN ('model','truth','residual','activation','activation_supersession','ensemble_member','native_arrival','ensemble_member_arrival_binding','ensemble_member_receipt_manifest','republisher_oos_artifact')),
  scope_key TEXT NOT NULL,
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  fact_json TEXT NOT NULL,
  lineage_json TEXT NOT NULL,
  ingested_at_utc TEXT NOT NULL,
  UNIQUE(fact_type, content_sha256),
  UNIQUE(fact_type, scope_key)
)
""".strip()
TARGET_NEW_TABLE_DDL = TARGET_TABLE_DDL.replace(
    "CREATE TABLE canonical_ssot_fact", f"CREATE TABLE {NEW_TABLE}", 1
)
TARGET_TRIGGER_DDL = (
    "CREATE TRIGGER canonical_ssot_fact_no_update BEFORE UPDATE ON canonical_ssot_fact "
    "BEGIN SELECT RAISE(ABORT,'append_only'); END",
    "CREATE TRIGGER canonical_ssot_fact_no_delete BEFORE DELETE ON canonical_ssot_fact "
    "BEGIN SELECT RAISE(ABORT,'append_only'); END",
    "CREATE TRIGGER canonical_ssot_fact_ingress_only BEFORE INSERT ON canonical_ssot_fact "
    "WHEN canonical_ingress_authorized()!=1 "
    "BEGIN SELECT RAISE(ABORT,'canonical_ingress_only'); END",
)


class CanonicalSSOTV7MigrationError(RuntimeError):
    """Fail-closed V7 migration error."""


def _schema_state(conn: sqlite3.Connection) -> str:
    inspection = inspect_canonical_ssot_schema(conn)
    state = str(inspection.get("state") or "")
    if state not in {"V6", "V7"} or inspection.get("errors"):
        raise CanonicalSSOTV7MigrationError(
            "canonical_ssot_v7_source_schema_invalid:"
            + ",".join(str(value) for value in inspection.get("errors") or [])
        )
    return state


def _fault(stage: str, requested: str | None) -> None:
    if requested == stage:
        raise CanonicalSSOTV7MigrationError(
            f"canonical_ssot_v7_injected_fault:{stage}"
        )


def _receipt(
    *,
    state: str,
    database: Path,
    schema_before: str,
    schema_after: str,
    before: tuple[int, str, dict[str, int]],
    after: tuple[int, str, dict[str, int]],
    capacity: Mapping[str, int],
    writer_receipt_sha256: str,
    triggers_before: Mapping[str, str],
    triggers_after: Mapping[str, str],
    writes_database: bool,
) -> dict[str, Any]:
    identity = {
        "schema": SCHEMA,
        "state": state,
        "database": str(database),
        "schema_before": schema_before,
        "schema_after": schema_after,
        "row_count_before": before[0],
        "row_count_after": after[0],
        "rows_content_sha256_before": before[1],
        "rows_content_sha256_after": after[1],
        "fact_type_counts_before": before[2],
        "fact_type_counts_after": after[2],
        "writer_quiescence_receipt_sha256": writer_receipt_sha256,
        "trigger_names_before": sorted(triggers_before),
        "trigger_names_after": sorted(triggers_after),
        **dict(capacity),
        "writes_database": writes_database,
        "writes_orders": False,
        "calls_order_gateway": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def migrate_canonical_ssot_fact_v7(
    db_path: Path,
    *,
    apply: bool = False,
    confirmation: str | None = None,
    writer_quiescence_receipt: Mapping[str, Any] | None = None,
    inject_fault_at: str | None = None,
) -> dict[str, Any]:
    supplied = db_path.expanduser()
    if supplied.is_symlink():
        raise CanonicalSSOTV7MigrationError(
            "canonical_ssot_v7_database_path_invalid"
        )
    database = supplied.resolve(strict=True)
    if not database.is_file():
        raise CanonicalSSOTV7MigrationError(
            "canonical_ssot_v7_database_path_invalid"
        )
    if inject_fault_at is not None and inject_fault_at not in _FAULT_STAGES:
        raise CanonicalSSOTV7MigrationError(
            "canonical_ssot_v7_injected_fault_stage_invalid"
        )
    capacity = _assert_disk_capacity(database)
    if not apply:
        conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA query_only=ON")
            state = _schema_state(conn)
            triggers = _trigger_snapshot(
                conn,
                trigger_names=(TARGET_TRIGGERS if state == "V7" else SOURCE_TRIGGERS),
            )
            before = _row_fingerprint(conn)
        finally:
            conn.close()
        identity = {
            "schema": SCHEMA,
            "state": (
                "PREVIEW_ALREADY_V7" if state == "V7" else "PREVIEW_REQUIRES_APPLY"
            ),
            "database": str(database),
            "schema_before": state,
            "schema_after_if_applied": "V7",
            "row_count": before[0],
            "rows_content_sha256": before[1],
            "fact_type_counts": before[2],
            "trigger_names": sorted(triggers),
            **capacity,
            "writes_database": False,
            "writes_orders": False,
            "calls_order_gateway": False,
            "requires_writer_quiescence": state != "V7",
            "confirmation_required": state != "V7",
        }
        return {**identity, "content_sha256": content_sha256(identity)}
    if confirmation != CONFIRMATION:
        raise CanonicalSSOTV7MigrationError(
            "canonical_ssot_v7_confirmation_required"
        )
    if writer_quiescence_receipt is None:
        raise CanonicalSSOTV7MigrationError(
            "canonical_ssot_v7_writer_quiescence_receipt_required"
        )
    try:
        validated = validate_writer_quiescence_receipt(
            writer_quiescence_receipt,
            db_path=database,
            require_current_database_identity=True,
        )
    except ValueError as exc:
        raise CanonicalSSOTV7MigrationError(
            "canonical_ssot_v7_writer_quiescence_invalid"
        ) from exc

    conn = sqlite3.connect(database, timeout=60.0, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("BEGIN EXCLUSIVE")
        try:
            try:
                validated = validate_writer_quiescence_receipt(
                    writer_quiescence_receipt,
                    db_path=database,
                    require_current_database_identity=True,
                )
            except ValueError as exc:
                raise CanonicalSSOTV7MigrationError(
                    "canonical_ssot_v7_writer_quiescence_changed_before_lock"
                ) from exc
            state_before = _schema_state(conn)
            triggers_before = _trigger_snapshot(
                conn,
                trigger_names=(
                    TARGET_TRIGGERS if state_before == "V7" else SOURCE_TRIGGERS
                ),
            )
            before = _row_fingerprint(conn)
            if state_before == "V7":
                conn.execute("ROLLBACK")
                return _receipt(
                    state="ALREADY_V7_NO_WRITE",
                    database=database,
                    schema_before="V7",
                    schema_after="V7",
                    before=before,
                    after=before,
                    capacity=capacity,
                    writer_receipt_sha256=str(validated["receipt_sha256"]),
                    triggers_before=triggers_before,
                    triggers_after=triggers_before,
                    writes_database=False,
                )
            for leftover in (NEW_TABLE, BACKUP_TABLE):
                if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (leftover,),
                ).fetchone() is not None:
                    raise CanonicalSSOTV7MigrationError(
                        "canonical_ssot_v7_leftover_table_present"
                    )
            _assert_no_incoming_foreign_keys(conn)
            conn.execute(TARGET_NEW_TABLE_DDL)
            _fault("after_create", inject_fault_at)
            columns = ",".join(EXPECTED_COLUMNS)
            conn.execute(
                f"INSERT INTO {NEW_TABLE}({columns}) SELECT {columns} FROM {TABLE}"
            )
            if _row_fingerprint(conn, table=NEW_TABLE) != before:
                raise CanonicalSSOTV7MigrationError(
                    "canonical_ssot_v7_copy_fingerprint_mismatch"
                )
            _fault("after_copy", inject_fault_at)
            for trigger in SOURCE_TRIGGERS:
                conn.execute(f"DROP TRIGGER {trigger}")
            conn.execute(f"ALTER TABLE {TABLE} RENAME TO {BACKUP_TABLE}")
            conn.execute(f"ALTER TABLE {NEW_TABLE} RENAME TO {TABLE}")
            for ddl in TARGET_TRIGGER_DDL:
                conn.execute(ddl)
            conn.execute(
                f"CREATE TRIGGER {RETIRED_TRIGGERS[0]} BEFORE UPDATE ON {BACKUP_TABLE} "
                "BEGIN SELECT RAISE(ABORT,'append_only'); END"
            )
            conn.execute(
                f"CREATE TRIGGER {RETIRED_TRIGGERS[1]} BEFORE DELETE ON {BACKUP_TABLE} "
                "BEGIN SELECT RAISE(ABORT,'append_only'); END"
            )
            _fault("after_swap", inject_fault_at)
            if _schema_state(conn) != "V7":
                raise CanonicalSSOTV7MigrationError(
                    "canonical_ssot_v7_target_schema_invalid"
                )
            retired = inspect_canonical_ssot_schema(
                conn,
                table=BACKUP_TABLE,
                trigger_names=RETIRED_TRIGGERS,
            )
            if retired["state"] != "V6" or retired["errors"]:
                raise CanonicalSSOTV7MigrationError(
                    "canonical_ssot_v7_retired_schema_invalid"
                )
            triggers_after = _trigger_snapshot(
                conn, trigger_names=TARGET_TRIGGERS
            )
            after = _row_fingerprint(conn)
            if after != before:
                raise CanonicalSSOTV7MigrationError(
                    "canonical_ssot_v7_post_swap_fingerprint_mismatch"
                )
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise CanonicalSSOTV7MigrationError(
                    "canonical_ssot_v7_quick_check_failed"
                )
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise CanonicalSSOTV7MigrationError(
                    "canonical_ssot_v7_foreign_key_check_failed"
                )
            _fault("before_commit", inject_fault_at)
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return _receipt(
        state="APPLIED_V7",
        database=database,
        schema_before="V6",
        schema_after="V7",
        before=before,
        after=after,
        capacity=capacity,
        writer_receipt_sha256=str(validated["receipt_sha256"]),
        triggers_before=triggers_before,
        triggers_after=triggers_after,
        writes_database=True,
    )


__all__ = [
    "BACKUP_TABLE",
    "CONFIRMATION",
    "CanonicalSSOTV7MigrationError",
    "NEW_TABLE",
    "RETIRED_TRIGGERS",
    "SCHEMA",
    "TARGET_TABLE_DDL",
    "migrate_canonical_ssot_fact_v7",
]

"""Atomic V7-to-V8 migration for republisher cross-selection facts."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .canonical_ssot_migration_common import (
    assert_disk_capacity,
    assert_no_incoming_foreign_keys,
    row_fingerprint,
    trigger_snapshot,
)
from .canonical_ssot_schema import (
    EXPECTED_COLUMNS,
    V7_TRIGGERS,
    V8_TRIGGERS,
    inspect_canonical_ssot_schema,
)
from .contracts import content_sha256
from .migration_quiescence import validate_writer_quiescence_receipt


SCHEMA = "bayes_basket_target_canonical_ssot_fact_v8_migration_receipt_v1"
CONFIRMATION = "migrate-canonical-ssot-fact-v8-production-no-live"
TABLE = "canonical_ssot_fact"
NEW_TABLE = "canonical_ssot_fact_v8_new"
BACKUP_TABLE = "canonical_ssot_fact_v7_replaced"
SOURCE_TRIGGERS = V7_TRIGGERS
TARGET_TRIGGERS = V8_TRIGGERS
RETIRED_TRIGGERS = (
    "canonical_ssot_fact_v7_replaced_no_update",
    "canonical_ssot_fact_v7_replaced_no_delete",
)
_FAULT_STAGES = {"after_create", "after_copy", "after_swap", "before_commit"}

TARGET_TABLE_DDL = """
CREATE TABLE canonical_ssot_fact (
  fact_id TEXT PRIMARY KEY,
  fact_type TEXT NOT NULL CHECK(fact_type IN ('model','truth','residual','activation','activation_supersession','ensemble_member','native_arrival','ensemble_member_arrival_binding','ensemble_member_receipt_manifest','republisher_oos_artifact','republisher_cross_selection')),
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


class CanonicalSSOTV8MigrationError(RuntimeError):
    """Fail-closed V8 migration error."""


def _capacity(database: Path) -> dict[str, int]:
    return assert_disk_capacity(
        database,
        error_type=CanonicalSSOTV8MigrationError,
        error_prefix="canonical_ssot_v8",
    )


def _fingerprint(
    conn: sqlite3.Connection, *, table: str = TABLE
) -> tuple[int, str, dict[str, int]]:
    return row_fingerprint(conn, table=table)


def _triggers(
    conn: sqlite3.Connection,
    *,
    table: str = TABLE,
    trigger_names: tuple[str, ...],
) -> dict[str, str]:
    return trigger_snapshot(
        conn,
        table=table,
        trigger_names=trigger_names,
        error_type=CanonicalSSOTV8MigrationError,
        error_prefix="canonical_ssot_v8",
    )


def _schema_state(conn: sqlite3.Connection) -> str:
    inspection = inspect_canonical_ssot_schema(conn)
    state = str(inspection.get("state") or "")
    if state not in {"V7", "V8"} or inspection.get("errors"):
        raise CanonicalSSOTV8MigrationError(
            "canonical_ssot_v8_source_schema_invalid:"
            + ",".join(str(value) for value in inspection.get("errors") or [])
        )
    return state


def _fault(stage: str, requested: str | None) -> None:
    if requested == stage:
        raise CanonicalSSOTV8MigrationError(
            f"canonical_ssot_v8_injected_fault:{stage}"
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


def migrate_canonical_ssot_fact_v8(
    db_path: Path,
    *,
    apply: bool = False,
    confirmation: str | None = None,
    writer_quiescence_receipt: Mapping[str, Any] | None = None,
    inject_fault_at: str | None = None,
) -> dict[str, Any]:
    supplied = db_path.expanduser()
    if supplied.is_symlink():
        raise CanonicalSSOTV8MigrationError(
            "canonical_ssot_v8_database_path_invalid"
        )
    database = supplied.resolve(strict=True)
    if not database.is_file():
        raise CanonicalSSOTV8MigrationError(
            "canonical_ssot_v8_database_path_invalid"
        )
    if inject_fault_at is not None and inject_fault_at not in _FAULT_STAGES:
        raise CanonicalSSOTV8MigrationError(
            "canonical_ssot_v8_injected_fault_stage_invalid"
        )
    capacity = _capacity(database)
    if not apply:
        conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA query_only=ON")
            state = _schema_state(conn)
            triggers = _triggers(
                conn,
                trigger_names=(TARGET_TRIGGERS if state == "V8" else SOURCE_TRIGGERS),
            )
            before = _fingerprint(conn)
        finally:
            conn.close()
        identity = {
            "schema": SCHEMA,
            "state": "PREVIEW_ALREADY_V8" if state == "V8" else "PREVIEW_REQUIRES_APPLY",
            "database": str(database),
            "schema_before": state,
            "schema_after_if_applied": "V8",
            "row_count": before[0],
            "rows_content_sha256": before[1],
            "fact_type_counts": before[2],
            "trigger_names": sorted(triggers),
            **capacity,
            "writes_database": False,
            "writes_orders": False,
            "calls_order_gateway": False,
            "requires_writer_quiescence": state != "V8",
            "confirmation_required": state != "V8",
        }
        return {**identity, "content_sha256": content_sha256(identity)}
    if confirmation != CONFIRMATION:
        raise CanonicalSSOTV8MigrationError(
            "canonical_ssot_v8_confirmation_required"
        )
    if writer_quiescence_receipt is None:
        raise CanonicalSSOTV8MigrationError(
            "canonical_ssot_v8_writer_quiescence_receipt_required"
        )
    try:
        validated = validate_writer_quiescence_receipt(
            writer_quiescence_receipt,
            db_path=database,
            require_current_database_identity=True,
        )
    except ValueError as exc:
        raise CanonicalSSOTV8MigrationError(
            "canonical_ssot_v8_writer_quiescence_invalid"
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
                raise CanonicalSSOTV8MigrationError(
                    "canonical_ssot_v8_writer_quiescence_changed_before_lock"
                ) from exc
            state_before = _schema_state(conn)
            triggers_before = _triggers(
                conn,
                trigger_names=(TARGET_TRIGGERS if state_before == "V8" else SOURCE_TRIGGERS),
            )
            before = _fingerprint(conn)
            if state_before == "V8":
                conn.execute("ROLLBACK")
                return _receipt(
                    state="ALREADY_V8_NO_WRITE",
                    database=database,
                    schema_before="V8",
                    schema_after="V8",
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
                    raise CanonicalSSOTV8MigrationError(
                        "canonical_ssot_v8_leftover_table_present"
                    )
            assert_no_incoming_foreign_keys(
                conn,
                table=TABLE,
                error_type=CanonicalSSOTV8MigrationError,
                error_prefix="canonical_ssot_v8",
            )
            conn.execute(TARGET_NEW_TABLE_DDL)
            _fault("after_create", inject_fault_at)
            columns = ",".join(EXPECTED_COLUMNS)
            conn.execute(
                f"INSERT INTO {NEW_TABLE}({columns}) SELECT {columns} FROM {TABLE}"
            )
            if _fingerprint(conn, table=NEW_TABLE) != before:
                raise CanonicalSSOTV8MigrationError(
                    "canonical_ssot_v8_copy_fingerprint_mismatch"
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
            if _schema_state(conn) != "V8":
                raise CanonicalSSOTV8MigrationError(
                    "canonical_ssot_v8_target_schema_invalid"
                )
            retired = inspect_canonical_ssot_schema(
                conn,
                table=BACKUP_TABLE,
                trigger_names=RETIRED_TRIGGERS,
            )
            if retired["state"] != "V7" or retired["errors"]:
                raise CanonicalSSOTV8MigrationError(
                    "canonical_ssot_v8_retired_schema_invalid"
                )
            triggers_after = _triggers(conn, trigger_names=TARGET_TRIGGERS)
            after = _fingerprint(conn)
            if after != before:
                raise CanonicalSSOTV8MigrationError(
                    "canonical_ssot_v8_post_swap_fingerprint_mismatch"
                )
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise CanonicalSSOTV8MigrationError(
                    "canonical_ssot_v8_quick_check_failed"
                )
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise CanonicalSSOTV8MigrationError(
                    "canonical_ssot_v8_foreign_key_check_failed"
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
        state="APPLIED_V8",
        database=database,
        schema_before="V7",
        schema_after="V8",
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
    "CanonicalSSOTV8MigrationError",
    "NEW_TABLE",
    "RETIRED_TRIGGERS",
    "SCHEMA",
    "TARGET_TABLE_DDL",
    "migrate_canonical_ssot_fact_v8",
]

"""Atomic V5-to-V6 migration of the canonical append-only fact store.

V6 appends ``ensemble_member_receipt_manifest`` to the fact-type CHECK.  It
retains the binding type for future correctly scoped facts, but a V5 binding
is a legacy one-object representation and must never be copied.  Therefore a
V5 source containing even one binding fails closed before any write.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
from typing import Any, Mapping

from .canonical_ssot_schema import (
    EXPECTED_COLUMNS,
    V5_TRIGGERS,
    V6_TRIGGERS,
    inspect_canonical_ssot_schema,
)
from .contracts import content_sha256
from .migration_quiescence import validate_writer_quiescence_receipt


SCHEMA = "bayes_basket_target_canonical_ssot_fact_v6_migration_receipt_v1"
CONFIRMATION = "migrate-canonical-ssot-fact-v6-production-no-live"
TABLE = "canonical_ssot_fact"
NEW_TABLE = "canonical_ssot_fact_v6_new"
BACKUP_TABLE = "canonical_ssot_fact_v5_replaced"
SOURCE_TRIGGERS = V5_TRIGGERS
TARGET_TRIGGERS = V6_TRIGGERS
RETIRED_TRIGGERS = (
    "canonical_ssot_fact_retired_v5_no_update",
    "canonical_ssot_fact_retired_v5_no_delete",
)
LEGACY_BINDING_FACT_TYPE = "ensemble_member_arrival_binding"
TARGET_TABLE_DDL = """
CREATE TABLE canonical_ssot_fact (
  fact_id TEXT PRIMARY KEY,
  fact_type TEXT NOT NULL CHECK(fact_type IN ('model','truth','residual','activation','activation_supersession','ensemble_member','native_arrival','ensemble_member_arrival_binding','ensemble_member_receipt_manifest')),
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
TARGET_TRIGGER_DDL = {
    "canonical_ssot_fact_no_update": (
        "CREATE TRIGGER canonical_ssot_fact_no_update BEFORE UPDATE ON canonical_ssot_fact "
        "BEGIN SELECT RAISE(ABORT,'append_only'); END"
    ),
    "canonical_ssot_fact_no_delete": (
        "CREATE TRIGGER canonical_ssot_fact_no_delete BEFORE DELETE ON canonical_ssot_fact "
        "BEGIN SELECT RAISE(ABORT,'append_only'); END"
    ),
    "canonical_ssot_fact_ingress_only": (
        "CREATE TRIGGER canonical_ssot_fact_ingress_only BEFORE INSERT ON canonical_ssot_fact "
        "WHEN canonical_ingress_authorized()!=1 "
        "BEGIN SELECT RAISE(ABORT,'canonical_ingress_only'); END"
    ),
}
_FAULT_STAGES = {"after_create", "after_copy", "after_swap", "before_commit"}


class CanonicalSSOTV6MigrationError(RuntimeError):
    """Fail-closed V6 migration error."""


def _schema_state(conn: sqlite3.Connection) -> str:
    inspection = inspect_canonical_ssot_schema(conn)
    state = str(inspection["state"])
    if state not in {"V5", "V6"}:
        raise CanonicalSSOTV6MigrationError(
            "canonical_ssot_v6_schema_invalid:"
            + ",".join(str(value) for value in inspection["errors"])
        )
    return state


def _trigger_snapshot(
    conn: sqlite3.Connection,
    *,
    table: str = TABLE,
    trigger_names: tuple[str, ...],
) -> dict[str, str]:
    rows = {
        str(name): str(sql or "")
        for name, sql in conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
            (table,),
        )
    }
    if set(rows) != set(trigger_names):
        raise CanonicalSSOTV6MigrationError(
            "canonical_ssot_v6_trigger_set_mismatch"
        )
    for name, sql in rows.items():
        compact = "".join(sql.split()).lower()
        if name == "canonical_ssot_fact_ingress_only":
            valid = (
                "beforeinsertoncanonical_ssot_fact" in compact
                and "canonical_ingress_authorized()!=1" in compact
                and "raise(abort,'canonical_ingress_only')" in compact
            )
        else:
            valid = "raise(abort,'append_only')" in compact
        if not valid:
            raise CanonicalSSOTV6MigrationError(
                f"canonical_ssot_v6_trigger_semantics_mismatch:{name}"
            )
    return dict(sorted(rows.items()))


def _row_fingerprint(
    conn: sqlite3.Connection, *, table: str = TABLE
) -> tuple[int, str, dict[str, int]]:
    digest = hashlib.sha256()
    counts: dict[str, int] = {}
    row_count = 0
    columns = ",".join(EXPECTED_COLUMNS)
    for row in conn.execute(f"SELECT {columns} FROM {table} ORDER BY fact_id"):
        row_count += 1
        fact_type = str(row[1])
        counts[fact_type] = counts.get(fact_type, 0) + 1
        encoded = json.dumps(
            list(row), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return row_count, digest.hexdigest(), dict(sorted(counts.items()))


def _legacy_binding_count(conn: sqlite3.Connection, *, table: str = TABLE) -> int:
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE fact_type=?",
            (LEGACY_BINDING_FACT_TYPE,),
        ).fetchone()[0]
    )


def _assert_no_legacy_bindings(conn: sqlite3.Connection) -> None:
    if _legacy_binding_count(conn) != 0:
        raise CanonicalSSOTV6MigrationError(
            "canonical_ssot_v6_legacy_v5_binding_present"
        )


def _assert_no_incoming_foreign_keys(conn: sqlite3.Connection) -> None:
    for (table_name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ):
        for row in conn.execute(
            f"PRAGMA foreign_key_list({json.dumps(str(table_name))})"
        ):
            if str(row[2]) == TABLE:
                raise CanonicalSSOTV6MigrationError(
                    "canonical_ssot_v6_incoming_foreign_key_present"
                )


def _assert_disk_capacity(database: Path) -> dict[str, int]:
    database_bytes = database.stat().st_size
    free_bytes = shutil.disk_usage(database.parent).free
    required_free_bytes = max(64 * 1024 * 1024, database_bytes * 3)
    if free_bytes < required_free_bytes:
        raise CanonicalSSOTV6MigrationError(
            "canonical_ssot_v6_disk_capacity_insufficient"
        )
    return {
        "database_bytes": database_bytes,
        "free_bytes": free_bytes,
        "required_free_bytes": required_free_bytes,
    }


def _fault(stage: str, requested: str | None) -> None:
    if requested is not None and requested not in _FAULT_STAGES:
        raise CanonicalSSOTV6MigrationError(
            "canonical_ssot_v6_injected_fault_stage_invalid"
        )
    if requested == stage:
        raise CanonicalSSOTV6MigrationError(
            f"canonical_ssot_v6_injected_fault:{stage}"
        )


def _identity(
    *,
    state: str,
    database: Path,
    schema_before: str,
    schema_after: str,
    before: tuple[int, str, dict[str, int]],
    after: tuple[int, str, dict[str, int]],
    v5_legacy_binding_count: int,
    capacity: Mapping[str, int],
    writer_receipt_sha256: str | None = None,
    triggers_before: Mapping[str, str] | None = None,
    triggers_after: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    identity: dict[str, Any] = {
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
        "v5_legacy_binding_count": v5_legacy_binding_count,
        "v5_legacy_binding_rows_copied": 0,
        **dict(capacity),
        "writes_orders": False,
        "calls_order_gateway": False,
    }
    if writer_receipt_sha256 is not None:
        identity["writer_quiescence_receipt_sha256"] = writer_receipt_sha256
    if triggers_before is not None:
        identity["trigger_names_before"] = sorted(triggers_before)
    if triggers_after is not None:
        identity["trigger_names_after"] = sorted(triggers_after)
    return {**identity, "content_sha256": content_sha256(identity)}


def migrate_canonical_ssot_fact_v6(
    db_path: Path,
    *,
    apply: bool = False,
    confirmation: str | None = None,
    writer_quiescence_receipt: Mapping[str, Any] | None = None,
    inject_fault_at: str | None = None,
) -> dict[str, Any]:
    """Preview or atomically apply the V5-to-V6 CHECK expansion."""

    supplied_path = db_path.expanduser()
    if supplied_path.is_symlink():
        raise CanonicalSSOTV6MigrationError(
            "canonical_ssot_v6_database_path_invalid"
        )
    database = supplied_path.resolve(strict=True)
    if not database.is_file():
        raise CanonicalSSOTV6MigrationError(
            "canonical_ssot_v6_database_path_invalid"
        )
    capacity = _assert_disk_capacity(database)
    if inject_fault_at is not None and inject_fault_at not in _FAULT_STAGES:
        raise CanonicalSSOTV6MigrationError(
            "canonical_ssot_v6_injected_fault_stage_invalid"
        )

    if not apply:
        conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA query_only=ON")
            state = _schema_state(conn)
            if state == "V5":
                _assert_no_legacy_bindings(conn)
            triggers = _trigger_snapshot(
                conn,
                trigger_names=(TARGET_TRIGGERS if state == "V6" else SOURCE_TRIGGERS),
            )
            before = _row_fingerprint(conn)
        finally:
            conn.close()
        identity = {
            "schema": SCHEMA,
            "state": "PREVIEW_ALREADY_V6" if state == "V6" else "PREVIEW_REQUIRES_APPLY",
            "database": str(database),
            "schema_before": state,
            "schema_after_if_applied": "V6",
            "row_count": before[0],
            "rows_content_sha256": before[1],
            "fact_type_counts": before[2],
            "v5_legacy_binding_count": 0,
            "v5_legacy_binding_rows_copied": 0,
            "trigger_names": sorted(triggers),
            **capacity,
            "writes_database": False,
            "writes_orders": False,
            "calls_order_gateway": False,
            "requires_writer_quiescence": state != "V6",
            "confirmation_required": state != "V6",
        }
        return {**identity, "content_sha256": content_sha256(identity)}

    if confirmation != CONFIRMATION:
        raise CanonicalSSOTV6MigrationError(
            "canonical_ssot_v6_confirmation_required"
        )
    if writer_quiescence_receipt is None:
        raise CanonicalSSOTV6MigrationError(
            "canonical_ssot_v6_writer_quiescence_receipt_required"
        )
    try:
        validated_quiescence = validate_writer_quiescence_receipt(
            writer_quiescence_receipt,
            db_path=database,
            require_current_database_identity=True,
        )
    except ValueError as exc:
        raise CanonicalSSOTV6MigrationError(
            "canonical_ssot_v6_writer_quiescence_invalid"
        ) from exc

    conn = sqlite3.connect(database, timeout=60.0, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("BEGIN EXCLUSIVE")
        try:
            try:
                validated_quiescence = validate_writer_quiescence_receipt(
                    writer_quiescence_receipt,
                    db_path=database,
                    require_current_database_identity=True,
                )
            except ValueError as exc:
                raise CanonicalSSOTV6MigrationError(
                    "canonical_ssot_v6_writer_quiescence_changed_before_lock"
                ) from exc
            state_before = _schema_state(conn)
            if state_before == "V5":
                _assert_no_legacy_bindings(conn)
            triggers_before = _trigger_snapshot(
                conn,
                trigger_names=(
                    TARGET_TRIGGERS if state_before == "V6" else SOURCE_TRIGGERS
                ),
            )
            before = _row_fingerprint(conn)
            if state_before == "V6":
                conn.execute("ROLLBACK")
                return _identity(
                    state="ALREADY_V6_NO_WRITE",
                    database=database,
                    schema_before="V6",
                    schema_after="V6",
                    before=before,
                    after=before,
                    v5_legacy_binding_count=0,
                    capacity=capacity,
                    writer_receipt_sha256=str(validated_quiescence["receipt_sha256"]),
                    triggers_before=triggers_before,
                    triggers_after=triggers_before,
                )

            for leftover in (NEW_TABLE, BACKUP_TABLE):
                if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (leftover,),
                ).fetchone() is not None:
                    raise CanonicalSSOTV6MigrationError(
                        "canonical_ssot_v6_leftover_table_present"
                    )
            _assert_no_incoming_foreign_keys(conn)
            conn.execute(TARGET_NEW_TABLE_DDL)
            _fault("after_create", inject_fault_at)
            columns = ",".join(EXPECTED_COLUMNS)
            conn.execute(
                f"INSERT INTO {NEW_TABLE}({columns}) "
                f"SELECT {columns} FROM {TABLE} WHERE fact_type<>?",
                (LEGACY_BINDING_FACT_TYPE,),
            )
            if _row_fingerprint(conn, table=NEW_TABLE) != before:
                raise CanonicalSSOTV6MigrationError(
                    "canonical_ssot_v6_copy_fingerprint_mismatch"
                )
            if _legacy_binding_count(conn, table=NEW_TABLE) != 0:
                raise CanonicalSSOTV6MigrationError(
                    "canonical_ssot_v6_legacy_binding_copied"
                )
            _fault("after_copy", inject_fault_at)
            for trigger in SOURCE_TRIGGERS:
                conn.execute(f"DROP TRIGGER {trigger}")
            conn.execute(f"ALTER TABLE {TABLE} RENAME TO {BACKUP_TABLE}")
            conn.execute(f"ALTER TABLE {NEW_TABLE} RENAME TO {TABLE}")
            for ddl in TARGET_TRIGGER_DDL.values():
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
            if _schema_state(conn) != "V6":
                raise CanonicalSSOTV6MigrationError(
                    "canonical_ssot_v6_target_schema_invalid"
                )
            retired = inspect_canonical_ssot_schema(
                conn,
                table=BACKUP_TABLE,
                trigger_names=RETIRED_TRIGGERS,
            )
            if retired["state"] != "V5" or retired["errors"]:
                raise CanonicalSSOTV6MigrationError(
                    "canonical_ssot_v6_retired_schema_invalid:"
                    + ",".join(str(value) for value in retired["errors"])
                )
            triggers_after = _trigger_snapshot(conn, trigger_names=TARGET_TRIGGERS)
            after = _row_fingerprint(conn)
            if after != before:
                raise CanonicalSSOTV6MigrationError(
                    "canonical_ssot_v6_post_swap_fingerprint_mismatch"
                )
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise CanonicalSSOTV6MigrationError(
                    "canonical_ssot_v6_quick_check_failed"
                )
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise CanonicalSSOTV6MigrationError(
                    "canonical_ssot_v6_foreign_key_check_failed"
                )
            _fault("before_commit", inject_fault_at)
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    return _identity(
        state="APPLIED_V6",
        database=database,
        schema_before="V5",
        schema_after="V6",
        before=before,
        after=after,
        v5_legacy_binding_count=0,
        capacity=capacity,
        writer_receipt_sha256=str(validated_quiescence["receipt_sha256"]),
        triggers_before=triggers_before,
        triggers_after=triggers_after,
    )


__all__ = [
    "BACKUP_TABLE",
    "CONFIRMATION",
    "CanonicalSSOTV6MigrationError",
    "LEGACY_BINDING_FACT_TYPE",
    "NEW_TABLE",
    "RETIRED_TRIGGERS",
    "SCHEMA",
    "TARGET_TABLE_DDL",
    "migrate_canonical_ssot_fact_v6",
]

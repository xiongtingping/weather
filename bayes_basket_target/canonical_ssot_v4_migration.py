"""Atomic V3-to-V4 expansion of the canonical append-only fact store.

V4 adds only the ``ensemble_member`` and ``native_arrival`` fact types.  It
does not reinterpret existing facts.  Preview is read-only; apply requires a
fresh writer-quiescence receipt and proves byte-for-byte row identity before
and after the transactional table swap.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from .canonical_ssot_schema import (
    EXPECTED_COLUMNS,
    V3_TRIGGERS,
    V4_TRIGGERS,
    inspect_canonical_ssot_schema,
)
from .contracts import content_sha256
from .migration_quiescence import validate_writer_quiescence_receipt


SCHEMA = "bayes_basket_target_canonical_ssot_fact_v4_migration_receipt_v1"
CONFIRMATION = "migrate-canonical-ssot-fact-v4-production-no-live"
TABLE = "canonical_ssot_fact"
NEW_TABLE = "canonical_ssot_fact_v4_new"
BACKUP_TABLE = "canonical_ssot_fact_v3_replaced"
SOURCE_TRIGGERS = V3_TRIGGERS
TARGET_TRIGGERS = V4_TRIGGERS
RETIRED_TRIGGERS = (
    "canonical_ssot_fact_retired_v3_no_update",
    "canonical_ssot_fact_retired_v3_no_delete",
)
TARGET_TABLE_DDL = """
CREATE TABLE canonical_ssot_fact (
  fact_id TEXT PRIMARY KEY,
  fact_type TEXT NOT NULL CHECK(fact_type IN ('model','truth','residual','activation','activation_supersession','ensemble_member','native_arrival')),
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


class CanonicalSSOTV4MigrationError(RuntimeError):
    """Fail-closed V4 migration error."""


def _schema_state(conn: sqlite3.Connection) -> str:
    inspection = inspect_canonical_ssot_schema(conn)
    state = str(inspection["state"])
    if state not in {"V3", "V4"}:
        raise CanonicalSSOTV4MigrationError(
            "canonical_ssot_v4_schema_invalid:"
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
        raise CanonicalSSOTV4MigrationError(
            "canonical_ssot_v4_trigger_set_mismatch"
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
            raise CanonicalSSOTV4MigrationError(
                f"canonical_ssot_v4_trigger_semantics_mismatch:{name}"
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


def _assert_no_incoming_foreign_keys(conn: sqlite3.Connection) -> None:
    for (table_name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ):
        for row in conn.execute(
            f"PRAGMA foreign_key_list({json.dumps(str(table_name))})"
        ):
            if str(row[2]) == TABLE:
                raise CanonicalSSOTV4MigrationError(
                    "canonical_ssot_v4_incoming_foreign_key_present"
                )


def _fault(stage: str, requested: str | None) -> None:
    if requested == stage:
        raise CanonicalSSOTV4MigrationError(
            f"canonical_ssot_v4_injected_fault:{stage}"
        )


def migrate_canonical_ssot_fact_v4(
    db_path: Path,
    *,
    apply: bool = False,
    confirmation: str | None = None,
    writer_quiescence_receipt: Mapping[str, Any] | None = None,
    inject_fault_at: str | None = None,
) -> dict[str, Any]:
    """Preview or atomically apply the V3-to-V4 CHECK expansion."""

    supplied_path = db_path.expanduser()
    if supplied_path.is_symlink():
        raise CanonicalSSOTV4MigrationError(
            "canonical_ssot_v4_database_path_invalid"
        )
    database = supplied_path.resolve(strict=True)
    if not database.is_file():
        raise CanonicalSSOTV4MigrationError(
            "canonical_ssot_v4_database_path_invalid"
        )

    if not apply:
        conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA query_only=ON")
            state = _schema_state(conn)
            triggers = _trigger_snapshot(
                conn,
                trigger_names=TARGET_TRIGGERS if state == "V4" else SOURCE_TRIGGERS,
            )
            row_count, rows_sha, counts = _row_fingerprint(conn)
        finally:
            conn.close()
        identity = {
            "schema": SCHEMA,
            "state": (
                "PREVIEW_ALREADY_V4"
                if state == "V4"
                else "PREVIEW_REQUIRES_APPLY"
            ),
            "database": str(database),
            "schema_before": state,
            "schema_after_if_applied": "V4",
            "row_count": row_count,
            "rows_content_sha256": rows_sha,
            "fact_type_counts": counts,
            "trigger_names": sorted(triggers),
            "writes_database": False,
            "writes_orders": False,
            "calls_order_gateway": False,
            "requires_writer_quiescence": state != "V4",
            "confirmation_required": state != "V4",
        }
        return {**identity, "content_sha256": content_sha256(identity)}

    if confirmation != CONFIRMATION:
        raise CanonicalSSOTV4MigrationError(
            "canonical_ssot_v4_confirmation_required"
        )
    if writer_quiescence_receipt is None:
        raise CanonicalSSOTV4MigrationError(
            "canonical_ssot_v4_writer_quiescence_receipt_required"
        )
    try:
        validated_quiescence = validate_writer_quiescence_receipt(
            writer_quiescence_receipt,
            db_path=database,
            require_current_database_identity=True,
        )
    except ValueError as exc:
        raise CanonicalSSOTV4MigrationError(
            "canonical_ssot_v4_writer_quiescence_invalid"
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
                raise CanonicalSSOTV4MigrationError(
                    "canonical_ssot_v4_writer_quiescence_changed_before_lock"
                ) from exc
            state_before = _schema_state(conn)
            triggers_before = _trigger_snapshot(
                conn,
                trigger_names=(
                    TARGET_TRIGGERS if state_before == "V4" else SOURCE_TRIGGERS
                ),
            )
            before_count, before_sha, before_counts = _row_fingerprint(conn)
            if state_before == "V4":
                conn.execute("ROLLBACK")
                identity = {
                    "schema": SCHEMA,
                    "state": "ALREADY_V4_NO_WRITE",
                    "database": str(database),
                    "schema_before": "V4",
                    "schema_after": "V4",
                    "row_count_before": before_count,
                    "row_count_after": before_count,
                    "rows_content_sha256_before": before_sha,
                    "rows_content_sha256_after": before_sha,
                    "fact_type_counts": before_counts,
                    "trigger_names": sorted(triggers_before),
                    "writer_quiescence_receipt_sha256": validated_quiescence[
                        "receipt_sha256"
                    ],
                    "writes_orders": False,
                    "calls_order_gateway": False,
                }
                return {**identity, "content_sha256": content_sha256(identity)}

            for leftover in (NEW_TABLE, BACKUP_TABLE):
                if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (leftover,),
                ).fetchone() is not None:
                    raise CanonicalSSOTV4MigrationError(
                        "canonical_ssot_v4_leftover_table_present"
                    )
            _assert_no_incoming_foreign_keys(conn)
            conn.execute(TARGET_NEW_TABLE_DDL)
            _fault("after_create", inject_fault_at)
            columns = ",".join(EXPECTED_COLUMNS)
            conn.execute(
                f"INSERT INTO {NEW_TABLE}({columns}) SELECT {columns} FROM {TABLE}"
            )
            copied = _row_fingerprint(conn, table=NEW_TABLE)
            if copied != (before_count, before_sha, before_counts):
                raise CanonicalSSOTV4MigrationError(
                    "canonical_ssot_v4_copy_fingerprint_mismatch"
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
            if _schema_state(conn) != "V4":
                raise CanonicalSSOTV4MigrationError(
                    "canonical_ssot_v4_target_schema_invalid"
                )
            retired = inspect_canonical_ssot_schema(
                conn,
                table=BACKUP_TABLE,
                trigger_names=RETIRED_TRIGGERS,
            )
            if retired["state"] != "V3" or retired["errors"]:
                raise CanonicalSSOTV4MigrationError(
                    "canonical_ssot_v4_retired_schema_invalid:"
                    + ",".join(str(value) for value in retired["errors"])
                )
            triggers_after = _trigger_snapshot(
                conn, trigger_names=TARGET_TRIGGERS
            )
            after_count, after_sha, after_counts = _row_fingerprint(conn)
            if (after_count, after_sha, after_counts) != (
                before_count,
                before_sha,
                before_counts,
            ):
                raise CanonicalSSOTV4MigrationError(
                    "canonical_ssot_v4_post_swap_fingerprint_mismatch"
                )
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise CanonicalSSOTV4MigrationError(
                    "canonical_ssot_v4_quick_check_failed"
                )
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise CanonicalSSOTV4MigrationError(
                    "canonical_ssot_v4_foreign_key_check_failed"
                )
            _fault("before_commit", inject_fault_at)
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    identity = {
        "schema": SCHEMA,
        "state": "APPLIED_V4",
        "database": str(database),
        "schema_before": state_before,
        "schema_after": "V4",
        "row_count_before": before_count,
        "row_count_after": after_count,
        "rows_content_sha256_before": before_sha,
        "rows_content_sha256_after": after_sha,
        "fact_type_counts": after_counts,
        "trigger_names_before": sorted(triggers_before),
        "trigger_names_after": sorted(triggers_after),
        "retired_table": BACKUP_TABLE,
        "retired_trigger_names": list(RETIRED_TRIGGERS),
        "writer_quiescence_receipt_sha256": validated_quiescence[
            "receipt_sha256"
        ],
        "writes_orders": False,
        "calls_order_gateway": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


__all__ = [
    "CONFIRMATION",
    "CanonicalSSOTV4MigrationError",
    "migrate_canonical_ssot_fact_v4",
]

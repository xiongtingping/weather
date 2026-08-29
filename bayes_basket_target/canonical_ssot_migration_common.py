"""Version-neutral primitives for canonical SSOT table migrations."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable, Sequence, TypeVar

from .contracts import content_sha256
from .migration_quiescence import validate_writer_quiescence_receipt
from .canonical_ssot_schema import EXPECTED_COLUMNS


MigrationError = TypeVar("MigrationError", bound=Exception)


def _raise(error_type: type[MigrationError], prefix: str, code: str) -> None:
    raise error_type(f"{prefix}_{code}")


def trigger_snapshot(
    conn: sqlite3.Connection,
    *,
    table: str,
    trigger_names: Sequence[str],
    error_type: type[MigrationError],
    error_prefix: str,
) -> dict[str, str]:
    rows = {
        str(name): str(sql or "")
        for name, sql in conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
            (table,),
        )
    }
    if set(rows) != set(trigger_names):
        _raise(error_type, error_prefix, "trigger_set_mismatch")
    for name, sql in rows.items():
        compact = "".join(sql.split()).lower()
        if name.endswith("_ingress_only"):
            valid = (
                f"beforeinserton{table.lower()}" in compact
                and "canonical_ingress_authorized()!=1" in compact
                and "raise(abort,'canonical_ingress_only')" in compact
            )
        else:
            valid = "raise(abort,'append_only')" in compact
        if not valid:
            _raise(
                error_type,
                error_prefix,
                f"trigger_semantics_mismatch:{name}",
            )
    return dict(sorted(rows.items()))


def row_fingerprint(
    conn: sqlite3.Connection, *, table: str
) -> tuple[int, str, dict[str, int]]:
    digest = hashlib.sha256()
    counts: dict[str, int] = {}
    row_count = 0
    columns = ",".join(EXPECTED_COLUMNS)
    for row in conn.execute(f'SELECT {columns} FROM "{table}" ORDER BY fact_id'):
        row_count += 1
        fact_type = str(row[1])
        counts[fact_type] = counts.get(fact_type, 0) + 1
        encoded = json.dumps(
            list(row), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return row_count, digest.hexdigest(), dict(sorted(counts.items()))


def assert_no_incoming_foreign_keys(
    conn: sqlite3.Connection,
    *,
    table: str,
    error_type: type[MigrationError],
    error_prefix: str,
) -> None:
    for (table_name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ):
        for row in conn.execute(
            f"PRAGMA foreign_key_list({json.dumps(str(table_name))})"
        ):
            if str(row[2]) == table:
                _raise(error_type, error_prefix, "incoming_foreign_key_present")


def assert_disk_capacity(
    database: Path,
    *,
    error_type: type[MigrationError],
    error_prefix: str,
    table: str = "canonical_ssot_fact",
    disk_usage: Callable[[Path], object] = shutil.disk_usage,
) -> dict[str, int]:
    database_bytes = database.stat().st_size
    wal = Path(str(database) + "-wal")
    shm = Path(str(database) + "-shm")
    wal_bytes = wal.stat().st_size if wal.exists() else 0
    shm_bytes = shm.stat().st_size if shm.exists() else 0
    source_footprint_bytes = database_bytes + wal_bytes + shm_bytes
    # A schema migration copies only the active canonical fact table.  The
    # database file also contains immutable ``*_replaced`` tables from prior
    # versions; charging their full size again made the preflight grow on
    # every migration even though SQLite never copies those pages.  Measure
    # the exact logical payload of the one source table and apply a generous
    # bounded expansion for its new table, indexes, page overhead and journal.
    # This remains fail-closed without treating unrelated retained tables as
    # migration working space.
    if not table.replace("_", "").isalnum():
        _raise(error_type, error_prefix, "source_table_invalid")
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
            row = conn.execute(
                f'SELECT COUNT(*), COALESCE(SUM('
                'LENGTH(CAST(fact_id AS BLOB))+'
                'LENGTH(CAST(fact_type AS BLOB))+'
                'LENGTH(CAST(scope_key AS BLOB))+'
                'LENGTH(CAST(content_sha256 AS BLOB))+'
                'LENGTH(CAST(fact_json AS BLOB))+'
                'LENGTH(CAST(lineage_json AS BLOB))+'
                'LENGTH(CAST(ingested_at_utc AS BLOB))'
                f'),0) FROM "{table}"'
            ).fetchone()
    except sqlite3.Error as exc:
        raise error_type(f"{error_prefix}_source_table_measurement_failed") from exc
    if row is None:
        _raise(error_type, error_prefix, "source_table_measurement_failed")
    source_table_row_count = int(row[0])
    source_table_logical_bytes = int(row[1])
    usage = disk_usage(database.parent)
    free_bytes = int(getattr(usage, "free"))
    migration_copy_estimate_bytes = (
        source_table_logical_bytes * 4
        + wal_bytes
        + shm_bytes
        + 256 * 1024 * 1024
    )
    required_free_bytes = max(64 * 1024 * 1024, migration_copy_estimate_bytes)
    if free_bytes < required_free_bytes:
        _raise(error_type, error_prefix, "disk_capacity_insufficient")
    return {
        "database_bytes": database_bytes,
        "database_wal_bytes": wal_bytes,
        "database_shm_bytes": shm_bytes,
        "source_footprint_bytes": source_footprint_bytes,
        "source_table_row_count": source_table_row_count,
        "source_table_logical_bytes": source_table_logical_bytes,
        "migration_copy_estimate_bytes": migration_copy_estimate_bytes,
        "free_bytes": free_bytes,
        "required_free_bytes": required_free_bytes,
    }


def migrate_append_only_fact_schema(
    db_path: Path,
    *,
    apply: bool,
    confirmation: str | None,
    expected_confirmation: str,
    writer_quiescence_receipt: Mapping[str, Any] | None,
    inject_fault_at: str | None,
    source_state: str,
    target_state: str,
    table: str,
    new_table: str,
    backup_table: str,
    target_new_table_ddl: str,
    target_trigger_ddl: Sequence[str],
    source_triggers: Sequence[str],
    target_triggers: Sequence[str],
    retired_triggers: Sequence[str],
    inspect_schema: Callable[..., Mapping[str, Any]],
    receipt_schema: str,
    error_type: type[MigrationError],
    error_prefix: str,
) -> dict[str, Any]:
    """Atomically append fact types while preserving every historical table.

    Version-specific modules own names and DDL.  This primitive owns the
    invariant migration mechanics so later versions cannot silently lose the
    capacity, quiescence, fingerprint, rollback, or append-only gates.
    """

    fault_stages = {"after_create", "after_copy", "after_swap", "before_commit"}

    def fail(code: str) -> None:
        _raise(error_type, error_prefix, code)

    def schema_state(conn: sqlite3.Connection) -> str:
        inspection = inspect_schema(conn)
        state = str(inspection.get("state") or "")
        errors = tuple(sorted(str(value) for value in inspection.get("errors") or ()))
        if state in {source_state, target_state} and not errors:
            return state
        fail("source_schema_invalid:" + ",".join(errors))
        raise AssertionError("unreachable")

    def fingerprint(
        conn: sqlite3.Connection, target_table: str = table
    ) -> tuple[int, str, dict[str, int]]:
        return row_fingerprint(conn, table=target_table)

    def triggers(
        conn: sqlite3.Connection,
        target_table: str,
        names: Sequence[str],
    ) -> dict[str, str]:
        return trigger_snapshot(
            conn,
            table=target_table,
            trigger_names=names,
            error_type=error_type,
            error_prefix=error_prefix,
        )

    def fault(stage: str) -> None:
        if inject_fault_at == stage:
            fail(f"injected_fault:{stage}")

    def receipt(
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
            "schema": receipt_schema,
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

    supplied = db_path.expanduser()
    if supplied.is_symlink():
        fail("database_path_invalid")
    try:
        database = supplied.resolve(strict=True)
    except FileNotFoundError:
        fail("database_path_invalid")
    if not database.is_file():
        fail("database_path_invalid")
    if inject_fault_at is not None and inject_fault_at not in fault_stages:
        fail("injected_fault_stage_invalid")
    capacity = assert_disk_capacity(
        database, error_type=error_type, error_prefix=error_prefix, table=table
    )
    if not apply:
        conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA query_only=ON")
            state = schema_state(conn)
            current_triggers = triggers(conn, table, target_triggers)
            before = fingerprint(conn)
        finally:
            conn.close()
        identity = {
            "schema": receipt_schema,
            "state": (
                f"PREVIEW_ALREADY_{target_state}"
                if state == target_state
                else "PREVIEW_REQUIRES_APPLY"
            ),
            "database": str(database),
            "schema_before": state,
            "schema_after_if_applied": target_state,
            "row_count": before[0],
            "rows_content_sha256": before[1],
            "fact_type_counts": before[2],
            "trigger_names": sorted(current_triggers),
            **capacity,
            "writes_database": False,
            "writes_orders": False,
            "calls_order_gateway": False,
            "requires_writer_quiescence": state != target_state,
            "confirmation_required": state != target_state,
        }
        return {**identity, "content_sha256": content_sha256(identity)}
    if confirmation != expected_confirmation:
        fail("confirmation_required")
    if writer_quiescence_receipt is None:
        fail("writer_quiescence_receipt_required")
    try:
        validated = validate_writer_quiescence_receipt(
            writer_quiescence_receipt,
            db_path=database,
            require_current_database_identity=True,
        )
    except ValueError as exc:
        raise error_type(f"{error_prefix}_writer_quiescence_invalid") from exc

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
                raise error_type(
                    f"{error_prefix}_writer_quiescence_changed_before_lock"
                ) from exc
            state_before = schema_state(conn)
            triggers_before = triggers(
                conn,
                table,
                target_triggers if state_before == target_state else source_triggers,
            )
            before = fingerprint(conn)
            if state_before == target_state:
                conn.execute("ROLLBACK")
                return receipt(
                    state=f"ALREADY_{target_state}_NO_WRITE",
                    database=database,
                    schema_before=target_state,
                    schema_after=target_state,
                    before=before,
                    after=before,
                    capacity=capacity,
                    writer_receipt_sha256=str(validated["receipt_sha256"]),
                    triggers_before=triggers_before,
                    triggers_after=triggers_before,
                    writes_database=False,
                )
            for leftover in (new_table, backup_table):
                if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (leftover,),
                ).fetchone() is not None:
                    fail("leftover_table_present")
            assert_no_incoming_foreign_keys(
                conn, table=table, error_type=error_type, error_prefix=error_prefix
            )
            conn.execute(target_new_table_ddl)
            fault("after_create")
            columns = ",".join(EXPECTED_COLUMNS)
            conn.execute(
                f'INSERT INTO "{new_table}"({columns}) '
                f'SELECT {columns} FROM "{table}"'
            )
            if fingerprint(conn, new_table) != before:
                fail("copy_fingerprint_mismatch")
            fault("after_copy")
            for trigger_name in source_triggers:
                conn.execute(f'DROP TRIGGER "{trigger_name}"')
            conn.execute(f'ALTER TABLE "{table}" RENAME TO "{backup_table}"')
            conn.execute(f'ALTER TABLE "{new_table}" RENAME TO "{table}"')
            for ddl in target_trigger_ddl:
                conn.execute(ddl)
            conn.execute(
                f"CREATE TRIGGER {retired_triggers[0]} BEFORE UPDATE ON {backup_table} "
                "BEGIN SELECT RAISE(ABORT,'append_only'); END"
            )
            conn.execute(
                f"CREATE TRIGGER {retired_triggers[1]} BEFORE DELETE ON {backup_table} "
                "BEGIN SELECT RAISE(ABORT,'append_only'); END"
            )
            if len(retired_triggers) == 3:
                conn.execute(
                    f"CREATE TRIGGER {retired_triggers[2]} BEFORE INSERT ON {backup_table} "
                    "BEGIN SELECT RAISE(ABORT,'append_only'); END"
                )
            fault("after_swap")
            if schema_state(conn) != target_state:
                fail("target_schema_invalid")
            retired = inspect_schema(
                conn,
                table=backup_table,
                trigger_names=retired_triggers,
            )
            if retired.get("state") != source_state or retired.get("errors"):
                fail(
                    "retired_schema_invalid:"
                    + str(retired.get("state") or "")
                    + ":"
                    + ",".join(
                        sorted(str(value) for value in retired.get("errors") or ())
                    )
                )
            triggers_after = triggers(conn, table, target_triggers)
            after = fingerprint(conn)
            if after != before:
                fail("post_swap_fingerprint_mismatch")
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                fail("quick_check_failed")
            # Scoped to what this migration touched: the rebuilt table's own
            # foreign keys and every table that declares one against it.  The
            # whole-database check this used to be made every future fact
            # migration hostage to unrelated historical debt -- on 2026-08-28
            # the V17 apply completed its copy and swap, then rolled the
            # whole thing back over 48 pre-existing
            # calibration_native_ensemble_historical_member_run orphans that
            # no fact-store migration created and none may repair (the
            # calibration tables are append-only).  A violation the swap
            # itself could cause still fails closed here.
            fk_scope = [table] + [
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND sql LIKE ?",
                    (f"%REFERENCES {table}%",),
                )
            ]
            for scoped_table in fk_scope:
                if conn.execute(
                    f'PRAGMA foreign_key_check("{scoped_table}")'
                ).fetchone() is not None:
                    fail("foreign_key_check_failed")
            fault("before_commit")
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return receipt(
        state=f"APPLIED_{target_state}",
        database=database,
        schema_before=source_state,
        schema_after=target_state,
        before=before,
        after=after,
        capacity=capacity,
        writer_receipt_sha256=str(validated["receipt_sha256"]),
        triggers_before=triggers_before,
        triggers_after=triggers_after,
        writes_database=True,
    )


__all__ = [
    "assert_disk_capacity",
    "assert_no_incoming_foreign_keys",
    "migrate_append_only_fact_schema",
    "row_fingerprint",
    "trigger_snapshot",
]

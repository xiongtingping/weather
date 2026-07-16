#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import phase0_schema_probe


SCHEMA_VERSION = "phase0_forward_schema_plan_v1"

PLANNED_TABLE_PRIMARY_KEYS: dict[str, str] = {
    "source_observations": "source_event_id",
    "awc_hard_source_signals": "signal_id",
    "signal_to_order_timing": "signal_id",
    "daily_weather_reviews": "review_id",
    "daily_calibration_records": "daily_calibration_record_id",
    "bias_correction_candidates": "calibration_candidate_id",
    "parameter_change_events": "id",
    "task_queue": "id",
}

SOURCE_OBSERVATION_EXTENSION_COLUMNS: tuple[str, ...] = (
    "source_observation_id",
    "role",
    "valid_time_local",
    "parsed_at",
    "response_completed_at",
    "response_duration_ms",
    "dewpoint_c",
    "observed_today_max_c",
    "forecast_peak_c",
    "hard_eligible",
    "demotion_reason",
    "quality_flags_json",
)


def _quote_ident(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _index_name(table: str, columns: list[str]) -> str:
    raw = "idx_phase0_" + table + "_" + "_".join(columns)
    safe = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in raw)
    return safe[:120]


def _column_type(column: str) -> str:
    if column.endswith("_json"):
        return "TEXT"
    if column in {"id"}:
        return "INTEGER"
    if column.endswith("_ms") or column.endswith("_c"):
        return "REAL"
    return "TEXT"


def _column_def(table: str, column: str) -> str:
    if table == "task_queue":
        task_queue_defs = {
            "id": '"id" INTEGER PRIMARY KEY',
            "task_type": '"task_type" TEXT NOT NULL',
            "status": '"status" TEXT NOT NULL DEFAULT \'pending\'',
            "priority": '"priority" INTEGER NOT NULL DEFAULT 0',
            "payload_json": '"payload_json" TEXT',
            "created_at": '"created_at" TEXT NOT NULL DEFAULT (datetime(\'now\'))',
            "started_at": '"started_at" TEXT',
            "completed_at": '"completed_at" TEXT',
            "error": '"error" TEXT',
            "worker_pid": '"worker_pid" INTEGER',
            "retry_count": '"retry_count" INTEGER NOT NULL DEFAULT 0',
            "max_retries": '"max_retries" INTEGER NOT NULL DEFAULT 3',
        }
        if column in task_queue_defs:
            return task_queue_defs[column]
    suffix = " PRIMARY KEY" if PLANNED_TABLE_PRIMARY_KEYS.get(table) == column else ""
    return f"{_quote_ident(column)} {_column_type(column)}{suffix}"


def _task_queue_index_ddls(
    ddl: list[dict[str, Any]],
    *,
    table_probe: dict[str, Any],
) -> None:
    if not table_probe.get("exists") and not table_probe.get("required_columns"):
        return
    existing_indexes = {str(index) for index in (table_probe.get("indexes") or [])}
    for index_name, sql in (
        (
            "idx_task_queue_status_priority",
            'CREATE INDEX IF NOT EXISTS "idx_task_queue_status_priority" '
            'ON "task_queue" ("status", "priority" DESC, "created_at")',
        ),
        (
            "idx_task_queue_type_status",
            'CREATE INDEX IF NOT EXISTS "idx_task_queue_type_status" '
            'ON "task_queue" ("task_type", "status")',
        ),
    ):
        if index_name in existing_indexes:
            continue
        ddl.append(
            {
                "action": "create_index",
                "table": "task_queue",
                "index": index_name,
                "sql": sql,
                "rollback_hint": f"DROP INDEX IF EXISTS {_quote_ident(index_name)}",
                "requires_backup": False,
                "apply_order_group": "task_queue_runtime_indexes",
            }
        )


def _rollback_hint(action: str, table: str, column: str = "") -> str:
    if action == "create_table":
        return f"DROP TABLE IF EXISTS {_quote_ident(table)}"
    if action == "add_column":
        return (
            "restore_pre_migration_backup_or_rebuild_table_without_column"
            + (f":{table}.{column}" if column else "")
        )
    if action == "create_unique_index":
        return f"DROP INDEX IF EXISTS {_quote_ident(_index_name(table, [column] if column else []))}"
    return "manual_review_required"


def _unique_index_where(columns: list[str]) -> str:
    parts = []
    for column in columns:
        quoted = _quote_ident(column)
        parts.append(f"{quoted} IS NOT NULL")
        parts.append(f"{quoted} <> ''")
    return " AND ".join(parts)


def _unique_index_sql(table: str, columns: list[str]) -> str:
    index_name = _index_name(table, columns)
    cols_sql = ", ".join(_quote_ident(column) for column in columns)
    where_sql = _unique_index_where(columns)
    return (
        f"CREATE UNIQUE INDEX IF NOT EXISTS {_quote_ident(index_name)} "
        f"ON {_quote_ident(table)} ({cols_sql}) WHERE {where_sql}"
    )


def _add_column_ddls(
    ddl: list[dict[str, Any]],
    *,
    table: str,
    table_probe: dict[str, Any],
    apply_order_group: str,
) -> None:
    if not table_probe.get("exists"):
        return
    for column in table_probe.get("missing_required_columns") or []:
        ddl.append(
            {
                "action": "add_column",
                "table": table,
                "column": column,
                "sql": f"ALTER TABLE {_quote_ident(table)} ADD COLUMN {_quote_ident(column)} {_column_type(column)}",
                "rollback_hint": _rollback_hint("add_column", table, column),
                "requires_backup": True,
                "apply_order_group": apply_order_group,
            }
        )


def _source_observation_extension_column_ddls(
    ddl: list[dict[str, Any]],
    *,
    table_probe: dict[str, Any],
) -> set[str]:
    table = "source_observations"
    existing = set(table_probe.get("columns") or ())
    planned = set(table_probe.get("required_columns") or ())
    if not table_probe.get("exists"):
        return planned | set(SOURCE_OBSERVATION_EXTENSION_COLUMNS)
    for column in SOURCE_OBSERVATION_EXTENSION_COLUMNS:
        if column in existing:
            continue
        ddl.append(
            {
                "action": "add_column",
                "table": table,
                "column": column,
                "sql": f"ALTER TABLE {_quote_ident(table)} ADD COLUMN {_quote_ident(column)} {_column_type(column)}",
                "rollback_hint": _rollback_hint("add_column", table, column),
                "requires_backup": True,
                "apply_order_group": "source_observation_identity_extension",
            }
        )
        planned.add(column)
    return existing | planned


def build_plan(db_path: Path, *, immutable: bool = False) -> dict[str, Any]:
    probe = phase0_schema_probe.run_probe(db_path, immutable=immutable)
    ddl: list[dict[str, Any]] = []
    source_observation_extension_columns_after_apply: set[str] = set()
    for table, table_probe in (probe.get("tables") or {}).items():
        _add_column_ddls(
            ddl,
            table=table,
            table_probe=table_probe,
            apply_order_group="lineage_forward_columns",
        )
    for table, table_probe in (probe.get("planned_tables") or {}).items():
        if table == "source_observations":
            source_observation_extension_columns_after_apply = _source_observation_extension_column_ddls(
                ddl,
                table_probe=table_probe,
            )
        if table_probe.get("exists"):
            _add_column_ddls(
                ddl,
                table=table,
                table_probe=table_probe,
                apply_order_group="phase0_planned_table_columns",
            )
            if table == "task_queue":
                _task_queue_index_ddls(ddl, table_probe=table_probe)
            continue
        columns = []
        create_columns = list(table_probe.get("required_columns") or [])
        if table == "source_observations":
            create_columns = [
                *create_columns,
                *[
                    column
                    for column in SOURCE_OBSERVATION_EXTENSION_COLUMNS
                    if column not in create_columns
                ],
            ]
        for column in create_columns:
            columns.append(_column_def(table, column))
        ddl.append(
                {
                    "action": "create_table",
                    "table": table,
                    "sql": f"CREATE TABLE IF NOT EXISTS {_quote_ident(table)} ({', '.join(columns)})",
                    "rollback_hint": _rollback_hint("create_table", table),
                    "requires_backup": False,
                    "apply_order_group": "phase0_planned_tables",
                }
            )
        if table == "task_queue":
            _task_queue_index_ddls(ddl, table_probe=table_probe)
    table_columns_after_apply: dict[str, set[str]] = {}
    for table, table_probe in {
        **(probe.get("tables") or {}),
        **(probe.get("planned_tables") or {}),
    }.items():
        if table == "source_observations":
            columns_after = set(table_probe.get("columns") or ())
            columns_after |= set(table_probe.get("missing_required_columns") or ())
            columns_after |= set(source_observation_extension_columns_after_apply)
            table_columns_after_apply[table] = columns_after
        elif table_probe.get("exists"):
            table_columns_after_apply[table] = set(table_probe.get("columns") or ()) | set(
                table_probe.get("missing_required_columns") or ()
            )
        elif table in (probe.get("planned_tables") or {}):
            table_columns_after_apply[table] = set(table_probe.get("required_columns") or ())
    unsupported_constraint_blockers: list[dict[str, Any]] = []
    for table, table_constraint in (
        (probe.get("id_constraint_matrix") or {}).get("tables") or {}
    ).items():
        exists = bool(table_constraint.get("exists"))
        will_create_planned_table = bool(
            not exists
            and table in (probe.get("planned_tables") or {})
            and table_columns_after_apply.get(table)
        )
        if not exists and not will_create_planned_table:
            continue
        if exists:
            for column in table_constraint.get("missing_primary_key_columns") or []:
                unsupported_constraint_blockers.append(
                    {
                        "table": table,
                        "column": column,
                        "reason": "sqlite_primary_key_requires_table_rebuild_not_forward_add_column",
                        "rollback_hint": "restore_pre_migration_backup_or_rebuild_table_with_primary_key",
                    }
                )
        for columns_raw in table_constraint.get("missing_unique_keys") or []:
            columns = [str(column) for column in columns_raw if str(column or "").strip()]
            if not columns:
                continue
            if tuple(columns) == tuple(table_constraint.get("target_primary_key") or ()):
                continue
            if not set(columns).issubset(table_columns_after_apply.get(table, set())):
                unsupported_constraint_blockers.append(
                    {
                        "table": table,
                        "columns": columns,
                        "reason": "unique_key_columns_missing_after_forward_column_plan",
                    }
                )
                continue
            ddl.append(
                {
                    "action": "create_unique_index",
                    "table": table,
                    "columns": columns,
                    "index": _index_name(table, columns),
                    "sql": _unique_index_sql(table, columns),
                    "rollback_hint": f"DROP INDEX IF EXISTS {_quote_ident(_index_name(table, columns))}",
                    "requires_backup": True,
                    "apply_order_group": "lineage_forward_unique_indexes",
                    "partial_unique_where": _unique_index_where(columns),
                }
            )
    source_obs_columns = table_columns_after_apply.get("source_observations") or set()
    if "source_observation_id" in source_obs_columns:
        source_obs_index = _index_name("source_observations", ["source_observation_id"])
        source_obs_probe = (
            (probe.get("planned_tables") or {}).get("source_observations")
            or (probe.get("tables") or {}).get("source_observations")
            or {}
        )
        source_obs_indexes = {str(index) for index in (source_obs_probe.get("indexes") or [])}
        if source_obs_index not in source_obs_indexes:
            ddl.append(
                {
                    "action": "create_unique_index",
                    "table": "source_observations",
                    "columns": ["source_observation_id"],
                    "index": source_obs_index,
                    "sql": _unique_index_sql("source_observations", ["source_observation_id"]),
                    "rollback_hint": f"DROP INDEX IF EXISTS {_quote_ident(source_obs_index)}",
                    "requires_backup": True,
                    "apply_order_group": "source_observation_identity_extension",
                    "partial_unique_where": _unique_index_where(["source_observation_id"]),
                }
            )
    return {
        "schema": SCHEMA_VERSION,
        "ok": not unsupported_constraint_blockers,
        "read_only": True,
        "dry_run": True,
        "execution_mode": "no_apply_dry_run",
        "apply_supported": False,
        "db": str(Path(db_path).expanduser().resolve()),
        "guards": {
            "executes_sql": False,
            "writes_archive_db": False,
            "historical_backfill": False,
            "restarts_launchd": False,
            "places_orders": False,
        },
        "pre_apply_requirements": [
            "explicit_user_confirmation_required",
            "fresh_archive_db_backup_required",
            "unique_index_duplicate_precheck_required",
            "launchd_restart_not_performed_by_this_script",
            "historical_backfill_not_included",
            "post_apply_phase0_schema_probe_strict_must_pass",
        ],
        "probe_phase0_ready": bool(probe.get("phase0_ready")),
        "probe_schema_surface_ready": bool(probe.get("schema_surface_ready")),
        "constraint_blockers": unsupported_constraint_blockers,
        "ddl_count": len(ddl),
        "ddl": ddl,
        "next_actions": probe.get("next_actions") or [],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run Phase 0 forward schema migration plan.")
    parser.add_argument("--db", default=str(os.environ.get("PM_ARCHIVE_DB_PATH") or phase0_schema_probe.DEFAULT_DB))
    parser.add_argument("--immutable", action="store_true", help="Open SQLite in immutable read-only mode.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    plan = build_plan(Path(args.db), immutable=bool(args.immutable))
    print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

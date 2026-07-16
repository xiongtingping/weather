#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "archive.db"
SCHEMA_VERSION = "phase0_schema_probe_v1"


REQUIRED_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "event_fact_history": (
        "id",
        "run_date",
        "slug",
        "event_date",
        "fact_as_of",
        "fact_json",
        "created_at",
        "correlation_id",
        "source_event_id",
        "fact_snapshot_id",
        "posterior_run_id",
        "signal_id",
        "review_id",
    ),
    "event_fact_latest": (
        "slug",
        "event_date",
        "fact_id",
        "updated_at",
        "fact_snapshot_id",
        "correlation_id",
        "lineage_state_json",
    ),
    "auto_trade_decisions": (
        "id",
        "run_date",
        "slug",
        "event_date",
        "outcome",
        "side",
        "action",
        "will_order",
        "details_json",
        "created_at",
        "correlation_id",
        "fact_snapshot_id",
        "posterior_run_id",
        "decision_id",
        "signal_id",
        "order_intent_id",
    ),
    "auto_trade_jobs": (
        "id",
        "run_date",
        "source",
        "state",
        "payload_json",
        "created_at",
        "updated_at",
        "correlation_id",
        "decision_id",
        "signal_id",
        "order_intent_id",
    ),
    "auto_orders": (
        "id",
        "order_id",
        "slug",
        "event_date",
        "outcome",
        "side",
        "action",
        "created_at",
        "correlation_id",
        "decision_id",
        "signal_id",
        "order_intent_id",
    ),
}

PLANNED_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "source_observations": (
        "source_event_id",
        "source_id",
        "source_class",
        "station_id",
        "event_date",
        "valid_time",
        "fetched_at",
        "temperature_c",
        "raw_ref",
        "raw_sha256",
        "parser_version",
        "observation_json",
        "created_at",
    ),
    "awc_hard_source_signals": (
        "signal_id",
        "source_event_id",
        "slug",
        "event_date",
        "awc_valid_time",
        "awc_hard_source_signal_at",
        "acceptance_state",
        "terminal_state",
        "state_reason",
        "created_at",
    ),
    "signal_to_order_timing": (
        "signal_id",
        "decision_id",
        "order_intent_id",
        "awc_hard_source_signal_at",
        "fact_snapshot_written_at",
        "posterior_written_at",
        "auto_trade_decision_written_at",
        "execution_consumer_seen_decision_at",
        "order_job_enqueued_at",
        "order_submitted_at",
        "signal_to_order_slo_violation_reason",
        "timing_json",
        "created_at",
    ),
    "daily_weather_reviews": (
        "review_id",
        "run_date",
        "slug",
        "event_date",
        "fact_snapshot_id",
        "first_bad_link",
        "review_json",
        "created_at",
    ),
    "daily_calibration_records": (
        "daily_calibration_record_id",
        "review_id",
        "run_date",
        "slug",
        "event_date",
        "fact_snapshot_id",
        "state",
        "calibration_metrics_json",
        "error_attribution_json",
        "strata_json",
        "record_json",
        "created_at",
        "updated_at",
    ),
    "bias_correction_candidates": (
        "calibration_candidate_id",
        "review_id",
        "candidate_type",
        "state",
        "proposal_json",
        "rollback_plan_json",
        "created_at",
        "updated_at",
    ),
    "parameter_change_events": (
        "id",
        "calibration_candidate_id",
        "state",
        "changed_by",
        "change_json",
        "created_at",
    ),
    "task_queue": (
        "id",
        "task_type",
        "status",
        "priority",
        "payload_json",
        "created_at",
        "started_at",
        "completed_at",
        "error",
        "worker_pid",
        "retry_count",
        "max_retries",
    ),
}

FACT_JSON_REQUIRED_KEYS: tuple[str, ...] = (
    "source_observations",
    "source_acquisition_trace",
    "source_latency_trace",
    "fact_layer_state",
    "db_write_guard",
    "lineage_state",
    "phase",
    "beta_weak",
    "beta_strong",
    "phase_reason",
    "hard_mask_reason",
    "hard_source_acceptance_state",
    "signal_to_order_timing",
    "daily_review_state",
)

TABLE_ID_CONSTRAINTS: dict[str, dict[str, Any]] = {
    "event_fact_history": {
        "primary_key": ("id",),
        "unique_keys": (("fact_snapshot_id",),),
        "lineage_fields": ("correlation_id", "source_event_id", "fact_snapshot_id", "posterior_run_id", "signal_id", "review_id"),
        "write_owner": "weather_fact_snapshot_writer",
        "consumers": ("event_fact_latest", "ui_snapshot", "daily_review", "ssot_chain_replay"),
    },
    "event_fact_latest": {
        "primary_key": (),
        "unique_keys": (("slug", "event_date"), ("fact_snapshot_id",)),
        "lineage_fields": ("fact_snapshot_id", "correlation_id", "lineage_state_json"),
        "write_owner": "weather_fact_snapshot_writer",
        "consumers": ("ui_snapshot", "posterior_runtime", "decision"),
    },
    "auto_trade_decisions": {
        "primary_key": ("id",),
        "unique_keys": (("decision_id",),),
        "lineage_fields": ("correlation_id", "fact_snapshot_id", "posterior_run_id", "decision_id", "signal_id", "order_intent_id"),
        "write_owner": "auto_trade_decision_writer",
        "consumers": ("execution", "ui_snapshot", "daily_review"),
    },
    "auto_trade_jobs": {
        "primary_key": ("id",),
        "unique_keys": (("order_intent_id",),),
        "lineage_fields": ("correlation_id", "decision_id", "signal_id", "order_intent_id"),
        "write_owner": "auto_trade_job_writer",
        "consumers": ("execution_worker", "ssot_chain_replay"),
    },
    "auto_orders": {
        "primary_key": ("id",),
        "unique_keys": (("order_id",), ("order_intent_id",)),
        "lineage_fields": ("correlation_id", "decision_id", "signal_id", "order_intent_id"),
        "write_owner": "auto_order_ledger_writer",
        "consumers": ("ui_snapshot", "daily_review", "risk_audit"),
    },
    "source_observations": {
        "primary_key": ("source_event_id",),
        "unique_keys": (("source_event_id",),),
        "lineage_fields": ("source_event_id",),
        "write_owner": "weather_source_acquisition_manager",
        "consumers": ("fact_snapshot_writer", "posterior", "daily_review"),
    },
    "awc_hard_source_signals": {
        "primary_key": ("signal_id",),
        "unique_keys": (("signal_id",),),
        "lineage_fields": ("signal_id", "source_event_id"),
        "write_owner": "awc_hard_source_signal_state_machine",
        "consumers": ("decision", "execution", "daily_review"),
    },
    "signal_to_order_timing": {
        "primary_key": ("signal_id",),
        "unique_keys": (("signal_id",),),
        "lineage_fields": ("signal_id", "decision_id", "order_intent_id"),
        "write_owner": "signal_to_order_ledger_writer",
        "consumers": ("ssot_chain_replay", "daily_review", "ui_snapshot"),
    },
    "daily_weather_reviews": {
        "primary_key": ("review_id",),
        "unique_keys": (("review_id",),),
        "lineage_fields": ("review_id", "fact_snapshot_id"),
        "write_owner": "daily_review_persistence_writer",
        "consumers": ("bias_correction_candidates", "ssot_chain_replay"),
    },
    "daily_calibration_records": {
        "primary_key": ("daily_calibration_record_id",),
        "unique_keys": (("daily_calibration_record_id",),),
        "lineage_fields": ("daily_calibration_record_id", "review_id", "fact_snapshot_id"),
        "write_owner": "daily_review_persistence_writer",
        "consumers": ("parameter_change_governance", "daily_review_coverage_audit", "rolling_calibration_summary"),
    },
    "bias_correction_candidates": {
        "primary_key": ("calibration_candidate_id",),
        "unique_keys": (("calibration_candidate_id",),),
        "lineage_fields": ("calibration_candidate_id", "review_id"),
        "write_owner": "daily_review_persistence_writer",
        "consumers": ("parameter_change_governance",),
    },
    "parameter_change_events": {
        "primary_key": ("id",),
        "unique_keys": (),
        "lineage_fields": ("calibration_candidate_id",),
        "write_owner": "daily_review_persistence_writer",
        "consumers": ("parameter_change_governance", "manual_review"),
    },
    "task_queue": {
        "primary_key": ("id",),
        "unique_keys": (),
        "lineage_fields": (),
        "write_owner": "task_queue_enqueuer",
        "consumers": ("task_queue_worker", "source_acquisition_manager", "posterior_materialization_worker"),
    },
}


def _connect(db: Path, *, immutable: bool) -> sqlite3.Connection:
    uri = f"file:{db}?mode=ro"
    if immutable:
        uri += "&immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [str(row["name"]) for row in rows]


def _indexes(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
    return [str(row["name"]) for row in rows]


def _primary_key_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    keyed = sorted(
        (
            (int(row["pk"] or 0), str(row["name"] or ""))
            for row in rows
            if int(row["pk"] or 0) > 0
        ),
        key=lambda item: item[0],
    )
    return [name for _, name in keyed]


def _unique_indexes(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in conn.execute(f"PRAGMA index_list({table})").fetchall():
        try:
            is_unique = bool(int(row["unique"] or 0))
        except Exception:
            is_unique = False
        if not is_unique:
            continue
        name = str(row["name"] or "")
        columns = [
            str(info["name"] or "")
            for info in conn.execute(f"PRAGMA index_info({name})").fetchall()
            if str(info["name"] or "")
        ]
        out.append({"name": name, "columns": columns})
    return out


def _sqlite_foreign_keys(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    rows = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "id": row["id"],
                "seq": row["seq"],
                "table": row["table"],
                "from": row["from"],
                "to": row["to"],
                "on_update": row["on_update"],
                "on_delete": row["on_delete"],
                "match": row["match"],
            }
        )
    return out


def _id_constraint_matrix(conn: sqlite3.Connection) -> dict[str, Any]:
    tables: dict[str, Any] = {}
    for table, target in TABLE_ID_CONSTRAINTS.items():
        exists = _table_exists(conn, table)
        columns = _columns(conn, table) if exists else []
        actual_pk = _primary_key_columns(conn, table) if exists else []
        unique_indexes = _unique_indexes(conn, table) if exists else []
        actual_foreign_keys = _sqlite_foreign_keys(conn, table) if exists else []
        target_pk = list(target.get("primary_key") or ())
        target_unique = [list(item) for item in target.get("unique_keys") or ()]
        lineage_fields = list(target.get("lineage_fields") or ())
        missing_pk = [col for col in target_pk if col not in actual_pk]
        missing_lineage_fields = [col for col in lineage_fields if col not in columns]
        unique_column_sets = {tuple(item.get("columns") or []) for item in unique_indexes}
        missing_unique_keys = [cols for cols in target_unique if tuple(cols) not in unique_column_sets]
        tables[table] = {
            "exists": exists,
            "target_primary_key": target_pk,
            "actual_primary_key": actual_pk,
            "missing_primary_key_columns": missing_pk,
            "target_unique_keys": target_unique,
            "actual_unique_indexes": unique_indexes,
            "missing_unique_keys": missing_unique_keys,
            "foreign_key_policy": "lineage_key_contract",
            "target_foreign_key_fields": lineage_fields,
            "actual_sqlite_foreign_keys": actual_foreign_keys,
            "missing_foreign_key_fields": missing_lineage_fields,
            "foreign_key_ready": bool(exists and not missing_lineage_fields),
            "lineage_fields": lineage_fields,
            "missing_lineage_fields": missing_lineage_fields,
            "write_owner": target.get("write_owner"),
            "consumers": list(target.get("consumers") or ()),
            "constraint_ready": bool(exists and not missing_pk and not missing_unique_keys and not missing_lineage_fields),
        }
    return {
        "schema": "phase0_id_constraint_matrix_v1",
        "read_only": True,
        "tables": tables,
        "ready_table_count": sum(1 for item in tables.values() if item.get("constraint_ready")),
        "gap_table_count": sum(1 for item in tables.values() if not item.get("constraint_ready")),
    }


def _count(conn: sqlite3.Connection, table: str) -> int | None:
    try:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        return int(row["n"] or 0)
    except sqlite3.Error:
        return None


def _table_probe(conn: sqlite3.Connection, table: str, required: Iterable[str]) -> dict[str, Any]:
    exists = _table_exists(conn, table)
    cols = _columns(conn, table) if exists else []
    missing = [col for col in required if col not in cols]
    return {
        "exists": exists,
        "rows": _count(conn, table) if exists else None,
        "columns": cols,
        "indexes": _indexes(conn, table) if exists else [],
        "required_columns": list(required),
        "missing_required_columns": missing,
        "lineage_ready": exists and not missing,
    }


def _latest_fact_json_contract(conn: sqlite3.Connection) -> dict[str, Any]:
    if not (_table_exists(conn, "event_fact_history") and _table_exists(conn, "event_fact_latest")):
        return {
            "checked": False,
            "reason": "fact_tables_missing",
            "missing_required_keys": list(FACT_JSON_REQUIRED_KEYS),
        }
    row = conn.execute(
        """
        SELECT h.id, h.slug, h.event_date, h.fact_as_of, h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        ORDER BY h.id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return {
            "checked": False,
            "reason": "latest_fact_missing",
            "missing_required_keys": list(FACT_JSON_REQUIRED_KEYS),
        }
    try:
        payload = json.loads(str(row["fact_json"] or "{}"))
    except json.JSONDecodeError:
        return {
            "checked": True,
            "fact_id": row["id"],
            "reason": "fact_json_invalid",
            "missing_required_keys": list(FACT_JSON_REQUIRED_KEYS),
        }
    present = [key for key in FACT_JSON_REQUIRED_KEYS if key in payload]
    missing = [key for key in FACT_JSON_REQUIRED_KEYS if key not in payload]
    return {
        "checked": True,
        "fact_id": row["id"],
        "slug": row["slug"],
        "event_date": row["event_date"],
        "fact_as_of": row["fact_as_of"],
        "present_required_keys": present,
        "missing_required_keys": missing,
        "contract_ready": not missing,
    }


def run_probe(db_path: Path, *, immutable: bool = False) -> dict[str, Any]:
    db = Path(db_path).expanduser().resolve()
    output: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "read_only": True,
        "db": str(db),
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_auto_trade_decisions": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
        },
        "tables": {},
        "planned_tables": {},
        "id_constraint_matrix": {},
        "fact_json_contract": {},
        "missing": [],
        "schema_missing": [],
        "constraint_missing": [],
        "fact_json_missing": [],
        "next_actions": [],
    }
    if not db.exists():
        output["ok"] = False
        output["missing"].append({"scope": "db", "item": "archive_db_missing"})
        output["schema_missing"].append({"scope": "db", "item": "archive_db_missing"})
        return output

    with _connect(db, immutable=immutable) as conn:
        for table, required in REQUIRED_TABLE_COLUMNS.items():
            probe = _table_probe(conn, table, required)
            output["tables"][table] = probe
            for col in probe["missing_required_columns"]:
                missing = {"scope": "table_column", "table": table, "item": col}
                output["missing"].append(missing)
                output["schema_missing"].append(missing)
        for table, required in PLANNED_TABLE_COLUMNS.items():
            probe = _table_probe(conn, table, required)
            output["planned_tables"][table] = probe
            if not probe["exists"]:
                missing = {"scope": "planned_table", "table": table, "item": table}
                output["missing"].append(missing)
                output["schema_missing"].append(missing)
            else:
                for col in probe["missing_required_columns"]:
                    missing = {"scope": "planned_table_column", "table": table, "item": col}
                    output["missing"].append(missing)
                    output["schema_missing"].append(missing)
        output["id_constraint_matrix"] = _id_constraint_matrix(conn)
        for table, constraint in (output["id_constraint_matrix"].get("tables") or {}).items():
            if not constraint.get("exists"):
                continue
            for column in constraint.get("missing_primary_key_columns") or []:
                missing = {"scope": "table_primary_key", "table": table, "item": column}
                output["missing"].append(missing)
                output["constraint_missing"].append(missing)
            for columns in constraint.get("missing_unique_keys") or []:
                item = ",".join(str(column) for column in columns)
                missing = {"scope": "table_unique_key", "table": table, "item": item}
                output["missing"].append(missing)
                output["constraint_missing"].append(missing)
        fact_contract = _latest_fact_json_contract(conn)
        output["fact_json_contract"] = fact_contract
        for key in fact_contract.get("missing_required_keys") or []:
            missing = {"scope": "fact_json_key", "item": key}
            output["missing"].append(missing)
            output["fact_json_missing"].append(missing)

    output["schema_surface_ready"] = not output["schema_missing"] and not output["constraint_missing"]
    output["phase0_ready"] = not output["missing"]
    output["next_actions"] = _next_actions(output)
    return output


def _next_actions(output: dict[str, Any]) -> list[str]:
    actions: list[str] = []
    if any(item.get("scope") == "table_column" for item in output.get("missing", [])):
        actions.append("prepare_forward_write_lineage_columns_migration")
    if any(item.get("scope") == "planned_table" for item in output.get("missing", [])):
        actions.append("design_forward_write_planned_tables_without_backfill")
    if any(item.get("scope") in {"table_primary_key", "table_unique_key"} for item in output.get("missing", [])):
        actions.append("prepare_forward_write_id_unique_constraints_migration")
    if any(item.get("scope") == "fact_json_key" for item in output.get("missing", [])):
        actions.append("add_lineage_and_phase_placeholders_to_future_fact_json")
    if not actions:
        actions.append("phase0_schema_probe_clean")
    return actions


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only Phase 0 schema and lineage readiness probe.")
    parser.add_argument("--db", default=str(os.environ.get("PM_ARCHIVE_DB_PATH") or DEFAULT_DB))
    parser.add_argument("--immutable", action="store_true", help="Open SQLite in immutable read-only mode.")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when Phase 0 schema gaps remain.")
    parser.add_argument(
        "--schema-only",
        action="store_true",
        help="With --strict, require only table/column/planned-table readiness; fact_json contract remains a separate forward-write gate.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = run_probe(Path(args.db), immutable=bool(args.immutable))
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if not report.get("ok"):
        return 2
    if args.strict and args.schema_only and not report.get("schema_surface_ready"):
        return 3
    if args.strict and not args.schema_only and not report.get("phase0_ready"):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

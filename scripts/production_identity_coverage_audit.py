#!/usr/bin/env python3
"""Read-only audit for production object identity coverage.

This audit separates schema readiness from real production-row identity
coverage. It does not write the database, repair lineage, create backups,
restart services, fetch weather sources, or place orders.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import phase0_schema_probe

DEFAULT_DB = ROOT / "data" / "archive.db"
SCHEMA_VERSION = "production_identity_coverage_audit_v1"
DEFAULT_SLOW_SCAN_ROW_THRESHOLD = 10_000


IDENTITY_ROW_CONTRACT: dict[str, dict[str, Any]] = {
    "event_fact_history": {
        "primary_key_fields": ("id",),
        "layer_identity_fields": ("fact_snapshot_id",),
        "correlation_fields": ("correlation_id",),
        "optional_lineage_fields": ("source_event_id", "posterior_run_id", "signal_id", "review_id"),
        "order_by": "id DESC",
    },
    "event_fact_latest": {
        "primary_key_fields": ("slug", "event_date"),
        "layer_identity_fields": ("fact_snapshot_id",),
        "correlation_fields": ("correlation_id",),
        "optional_lineage_fields": (),
        "order_by": "updated_at DESC",
    },
    "auto_trade_decisions": {
        "primary_key_fields": ("id",),
        "layer_identity_fields": ("decision_id",),
        "correlation_fields": ("correlation_id",),
        "optional_lineage_fields": ("fact_snapshot_id", "posterior_run_id", "signal_id", "order_intent_id"),
        "order_by": "id DESC",
    },
    "auto_trade_jobs": {
        "primary_key_fields": ("id",),
        "layer_identity_fields": ("order_intent_id",),
        "correlation_fields": ("correlation_id",),
        "optional_lineage_fields": ("decision_id", "signal_id"),
        "order_by": "id DESC",
    },
    "auto_orders": {
        "primary_key_fields": ("id",),
        "layer_identity_fields": ("order_intent_id",),
        "correlation_fields": ("correlation_id",),
        "optional_lineage_fields": ("decision_id", "signal_id"),
        "order_by": "id DESC",
    },
    "source_observations": {
        "primary_key_fields": ("source_event_id",),
        "layer_identity_fields": ("source_observation_id",),
        "correlation_fields": (),
        "correlation_policy": "source_event_id_is_source_layer_anchor",
        "optional_lineage_fields": (),
        "order_by": "created_at DESC",
    },
    "awc_hard_source_signals": {
        "primary_key_fields": ("signal_id",),
        "layer_identity_fields": ("signal_id",),
        "correlation_fields": (),
        "correlation_policy": "signal_id_is_hard_source_layer_anchor",
        "optional_lineage_fields": ("source_event_id",),
        "order_by": "created_at DESC",
    },
    "signal_to_order_timing": {
        "primary_key_fields": ("signal_id",),
        "layer_identity_fields": ("signal_id",),
        "correlation_fields": (),
        "correlation_policy": "signal_id_is_execution_timing_anchor",
        "optional_lineage_fields": ("decision_id", "order_intent_id"),
        "order_by": "created_at DESC",
    },
    "daily_weather_reviews": {
        "primary_key_fields": ("review_id",),
        "layer_identity_fields": ("review_id",),
        "correlation_fields": (),
        "correlation_policy": "review_id_links_to_fact_snapshot_id",
        "optional_lineage_fields": ("fact_snapshot_id",),
        "order_by": "created_at DESC",
    },
    "daily_calibration_records": {
        "primary_key_fields": ("daily_calibration_record_id",),
        "layer_identity_fields": ("daily_calibration_record_id",),
        "correlation_fields": (),
        "correlation_policy": "daily_calibration_record_id_links_to_review_id",
        "optional_lineage_fields": ("review_id", "fact_snapshot_id"),
        "order_by": "created_at DESC",
    },
    "bias_correction_candidates": {
        "primary_key_fields": ("calibration_candidate_id",),
        "layer_identity_fields": ("calibration_candidate_id",),
        "correlation_fields": (),
        "correlation_policy": "calibration_candidate_id_links_to_review_id",
        "optional_lineage_fields": ("review_id",),
        "order_by": "created_at DESC",
    },
    "parameter_change_events": {
        "primary_key_fields": ("id",),
        "layer_identity_fields": ("id",),
        "correlation_fields": (),
        "correlation_policy": "parameter_event_id_links_to_calibration_candidate_id",
        "optional_lineage_fields": ("calibration_candidate_id",),
        "order_by": "id DESC",
    },
}


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_event_fact_history": False,
        "writes_event_fact_latest": False,
        "writes_auto_trade_decisions": False,
        "writes_daily_weather_reviews": False,
        "writes_daily_calibration_records": False,
        "writes_bias_correction_candidates": False,
        "writes_parameter_change_events": False,
        "writes_manifest_file": False,
        "executes_sql_writes": False,
        "creates_backup": False,
        "fetches_external_weather_sources": False,
        "historical_backfill": False,
        "places_orders": False,
        "restarts_launchd": False,
        "changes_parameters": False,
    }


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


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


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"] or "") for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _column_pk_fields(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row["name"] or "")
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        if int(row["pk"] or 0) > 0
    }


def _quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _nonempty_predicate(column: str) -> str:
    quoted = _quote_identifier(column)
    return f"{quoted} IS NOT NULL AND trim(CAST({quoted} AS TEXT)) <> ''"


def _simple_nonempty_predicate(column: str) -> str:
    quoted = _quote_identifier(column)
    return f"{quoted} IS NOT NULL AND {quoted} <> ''"


def _empty_count(conn: sqlite3.Connection, table: str, column: str) -> int:
    predicate = _nonempty_predicate(column)
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE NOT ({predicate})").fetchone()
    return int(row["n"] or 0)


def _row_count(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    return int(row["n"] or 0)


def _row_and_empty_counts(
    conn: sqlite3.Connection,
    *,
    table: str,
    fields: Sequence[str],
    allow_slow_scan: bool = False,
    slow_scan_row_threshold: int = DEFAULT_SLOW_SCAN_ROW_THRESHOLD,
) -> tuple[int, dict[str, int]]:
    selected_fields = [str(field) for field in fields if str(field or "").strip()]
    if not selected_fields:
        return _row_count(conn, table), {}
    counts, _ = _row_and_empty_counts_with_evidence(
        conn,
        table=table,
        fields=selected_fields,
        allow_slow_scan=allow_slow_scan,
        slow_scan_row_threshold=slow_scan_row_threshold,
    )
    return int(counts["row_count"] or 0), dict(counts["empty_counts"])


def _index_columns(conn: sqlite3.Connection, index_name: str) -> list[str]:
    return [str(row["name"] or "") for row in conn.execute(f"PRAGMA index_info({_quote_identifier(index_name)})")]


def _partial_nonempty_index(
    conn: sqlite3.Connection,
    *,
    table: str,
    column: str,
) -> str:
    table_quoted = _quote_identifier(table)
    column_quoted = _quote_identifier(column)
    expected_where = f"{column_quoted} IS NOT NULL AND {column_quoted} <> ''"
    expected_where_unquoted = f"{column} IS NOT NULL AND {column} <> ''"
    for row in conn.execute(f"PRAGMA index_list({table_quoted})").fetchall():
        index_name = str(row["name"] or "")
        if not index_name or int(row["partial"] or 0) != 1:
            continue
        if _index_columns(conn, index_name) != [column]:
            continue
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            (index_name,),
        ).fetchone()
        sql = str(ddl["sql"] if ddl and "sql" in ddl.keys() else ddl[0] if ddl else "")
        normalized = " ".join(sql.split())
        normalized_unquoted = normalized.replace('"', "")
        if expected_where in normalized or expected_where_unquoted in normalized_unquoted:
            return index_name
    return ""


def _supporting_index_for_column(
    conn: sqlite3.Connection,
    *,
    table: str,
    column: str,
) -> str:
    table_quoted = _quote_identifier(table)
    for row in conn.execute(f"PRAGMA index_list({table_quoted})").fetchall():
        index_name = str(row["name"] or "")
        if not index_name:
            continue
        columns = _index_columns(conn, index_name)
        if columns and columns[0] == column:
            return index_name
    return ""


def _row_and_empty_counts_with_evidence(
    conn: sqlite3.Connection,
    *,
    table: str,
    fields: Sequence[str],
    allow_slow_scan: bool = False,
    slow_scan_row_threshold: int = DEFAULT_SLOW_SCAN_ROW_THRESHOLD,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    row_count = _row_count(conn, table)
    pk_fields = _column_pk_fields(conn, table)
    empty_counts: dict[str, int] = {}
    evidence: dict[str, dict[str, Any]] = {}
    threshold = max(0, int(slow_scan_row_threshold))

    for field in dict.fromkeys(str(item) for item in fields if str(item or "").strip()):
        if field in pk_fields:
            empty_counts[field] = 0
            evidence[field] = {"method": "primary_key_schema_constraint", "index": ""}
            continue

        partial_index = _partial_nonempty_index(conn, table=table, column=field)
        if partial_index:
            quoted_index = _quote_identifier(partial_index)
            predicate = _simple_nonempty_predicate(field)
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} INDEXED BY {quoted_index} WHERE {predicate}"
            ).fetchone()
            nonempty_count = int(row["n"] or 0)
            empty_counts[field] = max(0, row_count - nonempty_count)
            evidence[field] = {
                "method": "partial_nonempty_index_count",
                "index": partial_index,
                "nonempty_count": nonempty_count,
            }
            continue

        supporting_index = _supporting_index_for_column(conn, table=table, column=field)
        if row_count > threshold and not allow_slow_scan:
            evidence[field] = {
                "method": "unverified_large_table_requires_supporting_index",
                "index": supporting_index,
                "row_count": row_count,
                "slow_scan_row_threshold": threshold,
                "recommended_index": f"idx_phase0_{table}_{field}_nonempty_scan",
            }
            continue

        predicate = _nonempty_predicate(field)
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE NOT ({predicate})"
        ).fetchone()
        empty_counts[field] = int(row["n"] or 0)
        evidence[field] = {
            "method": "bounded_row_scan" if row_count <= threshold else "operator_allowed_slow_row_scan",
            "index": supporting_index,
            "slow_scan_row_threshold": threshold,
        }

    return {"row_count": row_count, "empty_counts": empty_counts}, evidence


def _sample_missing_rows(
    conn: sqlite3.Connection,
    *,
    table: str,
    fields: Sequence[str],
    primary_key_fields: Sequence[str],
    order_by: str,
    limit: int,
) -> list[dict[str, Any]]:
    if not fields or limit <= 0:
        return []
    missing_predicates = [f"NOT ({_nonempty_predicate(field)})" for field in fields]
    select_fields = list(dict.fromkeys([*primary_key_fields, *fields]))
    select_sql = ", ".join('"' + field.replace('"', '""') + '"' for field in select_fields)
    rows = conn.execute(
        f"""
        SELECT {select_sql}
        FROM {table}
        WHERE {" OR ".join(missing_predicates)}
        ORDER BY {order_by}
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    samples: list[dict[str, Any]] = []
    for row in rows:
        item = {field: row[field] for field in select_fields}
        item["missing_fields"] = [
            field
            for field in fields
            if row[field] is None or str(row[field]).strip() == ""
        ]
        samples.append(item)
    return samples


def _constraint_ready_from_phase0(phase0: Mapping[str, Any], table: str) -> bool:
    matrix = phase0.get("id_constraint_matrix")
    if not isinstance(matrix, Mapping):
        return False
    tables = matrix.get("tables")
    if not isinstance(tables, Mapping):
        return False
    item = tables.get(table)
    return bool(isinstance(item, Mapping) and item.get("constraint_ready"))


def audit(
    db_path: Path,
    *,
    immutable: bool = False,
    sample_limit: int = 5,
    allow_slow_scan: bool = False,
    slow_scan_row_threshold: int = DEFAULT_SLOW_SCAN_ROW_THRESHOLD,
) -> dict[str, Any]:
    db = Path(db_path).expanduser().resolve()
    blockers: list[dict[str, Any]] = []
    warnings: list[str] = []
    tables: dict[str, Any] = {}
    if not db.exists():
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "read_only": True,
            "db": str(db),
            "blockers": [{"blocker": "archive_db_missing", "db": str(db)}],
            "warnings": [],
            "guards": _guards(),
            "next_boundary": "provide_archive_db_before_identity_coverage_audit",
        }

    phase0 = phase0_schema_probe.run_probe(db, immutable=immutable)
    with _connect(db, immutable=immutable) as conn:
        for table, contract in IDENTITY_ROW_CONTRACT.items():
            exists = _table_exists(conn, table)
            primary_fields = [str(item) for item in contract.get("primary_key_fields") or ()]
            layer_fields = [str(item) for item in contract.get("layer_identity_fields") or ()]
            correlation_fields = [str(item) for item in contract.get("correlation_fields") or ()]
            required_fields = list(dict.fromkeys([*primary_fields, *layer_fields, *correlation_fields]))
            columns = _columns(conn, table) if exists else set()
            missing_columns = [field for field in required_fields if field not in columns]
            present_required_fields = [field for field in required_fields if field in columns]
            row_count = 0
            empty_counts: dict[str, int] = {}
            field_evidence: dict[str, dict[str, Any]] = {}
            if exists:
                count_result, field_evidence = _row_and_empty_counts_with_evidence(
                    conn,
                    table=table,
                    fields=present_required_fields,
                    allow_slow_scan=bool(allow_slow_scan),
                    slow_scan_row_threshold=int(slow_scan_row_threshold),
                )
                row_count = int(count_result.get("row_count") or 0)
                empty_counts = dict(count_result.get("empty_counts") or {})
            missing_required_fields = [
                field
                for field in required_fields
                if field in empty_counts and int(empty_counts[field] or 0) > 0
            ]
            unverified_required_fields = [
                field
                for field in present_required_fields
                if str(_as_mapping(field_evidence.get(field)).get("method") or "")
                == "unverified_large_table_requires_supporting_index"
            ]
            sample_skipped_reason = ""
            if (
                exists
                and missing_required_fields
                and row_count > max(0, int(slow_scan_row_threshold))
                and not bool(allow_slow_scan)
            ):
                samples = []
                sample_skipped_reason = "large_table_missing_row_samples_require_allow_slow_scan"
            else:
                samples = (
                    _sample_missing_rows(
                        conn,
                        table=table,
                        fields=missing_required_fields,
                        primary_key_fields=primary_fields,
                        order_by=str(contract.get("order_by") or ""),
                        limit=max(0, int(sample_limit)),
                    )
                    if exists and missing_required_fields
                    else []
                )
            constraint_ready = _constraint_ready_from_phase0(phase0, table)
            schema_ready = bool(exists and not missing_columns and constraint_ready)
            row_identity_ready = bool(
                exists
                and not missing_columns
                and not missing_required_fields
                and not unverified_required_fields
            )
            table_report = {
                "exists": exists,
                "row_count": row_count,
                "constraint_ready": constraint_ready,
                "schema_identity_ready": schema_ready,
                "production_row_identity_ready": row_identity_ready,
                "primary_key_fields": primary_fields,
                "layer_identity_fields": layer_fields,
                "correlation_fields": correlation_fields,
                "correlation_policy": str(contract.get("correlation_policy") or "direct_correlation_id_required"),
                "required_nonempty_fields": required_fields,
                "missing_required_columns": missing_columns,
                "empty_required_field_counts": empty_counts,
                "required_field_evidence": field_evidence,
                "missing_required_fields": missing_required_fields,
                "unverified_required_fields": unverified_required_fields,
                "sample_missing_rows": samples,
                "sample_missing_rows_skipped_reason": sample_skipped_reason,
            }
            tables[table] = table_report
            if not exists:
                blockers.append({"blocker": "identity_table_missing", "table": table})
            if missing_columns:
                blockers.append(
                    {
                        "blocker": "identity_required_columns_missing",
                        "table": table,
                        "columns": missing_columns,
                    }
                )
            if exists and not constraint_ready:
                blockers.append({"blocker": "identity_schema_constraint_not_ready", "table": table})
            for field in missing_required_fields:
                blockers.append(
                    {
                        "blocker": "production_identity_required_field_empty",
                        "table": table,
                        "field": field,
                        "empty_count": int(empty_counts.get(field) or 0),
                        "row_count": row_count,
                    }
                )
            for field in unverified_required_fields:
                evidence = _as_mapping(field_evidence.get(field))
                blockers.append(
                    {
                        "blocker": "production_identity_required_field_unverified_large_table",
                        "table": table,
                        "field": field,
                        "row_count": row_count,
                        "slow_scan_row_threshold": int(evidence.get("slow_scan_row_threshold") or 0),
                        "recommended_index": str(evidence.get("recommended_index") or ""),
                    }
                )
            if exists and row_count == 0:
                warnings.append(f"identity_table_empty:{table}")

    gap_tables = [table for table, item in tables.items() if not item.get("production_row_identity_ready")]
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "read_only": True,
        "db": str(db),
        "phase0_schema": str(phase0.get("schema") or ""),
        "schema_identity_ready": all(bool(item.get("schema_identity_ready")) for item in tables.values()),
        "production_row_identity_ready": not gap_tables,
        "table_count": len(tables),
        "ready_table_count": sum(1 for item in tables.values() if item.get("production_row_identity_ready")),
        "gap_table_count": len(gap_tables),
        "gap_tables": gap_tables,
        "tables": tables,
        "blockers": blockers,
        "warnings": sorted(set(warnings)),
        "allow_slow_scan": bool(allow_slow_scan),
        "slow_scan_row_threshold": max(0, int(slow_scan_row_threshold)),
        "guards": _guards(),
        "next_boundary": (
            "production_identity_coverage_ready"
            if not blockers
            else "run_controlled_merge_fill_or_forward_writer_before_identity_closeout"
        ),
    }


def compact_report(report: Mapping[str, Any], *, max_tables: int = 8) -> dict[str, Any]:
    tables = report.get("tables")
    compact_tables: dict[str, Any] = {}
    if isinstance(tables, Mapping):
        for table, item in list(tables.items())[: max(0, int(max_tables))]:
            if not isinstance(item, Mapping):
                continue
            compact_tables[str(table)] = {
                "row_count": item.get("row_count"),
                "schema_identity_ready": bool(item.get("schema_identity_ready")),
                "production_row_identity_ready": bool(item.get("production_row_identity_ready")),
                "required_nonempty_fields": list(item.get("required_nonempty_fields") or []),
                "missing_required_columns": list(item.get("missing_required_columns") or []),
                "missing_required_fields": list(item.get("missing_required_fields") or []),
                "empty_required_field_counts": dict(item.get("empty_required_field_counts") or {}),
                "required_field_evidence": dict(item.get("required_field_evidence") or {}),
                "correlation_policy": str(item.get("correlation_policy") or ""),
                "unverified_required_fields": list(item.get("unverified_required_fields") or []),
            }
    return {
        "schema": str(report.get("schema") or SCHEMA_VERSION) + "_summary",
        "source_schema": str(report.get("schema") or ""),
        "ok": bool(report.get("ok")),
        "read_only": bool(report.get("read_only")),
        "db": str(report.get("db") or ""),
        "schema_identity_ready": bool(report.get("schema_identity_ready")),
        "production_row_identity_ready": bool(report.get("production_row_identity_ready")),
        "table_count": int(report.get("table_count") or 0),
        "ready_table_count": int(report.get("ready_table_count") or 0),
        "gap_table_count": int(report.get("gap_table_count") or 0),
        "gap_tables": list(report.get("gap_tables") or [])[: max(0, int(max_tables))],
        "tables": compact_tables,
        "blockers": list(report.get("blockers") or [])[:50],
        "warnings": list(report.get("warnings") or [])[:50],
        "allow_slow_scan": bool(report.get("allow_slow_scan")),
        "slow_scan_row_threshold": int(report.get("slow_scan_row_threshold") or 0),
        "guards": dict(report.get("guards") or {}),
        "next_boundary": str(report.get("next_boundary") or ""),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only production identity coverage audit.")
    parser.add_argument("--db", default=str(os.environ.get("PM_ARCHIVE_DB_PATH") or DEFAULT_DB))
    parser.add_argument("--immutable", action="store_true")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--sample-limit", type=int, default=5)
    parser.add_argument("--max-items-output", type=int, default=8)
    parser.add_argument("--allow-slow-scan", action="store_true")
    parser.add_argument("--slow-scan-row-threshold", type=int, default=DEFAULT_SLOW_SCAN_ROW_THRESHOLD)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report = audit(
        Path(args.db),
        immutable=bool(args.immutable),
        sample_limit=int(args.sample_limit),
        allow_slow_scan=bool(args.allow_slow_scan),
        slow_scan_row_threshold=int(args.slow_scan_row_threshold),
    )
    output: Mapping[str, Any] = (
        compact_report(report, max_tables=int(args.max_items_output))
        if args.summary
        else report
    )
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())

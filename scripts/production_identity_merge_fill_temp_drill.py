#!/usr/bin/env python3
"""Drill duplicate repair plus identity merge-fill on a temporary DB copy.

This is the production-sequence rehearsal for identity closeout: copy identity
tables to a temp DB, repair duplicate fact_snapshot IDs there, then run the same
controlled identity merge-fill apply against the temp DB. Production archive.db
is read-only throughout.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import production_identity_duplicate_fact_snapshot_apply as duplicate_apply  # noqa: E402
from scripts import production_identity_duplicate_fact_snapshot_operator_runbook as duplicate_runbook  # noqa: E402
from scripts import production_identity_duplicate_fact_snapshot_packet as duplicate_packet  # noqa: E402
from scripts import production_identity_merge_fill_apply as merge_apply  # noqa: E402
from scripts import production_identity_merge_fill_apply_contract as merge_contract  # noqa: E402
from scripts import production_identity_merge_fill_plan as merge_plan  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_LATEST_ANCHOR_POLICY = "regenerate_latest_row_fact_snapshot_id_and_keep_latest_anchor"
SCHEMA_VERSION = "production_identity_merge_fill_temp_drill_v1"
IDENTITY_TABLES = (
    "event_fact_history",
    "event_fact_latest",
    "auto_trade_decisions",
    "auto_trade_jobs",
    "auto_orders",
    "source_observations",
)


def _timer() -> float:
    return time.monotonic()


def _elapsed_ms(started: float) -> int:
    return max(0, int(round((time.monotonic() - started) * 1000)))


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _quote_ident(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def _table_count(conn: sqlite3.Connection, table: str) -> int | None:
    if not _table_exists(conn, table):
        return None
    row = conn.execute(f"SELECT COUNT(*) AS c FROM {_quote_ident(table)}").fetchone()
    return int(row["c"] or 0)


def _db_counts(db_path: Path) -> dict[str, int | None]:
    with _connect_ro(db_path) as conn:
        return {table: _table_count(conn, table) for table in IDENTITY_TABLES}


def _identity_missing_counts(db_path: Path) -> dict[str, int]:
    predicates = {
        "event_fact_history": ("fact_snapshot_id", "correlation_id"),
        "event_fact_latest": ("fact_snapshot_id", "correlation_id"),
        "auto_trade_decisions": ("decision_id", "correlation_id"),
        "auto_trade_jobs": ("order_intent_id", "correlation_id"),
        "auto_orders": ("order_intent_id", "correlation_id"),
        "source_observations": ("source_observation_id",),
    }
    counts: dict[str, int] = {}
    with _connect_ro(db_path) as conn:
        for table, fields in predicates.items():
            if not _table_exists(conn, table):
                counts[table] = 0
                continue
            where = " OR ".join(
                f"{_quote_ident(field)} IS NULL OR trim(CAST({_quote_ident(field)} AS TEXT)) = ''"
                for field in fields
            )
            row = conn.execute(f"SELECT COUNT(*) AS c FROM {_quote_ident(table)} WHERE {where}").fetchone()
            counts[table] = int(row["c"] or 0)
    counts["total"] = sum(count for table, count in counts.items() if table != "total")
    return counts


def _copy_table_schema(source: sqlite3.Connection, dest: sqlite3.Connection, table: str) -> None:
    row = source.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if row is not None and row["sql"]:
        dest.execute(str(row["sql"]))


def _copy_table_indexes(source: sqlite3.Connection, dest: sqlite3.Connection, table: str) -> None:
    rows = source.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type='index' AND tbl_name=? AND sql IS NOT NULL
        ORDER BY name
        """,
        (table,),
    ).fetchall()
    for row in rows:
        if row["sql"]:
            try:
                dest.execute(str(row["sql"]))
            except sqlite3.IntegrityError:
                continue


def _copy_table_rows(
    source: sqlite3.Connection,
    dest: sqlite3.Connection,
    table: str,
    *,
    where: str = "",
    params: Sequence[Any] = (),
    insert_or_ignore: bool = False,
    columns: Sequence[str] | None = None,
) -> None:
    if not _table_exists(source, table):
        return
    available_columns = [
        str(row["name"] or "")
        for row in source.execute(f"PRAGMA table_info({_quote_ident(table)})").fetchall()
    ]
    selected_columns = (
        [str(column) for column in columns if str(column) in set(available_columns)]
        if columns is not None
        else available_columns
    )
    if not selected_columns:
        return
    column_sql = ", ".join(_quote_ident(column) for column in selected_columns)
    placeholders = ", ".join("?" for _ in selected_columns)
    where_sql = f" WHERE {where}" if where else ""
    rows = source.execute(f"SELECT {column_sql} FROM {_quote_ident(table)}{where_sql}", tuple(params)).fetchall()
    if rows:
        insert_sql = "INSERT OR IGNORE" if insert_or_ignore else "INSERT"
        dest.executemany(
            f"{insert_sql} INTO {_quote_ident(table)} ({column_sql}) VALUES ({placeholders})",
            [[row[column] for column in selected_columns] for row in rows],
        )


def _copy_identity_tables_to_temp(db_path: Path) -> tuple[Path, Path]:
    tmp_dir = Path(tempfile.mkdtemp(prefix="weather_identity_merge_fill_drill_"))
    tmp_db = tmp_dir / db_path.name
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    source.row_factory = sqlite3.Row
    try:
        dest = sqlite3.connect(str(tmp_db), timeout=30)
        dest.row_factory = sqlite3.Row
        try:
            for table in IDENTITY_TABLES:
                _copy_table_schema(source, dest, table)
            for table in IDENTITY_TABLES:
                _copy_table_rows(source, dest, table)
            dest.commit()
        finally:
            dest.close()
    finally:
        source.close()
    return tmp_dir, tmp_db


def _duplicate_row_ids(packet: Mapping[str, Any]) -> list[int]:
    row_ids: list[int] = []
    for group in packet.get("groups") or []:
        if not isinstance(group, Mapping):
            continue
        for row in group.get("rows") or []:
            if isinstance(row, Mapping) and str(row.get("row_id") or "").isdigit():
                row_ids.append(int(row["row_id"]))
    return sorted(set(row_ids))


def _as_sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _candidate_copy_keys(candidates: Sequence[Any]) -> dict[str, set[Any]]:
    keys: dict[str, set[Any]] = {table: set() for table in IDENTITY_TABLES}
    keys["event_fact_latest_tuple"] = set()
    latest_history_ids: set[int] = set()
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        table = str(candidate.get("table") or "")
        primary = candidate.get("primary_key")
        natural = candidate.get("natural_key")
        if not isinstance(primary, Mapping):
            continue
        if table in {"event_fact_history", "auto_trade_decisions", "auto_trade_jobs", "auto_orders"}:
            value = primary.get("id")
            if str(value or "").strip().isdigit():
                keys[table].add(int(value))
        elif table == "source_observations":
            source_event_id = str(primary.get("source_event_id") or "").strip()
            if source_event_id:
                keys[table].add(source_event_id)
        elif table == "event_fact_latest":
            slug = str(primary.get("slug") or "").strip()
            event_date = str(primary.get("event_date") or "").strip()
            if slug and event_date:
                keys["event_fact_latest_tuple"].add((slug, event_date))
            if isinstance(natural, Mapping):
                fact_id = natural.get("fact_id")
                if str(fact_id or "").strip().isdigit():
                    latest_history_ids.add(int(fact_id))
    keys["event_fact_history"].update(latest_history_ids)
    return keys


def _copy_rows_by_id(
    source: sqlite3.Connection,
    dest: sqlite3.Connection,
    table: str,
    ids: Sequence[Any],
    *,
    id_column: str = "id",
    columns: Sequence[str] | None = None,
) -> None:
    clean_ids = sorted({int(item) for item in ids if str(item or "").strip().isdigit()})
    for idx in range(0, len(clean_ids), 500):
        chunk = clean_ids[idx : idx + 500]
        placeholders = ", ".join("?" for _ in chunk)
        _copy_table_rows(
            source,
            dest,
            table,
            where=f"{_quote_ident(id_column)} IN ({placeholders})",
            params=chunk,
            insert_or_ignore=True,
            columns=columns,
        )


def _copy_source_observations_by_event_id(
    source: sqlite3.Connection,
    dest: sqlite3.Connection,
    source_event_ids: Sequence[Any],
) -> None:
    clean_ids = sorted({str(item or "").strip() for item in source_event_ids if str(item or "").strip()})
    for idx in range(0, len(clean_ids), 500):
        chunk = clean_ids[idx : idx + 500]
        placeholders = ", ".join("?" for _ in chunk)
        _copy_table_rows(
            source,
            dest,
            "source_observations",
            where=f"{_quote_ident('source_event_id')} IN ({placeholders})",
            params=chunk,
            insert_or_ignore=True,
            columns=(
                "source_event_id",
                "source_id",
                "station_id",
                "event_date",
                "valid_time",
                "valid_time_local",
                "fetched_at",
                "temperature_c",
                "dewpoint_c",
                "observed_today_max_c",
                "forecast_peak_c",
                "raw_ref",
                "raw_sha256",
                "source_observation_id",
                "created_at",
            ),
        )


def _copy_latest_by_key(
    source: sqlite3.Connection,
    dest: sqlite3.Connection,
    keys: Sequence[Any],
) -> None:
    clean_keys = sorted(
        {
            (str(item[0] or "").strip(), str(item[1] or "").strip())
            for item in keys
            if isinstance(item, Sequence)
            and not isinstance(item, (str, bytes, bytearray))
            and len(item) == 2
            and str(item[0] or "").strip()
            and str(item[1] or "").strip()
        }
    )
    for idx in range(0, len(clean_keys), 200):
        chunk = clean_keys[idx : idx + 200]
        where = " OR ".join(
            f"({_quote_ident('slug')} = ? AND {_quote_ident('event_date')} = ?)"
            for _ in chunk
        )
        params: list[Any] = []
        for slug, event_date in chunk:
            params.extend([slug, event_date])
        _copy_table_rows(
            source,
            dest,
            "event_fact_latest",
            where=where,
            params=params,
            insert_or_ignore=True,
            columns=(
                "slug",
                "event_date",
                "fact_id",
                "updated_at",
                "fact_snapshot_id",
                "correlation_id",
            ),
        )


def _latest_keys_for_fact_ids(source: sqlite3.Connection, fact_ids: Sequence[Any]) -> set[tuple[str, str]]:
    clean_ids = sorted({int(item) for item in fact_ids if str(item or "").strip().isdigit()})
    out: set[tuple[str, str]] = set()
    for idx in range(0, len(clean_ids), 500):
        chunk = clean_ids[idx : idx + 500]
        placeholders = ", ".join("?" for _ in chunk)
        for row in source.execute(
            f"""
            SELECT slug, event_date
            FROM event_fact_latest
            WHERE fact_id IN ({placeholders})
            """,
            chunk,
        ).fetchall():
            slug = str(row["slug"] or "").strip()
            event_date = str(row["event_date"] or "").strip()
            if slug and event_date:
                out.add((slug, event_date))
    return out


def _copy_focused_identity_rows_to_temp(
    db_path: Path,
    duplicate_source_packet: Mapping[str, Any],
    *,
    limit: int,
    candidates: Sequence[Any] = (),
) -> tuple[Path, Path]:
    tmp_dir = Path(tempfile.mkdtemp(prefix="weather_identity_merge_fill_focused_drill_"))
    tmp_db = tmp_dir / db_path.name
    duplicate_ids = _duplicate_row_ids(duplicate_source_packet)
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    source.row_factory = sqlite3.Row
    try:
        dest = sqlite3.connect(str(tmp_db), timeout=30)
        dest.row_factory = sqlite3.Row
        try:
            for table in IDENTITY_TABLES:
                _copy_table_schema(source, dest, table)
            candidate_keys = _candidate_copy_keys(candidates)
            if duplicate_ids:
                candidate_keys["event_fact_history"].update(duplicate_ids)
                candidate_keys["event_fact_latest_tuple"].update(_latest_keys_for_fact_ids(source, duplicate_ids))
            if candidates:
                _copy_rows_by_id(
                    source,
                    dest,
                    "event_fact_history",
                    candidate_keys["event_fact_history"],
                    columns=(
                        "id",
                        "run_date",
                        "slug",
                        "event_date",
                        "fact_as_of",
                        "composer_version",
                        "fact_json",
                        "created_at",
                        "correlation_id",
                        "source_event_id",
                        "fact_snapshot_id",
                        "posterior_run_id",
                    ),
                )
                _copy_latest_by_key(source, dest, candidate_keys["event_fact_latest_tuple"])
                _copy_rows_by_id(
                    source,
                    dest,
                    "auto_trade_decisions",
                    candidate_keys["auto_trade_decisions"],
                    columns=("id", "decision_id", "correlation_id"),
                )
                _copy_rows_by_id(
                    source,
                    dest,
                    "auto_trade_jobs",
                    candidate_keys["auto_trade_jobs"],
                    columns=("id", "order_intent_id", "correlation_id"),
                )
                _copy_rows_by_id(
                    source,
                    dest,
                    "auto_orders",
                    candidate_keys["auto_orders"],
                    columns=(
                        "id",
                        "order_id",
                        "slug",
                        "event_date",
                        "outcome",
                        "side",
                        "action",
                        "created_at",
                        "order_intent_id",
                        "correlation_id",
                    ),
                )
                _copy_source_observations_by_event_id(source, dest, candidate_keys["source_observations"])
            elif not duplicate_ids:
                _copy_table_rows(
                    source,
                    dest,
                    "event_fact_history",
                    where=(
                        "id IN (SELECT id FROM event_fact_history "
                        "WHERE fact_snapshot_id IS NULL OR trim(CAST(fact_snapshot_id AS TEXT)) = '' "
                        "OR correlation_id IS NULL OR trim(CAST(correlation_id AS TEXT)) = '' "
                        "ORDER BY id DESC LIMIT ?)"
                    ),
                    params=[int(limit)],
                )
                _copy_table_rows(
                    source,
                    dest,
                    "event_fact_latest",
                    where=(
                        "rowid IN (SELECT rowid FROM event_fact_latest "
                        "WHERE fact_snapshot_id IS NULL OR trim(CAST(fact_snapshot_id AS TEXT)) = '' "
                        "OR correlation_id IS NULL OR trim(CAST(correlation_id AS TEXT)) = '' "
                        "ORDER BY updated_at DESC LIMIT ?)"
                    ),
                    params=[int(limit)],
                )
                _copy_table_rows(
                    source,
                    dest,
                    "auto_trade_decisions",
                    where=(
                        "id IN (SELECT id FROM auto_trade_decisions "
                        "WHERE decision_id IS NULL OR trim(CAST(decision_id AS TEXT)) = '' "
                        "OR correlation_id IS NULL OR trim(CAST(correlation_id AS TEXT)) = '' "
                        "ORDER BY id DESC LIMIT ?)"
                    ),
                    params=[int(limit)],
                )
                _copy_table_rows(
                    source,
                    dest,
                    "auto_trade_jobs",
                    where=(
                        "id IN (SELECT id FROM auto_trade_jobs "
                        "WHERE order_intent_id IS NULL OR trim(CAST(order_intent_id AS TEXT)) = '' "
                        "OR correlation_id IS NULL OR trim(CAST(correlation_id AS TEXT)) = '' "
                        "ORDER BY id DESC LIMIT ?)"
                    ),
                    params=[int(limit)],
                )
                _copy_table_rows(
                    source,
                    dest,
                    "auto_orders",
                    where=(
                        "id IN (SELECT id FROM auto_orders "
                        "WHERE order_intent_id IS NULL OR trim(CAST(order_intent_id AS TEXT)) = '' "
                        "OR correlation_id IS NULL OR trim(CAST(correlation_id AS TEXT)) = '' "
                        "ORDER BY id DESC LIMIT ?)"
                    ),
                    params=[int(limit)],
                )
                _copy_table_rows(
                    source,
                    dest,
                    "source_observations",
                    where=(
                        "rowid IN (SELECT rowid FROM source_observations "
                        "WHERE source_observation_id IS NULL OR trim(CAST(source_observation_id AS TEXT)) = '' "
                        "ORDER BY created_at DESC LIMIT ?)"
                    ),
                    params=[int(limit)],
                )
            dest.commit()
        finally:
            dest.close()
    finally:
        source.close()
    return tmp_dir, tmp_db


def _copy_sqlite_backup(source_db: Path, backup_path: Path) -> str:
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path.unlink(missing_ok=True)
    try:
        shutil.copy2(source_db, backup_path)
        if backup_path.exists() and backup_path.stat().st_size > 0:
            return "file_copy"
    except OSError:
        backup_path.unlink(missing_ok=True)
    source = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True, timeout=30)
    try:
        dest = sqlite3.connect(str(backup_path), timeout=30)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    return "sqlite_backup"


def _temp_maintenance_window_packet(db_path: Path) -> dict[str, Any]:
    return {
        "schema": "controlled_apply_maintenance_window_packet_v1",
        "ok": True,
        "writer_quiescent": True,
        "db": str(Path(db_path).expanduser().resolve()),
        "counts": {
            "db_holder_process_count": 0,
            "writer_quiescence_blocker_count": 0,
            "reader_warning_count": 0,
            "write_capable_handle_process_count": 0,
        },
        "required_stop_labels": [],
        "blockers": [],
        "next_boundary": "temp_identity_tables_ready_for_merge_fill_drill",
    }


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_temp_archive_db": True,
        "writes_event_fact_history": False,
        "writes_event_fact_latest": False,
        "writes_auto_trade_decisions": False,
        "writes_auto_trade_jobs": False,
        "writes_auto_orders": False,
        "writes_source_observations": False,
        "writes_manifest_file": False,
        "creates_production_backup": False,
        "creates_temp_backup": True,
        "historical_backfill": False,
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "restarts_launchd": False,
        "changes_parameters": False,
    }


def _compact_duplicate_apply(result: Mapping[str, Any]) -> dict[str, Any]:
    apply_result = _as_mapping(result.get("apply_result"))
    contract = _as_mapping(result.get("contract"))
    return {
        "ok": bool(result.get("ok")),
        "applied": bool(result.get("applied")),
        "block_reason": str(result.get("block_reason") or apply_result.get("block_reason") or ""),
        "contract_ok": bool(contract.get("ok")),
        "contract_operation_count": int(contract.get("operation_count") or 0),
        "operation_count": int(apply_result.get("operation_count") or 0),
        "applied_operation_count": int(apply_result.get("applied_operation_count") or 0),
        "guards": dict(_as_mapping(result.get("guards"))),
    }


def _compact_merge_apply(result: Mapping[str, Any]) -> dict[str, Any]:
    apply_result = _as_mapping(result.get("apply_result")) or _as_mapping(result)
    contract = _as_mapping(result.get("contract"))
    return {
        "ok": bool(result.get("ok")),
        "applied": bool(result.get("applied")),
        "contract_ok": bool(contract.get("ok")) if contract else True,
        "contract_operation_count": int(contract.get("operation_count") or 0),
        "operation_count": int(apply_result.get("operation_count") or 0),
        "applied_operation_count": int(apply_result.get("applied_operation_count") or 0),
        "guards": dict(_as_mapping(result.get("guards"))),
    }


def run_drill(
    *,
    db_path: Path | str = DEFAULT_DB,
    latest_anchor_policy: str = DEFAULT_LATEST_ANCHOR_POLICY,
    limit: int = 1000,
    max_items_output: int = 20,
    copy_mode: str = "focused",
    keep_temp: bool = False,
) -> dict[str, Any]:
    source_db = Path(db_path).expanduser().resolve()
    started = _timer()
    stage_durations_ms: dict[str, int] = {}
    production_counts_before = _db_counts(source_db)

    stage_start = _timer()
    source_duplicate_packet = duplicate_packet.build_packet(source_db, immutable=True, max_items_output=1000000)
    stage_durations_ms["build_source_duplicate_packet_no_apply"] = _elapsed_ms(stage_start)

    stage_start = _timer()
    source_runbook = duplicate_runbook.build_operator_runbook(
        source_db,
        latest_anchor_policy=latest_anchor_policy,
        max_items_output=int(max_items_output),
    )
    reviewed_manifest = dict(_as_mapping(source_runbook.get("reviewed_manifest_candidate")))
    stage_durations_ms["build_source_duplicate_reviewed_manifest_no_apply"] = _elapsed_ms(stage_start)

    stage_start = _timer()
    source_merge_plan = merge_plan.build_plan(
        source_db,
        limit=int(limit),
        max_items_output=int(max_items_output),
        duplicate_decision_manifest=reviewed_manifest,
        include_coverage=False,
    )
    source_candidates = list(_as_sequence(source_merge_plan.get("candidate_operations")))
    stage_durations_ms["build_source_merge_fill_candidate_plan_no_apply"] = _elapsed_ms(stage_start)

    stage_start = _timer()
    if copy_mode == "all-identity":
        tmp_dir, tmp_db = _copy_identity_tables_to_temp(source_db)
    else:
        tmp_dir, tmp_db = _copy_focused_identity_rows_to_temp(
            source_db,
            source_duplicate_packet,
            limit=int(limit),
            candidates=source_candidates,
        )
    stage_durations_ms["copy_identity_tables_to_temp"] = _elapsed_ms(stage_start)
    retained = bool(keep_temp)
    duplicate_backup = tmp_dir / f"{tmp_db.name}.duplicate-before-merge-fill-drill.bak"
    merge_backup = tmp_dir / f"{tmp_db.name}.merge-fill-drill.bak"
    try:
        stage_start = _timer()
        duplicate_backup_mode = _copy_sqlite_backup(tmp_db, duplicate_backup)
        stage_durations_ms["create_temp_duplicate_backup"] = _elapsed_ms(stage_start)

        stage_start = _timer()
        duplicate_apply_result = duplicate_apply.build_cli_result(
            db_path=tmp_db,
            decision_manifest=reviewed_manifest,
            backup_path=duplicate_backup,
            apply=True,
            confirm=duplicate_apply.CONFIRMATION_PHRASE,
            writer_quiescent=True,
            max_items_output=int(max_items_output),
            maintenance_packet=_temp_maintenance_window_packet(tmp_db),
        )
        stage_durations_ms["apply_duplicate_repair_on_temp"] = _elapsed_ms(stage_start)

        stage_start = _timer()
        post_duplicate_packet = duplicate_packet.build_packet(tmp_db, immutable=False, max_items_output=int(max_items_output))
        stage_durations_ms["post_duplicate_packet"] = _elapsed_ms(stage_start)

        stage_start = _timer()
        pre_merge_contract = merge_contract.build_contract(
            tmp_db,
            limit=int(limit),
            max_items_output=int(max_items_output),
            include_coverage=False,
        )
        stage_durations_ms["build_pre_merge_fill_contract"] = _elapsed_ms(stage_start)

        stage_start = _timer()
        merge_backup_mode = _copy_sqlite_backup(tmp_db, merge_backup)
        stage_durations_ms["create_temp_merge_backup"] = _elapsed_ms(stage_start)

        stage_start = _timer()
        conn = sqlite3.connect(str(tmp_db), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            merge_apply_payload = merge_apply.apply_identity_merge_fill_contract(conn, pre_merge_contract)
        finally:
            conn.close()
        merge_apply_result = {
            "schema": merge_apply.RESULT_SCHEMA,
            "ok": bool(merge_apply_payload.get("ok")),
            "applied": bool(merge_apply_payload.get("applied")),
            "contract": merge_contract.compact_contract(pre_merge_contract, max_items_output=int(max_items_output)),
            "apply_result": merge_apply_payload,
            "backup": {"ok": merge_backup.exists(), "path": str(merge_backup), "mode": merge_backup_mode},
            "maintenance_window": _temp_maintenance_window_packet(tmp_db),
            "guards": dict(_as_mapping(merge_apply_payload.get("guards"))),
        }
        stage_durations_ms["apply_identity_merge_fill_on_temp"] = _elapsed_ms(stage_start)

        stage_start = _timer()
        post_merge_contract = merge_contract.build_contract(
            tmp_db,
            limit=int(limit),
            max_items_output=int(max_items_output),
            include_coverage=False,
        )
        stage_durations_ms["post_merge_fill_contract"] = _elapsed_ms(stage_start)

        stage_start = _timer()
        post_merge_missing_counts = _identity_missing_counts(tmp_db)
        stage_durations_ms["post_merge_identity_missing_counts"] = _elapsed_ms(stage_start)

        production_counts_after = _db_counts(source_db)
        production_unchanged = production_counts_before == production_counts_after
        source_duplicate_row_count = int(source_duplicate_packet.get("duplicate_row_count") or 0)
        duplicate_cleared = int(post_duplicate_packet.get("duplicate_row_count") or 0) == 0
        duplicate_apply_satisfied = (
            source_duplicate_row_count == 0
            or (bool(duplicate_apply_result.get("ok")) and bool(duplicate_apply_result.get("applied")))
        )
        pre_merge_ready = bool(pre_merge_contract.get("ok"))
        merge_applied = bool(merge_apply_result.get("ok")) and bool(merge_apply_result.get("applied"))
        post_merge_clear = bool(post_merge_contract.get("ok")) and int(post_merge_contract.get("operation_count") or 0) == 0
        post_merge_identity_clear = int(post_merge_missing_counts.get("total") or 0) == 0
        ok = bool(
            production_unchanged
            and duplicate_apply_satisfied
            and duplicate_cleared
            and pre_merge_ready
            and merge_applied
            and post_merge_clear
            and post_merge_identity_clear
        )
        return {
            "schema": SCHEMA_VERSION,
            "ok": ok,
            "execution_mode": "production_identity_merge_fill_temp_drill",
            "db": str(source_db),
            "temp_db": str(tmp_db),
            "temp_db_retained": retained,
            "copy_mode": copy_mode,
            "read_only_for_production": True,
            "production_counts_before": production_counts_before,
            "production_counts_after": production_counts_after,
            "production_counts_unchanged": production_unchanged,
            "latest_anchor_policy": latest_anchor_policy,
            "source_duplicate_packet": {
                "ok": bool(source_duplicate_packet.get("ok")),
                "duplicate_group_count": int(source_duplicate_packet.get("duplicate_group_count") or 0),
                "duplicate_row_count": int(source_duplicate_packet.get("duplicate_row_count") or 0),
                "hard_blockers": list(source_duplicate_packet.get("hard_blockers") or [])[:20],
            },
            "reviewed_manifest_summary": {
                "schema": reviewed_manifest.get("schema"),
                "latest_anchor_policy": reviewed_manifest.get("latest_anchor_policy"),
                "latest_anchor_policy_required": bool(reviewed_manifest.get("latest_anchor_policy_required")),
                "decision_count": int(reviewed_manifest.get("decision_count") or 0),
            },
            "source_merge_plan_summary": merge_plan.compact_plan(
                source_merge_plan,
                max_items_output=int(max_items_output),
            ),
            "duplicate_apply_result": _compact_duplicate_apply(duplicate_apply_result),
            "post_duplicate_packet": {
                "ok": bool(post_duplicate_packet.get("ok")),
                "duplicate_group_count": int(post_duplicate_packet.get("duplicate_group_count") or 0),
                "duplicate_row_count": int(post_duplicate_packet.get("duplicate_row_count") or 0),
                "hard_blockers": list(post_duplicate_packet.get("hard_blockers") or [])[:20],
            },
            "pre_merge_contract": merge_contract.compact_contract(
                pre_merge_contract,
                max_items_output=int(max_items_output),
            ),
            "merge_apply_result": _compact_merge_apply(merge_apply_result),
            "post_merge_contract": merge_contract.compact_contract(
                post_merge_contract,
                max_items_output=int(max_items_output),
            ),
            "post_merge_identity_missing_counts": post_merge_missing_counts,
            "temp_backups": {
                "duplicate_backup_path": str(duplicate_backup),
                "duplicate_backup_exists": duplicate_backup.exists(),
                "duplicate_backup_mode": duplicate_backup_mode,
                "merge_backup_path": str(merge_backup),
                "merge_backup_exists": merge_backup.exists(),
                "merge_backup_mode": merge_backup_mode,
            },
            "stage_durations_ms": stage_durations_ms,
            "elapsed_ms": _elapsed_ms(started),
            "guards": _guards(),
            "next_boundary": (
                "operator_may_run_duplicate_then_identity_merge_fill_apply_in_writer_quiescent_window"
                if ok
                else "fix_identity_merge_fill_temp_drill_blockers_before_production_apply"
            ),
        }
    finally:
        if not retained:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def compact_drill(result: Mapping[str, Any], *, max_items_output: int = 8) -> dict[str, Any]:
    visible_limit = max(0, int(max_items_output))
    source_duplicate = _as_mapping(result.get("source_duplicate_packet"))
    post_duplicate = _as_mapping(result.get("post_duplicate_packet"))
    pre_merge = _as_mapping(result.get("pre_merge_contract"))
    post_merge = _as_mapping(result.get("post_merge_contract"))
    duplicate_apply_result = _as_mapping(result.get("duplicate_apply_result"))
    merge_apply_result = _as_mapping(result.get("merge_apply_result"))
    return {
        "schema": SCHEMA_VERSION + "_summary",
        "source_schema": result.get("schema"),
        "ok": bool(result.get("ok")),
        "execution_mode": result.get("execution_mode"),
        "db": result.get("db"),
        "copy_mode": result.get("copy_mode"),
        "read_only_for_production": bool(result.get("read_only_for_production")),
        "production_counts_unchanged": bool(result.get("production_counts_unchanged")),
        "latest_anchor_policy": result.get("latest_anchor_policy"),
        "source_duplicate_packet": {
            "ok": bool(source_duplicate.get("ok")),
            "duplicate_group_count": int(source_duplicate.get("duplicate_group_count") or 0),
            "duplicate_row_count": int(source_duplicate.get("duplicate_row_count") or 0),
            "hard_blockers": list(source_duplicate.get("hard_blockers") or [])[:visible_limit],
        },
        "reviewed_manifest_summary": dict(_as_mapping(result.get("reviewed_manifest_summary"))),
        "duplicate_apply_result": {
            "ok": bool(duplicate_apply_result.get("ok")),
            "applied": bool(duplicate_apply_result.get("applied")),
            "block_reason": str(duplicate_apply_result.get("block_reason") or ""),
            "operation_count": int(duplicate_apply_result.get("operation_count") or 0),
            "applied_operation_count": int(duplicate_apply_result.get("applied_operation_count") or 0),
            "contract_operation_count": int(duplicate_apply_result.get("contract_operation_count") or 0),
        },
        "post_duplicate_packet": {
            "ok": bool(post_duplicate.get("ok")),
            "duplicate_group_count": int(post_duplicate.get("duplicate_group_count") or 0),
            "duplicate_row_count": int(post_duplicate.get("duplicate_row_count") or 0),
            "hard_blockers": list(post_duplicate.get("hard_blockers") or [])[:visible_limit],
        },
        "pre_merge_contract": {
            "ok": bool(pre_merge.get("ok")),
            "operation_count": int(pre_merge.get("operation_count") or 0),
            "candidate_count": int(pre_merge.get("candidate_count") or 0),
            "safe_candidate_count": int(pre_merge.get("safe_candidate_count") or 0),
            "blocked_candidate_count": int(pre_merge.get("blocked_candidate_count") or 0),
            "blockers": list(pre_merge.get("blockers") or [])[:visible_limit],
            "next_boundary": pre_merge.get("next_boundary"),
        },
        "merge_apply_result": {
            "ok": bool(merge_apply_result.get("ok")),
            "applied": bool(merge_apply_result.get("applied")),
            "operation_count": int(merge_apply_result.get("operation_count") or 0),
            "applied_operation_count": int(merge_apply_result.get("applied_operation_count") or 0),
            "contract_operation_count": int(merge_apply_result.get("contract_operation_count") or 0),
        },
        "post_merge_contract": {
            "ok": bool(post_merge.get("ok")),
            "operation_count": int(post_merge.get("operation_count") or 0),
            "candidate_count": int(post_merge.get("candidate_count") or 0),
            "safe_candidate_count": int(post_merge.get("safe_candidate_count") or 0),
            "blocked_candidate_count": int(post_merge.get("blocked_candidate_count") or 0),
            "blockers": list(post_merge.get("blockers") or [])[:visible_limit],
            "next_boundary": post_merge.get("next_boundary"),
        },
        "post_merge_identity_missing_counts": dict(_as_mapping(result.get("post_merge_identity_missing_counts"))),
        "stage_durations_ms": dict(_as_mapping(result.get("stage_durations_ms"))),
        "elapsed_ms": int(result.get("elapsed_ms") or 0),
        "guards": dict(_as_mapping(result.get("guards"))),
        "next_boundary": result.get("next_boundary"),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--latest-anchor-policy", default=DEFAULT_LATEST_ANCHOR_POLICY)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--max-items-output", type=int, default=20)
    parser.add_argument("--copy-mode", choices=("focused", "all-identity"), default="focused")
    parser.add_argument("--keep-temp", action="store_true")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_drill(
            db_path=args.db,
            latest_anchor_policy=str(args.latest_anchor_policy or ""),
            limit=int(args.limit),
            max_items_output=int(args.max_items_output),
            copy_mode=str(args.copy_mode or "focused"),
            keep_temp=bool(args.keep_temp),
        )
    except Exception as exc:
        result = {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "execution_mode": "production_identity_merge_fill_temp_drill_exception",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "read_only_for_production": True,
            "guards": _guards(),
            "next_boundary": "fix_identity_merge_fill_temp_drill_exception_before_production_apply",
        }
    payload: Mapping[str, Any] = (
        compact_drill(result, max_items_output=int(args.max_items_output))
        if args.summary
        else result
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

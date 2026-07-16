#!/usr/bin/env python3
"""Drill duplicate fact_snapshot_id repair on a temporary DB copy.

The drill copies archive.db with SQLite's backup API, builds the same reviewed
duplicate manifest used by the operator runbook, applies the same controlled
repair entrypoint against the temporary copy only, and verifies the duplicate
packet clears afterward. It never writes the production database, never writes a
production manifest, never fetches weather data, never restarts services, and
never places orders.
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

from scripts import production_identity_coverage_audit as coverage_audit  # noqa: E402
from scripts import production_identity_duplicate_fact_snapshot_apply as duplicate_apply  # noqa: E402
from scripts import production_identity_duplicate_fact_snapshot_apply_contract as apply_contract  # noqa: E402
from scripts import production_identity_duplicate_fact_snapshot_operator_runbook as operator_runbook  # noqa: E402
from scripts import production_identity_duplicate_fact_snapshot_packet as duplicate_packet  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_LATEST_ANCHOR_POLICY = "regenerate_latest_row_fact_snapshot_id_and_keep_latest_anchor"
SCHEMA_VERSION = "production_identity_duplicate_fact_snapshot_temp_drill_v1"


def _timer() -> float:
    return time.monotonic()


def _elapsed_ms(started: float) -> int:
    return max(0, int(round((time.monotonic() - started) * 1000)))


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def _table_count(conn: sqlite3.Connection, table: str) -> int | None:
    if not _table_exists(conn, table):
        return None
    row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
    return int(row["c"] or 0)


def _db_counts(db_path: Path) -> dict[str, int | None]:
    with _connect_ro(db_path) as conn:
        return {
            "event_fact_history": _table_count(conn, "event_fact_history"),
            "event_fact_latest": _table_count(conn, "event_fact_latest"),
            "event_fact_summary_cache": _table_count(conn, "event_fact_summary_cache"),
            "source_observations": _table_count(conn, "source_observations"),
            "auto_trade_decisions": _table_count(conn, "auto_trade_decisions"),
            "auto_trade_jobs": _table_count(conn, "auto_trade_jobs"),
            "auto_orders": _table_count(conn, "auto_orders"),
        }


def _copy_sqlite_db_to_temp(db_path: Path) -> tuple[Path, Path]:
    tmp_dir = Path(tempfile.mkdtemp(prefix="weather_duplicate_fact_snapshot_drill_"))
    tmp_db = tmp_dir / db_path.name
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        dest = sqlite3.connect(str(tmp_db), timeout=30)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    return tmp_dir, tmp_db


def _quote_ident(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row["name"] or "") for row in conn.execute(f"PRAGMA table_info({_quote_ident(table)})").fetchall()]


def _copy_table_schema(source: sqlite3.Connection, dest: sqlite3.Connection, table: str) -> None:
    row = source.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if row is None or not row["sql"]:
        return
    dest.execute(str(row["sql"]))


def _insert_rows(dest: sqlite3.Connection, table: str, columns: Sequence[str], rows: Sequence[sqlite3.Row]) -> None:
    if not columns or not rows:
        return
    column_sql = ", ".join(_quote_ident(column) for column in columns)
    placeholders = ", ".join("?" for _ in columns)
    dest.executemany(
        f"INSERT OR REPLACE INTO {_quote_ident(table)} ({column_sql}) VALUES ({placeholders})",
        [[row[column] for column in columns] for row in rows],
    )


def _duplicate_row_ids(packet: Mapping[str, Any]) -> list[int]:
    row_ids: list[int] = []
    for group in _as_sequence(packet.get("groups")):
        for row in _as_sequence(_as_mapping(group).get("rows")):
            value = _as_mapping(row).get("row_id")
            if str(value or "").isdigit():
                row_ids.append(int(value))
    return sorted(set(row_ids))


def _copy_duplicate_scope_to_temp(db_path: Path, packet: Mapping[str, Any]) -> tuple[Path, Path]:
    tmp_dir = Path(tempfile.mkdtemp(prefix="weather_duplicate_fact_snapshot_scope_drill_"))
    tmp_db = tmp_dir / db_path.name
    row_ids = _duplicate_row_ids(packet)
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    source.row_factory = sqlite3.Row
    try:
        dest = sqlite3.connect(str(tmp_db), timeout=30)
        dest.row_factory = sqlite3.Row
        try:
            for table in ("event_fact_history", "event_fact_latest"):
                _copy_table_schema(source, dest, table)
            if row_ids:
                placeholders = ", ".join("?" for _ in row_ids)
                history_columns = _table_columns(source, "event_fact_history")
                history_rows = source.execute(
                    f"SELECT * FROM event_fact_history WHERE id IN ({placeholders})",
                    row_ids,
                ).fetchall()
                _insert_rows(dest, "event_fact_history", history_columns, history_rows)

                latest_columns = _table_columns(source, "event_fact_latest")
                if "fact_id" in latest_columns:
                    latest_rows = source.execute(
                        f"SELECT * FROM event_fact_latest WHERE fact_id IN ({placeholders})",
                        row_ids,
                    ).fetchall()
                    _insert_rows(dest, "event_fact_latest", latest_columns, latest_rows)
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
        "stop_authorization": {
            "blockers": [],
            "can_pause_required_services_now": True,
        },
        "guards": {
            "writes_archive_db": False,
            "restarts_launchd": False,
            "places_orders": False,
        },
        "next_boundary": "temp_db_private_copy_ready_for_duplicate_fact_snapshot_drill",
    }


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_temp_archive_db": True,
        "writes_event_fact_history": False,
        "writes_event_fact_latest": False,
        "writes_manifest_file": False,
        "creates_production_backup": False,
        "creates_temp_backup": True,
        "historical_backfill": False,
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "restarts_launchd": False,
        "changes_parameters": False,
    }


def _compact_apply_result(result: Mapping[str, Any]) -> dict[str, Any]:
    apply_result = _as_mapping(result.get("apply_result"))
    contract = _as_mapping(result.get("contract"))
    backup = _as_mapping(result.get("backup"))
    maintenance = _as_mapping(result.get("maintenance_window"))
    return {
        "ok": bool(result.get("ok")),
        "applied": bool(result.get("applied")),
        "blocked": bool(result.get("blocked")),
        "block_reason": str(result.get("block_reason") or ""),
        "contract_ok": bool(contract.get("ok")),
        "contract_operation_count": int(contract.get("operation_count") or 0),
        "operation_count": int(apply_result.get("operation_count") or 0),
        "applied_operation_count": int(apply_result.get("applied_operation_count") or 0),
        "backup_ok": bool(backup.get("ok")),
        "maintenance_window_writer_quiescent": bool(maintenance.get("writer_quiescent")),
        "guards": dict(_as_mapping(result.get("guards"))),
    }


def _compact_contract(contract: Mapping[str, Any], *, max_items_output: int) -> dict[str, Any]:
    return apply_contract.compact_contract(contract, max_items_output=max_items_output)


def run_drill(
    *,
    db_path: Path | str = DEFAULT_DB,
    latest_anchor_policy: str = DEFAULT_LATEST_ANCHOR_POLICY,
    max_items_output: int = 20,
    include_identity_coverage: bool = False,
    copy_mode: str = "focused",
    keep_temp: bool = False,
) -> dict[str, Any]:
    source_db = Path(db_path).expanduser().resolve()
    started = _timer()
    stage_durations_ms: dict[str, int] = {}
    production_counts_before = _db_counts(source_db)

    stage_start = _timer()
    production_packet = duplicate_packet.build_packet(
        source_db,
        immutable=True,
        max_items_output=1000000,
    )
    stage_durations_ms["build_production_duplicate_packet_no_apply"] = _elapsed_ms(stage_start)

    stage_start = _timer()
    production_runbook = operator_runbook.build_operator_runbook(
        source_db,
        latest_anchor_policy=latest_anchor_policy,
        max_items_output=int(max_items_output),
    )
    stage_durations_ms["build_production_operator_runbook_no_apply"] = _elapsed_ms(stage_start)

    stage_start = _timer()
    if copy_mode == "full":
        tmp_dir, tmp_db = _copy_sqlite_db_to_temp(source_db)
    else:
        tmp_dir, tmp_db = _copy_duplicate_scope_to_temp(source_db, production_packet)
    stage_durations_ms["copy_source_to_temp"] = _elapsed_ms(stage_start)
    retained = bool(keep_temp)
    backup_path = tmp_dir / f"{tmp_db.name}.duplicate-fact-snapshot-drill.bak"
    try:
        stage_start = _timer()
        temp_runbook = operator_runbook.build_operator_runbook(
            tmp_db,
            latest_anchor_policy=latest_anchor_policy,
            max_items_output=int(max_items_output),
        )
        reviewed_manifest = dict(_as_mapping(temp_runbook.get("reviewed_manifest_candidate")))
        stage_durations_ms["build_temp_reviewed_manifest_no_file_write"] = _elapsed_ms(stage_start)

        stage_start = _timer()
        pre_apply_contract = apply_contract.build_contract(
            tmp_db,
            decision_manifest=reviewed_manifest,
            immutable=False,
            max_items_output=int(max_items_output),
        )
        stage_durations_ms["build_temp_pre_apply_contract_no_apply"] = _elapsed_ms(stage_start)

        stage_start = _timer()
        backup_mode = _copy_sqlite_backup(tmp_db, backup_path)
        stage_durations_ms["create_temp_backup"] = _elapsed_ms(stage_start)

        stage_start = _timer()
        apply_result = duplicate_apply.build_cli_result(
            db_path=tmp_db,
            decision_manifest=reviewed_manifest,
            backup_path=backup_path,
            apply=True,
            confirm=duplicate_apply.CONFIRMATION_PHRASE,
            writer_quiescent=True,
            max_items_output=int(max_items_output),
            maintenance_packet=_temp_maintenance_window_packet(tmp_db),
        )
        stage_durations_ms["apply_duplicate_repair_on_temp"] = _elapsed_ms(stage_start)

        stage_start = _timer()
        post_apply_packet = duplicate_packet.build_packet(
            tmp_db,
            immutable=False,
            max_items_output=int(max_items_output),
        )
        stage_durations_ms["post_apply_duplicate_packet"] = _elapsed_ms(stage_start)

        stage_start = _timer()
        post_apply_contract = apply_contract.build_contract(
            tmp_db,
            immutable=False,
            max_items_output=int(max_items_output),
        )
        stage_durations_ms["post_apply_contract_no_apply"] = _elapsed_ms(stage_start)

        if include_identity_coverage:
            stage_start = _timer()
            temp_identity_coverage = coverage_audit.audit(
                tmp_db,
                immutable=False,
                sample_limit=int(max_items_output),
            )
            stage_durations_ms["post_apply_identity_coverage_audit"] = _elapsed_ms(stage_start)
            coverage_summary = coverage_audit.compact_report(
                temp_identity_coverage,
                max_tables=int(max_items_output),
            )
        else:
            coverage_summary = {
                "schema": coverage_audit.SCHEMA_VERSION + "_summary",
                "ok": True,
                "skipped": True,
                "skip_reason": "identity_coverage_audit_is_opt_in_for_duplicate_temp_drill",
                "guards": coverage_audit._guards(),
                "next_boundary": "run_with_include_identity_coverage_if_full_identity_scan_is_required",
            }

        production_counts_after = _db_counts(source_db)
        production_unchanged = production_counts_before == production_counts_after
        duplicate_row_count_before = int(production_packet.get("duplicate_row_count") or 0)
        duplicate_row_count_after = int(post_apply_packet.get("duplicate_row_count") or 0)
        apply_ok = bool(apply_result.get("ok"))
        pre_contract_ok = bool(pre_apply_contract.get("ok"))
        temp_runbook_ready = bool(temp_runbook.get("reviewed_manifest_would_clear_duplicate_apply_contract"))
        no_duplicates_required = duplicate_row_count_before == 0
        duplicate_cleared = duplicate_row_count_after == 0
        ok = bool(
            production_unchanged
            and apply_ok
            and duplicate_cleared
            and (no_duplicates_required or (pre_contract_ok and temp_runbook_ready))
        )
        return {
            "schema": SCHEMA_VERSION,
            "ok": ok,
            "execution_mode": "production_identity_duplicate_fact_snapshot_temp_drill",
            "db": str(source_db),
            "temp_db": str(tmp_db),
            "temp_db_retained": retained,
            "copy_mode": copy_mode,
            "backup_path": str(backup_path),
            "backup_exists": backup_path.exists(),
            "temp_backup_mode": backup_mode,
            "read_only_for_production": True,
            "production_counts_before": production_counts_before,
            "production_counts_after": production_counts_after,
            "production_counts_unchanged": production_unchanged,
            "latest_anchor_policy": latest_anchor_policy,
            "source_duplicate_packet": {
                "ok": bool(production_packet.get("ok")),
                "duplicate_group_count": int(production_packet.get("duplicate_group_count") or 0),
                "duplicate_row_count": duplicate_row_count_before,
                "hard_blockers": list(production_packet.get("hard_blockers") or [])[:20],
                "next_boundary": production_packet.get("next_boundary"),
            },
            "source_runbook": {
                "ok": bool(production_runbook.get("ok")),
                "duplicate_group_count": int(production_runbook.get("duplicate_group_count") or 0),
                "duplicate_row_count": int(production_runbook.get("duplicate_row_count") or 0),
                "reviewed_manifest_would_clear_duplicate_apply_contract": bool(
                    production_runbook.get("reviewed_manifest_would_clear_duplicate_apply_contract")
                ),
                "existing_manifest_clears_duplicate_apply_contract": bool(
                    production_runbook.get("existing_manifest_clears_duplicate_apply_contract")
                ),
                "manifest_output_exists": bool(production_runbook.get("manifest_output_exists")),
                "backup_exists": bool(production_runbook.get("backup_exists")),
                "next_boundary": production_runbook.get("next_boundary"),
            },
            "reviewed_manifest_summary": {
                "schema": reviewed_manifest.get("schema"),
                "read_only_template": bool(reviewed_manifest.get("read_only_template")),
                "operator_review_required": bool(reviewed_manifest.get("operator_review_required")),
                "latest_anchor_policy": reviewed_manifest.get("latest_anchor_policy"),
                "latest_anchor_policy_required": bool(reviewed_manifest.get("latest_anchor_policy_required")),
                "decision_count": int(reviewed_manifest.get("decision_count") or 0),
                "boundary": reviewed_manifest.get("boundary"),
            },
            "pre_apply_contract": _compact_contract(pre_apply_contract, max_items_output=max_items_output),
            "apply_result": _compact_apply_result(apply_result),
            "post_apply_duplicate_packet": {
                "ok": bool(post_apply_packet.get("ok")),
                "duplicate_group_count": int(post_apply_packet.get("duplicate_group_count") or 0),
                "duplicate_row_count": duplicate_row_count_after,
                "hard_blockers": list(post_apply_packet.get("hard_blockers") or [])[:20],
                "next_boundary": post_apply_packet.get("next_boundary"),
            },
            "post_apply_contract": _compact_contract(post_apply_contract, max_items_output=max_items_output),
            "post_apply_identity_coverage": coverage_summary,
            "stage_durations_ms": stage_durations_ms,
            "elapsed_ms": _elapsed_ms(started),
            "guards": _guards(),
            "next_boundary": (
                "operator_may_run_controlled_duplicate_apply_in_writer_quiescent_window_after_reviewing_drill"
                if ok and not no_duplicates_required
                else "duplicate_fact_snapshot_apply_not_required"
                if ok
                else "fix_duplicate_fact_snapshot_temp_drill_blockers_before_production_apply"
            ),
        }
    finally:
        if not retained:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--latest-anchor-policy", default=DEFAULT_LATEST_ANCHOR_POLICY)
    parser.add_argument("--max-items-output", type=int, default=20)
    parser.add_argument("--include-identity-coverage", action="store_true")
    parser.add_argument("--copy-mode", choices=("focused", "full"), default="focused")
    parser.add_argument("--keep-temp", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_drill(
            db_path=args.db,
            latest_anchor_policy=str(args.latest_anchor_policy or ""),
            max_items_output=int(args.max_items_output),
            include_identity_coverage=bool(args.include_identity_coverage),
            copy_mode=str(args.copy_mode or "focused"),
            keep_temp=bool(args.keep_temp),
        )
    except Exception as exc:
        result = {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "execution_mode": "production_identity_duplicate_fact_snapshot_temp_drill_exception",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "read_only_for_production": True,
            "guards": _guards(),
            "next_boundary": "fix_duplicate_fact_snapshot_temp_drill_exception_before_production_apply",
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

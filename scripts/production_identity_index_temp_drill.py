#!/usr/bin/env python3
"""Temp DB drill for production identity audit-support indexes.

Production archive.db is read-only. The drill copies the minimal required
schema/data to a temporary SQLite DB, runs the same controlled index apply
entrypoint there, and verifies the post-apply no-apply contract.
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

from scripts import production_identity_index_apply as index_apply  # noqa: E402
from scripts import production_identity_index_apply_contract as index_contract  # noqa: E402


DEFAULT_DB = index_contract.DEFAULT_DB
SCHEMA_VERSION = "production_identity_index_temp_drill_v1"


def _timer() -> float:
    return time.monotonic()


def _elapsed_ms(started: float) -> int:
    return max(0, int(round((time.monotonic() - started) * 1000)))


def _quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _connect_ro(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _copy_identity_index_tables_minimal(source_db: Path, tmp_db: Path, *, row_limit: int) -> None:
    with _connect_ro(source_db) as source:
        dest = sqlite3.connect(str(tmp_db), timeout=30)
        try:
            tables = list(
                dict.fromkeys(str(spec.get("table") or "") for spec in index_contract.REQUIRED_INDEXES)
            )
            for table in tables:
                if not table:
                    continue
                column_names = ["id"]
                if table == "auto_trade_decisions":
                    column_names.append("decision_id")
                if table == "auto_trade_jobs":
                    column_names.append("order_intent_id")
                column_names.append("correlation_id")
                column_defs = ["id INTEGER PRIMARY KEY", *[f"{_quote_identifier(column)} TEXT" for column in column_names[1:]]]
                dest.execute(
                    f"CREATE TABLE {_quote_identifier(table)} ({', '.join(column_defs)})"
                )
                if not _table_exists(source, table):
                    continue
                select_sql = ", ".join(_quote_identifier(column) for column in column_names)
                rows = source.execute(
                    f"""
                    SELECT {select_sql}
                    FROM {_quote_identifier(table)}
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (max(1, int(row_limit)),),
                ).fetchall()
                placeholders = ", ".join("?" for _ in column_names)
                dest.executemany(
                    f"""
                    INSERT OR IGNORE INTO {_quote_identifier(table)}({select_sql})
                    VALUES ({placeholders})
                    """,
                    [tuple(row[column] for column in column_names) for row in rows],
                )
            dest.commit()
        finally:
            dest.close()


def _copy_sqlite_backup(source_db: Path, backup_path: Path) -> str:
    backup_path.parent.mkdir(parents=True, exist_ok=True)
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


def _temp_maintenance_packet(db: Path) -> dict[str, Any]:
    return {
        "schema": "controlled_apply_maintenance_window_packet_v1",
        "ok": True,
        "writer_quiescent": True,
        "db": str(db),
        "counts": {
            "db_holder_process_count": 0,
            "writer_quiescence_blocker_count": 0,
            "reader_warning_count": 0,
            "write_capable_handle_process_count": 0,
        },
        "required_stop_labels": [],
        "blockers": [],
        "next_boundary": "temp_identity_index_drill_ready",
    }


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_temp_archive_db": True,
        "creates_temp_backup": True,
        "creates_production_backup": False,
        "executes_temp_sql_writes": True,
        "historical_backfill": False,
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "restarts_launchd": False,
        "changes_parameters": False,
    }


def run_drill(
    *,
    db_path: Path | str = DEFAULT_DB,
    row_limit: int = 1000,
    keep_temp: bool = False,
) -> dict[str, Any]:
    started = _timer()
    production_db = Path(db_path).expanduser().resolve()
    tmp_dir = Path(tempfile.mkdtemp(prefix="weather_identity_index_drill_"))
    tmp_db = tmp_dir / production_db.name
    temp_backup = tmp_dir / f"{production_db.name}.index-drill.bak"
    try:
        _copy_identity_index_tables_minimal(production_db, tmp_db, row_limit=int(row_limit))
        pre_contract = index_contract.build_contract(tmp_db)
        backup_method = _copy_sqlite_backup(tmp_db, temp_backup)
        apply_result = index_apply.build_cli_result(
            db_path=tmp_db,
            backup_path=temp_backup,
            apply=True,
            confirm=index_apply.CONFIRMATION_PHRASE,
            writer_quiescent=True,
            max_items_output=8,
            maintenance_packet=_temp_maintenance_packet(tmp_db),
        )
        post_contract = index_contract.build_contract(tmp_db)
        ok = (
            pre_contract.get("needs_apply") is True
            and apply_result.get("ok") is True
            and apply_result.get("applied") is True
            and post_contract.get("ok") is True
        )
        return {
            "schema": SCHEMA_VERSION,
            "ok": ok,
            "read_only": True,
            "production_db": str(production_db),
            "temp_dir": str(tmp_dir),
            "temp_db": str(tmp_db),
            "keep_temp": bool(keep_temp),
            "row_limit": max(1, int(row_limit)),
            "pre_contract": index_contract.compact_contract(pre_contract, max_items_output=8),
            "backup": {
                "path": str(temp_backup),
                "method": backup_method,
                "exists": temp_backup.exists(),
                "size_bytes": temp_backup.stat().st_size if temp_backup.exists() else 0,
            },
            "apply_result": apply_result,
            "post_contract": index_contract.compact_contract(post_contract, max_items_output=8),
            "duration_ms": _elapsed_ms(started),
            "guards": _guards(),
            "next_boundary": (
                "production_identity_index_drill_passed_ready_for_controlled_maintenance_apply"
                if ok
                else "fix_production_identity_index_drill_before_apply"
            ),
        }
    finally:
        if not keep_temp:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--row-limit", type=int, default=1000)
    parser.add_argument("--keep-temp", action="store_true")
    parser.add_argument("--summary", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report = run_drill(
        db_path=args.db,
        row_limit=int(args.row_limit),
        keep_temp=bool(args.keep_temp),
    )
    if args.summary:
        report = {
            "schema": report["schema"] + "_summary",
            "ok": report["ok"],
            "read_only": report["read_only"],
            "production_db": report["production_db"],
            "row_limit": report["row_limit"],
            "pre_needs_apply": report["pre_contract"].get("needs_apply"),
            "apply_ok": report["apply_result"].get("ok"),
            "applied": report["apply_result"].get("applied"),
            "post_ok": report["post_contract"].get("ok"),
            "duration_ms": report["duration_ms"],
            "guards": report["guards"],
            "next_boundary": report["next_boundary"],
        }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if report.get("ok") else 3


if __name__ == "__main__":
    raise SystemExit(main())

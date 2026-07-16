#!/usr/bin/env python3
"""Run the formal fact materializer against a temporary archive.db copy.

This is a production-shape drill: it calls the same UI/server fact
materialization functions, but only after copying archive.db with SQLite's
backup API. It never writes the production database and never places orders.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "archive.db"

import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ui import server  # noqa: E402


def _normalize_date_key(value: Any) -> str:
    text = str(value or "").strip()
    if "T" in text:
        text = text.split("T", 1)[0]
    return text


def _copy_sqlite_db_to_temp(db_path: Path) -> tuple[Path, Path]:
    source = Path(db_path)
    if not source.exists():
        raise FileNotFoundError(str(source))
    tmp_dir = Path(tempfile.mkdtemp(prefix="weather_fact_materialize_drill_"))
    tmp_db = tmp_dir / source.name
    src_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst_conn = sqlite3.connect(tmp_db)
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()
    return tmp_dir, tmp_db


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _payload_summary(row: sqlite3.Row) -> dict[str, Any]:
    payload = server.safe_json_loads(str(row["fact_json"] or ""))
    payload = payload if isinstance(payload, dict) else {}
    ssot = _as_mapping(payload.get("posterior_ssot_fact"))
    provenance = _as_mapping(payload.get("fact_provenance"))
    resolver = _as_mapping(provenance.get("fact_materialization_source_resolver"))
    materialization_state = _as_mapping(payload.get("posterior_materialization_state"))
    auto_trade = _as_mapping(payload.get("auto_trade_ssot"))
    return {
        "fact_id": row["id"],
        "slug": row["slug"],
        "event_date": row["event_date"],
        "run_date": row["run_date"],
        "fact_as_of": row["fact_as_of"],
        "composer_version": row["composer_version"],
        "data_status": payload.get("data_status"),
        "resolver_source": resolver.get("source"),
        "formal_fact_chain": resolver.get("formal_fact_chain"),
        "posterior_run_id": payload.get("posterior_run_id"),
        "posterior_materialization_state": materialization_state.get("state"),
        "posterior_ssot_schema": ssot.get("schema"),
        "posterior_ssot_state": ssot.get("state"),
        "posterior_ssot_state_reason": ssot.get("state_reason"),
        "posterior_target_bucket": ssot.get("target_bucket"),
        "posterior_target_probability": ssot.get("target_probability"),
        "distribution_count": len(payload.get("distribution") or []),
        "main_entry_ok": auto_trade.get("main_entry_ok"),
    }


def _latest_rows(
    conn: sqlite3.Connection,
    *,
    start_date: str,
    end_date: str,
    slug_like: str,
    limit: int,
) -> list[dict[str, Any]]:
    params: list[Any] = [start_date, end_date]
    slug_clause = ""
    if slug_like:
        slug_clause = "AND h.slug LIKE ?"
        params.append(slug_like)
    params.append(max(1, min(int(limit or 20), 200)))
    rows = conn.execute(
        f"""
        SELECT h.id, h.run_date, h.slug, h.event_date, h.fact_as_of,
               h.composer_version, h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE h.event_date >= ? AND h.event_date <= ?
          {slug_clause}
        ORDER BY h.event_date ASC, h.fact_as_of DESC, h.id DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [_payload_summary(row) for row in rows]


def run_drill(
    *,
    db_path: Path = DEFAULT_DB,
    start_date: str,
    end_date: str,
    slug_like: str = "highest-temperature-in-chengdu%",
    max_items_output: int = 20,
    keep_temp: bool = False,
) -> dict[str, Any]:
    start_key = _normalize_date_key(start_date)
    end_key = _normalize_date_key(end_date)
    if not start_key or not end_key:
        raise ValueError("start_date and end_date are required")
    if start_key > end_key:
        raise ValueError("start_date must be <= end_date")
    tmp_dir, tmp_db = _copy_sqlite_db_to_temp(Path(db_path))
    old_db_path = server.DB_PATH
    server.DB_PATH = tmp_db
    retained = bool(keep_temp)
    try:
        with sqlite3.connect(tmp_db) as conn:
            conn.row_factory = sqlite3.Row
            server.ensure_fact_tables(conn)
            created = server._materialize_facts_from_snapshots_for_range(conn, start_key, end_key)
            conn.commit()
            rows = _latest_rows(
                conn,
                start_date=start_key,
                end_date=end_key,
                slug_like=str(slug_like or ""),
                limit=max_items_output,
            )
        return {
            "ok": True,
            "schema": "fact_materialization_temp_drill_v1",
            "read_only_production": True,
            "db": str(db_path),
            "temp_db": str(tmp_db),
            "temp_db_retained": retained,
            "scope": {
                "start_date": start_key,
                "end_date": end_key,
                "slug_like": str(slug_like or ""),
            },
            "created_count": int(created or 0),
            "latest_rows": rows,
            "guards": {
                "writes_production_archive_db": False,
                "writes_temp_archive_db": True,
                "places_orders": False,
                "restarts_launchd": False,
                "changes_parameters": False,
                "historical_backfill_production": False,
            },
            "next_boundary": "use_formal_materializer_apply_on_production_only_after_operator_confirmation",
        }
    finally:
        server.DB_PATH = old_db_path
        if not retained:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", default="")
    parser.add_argument("--slug-like", default="highest-temperature-in-chengdu%")
    parser.add_argument("--max-items-output", type=int, default=20)
    parser.add_argument("--keep-temp", action="store_true")
    args = parser.parse_args(argv)
    try:
        payload = run_drill(
            db_path=Path(args.db),
            start_date=args.start_date,
            end_date=args.end_date or args.start_date,
            slug_like=args.slug_like,
            max_items_output=args.max_items_output,
            keep_temp=bool(args.keep_temp),
        )
    except Exception as exc:
        payload = {
            "ok": False,
            "schema": "fact_materialization_temp_drill_v1",
            "read_only_production": True,
            "error": f"{exc.__class__.__name__}: {exc}",
            "guards": {
                "writes_production_archive_db": False,
                "places_orders": False,
                "restarts_launchd": False,
                "changes_parameters": False,
            },
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

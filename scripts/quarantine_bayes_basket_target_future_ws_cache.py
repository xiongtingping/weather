#!/usr/bin/env python3
"""Audit and purge future ET-day rows from the target WS evidence cache.

The official WS manifest may observe markets that have not started in ET yet,
but those rows are not target inputs.  This command only touches the isolated
target-owned ``basket_official_ws_evidence_cache`` table.  It never changes
the shared WS manifest, the market registry snapshot, the protected live
database, a service, or an order gateway.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.et_history import event_day_window  # noqa: E402
from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402


SCHEMA = "bayes_basket_target_future_ws_cache_cleanup_v1"
DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
DEFAULT_OUTPUT = ROOT / "output" / "audits" / "bayes_basket_target_future_ws_cache_cleanup.json"


def _utc(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError("future_ws_cache_as_of_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("future_ws_cache_as_of_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='basket_official_ws_evidence_cache'"
    ).fetchone()
    return row is not None


def _named_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _rows(conn: sqlite3.Connection, *, as_of_utc: str) -> list[dict[str, Any]]:
    if not _table_exists(conn):
        return []
    result: list[dict[str, Any]] = []
    columns = (
        "scope_key,evidence_id,station_id,event_date,observed_at,quote_version,"
        "manifest_sha256,quote_source,evidence_only,authorizes_order,content_sha256,updated_at"
    )
    for raw in conn.execute(
        f"SELECT {columns} FROM basket_official_ws_evidence_cache ORDER BY event_date,station_id,scope_key"
    ).fetchall():
        row = dict(zip(columns.split(","), raw))
        try:
            window = event_day_window(str(row.get("event_date") or ""), as_of_utc=as_of_utc)
        except Exception as exc:
            row["state"] = "invalid"
            row["reason"] = f"event_day_window_invalid:{type(exc).__name__}"
        else:
            row["state"] = str(window["state"])
            row["reason"] = str(window["reason"])
            row["target_day_start_utc"] = window["target_day_start_utc"]
            row["target_day_end_utc"] = window["target_day_end_utc"]
        if row.get("state") == "not_started":
            result.append(row)
    return result


def run(*, db: Path, as_of_utc: str, output: Path, purge: bool) -> dict[str, Any]:
    decision = _utc(as_of_utc)
    database = db.expanduser().resolve()
    if not database.is_file():
        raise ContractError("future_ws_cache_target_db_missing")
    with sqlite3.connect(database, timeout=60.0) as conn:
        conn.execute("PRAGMA busy_timeout=60000")
        candidates = _rows(conn, as_of_utc=decision)
        candidate_keys = [str(row["scope_key"]) for row in candidates]
        # The cache is deliberately independent of executable snapshots.  Do
        # not purge a row if a future row somehow became an executable quote,
        # inventory, or order-state fact; that would indicate a deeper breach
        # requiring a separate audit instead of silent cleanup.
        executable_dependencies: list[dict[str, Any]] = []
        for row in candidates:
            station, event_date = str(row["station_id"]), str(row["event_date"])
            checks = {
                "basket_official_ws_snapshot": ("station_id", "event_date"),
                "basket_confirmed_inventory_snapshot": ("station_id", "event_date"),
                "basket_order_state_snapshot": ("station_id", "event_date"),
                "basket_probability_envelope": ("station_id", "event_date"),
            }
            for table, _ in checks.items():
                if not _named_table_exists(conn, table):
                    continue
                exists = conn.execute(
                    f"SELECT 1 FROM {table} WHERE station_id=? AND event_date=? LIMIT 1",
                    (station, event_date),
                ).fetchone()
                if exists:
                    executable_dependencies.append({"scope_key": row["scope_key"], "table": table})
        blockers = ["future_ws_cache_has_executable_dependency"] if executable_dependencies else []
        body: dict[str, Any] = {
            "schema": SCHEMA,
            "as_of_utc": decision,
            "database": str(database),
            "mode": "purge" if purge else "audit_only",
            "candidate_count": len(candidates),
            "candidate_scope_keys": candidate_keys,
            "candidates": candidates,
            "executable_dependencies": executable_dependencies,
            "blockers": blockers,
            "checks": {
                "future_rows_only": all(row.get("state") == "not_started" for row in candidates),
                "no_executable_dependency": not executable_dependencies,
                "protected_production_line_touched": False,
                "shared_ws_manifest_touched": False,
                "calls_order_gateway": False,
                "purged": False,
            },
        }
        if purge and not blockers and candidates:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for scope_key in candidate_keys:
                    conn.execute(
                        "DELETE FROM basket_official_ws_evidence_cache WHERE scope_key=?",
                        (scope_key,),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            body["checks"]["purged"] = True
            body["purged_count"] = len(candidate_keys)
        body["remaining_future_count"] = len(_rows(conn, as_of_utc=decision))
    body["audit_sha256"] = content_sha256(body)
    write_bounded_json(output.expanduser().resolve(), body)
    return body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--purge", action="store_true")
    args = parser.parse_args(argv)
    result = run(db=args.db, as_of_utc=args.as_of_utc, output=args.output, purge=args.purge)
    print(json.dumps({
        "schema": result["schema"],
        "candidate_count": result["candidate_count"],
        "purged": result["checks"]["purged"],
        "remaining_future_count": result["remaining_future_count"],
        "blockers": result["blockers"],
    }, ensure_ascii=False, sort_keys=True))
    return 0 if not result["blockers"] and result["remaining_future_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

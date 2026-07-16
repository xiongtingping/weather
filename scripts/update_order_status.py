#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from archive_db_write_queue import connect_archive_db_rw  # noqa: E402
from auto_order_ledger_writer import update_auto_order_status_by_order_id  # noqa: E402

DEFAULT_DB = ROOT_DIR / "data" / "archive.db"
POLYMARKET_ORDER_SCRIPT = ROOT_DIR / "scripts" / "polymarket_order.mjs"


def _archive_db_path() -> Path:
    raw = os.environ.get("PM_ARCHIVE_DB") or os.environ.get("PM_ARCHIVE_DB_PATH") or ""
    return Path(raw).expanduser() if raw else DEFAULT_DB


@contextmanager
def _open_db() -> Iterator[sqlite3.Connection]:
    with connect_archive_db_rw(
        _archive_db_path(),
        owner="update_order_status",
        timeout_sec=30.0,
        busy_timeout_ms=30000,
        row_factory=False,
    ) as conn:
        yield conn


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_orders(conn, only_missing: bool, limit: Optional[int]) -> List[Tuple[Any, ...]]:
    cur = conn.cursor()
    if only_missing:
        sql = (
            "SELECT order_id, slug, event_date, outcome, side, action, size, created_at "
            "FROM auto_orders "
            "WHERE order_id IS NOT NULL "
            "  AND order_id NOT IN (SELECT order_id FROM edge_trade_fills WHERE order_id IS NOT NULL) "
            "ORDER BY created_at DESC"
        )
    else:
        sql = (
            "SELECT order_id, slug, event_date, outcome, side, action, size, created_at "
            "FROM auto_orders "
            "WHERE order_id IS NOT NULL "
            "ORDER BY created_at DESC"
        )
    if limit and limit > 0:
        sql += f" LIMIT {int(limit)}"
    cur.execute(sql)
    return cur.fetchall()


def _parse_payload(stdout: str, stderr: str, code: int) -> Dict[str, Any]:
    payload: Dict[str, Any]
    raw = stdout.strip() or stderr.strip()
    if raw:
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"ok": False, "error": "parse_error", "raw": raw}
    else:
        payload = {"ok": False, "error": "no_output", "code": code}
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync auto order status via get-order")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of orders")
    parser.add_argument("--include-all", action="store_true", help="Include all auto_orders (not only missing)")
    parser.add_argument("--no-write", action="store_true", help="Do not write to DB")
    args = parser.parse_args()

    with _open_db() as conn:
        rows = _load_orders(conn, only_missing=not args.include_all, limit=args.limit or None)

    cmd_base = ["node", str(POLYMARKET_ORDER_SCRIPT), "get-order", "--order-id"]

    by_status: Dict[str, int] = {}
    updated = 0
    now_iso = _utc_now_iso()

    with _open_db() as conn:
        cur = conn.cursor()
        for r in rows:
            order_id = r[0]
            cmd = cmd_base + [str(order_id)]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            payload = _parse_payload(proc.stdout, proc.stderr, proc.returncode)

            status = "UNKNOWN"
            if isinstance(payload, dict) and payload.get("ok") and isinstance(payload.get("order"), dict):
                order_info = payload["order"]
                status = str(order_info.get("status") or "UNKNOWN")

            by_status[status] = by_status.get(status, 0) + 1

            if not args.no_write:
                update_auto_order_status_by_order_id(
                    cur,
                    order_id=str(order_id),
                    order_status=status,
                    order_checked_at=now_iso,
                )
                updated += 1

        if not args.no_write:
            conn.commit()

    print("count", len(rows))
    print("by_status", by_status)
    print("updated", updated)
    if args.no_write:
        print("db_write", "disabled")


if __name__ == "__main__":
    main()

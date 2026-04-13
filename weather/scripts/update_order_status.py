#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from typing import Any, Dict, List, Optional, Tuple

import scan_weather_market as s


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
    parser = argparse.ArgumentParser(description="Update auto_orders.order_status via get-order")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of orders")
    parser.add_argument("--include-all", action="store_true", help="Include all auto_orders (not only missing)")
    parser.add_argument("--no-write", action="store_true", help="Do not write to DB")
    args = parser.parse_args()

    with s.open_db() as conn:
        rows = _load_orders(conn, only_missing=not args.include_all, limit=args.limit or None)

    cmd_base = ["node", "/Users/xiong/weather/scripts/polymarket_order.mjs", "get-order", "--order-id"]

    by_status: Dict[str, int] = {}
    updated = 0
    now_iso = s.utc_now_iso()

    with s.open_db() as conn:
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
                cur.execute(
                    "UPDATE auto_orders SET order_status = ?, order_checked_at = ? WHERE order_id = ?",
                    (status, now_iso, str(order_id)),
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

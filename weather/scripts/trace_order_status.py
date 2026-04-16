#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import scan_weather_market as s


ROOT_DIR = Path(__file__).resolve().parents[1]
POLYMARKET_ORDER_SCRIPT = ROOT_DIR / "scripts" / "polymarket_order.mjs"
DEFAULT_OUTPUT_PATH = ROOT_DIR / "data" / "order_status_trace.json"


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
    parser = argparse.ArgumentParser(description="Trace order status via polymarket get-order")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of orders")
    parser.add_argument("--output", type=str, default=str(DEFAULT_OUTPUT_PATH))
    parser.add_argument("--include-all", action="store_true", help="Include all auto_orders (not only missing)")
    args = parser.parse_args()

    with s.open_db() as conn:
        rows = _load_orders(conn, only_missing=not args.include_all, limit=args.limit or None)

    results: Dict[str, Any] = {
        "generated_at": s.utc_now_iso(),
        "count": len(rows),
        "only_missing": not args.include_all,
        "orders": [],
    }

    cmd_base = ["node", str(POLYMARKET_ORDER_SCRIPT), "get-order", "--order-id"]

    for r in rows:
        order_id, slug, event_date, outcome, side, action, size, created_at = r
        cmd = cmd_base + [str(order_id)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        payload = _parse_payload(proc.stdout, proc.stderr, proc.returncode)

        order_info = None
        status = None
        associate_trades = None
        if isinstance(payload, dict) and payload.get("ok") and isinstance(payload.get("order"), dict):
            order_info = payload["order"]
            status = order_info.get("status")
            associate_trades = order_info.get("associate_trades")

        results["orders"].append(
            {
                "order_id": order_id,
                "slug": slug,
                "event_date": event_date,
                "outcome": outcome,
                "side": side,
                "action": action,
                "size": size,
                "created_at": created_at,
                "ok": bool(payload.get("ok")) if isinstance(payload, dict) else False,
                "status": status,
                "associate_trades": associate_trades,
                "raw": payload,
            }
        )

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    by_status: Dict[str, int] = {}
    with_trades = 0
    for o in results["orders"]:
        st = o.get("status") or "UNKNOWN"
        by_status[st] = by_status.get(st, 0) + 1
        if o.get("associate_trades"):
            with_trades += 1

    print("count", results["count"])
    print("by_status", by_status)
    print("with_associate_trades", with_trades)
    print("saved", args.output)


if __name__ == "__main__":
    main()

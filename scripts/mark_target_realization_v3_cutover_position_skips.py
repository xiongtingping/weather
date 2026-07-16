#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archive_db_write_queue import connect_archive_db_rw  # noqa: E402
from auto_trade_decision_writer import insert_auto_trade_decision  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_plan(conn: sqlite3.Connection, activation_date: str) -> List[Dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, slug, substr(event_date, 1, 10) AS event_date,
               outcome, UPPER(COALESCE(side, '')) AS side,
               state, position_size, avg_price,
               exit_skip_reason, exit_skip_at
        FROM edge_trades
        WHERE (
            COALESCE(position_size, 0) > 0.000001
            OR LOWER(COALESCE(state, '')) IN ('entry', 'entry_pending', 'exit_pending')
        )
          AND (
            COALESCE(exit_skip_reason, '') = ''
            OR COALESCE(exit_skip_at, '') = ''
          )
        ORDER BY event_date, id
        """
    ).fetchall()
    result: List[Dict[str, Any]] = []
    for row in rows:
        event_date = str(row["event_date"] or "")
        reason = (
            "target_realization_d1_existing_positions_frozen"
            if event_date >= activation_date
            else "target_realization_v3_cutover_legacy_position_preserved"
        )
        result.append({**dict(row), "reason_code": reason})
    return result


def apply_plan(conn: sqlite3.Connection, rows: List[Dict[str, Any]], now_iso: str) -> int:
    updated = 0
    cur = conn.cursor()
    for row in rows:
        details = {
            "schema": "target_realization_v3_cutover_position_skip_v1",
            "source": "target_realization_v3_cutover",
            "strategy_family": "posterior_settlement",
            "reason_code": row["reason_code"],
            "edge_trade_id": int(row["id"]),
            "position_size": row.get("position_size"),
            "avg_price": row.get("avg_price"),
            "no_position_or_order_mutation": True,
            "historical_backfill": False,
        }
        cur.execute(
            """
            UPDATE edge_trades
            SET exit_skip_reason = ?, exit_skip_detail = ?, exit_skip_at = ?, updated_at = ?
            WHERE id = ?
              AND (
                  COALESCE(position_size, 0) > 0.000001
                  OR LOWER(COALESCE(state, '')) IN ('entry', 'entry_pending', 'exit_pending')
              )
            """,
            (
                row["reason_code"],
                json.dumps(details, ensure_ascii=False, sort_keys=True),
                now_iso,
                now_iso,
                int(row["id"]),
            ),
        )
        if int(cur.rowcount or 0) <= 0:
            continue
        insert_auto_trade_decision(
            cur,
            run_id=None,
            run_date=now_iso[:10],
            slug=str(row["slug"] or ""),
            event_date=str(row["event_date"] or ""),
            title=str(row["slug"] or ""),
            outcome=str(row["outcome"] or ""),
            side=str(row["side"] or ""),
            action="hold",
            decided_state=(
                "frozen"
                if row["reason_code"] == "target_realization_d1_existing_positions_frozen"
                else "hold"
            ),
            will_order=False,
            reason_codes=[str(row["reason_code"])],
            reasons=["v3 切换时保留既有持仓；本动作只补齐退出未触发审计，不改仓、不撤单、不下单。"],
            details=details,
            created_at=now_iso,
        )
        updated += 1
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit and optionally mark v3 cutover exit-skip fields for current open positions."
    )
    parser.add_argument("--db", default=str(ROOT / "data" / "archive.db"))
    parser.add_argument("--activation-date", default="2026-07-11")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    db = Path(args.db)
    if not db.exists():
        print(json.dumps({"ok": False, "reason": "db_missing", "db": str(db)}))
        return 2
    connection_context = (
        connect_archive_db_rw(db, owner="target_realization_v3_cutover_skip")
        if args.apply
        else closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True))
    )
    with connection_context as conn:
        plan = build_plan(conn, str(args.activation_date)[:10])
        updated = 0
        if args.apply and plan:
            updated = apply_plan(conn, plan, _now())
            conn.commit()
        print(
            json.dumps(
                {
                    "ok": True,
                    "read_only": not args.apply,
                    "apply": bool(args.apply),
                    "activation_date": str(args.activation_date)[:10],
                    "planned_rows": len(plan),
                    "updated_rows": updated,
                    "positions": plan,
                    "guards": {
                        "changes_position_size": False,
                        "cancels_orders": False,
                        "places_orders": False,
                        "historical_backfill": False,
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

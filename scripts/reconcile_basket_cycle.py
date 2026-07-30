#!/usr/bin/env python3
"""Close a basket cycle only after proving no exchange submission occurred.

This is deliberately narrower than an exchange-order reconciler.  It may
close a stale ``prepared``/``reconcile_required`` basket only when every leg
is still prepared, has no exchange identity, and has zero matched size.  Any
ambiguous or partially submitted cycle remains blocked.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "archive.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def reconcile_no_exchange_submission(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    confirm: bool,
) -> dict[str, object]:
    if not confirm:
        raise ValueError("confirmation_required:--confirm-no-exchange-submission")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("BEGIN IMMEDIATE")
    try:
        cycle = conn.execute(
            "SELECT cycle_id,state,slug,event_date FROM basket_strategy_cycles WHERE cycle_id=?",
            (cycle_id,),
        ).fetchone()
        if cycle is None:
            raise ValueError("basket_cycle_not_found")
        cycle_state = str(cycle[1] or "")
        if cycle_state not in {"prepared", "reconcile_required"}:
            raise ValueError(f"basket_cycle_not_reconcilable:{cycle_state}")

        legs = conn.execute(
            """SELECT leg_id,state,exchange_order_id,size_matched,gateway_response_json
                 FROM basket_strategy_legs WHERE cycle_id=? ORDER BY leg_id""",
            (cycle_id,),
        ).fetchall()
        if not legs:
            raise ValueError("basket_cycle_legs_missing")
        unsafe = []
        for leg_id, state, exchange_order_id, size_matched, gateway_json in legs:
            if str(state or "") != "prepared" or str(exchange_order_id or "") or float(size_matched or 0.0) != 0.0:
                unsafe.append(str(leg_id))
                continue
            if str(gateway_json or "{}").strip() not in {"", "{}"}:
                unsafe.append(str(leg_id))
        if unsafe:
            raise ValueError("basket_cycle_exchange_evidence_present:" + ",".join(unsafe))

        now = _now()
        conn.execute(
            """UPDATE basket_strategy_legs
                  SET state='submit_failed',last_error=?,updated_at=?
                WHERE cycle_id=? AND state='prepared'""",
            ("reconciled_no_exchange_submission", now, cycle_id),
        )
        conn.execute(
            """UPDATE basket_strategy_cycles
                  SET state='failed',updated_at=?
                WHERE cycle_id=? AND state IN ('prepared','reconcile_required')""",
            (now, cycle_id),
        )
        conn.commit()
        return {
            "ok": True,
            "cycle_id": str(cycle[0]),
            "slug": str(cycle[2] or ""),
            "event_date": str(cycle[3] or ""),
            "prior_state": cycle_state,
            "final_state": "failed",
            "legs_closed": len(legs),
            "reason": "reconciled_no_exchange_submission",
            "reconciled_at": now,
        }
    except Exception:
        conn.rollback()
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycle-id", required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--confirm-no-exchange-submission", action="store_true", dest="confirm")
    args = parser.parse_args()
    conn = sqlite3.connect(args.db, timeout=20.0, isolation_level=None)
    try:
        result = reconcile_no_exchange_submission(conn, cycle_id=args.cycle_id, confirm=args.confirm)
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Commit a P0-M HOLD as an isolated no-live decision and reconcile."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.atomic_ledger import commit_blocked_scope_decision  # noqa: E402
from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    assert_connection_target_capacity,
    load_bounded_json,
    write_bounded_json,
)
from bayes_basket_target.ssot import apply_production_ssot_schema  # noqa: E402


SCHEMA = "bayes_basket_p0m_no_live_reconcile_receipt_v1"


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", type=Path, required=True)
    parser.add_argument("--scratch-db", type=Path, required=True)
    parser.add_argument("--receipt-output", type=Path, required=True)
    args = parser.parse_args()
    replay = load_bounded_json(args.replay.expanduser().resolve())
    if not isinstance(replay, Mapping):
        raise SystemExit("replay must be a JSON object")
    if (
        replay.get("schema") != "bayes_basket_p0m_retrospective_replay_v1"
        or replay.get("state") != "retrospective_no_live_materialized"
        or (replay.get("boundary") or {}).get("places_orders") is not False
    ):
        raise SystemExit("replay boundary invalid")
    plan = replay["p0m_preselection"]["compiled_plan"]["plan"]
    envelope = replay["p0m_envelope"]
    if plan.get("state") != "HOLD" or plan.get("places_orders") is not False:
        raise SystemExit("only a zero-order HOLD replay may use this writer")
    reasons = [str(item) for item in plan.get("reason_codes", []) if str(item)]
    if not reasons:
        raise SystemExit("HOLD replay reason codes missing")
    db_path = args.scratch_db.expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path, timeout=30.0) as conn:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=DELETE")
        apply_production_ssot_schema(conn)
        assert_connection_target_capacity(conn)
        decision = commit_blocked_scope_decision(
            conn,
            station_id=str(envelope["station_id"]),
            event_date=str(envelope["event_date"]),
            settlement_timezone=str(envelope["settlement_timezone"]),
            reason_codes=reasons,
            details={
                "replay_content_sha256": replay["content_sha256"],
                "p0m_checkpoint_id": envelope["checkpoint_id"],
                "taf_revision_proof_id": replay["taf_revision_proof"]["proof_id"],
                "plan_id": plan["plan_id"],
                "plan_state": plan["state"],
                "probability_stage": "P0-M",
                "production_mode": "production_no_live",
                "native_local_taf_receipt": False,
            },
            created_at_utc=str(plan["observed_at"]),
            strategy_lane="basket",
        )
        counts = {
            table: _count(conn, table)
            for table in (
                "auto_trade_decisions",
                "basket_target_execution_cycles",
                "basket_target_order_intents",
                "basket_target_capital_reservations",
                "basket_target_order_events",
            )
        }
        will_order = int(
            conn.execute(
                "SELECT COALESCE(SUM(will_order),0) FROM auto_trade_decisions"
            ).fetchone()[0]
        )
        reconcile_required = int(
            conn.execute(
                "SELECT COUNT(*) FROM basket_target_execution_cycles "
                "WHERE state='reconcile_required'"
            ).fetchone()[0]
        )
        conn.commit()
    reconciled = (
        counts["auto_trade_decisions"] == 1
        and will_order == 0
        and counts["basket_target_order_intents"] == 0
        and counts["basket_target_capital_reservations"] == 0
        and counts["basket_target_order_events"] == 0
        and reconcile_required == 0
    )
    identity: dict[str, Any] = {
        "schema": SCHEMA,
        "state": "NO_ORDER_RECONCILED" if reconciled else "BLOCKED_FAIL_CLOSED",
        "decision": decision,
        "counts": counts,
        "will_order_sum": will_order,
        "reconcile_required_count": reconcile_required,
        "scratch_db": str(db_path),
        "boundary": {
            "production_mode": "production_no_live",
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "orders_submitted": 0,
            "live_submit_authorized": False,
        },
    }
    result = {**identity, "content_sha256": content_sha256(identity)}
    write_bounded_json(args.receipt_output.expanduser().resolve(), result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if reconciled else 2


if __name__ == "__main__":
    raise SystemExit(main())

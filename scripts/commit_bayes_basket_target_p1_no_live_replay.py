#!/usr/bin/env python3
"""Atomically commit one isolated P1 no-live plan and reconcile zero orders."""

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

from bayes_basket_target.atomic_ledger import initialize_capital_pool  # noqa: E402
from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.production import commit_production_candidate_atomic  # noqa: E402
from bayes_basket_target.production_io import target_atomic_facts_from_cycle  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    assert_connection_target_capacity,
    load_bounded_json,
    write_bounded_json,
)
from bayes_basket_target.ssot import apply_production_ssot_schema  # noqa: E402


SCHEMA = "bayes_basket_p1_no_live_reconcile_receipt_v1"


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _stable_decision_identity(value: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize first-write/replay receipts onto one content identity."""

    receipt = value.get("receipt")
    if not isinstance(receipt, Mapping):
        raise SystemExit("P1 atomic decision receipt missing")
    required = ("decision_id", "cycle_id", "reservation_id")
    if any(not str(receipt.get(field) or "") for field in required):
        raise SystemExit("P1 atomic decision receipt identity missing")
    if (
        value.get("candidate_will_order") is not False
        or value.get("live_submit_authorized") is not False
        or value.get("calls_order_gateway") is not False
        or int(value.get("orders_submitted", -1)) != 0
    ):
        raise SystemExit("P1 atomic decision execution boundary invalid")
    return {
        "schema": "bayes_basket_target_production_candidate_atomic_identity_v1",
        "state": "committed_idempotent",
        "decision_id": str(receipt["decision_id"]),
        "cycle_id": str(receipt["cycle_id"]),
        "reservation_id": str(receipt["reservation_id"]),
        "candidate_will_order": False,
        "live_submit_authorized": False,
        "calls_order_gateway": False,
        "places_orders": False,
        "orders_submitted": 0,
        "idempotent": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", type=Path, required=True)
    parser.add_argument("--scratch-db", type=Path, required=True)
    parser.add_argument("--receipt-output", type=Path, required=True)
    args = parser.parse_args()
    replay = load_bounded_json(args.replay.expanduser().resolve())
    if not isinstance(replay, Mapping):
        raise SystemExit("P1 replay must be a JSON object")
    schema = replay.get("schema")
    if schema == "bayes_basket_p1_retrospective_replay_v1":
        if (
            replay.get("state") != "retrospective_no_live_materialized"
            or (replay.get("boundary") or {}).get("places_orders") is not False
            or (replay.get("boundary") or {}).get("live_submit_authorized") is not False
        ):
            raise SystemExit("P1 replay boundary invalid")
        compiled = replay.get("p1_production_plan")
        market = replay.get("production_market_snapshot")
    elif schema == "bayes_basket_target_no_live_cycle_compile_v1":
        if (
            replay.get("state") != "COMPILED_NO_WRITE"
            or (replay.get("boundary") or {}).get("places_orders") is not False
            or (replay.get("boundary") or {}).get("live_submit_authorized") is not False
        ):
            raise SystemExit("P1 compiled cycle boundary invalid")
        compiled = replay.get("plan")
        market = (
            compiled.get("production_market_snapshot")
            if isinstance(compiled, Mapping)
            else None
        )
    else:
        raise SystemExit("P1 replay schema invalid")
    if not isinstance(compiled, Mapping) or not isinstance(market, Mapping):
        raise SystemExit("P1 production plan or market snapshot missing")
    if (
        compiled.get("execution_mode") != "production_no_live"
        or compiled.get("stage") != "P1"
        or compiled.get("live_submit_authorized") is not False
        or (compiled.get("boundary") or {}).get("calls_order_gateway") is not False
    ):
        raise SystemExit("P1 compiled production boundary invalid")
    facts = target_atomic_facts_from_cycle(compiled)
    created_at = str((compiled.get("plan") or {}).get("observed_at") or "")
    db_path = args.scratch_db.expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path, timeout=30.0) as conn:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=DELETE")
        apply_production_ssot_schema(conn)
        initialize_capital_pool(
            conn,
            balance_usdc=0.0,
            capital_epoch_id="rkpk-p1-no-live-zero-capital-v1",
            updated_at_utc=created_at,
        )
        assert_connection_target_capacity(conn)
        decision = commit_production_candidate_atomic(
            conn,
            compiled_plan=compiled,
            market_snapshot=market,
            observation=facts["observation"],
            bound=facts["bound"],
            created_at_utc=created_at,
        )
        stable_decision = _stable_decision_identity(decision)
        counts = {
            table: _count(conn, table)
            for table in (
                "basket_target_observation_facts",
                "basket_target_bound_facts",
                "auto_trade_decisions",
                "basket_target_execution_cycles",
                "basket_target_order_intents",
                "basket_target_capital_reservations",
                "basket_target_order_events",
            )
        }
        reserved = float(
            conn.execute(
                "SELECT COALESCE(SUM(amount_usdc),0) "
                "FROM basket_target_capital_reservations"
            ).fetchone()[0]
        )
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
        counts["basket_target_observation_facts"] == 1
        and counts["basket_target_bound_facts"] == 1
        and counts["auto_trade_decisions"] == 1
        and counts["basket_target_execution_cycles"] == 1
        and counts["basket_target_order_intents"] == 0
        and counts["basket_target_capital_reservations"] == 1
        and counts["basket_target_order_events"] == 0
        and reserved == 0.0
        and will_order == 0
        and reconcile_required == 0
    )
    identity: dict[str, Any] = {
        "schema": SCHEMA,
        "state": "NO_ORDER_RECONCILED" if reconciled else "BLOCKED_FAIL_CLOSED",
        "decision": stable_decision,
        "counts": counts,
        "reserved_amount_usdc": reserved,
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

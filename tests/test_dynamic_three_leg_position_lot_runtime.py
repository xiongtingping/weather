from __future__ import annotations

import json
import sqlite3

from dynamic_three_leg_position_lot_runtime import (
    apply_reconciled_fill_to_position_lot,
    build_position_lot_identity,
    load_dynamic_three_leg_position_snapshot,
)
from dynamic_three_leg_position_lot_schema_contract import (
    apply_dynamic_three_leg_position_lot_schema,
)
from sequential_bayes_schema_contract import apply_forward_schema


NOW = "2026-07-14T08:00:00.000Z"
SLUG = "highest-temperature-in-chengdu-on-july-14-2026"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    apply_dynamic_three_leg_position_lot_schema(conn, writer_quiescent=True)
    return conn


def _intent(
    *, intent_id: str = "intent-buy", action: str = "BUY", lot_id: str = "",
    version: str = "1", limit_price: float = 0.40,
) -> dict:
    bucket, side, token = "37C", "YES", "token-37-yes"
    if not lot_id:
        lot_id, version = build_position_lot_identity(
            order_intent_id=intent_id, bucket=bucket, outcome_side=side, token_id=token
        )
    return {
        "order_intent_id": intent_id,
        "order_action": action,
        "position_lot_id": lot_id,
        "position_lot_version": version,
        "bucket": bucket,
        "outcome_side": side,
        "token_id": token,
        "limit_price": limit_price,
        "strategy_artifact_sha256": "artifact-1",
        "posterior_run_id": "post-1",
        "reason_family": "strategy_leg" if action == "BUY" else "target_realization",
        "reason_code": "top_yes_wave" if action == "BUY" else "target_realization_top_yes_invalidated",
        "intent_json": json.dumps({"slug": SLUG, "event_date": "2026-07-14"}),
    }


def test_partial_then_full_buy_is_cumulative_idempotent_and_freezes_only_take_profit_price() -> None:
    conn = _conn()
    intent = _intent()

    conn.execute("BEGIN")
    first = apply_reconciled_fill_to_position_lot(
        conn, intent=intent, cumulative_filled_size=4.0, fill_price=0.40, applied_at=NOW
    )
    conn.commit()
    assert first["position_lot_version"] == "1"

    conn.execute("BEGIN")
    second = apply_reconciled_fill_to_position_lot(
        conn, intent=intent, cumulative_filled_size=10.0, fill_price=0.40,
        applied_at="2026-07-14T08:00:01.000Z",
    )
    conn.commit()
    assert second["applied_size"] == 6.0
    row = conn.execute(
        """SELECT position_lot_version,acquired_size,remaining_size,remaining_cost_usdc,
                  frozen_take_profit_price,state FROM dynamic_three_leg_position_lots"""
    ).fetchone()
    assert row == (2, 10.0, 10.0, 4.0, 0.9, "open")

    conn.execute("BEGIN")
    replay = apply_reconciled_fill_to_position_lot(
        conn, intent=intent, cumulative_filled_size=10.0, fill_price=0.40,
        applied_at="2026-07-14T08:00:02.000Z",
    )
    conn.commit()
    assert replay["state"] == "already_applied"
    assert conn.execute("SELECT COUNT(*) FROM dynamic_three_leg_position_lot_events").fetchone()[0] == 2


def test_sell_fill_reduces_exact_lot_and_snapshot_reads_no_legacy_edge_trade() -> None:
    conn = _conn()
    buy = _intent()
    conn.execute("BEGIN")
    applied = apply_reconciled_fill_to_position_lot(
        conn, intent=buy, cumulative_filled_size=10.0, fill_price=0.40, applied_at=NOW
    )
    conn.commit()
    sell = _intent(
        intent_id="intent-sell",
        action="SELL",
        lot_id=applied["position_lot_id"],
        version=applied["position_lot_version"],
        limit_price=0.91,
    )
    conn.execute("BEGIN")
    reduced = apply_reconciled_fill_to_position_lot(
        conn, intent=sell, cumulative_filled_size=3.0, fill_price=0.91,
        applied_at="2026-07-14T08:01:00.000Z",
    )
    conn.commit()
    assert reduced["position_lot_version"] == "2"

    snapshot = load_dynamic_three_leg_position_snapshot(
        conn, slug=SLUG, event_date="2026-07-14", as_of="2026-07-14T08:01:00.100Z"
    )
    assert snapshot["ok"] is True
    assert snapshot["legacy_edge_trades_consumed"] is False
    assert snapshot["positions"][0]["position_size"] == 7.0
    assert snapshot["positions"][0]["entry_notional_usdc"] == 2.8
    assert snapshot["positions"][0]["strategy_leg"] == "top_yes_wave"


def test_lot_update_rolls_back_with_caller_transaction() -> None:
    conn = _conn()
    conn.execute("BEGIN")
    assert apply_reconciled_fill_to_position_lot(
        conn, intent=_intent(), cumulative_filled_size=5.0, fill_price=0.40, applied_at=NOW
    )["ok"] is True
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM dynamic_three_leg_position_lots").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM dynamic_three_leg_position_lot_events").fetchone()[0] == 0


def test_position_lot_rejects_accounting_price_drift_from_frozen_intent() -> None:
    conn = _conn()
    intent = _intent(limit_price=0.40)
    conn.execute("BEGIN")
    result = apply_reconciled_fill_to_position_lot(
        conn,
        intent=intent,
        cumulative_filled_size=2.0,
        fill_price=0.41,
        applied_at=NOW,
    )
    conn.rollback()
    assert result["state"] == "blocked"
    assert result["reason"] == "dynamic_position_lot_accounting_price_mismatch"

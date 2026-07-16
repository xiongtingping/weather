from __future__ import annotations

import sqlite3

from production_dynamic_three_leg_risk_reservation_writer import reserve_dynamic_three_leg_risk_budget
from dynamic_three_leg_production_activation import materialize_dynamic_three_leg_activation
from sequential_bayes_schema_contract import apply_forward_schema


def _plan(reservation="risk-1") -> dict:
    return {"trading_plan_id": "plan-1", "posterior_run_id": "post-1", "capital_snapshot_id": "capital-1", "risk_snapshot_id": "risk-snapshot-1", "capital_epoch_id": "epoch-1", "risk_reservation_id": reservation, "strategy_artifact_sha256": "artifact-1", "candidates": [{"notional": 20.0}, {"notional": 5.0}, {"notional": 5.0}]}


def _capital() -> dict:
    return {"capital_snapshot_id": "capital-1", "capital_epoch_id": "epoch-1", "available_cash_usdc": 30.0, "expires_at": "2026-07-13T00:00:03Z"}


def _risk() -> dict:
    return {"risk_snapshot_id": "risk-snapshot-1", "capital_epoch_id": "epoch-1", "new_buy_authorized": True, "maximum_new_buy_notional_usdc": 30.0, "expires_at": "2026-07-13T00:00:03Z"}


def _activation() -> dict:
    return materialize_dynamic_three_leg_activation({"schema": "dynamic_three_leg_production_activation_v1", "state": "enabled", "risk_reservation_write_authorized": True, "live_decision_write_authorized": True, "live_submit_authorized": True, "strategy_artifact_sha256": "artifact-1", "cutover_artifact_sha256": "cutover-1"})


def test_reservation_is_default_off_then_atomic_and_idempotent(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    blocked = reserve_dynamic_three_leg_risk_budget(conn, desired_trading_plan=_plan(), capital_snapshot=_capital(), risk_snapshot=_risk(), production_activation=_activation(), reservation_as_of="2026-07-13T00:00:00Z", expires_at="2026-07-13T00:00:03Z")
    assert blocked["ok"] is False
    assert conn.execute("SELECT COUNT(*) FROM dynamic_three_leg_risk_reservations").fetchone()[0] == 0

    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_PRODUCTION_ENABLED", "1")
    first = reserve_dynamic_three_leg_risk_budget(conn, desired_trading_plan=_plan(), capital_snapshot=_capital(), risk_snapshot=_risk(), production_activation=_activation(), reservation_as_of="2026-07-13T00:00:00Z", expires_at="2026-07-13T00:00:03Z")
    second = reserve_dynamic_three_leg_risk_budget(conn, desired_trading_plan=_plan(), capital_snapshot=_capital(), risk_snapshot=_risk(), production_activation=_activation(), reservation_as_of="2026-07-13T00:00:00Z", expires_at="2026-07-13T00:00:03Z")
    assert first["state"] == "active"
    assert second["idempotent"] is True
    assert conn.execute("SELECT reserved_buy_notional,state FROM dynamic_three_leg_risk_reservations").fetchone() == (30.0, "active")


def test_reservation_rejects_epoch_overcommit(monkeypatch) -> None:
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_PRODUCTION_ENABLED", "1")
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    assert reserve_dynamic_three_leg_risk_budget(conn, desired_trading_plan=_plan(), capital_snapshot=_capital(), risk_snapshot=_risk(), production_activation=_activation(), reservation_as_of="2026-07-13T00:00:00Z", expires_at="2026-07-13T00:00:03Z")["ok"] is True
    second_plan = _plan("risk-2")
    second_plan["trading_plan_id"] = "plan-2"
    result = reserve_dynamic_three_leg_risk_budget(conn, desired_trading_plan=second_plan, capital_snapshot=_capital(), risk_snapshot=_risk(), production_activation=_activation(), reservation_as_of="2026-07-13T00:00:00Z", expires_at="2026-07-13T00:00:03Z")
    assert result["block_reasons"] == ["dynamic_three_leg_risk_reservation_epoch_capacity_exhausted"]

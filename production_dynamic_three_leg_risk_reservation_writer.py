"""Atomic capital/risk reservation writer for dynamic-three-leg production.

The reservation is created before Transaction C and is the only durable proof
that a plan has budget.  It does not create decisions, intents, or orders.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from dynamic_three_leg_production_activation import validate_dynamic_three_leg_activation


SCHEMA_VERSION = "production_dynamic_three_leg_risk_reservation_writer_v1"
ACTIVATION_SCHEMA = "dynamic_three_leg_production_activation_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [row for row in value if isinstance(row, Mapping)]


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _time(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return result.astimezone(timezone.utc) if result.tzinfo is not None else None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _enabled(name: str) -> bool:
    return _text(os.environ.get(name)).lower() in {"1", "true", "yes", "on"}


def reserve_dynamic_three_leg_risk_budget(
    conn: sqlite3.Connection,
    *,
    desired_trading_plan: Mapping[str, Any],
    capital_snapshot: Mapping[str, Any],
    risk_snapshot: Mapping[str, Any],
    production_activation: Mapping[str, Any],
    reservation_as_of: str,
    expires_at: str,
) -> dict[str, Any]:
    """Reserve one plan's complete BUY budget without affecting live orders."""

    plan = _mapping(desired_trading_plan)
    capital = _mapping(capital_snapshot)
    risk = _mapping(risk_snapshot)
    try:
        activation = validate_dynamic_three_leg_activation(_mapping(production_activation))
    except ValueError:
        activation = {}
    blockers: list[str] = []
    now = _time(reservation_as_of)
    expiry = _time(expires_at)
    capital_expiry = _time(capital.get("expires_at"))
    risk_expiry = _time(risk.get("expires_at"))
    reservation_id = _text(plan.get("risk_reservation_id"))
    strategy_artifact = _text(plan.get("strategy_artifact_sha256"))
    available_cash = _finite(capital.get("available_cash_usdc"))
    risk_limit = _finite(risk.get("maximum_new_buy_notional_usdc"))
    required = sum(max(0.0, _finite(row.get("notional")) or 0.0) for row in _rows(plan.get("candidates")))

    if not _enabled("PM_DYNAMIC_THREE_LEG_PRODUCTION_ENABLED"):
        blockers.append("dynamic_three_leg_production_environment_disabled")
    if activation.get("schema") != ACTIVATION_SCHEMA or activation.get("state") != "enabled":
        blockers.append("dynamic_three_leg_risk_reservation_activation_invalid")
    if activation.get("risk_reservation_write_authorized") is not True:
        blockers.append("dynamic_three_leg_risk_reservation_write_not_authorized")
    if _text(activation.get("strategy_artifact_sha256")) != strategy_artifact:
        blockers.append("dynamic_three_leg_risk_reservation_strategy_artifact_mismatch")
    required_bindings = (
        reservation_id, _text(plan.get("trading_plan_id")), _text(plan.get("posterior_run_id")),
        _text(plan.get("capital_snapshot_id")), _text(plan.get("risk_snapshot_id")),
        _text(plan.get("capital_epoch_id")), strategy_artifact,
    )
    if not all(required_bindings):
        blockers.append("dynamic_three_leg_risk_reservation_plan_identity_missing")
    if _text(capital.get("capital_snapshot_id")) != required_bindings[3] or _text(capital.get("capital_epoch_id")) != required_bindings[5]:
        blockers.append("dynamic_three_leg_risk_reservation_capital_binding_mismatch")
    if _text(risk.get("risk_snapshot_id")) != required_bindings[4] or _text(risk.get("capital_epoch_id")) != required_bindings[5]:
        blockers.append("dynamic_three_leg_risk_reservation_risk_binding_mismatch")
    if required > 0.0 and risk.get("new_buy_authorized") is not True:
        blockers.append("dynamic_three_leg_risk_reservation_new_buy_not_authorized")
    if available_cash is None or (required > 0.0 and available_cash + 1e-9 < required):
        blockers.append("dynamic_three_leg_risk_reservation_cash_insufficient")
    if risk_limit is None or (required > 0.0 and risk_limit + 1e-9 < required):
        blockers.append("dynamic_three_leg_risk_reservation_limit_insufficient")
    if now is None or expiry is None or capital_expiry is None or risk_expiry is None:
        blockers.append("dynamic_three_leg_risk_reservation_time_invalid")
    elif expiry <= now or expiry > capital_expiry or expiry > risk_expiry:
        blockers.append("dynamic_three_leg_risk_reservation_expiry_outside_snapshot_window")
    if blockers:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": sorted(set(blockers)), "wrote_row": False, "places_orders": False}

    payload = {
        "schema": "dynamic_three_leg_risk_reservation_v1",
        "risk_reservation_id": reservation_id,
        "trading_plan_id": required_bindings[1],
        "posterior_run_id": required_bindings[2],
        "capital_snapshot_id": required_bindings[3],
        "risk_snapshot_id": required_bindings[4],
        "capital_epoch_id": required_bindings[5],
        "strategy_artifact_sha256": strategy_artifact,
        "available_cash_usdc": available_cash,
        "risk_limit_usdc": risk_limit,
        "reserved_buy_notional": required,
        "expires_at": _text(expires_at),
    }
    payload_sha = _sha(payload)
    existing = conn.execute(
        "SELECT reservation_sha256,state FROM dynamic_three_leg_risk_reservations WHERE risk_reservation_id=?",
        (reservation_id,),
    ).fetchone()
    if existing is not None:
        if _text(existing[0]) != payload_sha:
            return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": ["dynamic_three_leg_risk_reservation_replay_drift"], "wrote_row": False, "places_orders": False}
        return {"schema": SCHEMA_VERSION, "ok": True, "state": _text(existing[1]), "idempotent": True, "risk_reservation_id": reservation_id, "wrote_row": False, "places_orders": False}

    reserved_elsewhere = conn.execute(
        """SELECT COALESCE(SUM(reserved_buy_notional),0)
           FROM dynamic_three_leg_risk_reservations
           WHERE capital_epoch_id=? AND state IN ('active','bound') AND expires_at>?""",
        (required_bindings[5], _text(reservation_as_of)),
    ).fetchone()[0]
    if float(reserved_elsewhere or 0.0) + required > min(float(available_cash), float(risk_limit)) + 1e-9:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": ["dynamic_three_leg_risk_reservation_epoch_capacity_exhausted"], "wrote_row": False, "places_orders": False}

    conn.execute("SAVEPOINT dynamic_three_leg_risk_reservation_tx")
    try:
        conn.execute(
            """INSERT INTO dynamic_three_leg_risk_reservations(
                   risk_reservation_id,trading_plan_id,posterior_run_id,capital_snapshot_id,
                   risk_snapshot_id,capital_epoch_id,strategy_artifact_sha256,
                   available_cash_usdc,risk_limit_usdc,reserved_buy_notional,state,
                   bound_transition_plan_id,expires_at,reservation_json,reservation_sha256,
                   created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?, 'active',NULL,?,?,?,?,?)""",
            (*required_bindings, float(available_cash), float(risk_limit), float(required), _text(expires_at), _json(payload), payload_sha, _text(reservation_as_of), _text(reservation_as_of)),
        )
    except Exception:
        conn.execute("ROLLBACK TO dynamic_three_leg_risk_reservation_tx")
        conn.execute("RELEASE dynamic_three_leg_risk_reservation_tx")
        raise
    conn.execute("RELEASE dynamic_three_leg_risk_reservation_tx")
    return {"schema": SCHEMA_VERSION, "ok": True, "state": "active", "idempotent": False, "risk_reservation_id": reservation_id, "reserved_buy_notional": required, "wrote_row": True, "places_orders": False}


__all__ = ["ACTIVATION_SCHEMA", "SCHEMA_VERSION", "reserve_dynamic_three_leg_risk_budget"]

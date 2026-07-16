"""One local Transaction-C runner for the production dynamic three-leg lane.

The resident outbox worker owns the outer ``BEGIN IMMEDIATE``.  This runner is
therefore intentionally free of network/data acquisition: it first reserves
the already-frozen BUY budget, then writes the decision bundle.  A blocked
reservation returns normally so the worker can roll back the entire lease.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping

from production_dynamic_three_leg_decision_writer import (
    commit_production_dynamic_three_leg_decision_bundle,
)
from production_dynamic_three_leg_risk_reservation_writer import (
    reserve_dynamic_three_leg_risk_budget,
)


SCHEMA_VERSION = "production_dynamic_three_leg_cycle_runner_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def run_production_dynamic_three_leg_cycle(
    conn: sqlite3.Connection,
    *,
    outbox_event_id: str,
    lease_owner: str,
    claim_generation: int,
    slug: str,
    event_date: str,
    desired_trading_plan: Mapping[str, Any],
    transition_plan: Mapping[str, Any],
    market_snapshot: Mapping[str, Any],
    capital_snapshot: Mapping[str, Any],
    risk_snapshot: Mapping[str, Any],
    production_activation: Mapping[str, Any],
    decision_as_of: str,
) -> dict[str, Any]:
    """Reserve then commit under the worker's single outer transaction."""

    if not conn.in_transaction:
        raise RuntimeError("production_dynamic_three_leg_cycle_runner_outer_transaction_required")
    desired = _mapping(desired_trading_plan)
    transition = _mapping(transition_plan)
    reservation_plan = dict(desired)
    if _text(transition.get("phase")) != "BUY_NEW":
        reservation_plan["candidates"] = []
    reservation = reserve_dynamic_three_leg_risk_budget(
        conn,
        desired_trading_plan=reservation_plan,
        capital_snapshot=_mapping(capital_snapshot),
        risk_snapshot=_mapping(risk_snapshot),
        production_activation=_mapping(production_activation),
        reservation_as_of=_text(decision_as_of),
        # The decision writer independently verifies this bound against the
        # execution contract and frozen capital/risk windows.
        expires_at=_text(_mapping(capital_snapshot).get("expires_at")),
    )
    if reservation.get("ok") is not True:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": list(reservation.get("block_reasons") or ["dynamic_three_leg_risk_reservation_blocked"]),
            "reservation": reservation,
            "places_orders": False,
        }
    decision = commit_production_dynamic_three_leg_decision_bundle(
        conn,
        outbox_event_id=_text(outbox_event_id),
        lease_owner=_text(lease_owner),
        claim_generation=int(claim_generation),
        slug=_text(slug),
        event_date=_text(event_date),
        desired_trading_plan=desired,
        transition_plan=transition,
        market_snapshot=_mapping(market_snapshot),
        production_activation=_mapping(production_activation),
        decision_as_of=_text(decision_as_of),
    )
    if decision.get("ok") is not True:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": list(decision.get("block_reasons") or ["dynamic_three_leg_decision_bundle_blocked"]),
            "reservation": reservation,
            "decision": decision,
            "places_orders": False,
        }
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "committed",
        "reservation": reservation,
        "decision": decision,
        "places_orders": False,
    }


__all__ = ["SCHEMA_VERSION", "run_production_dynamic_three_leg_cycle"]

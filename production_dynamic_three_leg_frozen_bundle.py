"""Pure builder for the production outbox worker's frozen decision inputs.

It is the only allowed assembly point between an already-committed posterior
RealizationState and Transaction C.  All arguments are caller-captured
snapshots; this module performs no database/network I/O and cannot change the
posterior, market quote, capital state, or positions while composing.
"""

from __future__ import annotations

from typing import Any, Mapping

from dynamic_three_leg_trading_plan_runtime import build_dynamic_three_leg_trading_plan
from dynamic_three_leg_transition_runtime import build_dynamic_three_leg_transition_plan
from settlement_window_runtime import build_settlement_window_state
from second_yes_entry_window_runtime import build_second_yes_entry_window_state


SCHEMA_VERSION = "production_dynamic_three_leg_frozen_bundle_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def build_production_dynamic_three_leg_frozen_bundle(
    *,
    outbox: Mapping[str, Any],
    realization_state: Mapping[str, Any],
    market_snapshot: Mapping[str, Any],
    capital_snapshot: Mapping[str, Any],
    risk_snapshot: Mapping[str, Any],
    position_snapshot: Mapping[str, Any],
    settlement_window_policy: Mapping[str, Any],
    top_yes_wave_reentry_state: Mapping[str, Any],
    strategy_artifact_sha256: str,
    production_activation: Mapping[str, Any],
    decision_as_of: str,
) -> dict[str, Any]:
    """Create a fully bound, read-only bundle before outbox claim.

    ``capital_snapshot`` carries the preallocated ``risk_reservation_id`` and
    leg caps.  The durable reservation is then created inside the worker's
    outer transaction, not here.
    """

    event = _mapping(outbox)
    realization = _mapping(realization_state)
    market = _mapping(market_snapshot)
    capital = _mapping(capital_snapshot)
    risk = _mapping(risk_snapshot)
    positions = _mapping(position_snapshot)
    blockers: list[str] = []
    if not _text(event.get("slug")) or not _text(event.get("event_date")):
        blockers.append("production_frozen_bundle_outbox_event_identity_missing")
    if _text(realization.get("posterior_run_id")) != _text(event.get("posterior_run_id")):
        blockers.append("production_frozen_bundle_realization_posterior_mismatch")
    if _text(realization.get("fact_snapshot_id")) != _text(event.get("fact_snapshot_id")):
        blockers.append("production_frozen_bundle_realization_fact_mismatch")
    if _text(capital.get("risk_snapshot_id")) != _text(risk.get("risk_snapshot_id")):
        blockers.append("production_frozen_bundle_capital_risk_snapshot_mismatch")
    if _text(capital.get("capital_epoch_id")) != _text(risk.get("capital_epoch_id")):
        blockers.append("production_frozen_bundle_capital_risk_epoch_mismatch")
    required_capital = (
        "capital_snapshot_id", "risk_snapshot_id", "capital_epoch_id", "risk_reservation_id",
    )
    if any(not _text(capital.get(key)) for key in required_capital):
        blockers.append("production_frozen_bundle_capital_identity_missing")
    if not _text(strategy_artifact_sha256):
        blockers.append("production_frozen_bundle_strategy_artifact_missing")
    if not _text(decision_as_of):
        blockers.append("production_frozen_bundle_decision_time_missing")
    if blockers:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": sorted(set(blockers)), "places_orders": False}

    settlement_window = build_settlement_window_state(
        resolution_local_date=_text(event.get("event_date")),
        decision_as_of=_text(decision_as_of),
        policy=_mapping(settlement_window_policy),
    )
    if settlement_window.get("ok") is not True:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": list(settlement_window.get("block_reasons") or ["production_frozen_bundle_settlement_window_blocked"]),
            "settlement_window_state": settlement_window,
            "places_orders": False,
        }
    second_yes_entry_window = build_second_yes_entry_window_state(
        realization_state=realization,
        decision_as_of=_text(decision_as_of),
    )
    if second_yes_entry_window.get("ok") is not True:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": list(second_yes_entry_window.get("block_reasons") or ["production_frozen_bundle_second_yes_window_blocked"]),
            "second_yes_entry_window_state": second_yes_entry_window,
            "places_orders": False,
        }

    desired = build_dynamic_three_leg_trading_plan(
        realization_state=realization,
        settlement_window_state=settlement_window,
        second_yes_entry_window_state=second_yes_entry_window,
        top_yes_wave_reentry_state=top_yes_wave_reentry_state,
        market_snapshot=market,
        capital_plan=capital,
        strategy_artifact_sha256=_text(strategy_artifact_sha256),
    )
    if desired.get("ok") is not True:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": list(desired.get("block_reasons") or ["production_frozen_bundle_desired_plan_blocked"]), "desired_trading_plan": desired, "places_orders": False}
    transition = build_dynamic_three_leg_transition_plan(
        desired_trading_plan=desired,
        position_snapshot=positions,
        decision_as_of=_text(decision_as_of),
    )
    if transition.get("ok") is not True:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": list(transition.get("block_reasons") or ["production_frozen_bundle_transition_blocked"]), "desired_trading_plan": desired, "transition_plan": transition, "places_orders": False}
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "available",
        "slug": _text(event.get("slug")), "event_date": _text(event.get("event_date")),
        "market_snapshot": market, "capital_snapshot": capital, "risk_snapshot": risk,
        "settlement_window_state": settlement_window,
        "second_yes_entry_window_state": second_yes_entry_window,
        "desired_trading_plan": desired, "transition_plan": transition,
        "production_activation": _mapping(production_activation),
        "boundary": {
            "frozen_before_outbox_claim": True,
            "realization_state_only_weather_input": True,
            "settlement_window_is_independent_frozen_execution_input": True,
            "second_yes_entry_window_is_frozen_execution_input": True,
            "database_or_network_io": False,
            "reservation_created_only_inside_transaction_c": True,
            "places_orders": False,
        },
    }


__all__ = ["SCHEMA_VERSION", "build_production_dynamic_three_leg_frozen_bundle"]

"""Pure sell-first transition planner for the dynamic three-leg strategy.

The current posterior plan defines desired exposure.  This module reconciles
that desired exposure with one frozen position snapshot.  It never reads the
ledger, refreshes a quote, writes an intent, or submits an order.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "dynamic_three_leg_transition_plan_v1"
MANAGEMENT_SCOPE = "dynamic_three_leg_weather"
SELL_REASON_FAMILY = "target_realization"
TARGET_REALIZATION_REASON_BY_LEG = {
    "top_yes_wave": "target_realization_top_yes_invalidated",
    "second_yes": "target_realization_second_yes_invalidated",
    "strongly_excluded_no": "target_realization_hard_zero_no_invalidated",
}
LEG_SIDE = {
    "top_yes_wave": "YES",
    "second_yes": "YES",
    "strongly_excluded_no": "NO",
}
LEG_NOTIONAL_CAP_USDC = {
    "top_yes_wave": 20.0,
    "second_yes": 4.99,
    "strongly_excluded_no": 100.0,
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _identity(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_json(value).encode("utf-8")).hexdigest()[:24]


def _rows(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [row for row in value if isinstance(row, Mapping)]


def _parse_utc(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def build_dynamic_three_leg_transition_plan(
    *,
    desired_trading_plan: Mapping[str, Any],
    position_snapshot: Mapping[str, Any],
    decision_as_of: str,
    max_position_snapshot_age_ms: int = 1_000,
) -> dict[str, Any]:
    """Return exactly one phase: SELL_OLD, BUY_NEW, HOLD, or BLOCKED.

    Any required sell suppresses every buy.  A later, newly frozen position
    snapshot and latest posterior must be used after fill reconciliation.
    """

    plan = _mapping(desired_trading_plan)
    positions = _mapping(position_snapshot)
    blockers: list[str] = []
    plan_id = _text(plan.get("trading_plan_id"))
    posterior_run_id = _text(plan.get("posterior_run_id"))
    realization_state_id = _text(plan.get("realization_state_id"))
    strategy_artifact_sha256 = _text(plan.get("strategy_artifact_sha256"))
    market_snapshot_id = _text(plan.get("market_snapshot_id"))
    capital_snapshot_id = _text(plan.get("capital_snapshot_id"))
    risk_snapshot_id = _text(plan.get("risk_snapshot_id"))
    capital_epoch_id = _text(plan.get("capital_epoch_id"))
    risk_reservation_id = _text(plan.get("risk_reservation_id"))
    second_yes_entry_window_state_id = _text(plan.get("second_yes_entry_window_state_id"))
    position_snapshot_id = _text(positions.get("position_snapshot_id"))
    position_snapshot_as_of = _text(positions.get("as_of"))
    if plan.get("ok") is not True or not all((plan_id, posterior_run_id, realization_state_id, strategy_artifact_sha256, market_snapshot_id, capital_snapshot_id, risk_snapshot_id, capital_epoch_id, risk_reservation_id, second_yes_entry_window_state_id)):
        blockers.append("dynamic_transition_desired_plan_invalid")
    if not position_snapshot_id or not position_snapshot_as_of:
        blockers.append("dynamic_transition_position_snapshot_identity_missing")
    if positions.get("complete") is not True:
        blockers.append("dynamic_transition_position_snapshot_incomplete")
    position_time = _parse_utc(position_snapshot_as_of)
    decision_time = _parse_utc(decision_as_of)
    age_ms = (
        (decision_time - position_time).total_seconds() * 1000.0
        if position_time is not None and decision_time is not None
        else None
    )
    if position_time is None or decision_time is None:
        blockers.append("dynamic_transition_position_or_decision_time_invalid")
    elif age_ms < 0:
        blockers.append("dynamic_transition_position_snapshot_from_future")
    elif age_ms > max(0, int(max_position_snapshot_age_ms)):
        blockers.append("dynamic_transition_position_snapshot_stale")
    unresolved = _rows(positions.get("unresolved_intents"))
    if unresolved:
        blockers.append("dynamic_transition_unresolved_intent_requires_reconcile")

    desired: dict[tuple[str, str, str], dict[str, Any]] = {}
    desired_rows = [
        *(dict(row, entry_allowed=True) for row in _rows(plan.get("candidates"))),
        *(dict(row, entry_allowed=False) for row in _rows(plan.get("retention_candidates"))),
    ]
    for row in desired_rows:
        bucket = _text(row.get("bucket"))
        side = _text(row.get("side")).upper()
        strategy_leg = _text(row.get("strategy_leg"))
        target = _finite(row.get("notional"))
        if not bucket or side not in {"YES", "NO"} or strategy_leg not in TARGET_REALIZATION_REASON_BY_LEG or target is None or target <= 0:
            blockers.append("dynamic_transition_desired_candidate_invalid")
            continue
        if side != LEG_SIDE[strategy_leg]:
            blockers.append("dynamic_transition_desired_candidate_side_role_mismatch")
            continue
        if target > LEG_NOTIONAL_CAP_USDC[strategy_leg] + 1e-9:
            blockers.append("dynamic_transition_desired_candidate_notional_cap_exceeded")
            continue
        key = (strategy_leg, bucket, side)
        if key in desired:
            blockers.append("dynamic_transition_desired_candidate_duplicate")
            continue
        desired[key] = {**row, "target_notional_usdc": target}

    managed: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    snapshot_token_balances: dict[str, float] = {}
    for raw in _rows(positions.get("positions")):
        raw_token_id = _text(raw.get("token_id"))
        raw_position_size = _finite(raw.get("position_size"))
        if raw_token_id and raw_position_size is not None and raw_position_size >= 0:
            snapshot_token_balances[raw_token_id] = snapshot_token_balances.get(raw_token_id, 0.0) + raw_position_size
        if _text(raw.get("management_scope")) != MANAGEMENT_SCOPE:
            continue
        row = dict(raw)
        bucket = _text(row.get("bucket") or row.get("outcome"))
        side = _text(row.get("side")).upper()
        lot_id = _text(row.get("position_lot_id"))
        lot_version = _text(row.get("position_lot_version"))
        size = _finite(row.get("position_size"))
        notional = _finite(row.get("entry_notional_usdc"))
        average_cost = _finite(row.get("average_cost") or row.get("entry_price"))
        token_id = _text(row.get("token_id"))
        strategy_leg = _text(row.get("strategy_leg"))
        if not bucket or side not in {"YES", "NO"} or not lot_id or not lot_version or not token_id or strategy_leg not in TARGET_REALIZATION_REASON_BY_LEG:
            blockers.append("dynamic_transition_managed_position_identity_invalid")
            continue
        if size is None or size <= 0 or notional is None or notional <= 0 or average_cost is None or average_cost <= 0:
            blockers.append("dynamic_transition_managed_position_cost_basis_invalid")
            continue
        key = (strategy_leg, bucket, side)
        managed.setdefault(key, []).append({
            **row,
            "bucket": bucket,
            "side": side,
            "position_size": size,
            "entry_notional_usdc": notional,
            "average_cost": average_cost,
            "token_id": token_id,
            "strategy_leg": strategy_leg,
        })

    if blockers:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "phase": "BLOCKED",
            "block_reasons": sorted(set(blockers)),
            "posterior_run_id": posterior_run_id,
            "position_snapshot_id": position_snapshot_id,
            "sell_intents": [],
            "buy_candidates": [],
            "read_only": True,
            "places_orders": False,
        }

    sell_intents: list[dict[str, Any]] = []
    buy_candidates: list[dict[str, Any]] = []
    allocation_epsilon = 0.01

    # Current role comes from the latest desired plan; the lot's strategy_leg
    # remains immutable entry provenance.  A YES lot follows its bucket in
    # both directions: top->second is trimmed to the smaller budget, while
    # second->top is retained and may be topped up.  Requiring a sell/re-entry
    # merely to change role would freeze the old posterior and add avoidable
    # latency, spread and exchange risk.
    allocated_lots: dict[tuple[str, str, str], list[dict[str, Any]]] = {
        key: [] for key in desired
    }
    unassigned_lots: list[dict[str, Any]] = []
    for source_key, lots in managed.items():
        allocation_key: tuple[str, str, str] | None = None
        if source_key in desired:
            allocation_key = source_key
        elif source_key[0] in {"top_yes_wave", "second_yes"} and source_key[2] == "YES":
            dynamic_role_keys = [
                key
                for key in desired
                if key[1] == source_key[1]
                and key[2] == "YES"
                and key[0] in {"top_yes_wave", "second_yes"}
            ]
            if len(dynamic_role_keys) == 1:
                allocation_key = dynamic_role_keys[0]
        if allocation_key is None:
            unassigned_lots.extend(lots)
        else:
            allocated_lots.setdefault(allocation_key, []).extend(lots)

    def append_excess_sells(
        lots: list[dict[str, Any]], *, excess_notional: float, target_notional: float,
    ) -> None:
        remaining_excess = excess_notional
        # Newest deterministic lot first keeps older cost-basis lots stable.
        for position in sorted(
            lots,
            key=lambda item: (_text(item.get("opened_at")), _text(item.get("position_lot_id"))),
            reverse=True,
        ):
            if remaining_excess <= allocation_epsilon:
                break
            lot_notional = float(position["entry_notional_usdc"])
            sell_notional = min(lot_notional, remaining_excess)
            requested_size = min(
                float(position["position_size"]), sell_notional / float(position["average_cost"])
            )
            entry_leg = _text(position.get("strategy_leg"))
            identity = {
                "posterior_run_id": posterior_run_id,
                "realization_state_id": realization_state_id,
                "position_snapshot_id": position_snapshot_id,
                "position_lot_id": position["position_lot_id"],
                "position_lot_version": position["position_lot_version"],
                "token_id": position["token_id"],
                "strategy_leg": entry_leg,
                "bucket": position["bucket"],
                "side": position["side"],
                "requested_sell_size": requested_size,
                "reason_family": SELL_REASON_FAMILY,
                "reason_code": TARGET_REALIZATION_REASON_BY_LEG[entry_leg],
                "strategy_artifact_sha256": strategy_artifact_sha256,
            }
            sell_intents.append(
                {
                    "logical_exit_intent_id": _identity("exitintent_", identity),
                    **identity,
                    "action": "SELL",
                    "full_exit": sell_notional + allocation_epsilon >= lot_notional,
                    "target_notional_after_fill_usdc": target_notional,
                }
            )
            remaining_excess -= sell_notional

    if unassigned_lots:
        append_excess_sells(
            unassigned_lots,
            excess_notional=sum(float(position["entry_notional_usdc"]) for position in unassigned_lots),
            target_notional=0.0,
        )
    for allocation_key, lots in sorted(allocated_lots.items()):
        target = float(_mapping(desired[allocation_key])["target_notional_usdc"])
        current = sum(float(position["entry_notional_usdc"]) for position in lots)
        excess = current - target
        if excess > allocation_epsilon:
            append_excess_sells(lots, excess_notional=excess, target_notional=target)

    # Intents are executed in this exact order.  Each expected balance is the
    # balance immediately before its own sell, not the original balance copied
    # to every sibling intent.
    running_token_balances = dict(snapshot_token_balances)
    for execution_sequence, row in enumerate(sell_intents, start=1):
        token_id = _text(row.get("token_id"))
        row["execution_sequence"] = execution_sequence
        row["expected_token_balance"] = running_token_balances.get(token_id, 0.0)
        running_token_balances[token_id] = max(
            0.0,
            running_token_balances.get(token_id, 0.0) - float(row["requested_sell_size"]),
        )

    if not sell_intents:
        for key, candidate in desired.items():
            current = sum(
                float(position["entry_notional_usdc"])
                for position in allocated_lots.get(key, [])
            )
            remaining = float(candidate["target_notional_usdc"]) - current
            if remaining <= allocation_epsilon:
                continue
            if candidate.get("entry_allowed") is not True:
                continue
            buy_candidates.append(
                {
                    **candidate,
                    "current_notional_usdc": current,
                    "notional": remaining,
                    "target_notional_usdc": float(candidate["target_notional_usdc"]),
                    "position_snapshot_id": position_snapshot_id,
                    "expected_token_balance": snapshot_token_balances.get(_text(candidate.get("token_id")), 0.0),
                }
            )

    phase = "SELL_OLD" if sell_intents else "BUY_NEW" if buy_candidates else "HOLD"
    transition_identity = {
        "trading_plan_id": plan_id,
        "posterior_run_id": posterior_run_id,
        "realization_state_id": realization_state_id,
        "strategy_artifact_sha256": strategy_artifact_sha256,
        "market_snapshot_id": market_snapshot_id,
        "capital_snapshot_id": capital_snapshot_id,
        "risk_snapshot_id": risk_snapshot_id,
        "capital_epoch_id": capital_epoch_id,
        "risk_reservation_id": risk_reservation_id,
        "second_yes_entry_window_state_id": second_yes_entry_window_state_id,
        "position_snapshot_id": position_snapshot_id,
        "sell_ids": [row["logical_exit_intent_id"] for row in sell_intents],
        "buy_candidate_ids": [_text(row.get("candidate_id")) for row in buy_candidates],
    }
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "available",
        "phase": phase,
        "transition_plan_id": _identity("transition_", transition_identity),
        **transition_identity,
        "position_snapshot_as_of": position_snapshot_as_of,
        "position_snapshot_age_ms": round(age_ms, 3) if age_ms is not None else None,
        "max_position_snapshot_age_ms": max(0, int(max_position_snapshot_age_ms)),
        "sell_intents": sell_intents,
        "buy_candidates": buy_candidates,
        "buy_suppressed_until_sell_reconciled": bool(sell_intents),
        "requires_latest_posterior_refresh_after_sell": bool(sell_intents),
        "requires_new_position_snapshot_after_sell": bool(sell_intents),
        "boundary": {
            "sell_first": True,
            "current_role_from_latest_posterior_plan": True,
            "entry_role_is_immutable_fill_provenance": True,
            "retention_candidate_never_generates_buy": True,
            "closed_second_yes_window_does_not_force_same_bucket_sell": True,
            "top_to_same_bucket_second_carry_allowed": True,
            "second_to_top_authority_inheritance_forbidden": True,
            "unmanaged_positions_ignored": True,
            "ev_edge_lcb_not_consumed": True,
            "market_alpha_not_created": True,
            "read_only": True,
            "places_orders": False,
        },
        "read_only": True,
        "places_orders": False,
    }


__all__ = [
    "MANAGEMENT_SCOPE",
    "SCHEMA_VERSION",
    "SELL_REASON_FAMILY",
    "TARGET_REALIZATION_REASON_BY_LEG",
    "build_dynamic_three_leg_transition_plan",
]

"""Deterministic replay of the materialized target chain's time boundary.

This module is deliberately a pure, no-live coordinator.  It does not
retrieve weather, market, inventory, order state, or gateway data.  Callers
must provide already materialized, content-addressed P0/P1 and official WS
evidence; the replay verifies their as-of relationship, advances cash phase
monotonically, and computes the same discrete sizing boundary used by the
target lane.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Mapping, Sequence

from .cash_phase import (
    BasketCashPhaseInput,
    BasketCashPhasePolicyArtifact,
    CashPhase,
    OrderSizingDecision,
    evaluate_cash_phase,
)
from .contracts import ContractError, content_sha256


SCHEMA = "bayes_basket_target_time_replay_v1"
STAGES = frozenset({"P0", "P0-M", "P1"})


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{name}_missing")
    return result


def _timestamp(name: str, value: Any) -> datetime:
    result = _text(name, value)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    return parsed


def _sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"{name}_invalid_sha256")
    return result


def _number(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{name}_invalid_number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid_number") from exc
    if not math.isfinite(result) or result < 0.0:
        raise ContractError(f"{name}_invalid_number")
    return result


def _phase(name: str, value: Any) -> CashPhase:
    try:
        return value if isinstance(value, CashPhase) else CashPhase(_text(name, value))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid") from exc


def _normalize_event(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ContractError("time_replay_event_mapping_required")
    stage = _text("probability_stage", raw.get("probability_stage"))
    if stage not in STAGES:
        raise ContractError("time_replay_probability_stage_invalid")
    event = {
        "event_id": _text("event_id", raw.get("event_id")),
        "station_id": _text("station_id", raw.get("station_id")),
        "sequence": int(raw.get("sequence", -1)),
        "observed_at_utc": _text("observed_at_utc", raw.get("observed_at_utc")),
        "event_date_local": _text("event_date_local", raw.get("event_date_local")),
        "checkpoint_id": _text("checkpoint_id", raw.get("checkpoint_id")),
        "probability_stage": stage,
        "posterior_version": _text("posterior_version", raw.get("posterior_version")),
        "posterior_sha256": _sha("posterior_sha256", raw.get("posterior_sha256")),
        "snapshot_sha256": _sha("snapshot_sha256", raw.get("snapshot_sha256")),
        "quote_source": _text("quote_source", raw.get("quote_source")),
        "quote_version": _text("quote_version", raw.get("quote_version")),
        "inventory_version": _text("inventory_version", raw.get("inventory_version")),
        "order_state_version": _text("order_state_version", raw.get("order_state_version")),
        "source_observed_at_utc": _text("source_observed_at_utc", raw.get("source_observed_at_utc")),
        "current_phase": _phase("current_phase", raw.get("current_phase")),
        "bucket": _text("bucket", raw.get("bucket")),
        "delta_shares": _number("delta_shares", raw.get("delta_shares")),
        "r_limit": _number("r_limit", raw.get("r_limit")),
        "remaining_budget_usdc": _number("remaining_budget_usdc", raw.get("remaining_budget_usdc")),
        "executable_depth_cash": _number("executable_depth_cash", raw.get("executable_depth_cash")),
        "candidate_present": raw.get("candidate_present"),
        "target_clip_notional_usdc": raw.get("target_clip_notional_usdc"),
        "minimum_clip_notional_usdc": _number("minimum_clip_notional_usdc", raw.get("minimum_clip_notional_usdc", 0.0)),
        "share_quantum": _number("share_quantum", raw.get("share_quantum", 0.01)),
    }
    if event["sequence"] < 0:
        raise ContractError("time_replay_sequence_invalid")
    if event["quote_source"] != "OFFICIAL_CLOB_WS":
        raise ContractError("time_replay_official_ws_required")
    if not isinstance(event["candidate_present"], bool):
        raise ContractError("time_replay_candidate_present_invalid")
    observed = _timestamp("observed_at_utc", event["observed_at_utc"])
    source_observed = _timestamp("source_observed_at_utc", event["source_observed_at_utc"])
    if source_observed > observed:
        raise ContractError("time_replay_future_source_leak")
    event["observed_at"] = observed
    event["source_observed_at"] = source_observed
    event["content_sha256"] = content_sha256({
        "schema": "bayes_basket_target_time_replay_event_v1",
        **{key: value.value if isinstance(value, CashPhase) else value for key, value in event.items() if key not in {"observed_at", "source_observed_at", "content_sha256"}},
    })
    return event


def replay_materialized_target_chain(
    events: Sequence[Mapping[str, Any]],
    *,
    policy: BasketCashPhasePolicyArtifact | Mapping[str, Any],
    station_id: str,
    initial_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Replay an ordered materialized chain and return a resumable receipt.

    ``initial_state`` is the immutable continuation from a previous prefix,
    allowing an incremental prefix+suffix replay to be compared with a full
    replay from the same initial state.
    """

    if not isinstance(events, Sequence) or isinstance(events, (str, bytes, bytearray)):
        raise ContractError("time_replay_events_list_required")
    if not events and initial_state is None:
        raise ContractError("time_replay_events_empty")
    artifact = policy if isinstance(policy, BasketCashPhasePolicyArtifact) else BasketCashPhasePolicyArtifact.from_mapping(policy)
    station = _text("station_id", station_id)
    prior = dict(initial_state or {})
    current_phase = _phase("initial_current_phase", prior.get("current_phase", CashPhase.HEAVY))
    remaining_budget = _number("initial_remaining_budget_usdc", prior.get("remaining_budget_usdc", 20.0))
    last_sequence = int(prior.get("last_sequence", -1))
    last_observed_at = _timestamp("initial_last_observed_at_utc", prior["last_observed_at_utc"]) if prior.get("last_observed_at_utc") else None
    seen_ids = {str(key): str(value) for key, value in (prior.get("seen_event_ids") or {}).items()}
    seen_digests = set(str(value) for value in (prior.get("seen_event_digests") or []))
    rows: list[dict[str, Any]] = []
    duplicates: list[str] = []

    for raw in events:
        event = _normalize_event(raw)
        event_id = event["event_id"]
        digest = event["content_sha256"]
        known_id = seen_ids.get(event_id)
        if known_id is not None and known_id != digest:
            raise ContractError("time_replay_event_id_content_conflict")
        if digest in seen_digests:
            duplicates.append(event_id)
            continue
        if event["sequence"] <= last_sequence:
            raise ContractError("time_replay_sequence_not_strictly_increasing")
        if last_observed_at is not None and event["observed_at"] < last_observed_at:
            raise ContractError("time_replay_timestamp_moved_backwards")
        if event["station_id"] != station:
            raise ContractError("time_replay_station_scope_mismatch")
        if event["current_phase"] != current_phase:
            raise ContractError("time_replay_phase_state_mismatch")
        if abs(event["remaining_budget_usdc"] - remaining_budget) > 1e-9:
            raise ContractError("time_replay_budget_state_mismatch")
        phase_input = BasketCashPhaseInput.from_mapping({
            "policy_version": artifact.policy_version,
            "station_id": station,
            "current_phase": current_phase.value,
            "event_date_local": event["event_date_local"],
            "observed_at_utc": event["observed_at_utc"],
            "station_timezone": artifact.station_timezone,
            "checkpoint_id": event["checkpoint_id"],
        })
        phase = evaluate_cash_phase(artifact, phase_input)
        sizing = OrderSizingDecision.compute(
            bucket=event["bucket"],
            phase_decision=phase,
            delta_shares=event["delta_shares"],
            r_limit=event["r_limit"],
            remaining_budget=remaining_budget,
            executable_depth_cash=event["executable_depth_cash"],
            candidate_present=event["candidate_present"],
            target_clip_notional_usdc=event["target_clip_notional_usdc"],
            minimum_clip_notional_usdc=event["minimum_clip_notional_usdc"],
            share_quantum=event["share_quantum"],
        )
        row = {
            "event_id": event_id,
            "sequence": event["sequence"],
            "observed_at_utc": event["observed_at_utc"],
            "probability_stage": event["probability_stage"],
            "posterior_version": event["posterior_version"],
            "posterior_sha256": event["posterior_sha256"],
            "snapshot_sha256": event["snapshot_sha256"],
            "quote_source": event["quote_source"],
            "quote_version": event["quote_version"],
            "inventory_version": event["inventory_version"],
            "order_state_version": event["order_state_version"],
            "phase_decision": phase.to_dict(),
            "sizing_decision": sizing.to_dict(),
            "event_content_sha256": digest,
        }
        row["row_sha256"] = content_sha256({"schema": "bayes_basket_target_time_replay_row_v1", **row})
        rows.append(row)
        current_phase = phase.next_phase
        remaining_budget = sizing.remaining_budget_after
        last_sequence = event["sequence"]
        last_observed_at = event["observed_at"]
        seen_ids[event_id] = digest
        seen_digests.add(digest)

    continuation = {
        "current_phase": current_phase.value,
        "remaining_budget_usdc": remaining_budget,
        "last_sequence": last_sequence,
        "last_observed_at_utc": last_observed_at.isoformat().replace("+00:00", "Z") if last_observed_at else None,
        "seen_event_ids": dict(sorted(seen_ids.items())),
        "seen_event_digests": sorted(seen_digests),
    }
    core = {
        "schema": SCHEMA,
        "status": "replayed_materialized_target_chain",
        "station_id": station,
        "policy_sha256": artifact.content_sha256,
        "input_event_count": len(events),
        "unique_event_count": len(rows),
        "duplicate_event_count": len(duplicates),
        "duplicate_event_ids": duplicates,
        "rows": rows,
        "continuation": continuation,
        "boundary": {
            "target_only": True,
            "places_orders": False,
            "orders_submitted": 0,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "network_access": False,
        },
    }
    return {**core, "replay_sha256": content_sha256(core)}


__all__ = ["SCHEMA", "replay_materialized_target_chain"]

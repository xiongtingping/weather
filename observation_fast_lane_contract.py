"""Target 3-second observation-to-order fast-lane contract (shadow only)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping


SCHEMA_VERSION = "observation_fast_lane_contract_v1"
TOTAL_SLO_MS = 3000
# Three seconds is both the objective and the last permissible execution
# instant.  A second, longer grace period would authorize objectively late
# orders and make the SLO non-binding.
HARD_EXECUTION_DEADLINE_MS = TOTAL_SLO_MS
SEGMENT_BUDGET_MS = {
    "canonicalization": 100,
    # L0 requires T1→T3 P99<=500ms. Canonicalization consumes up to 100ms
    # of that interval, leaving no more than 400ms for Transaction B itself.
    "strong_inference_and_transaction_b": 400,
    "decision_and_durable_intent": 500,
    "sign_and_submit": 2000,
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _parse_time(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _elapsed_ms(end: datetime | None, start: datetime | None) -> float | None:
    return (end - start).total_seconds() * 1000.0 if end is not None and start is not None else None


def validate_observation_fast_lane_topology(
    topology: Mapping[str, Any] | None,
    *,
    require_execution_readiness: bool = True,
) -> dict[str, Any]:
    """Reject paths that trade correctness for speed or contain known blockers."""

    value = topology if isinstance(topology, Mapping) else {}
    blockers: list[str] = []
    if int(value.get("release_delay_ms") or 0) != 0:
        blockers.append("fast_lane_release_delay_must_be_zero")
    if value.get("synchronous_forecast_refresh_on_hot_path") is not False:
        blockers.append("fast_lane_synchronous_forecast_refresh_forbidden")
    if value.get("generic_task_queue_on_hot_path") is not False:
        blockers.append("fast_lane_generic_task_queue_forbidden")
    if value.get("resident_canonical_and_inference_worker") is not True:
        blockers.append("fast_lane_resident_worker_missing")
    if value.get("transaction_b_atomic_before_intent") is not True:
        blockers.append("fast_lane_transaction_b_atomicity_missing")
    if value.get("posterior_commit_outbox_durable_before_intent") is not True:
        blockers.append("fast_lane_durable_outbox_before_intent_missing")
    if value.get("decision_reads_committed_ssot") is not True:
        blockers.append("fast_lane_decision_not_bound_to_committed_ssot")
    if require_execution_readiness and value.get("signer_connection_pool_resident") is not True:
        blockers.append("fast_lane_resident_signer_or_connection_pool_missing")
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "ready" if not blockers else "blocked",
        "blockers": sorted(set(blockers)),
        "read_only": True,
        "require_execution_readiness": bool(require_execution_readiness),
    }


def build_observation_fast_lane_contract(
    *,
    correlation_id: str,
    timestamps: Mapping[str, Any],
    topology: Mapping[str, Any],
    mode: str = "shadow",
) -> dict[str, Any]:
    """Audit one observation-to-order attempt against the immutable budgets."""

    values = timestamps if isinstance(timestamps, Mapping) else {}
    correlation = _text(correlation_id)
    parsed = {
        key: _parse_time(values.get(key))
        for key in (
            "first_actionable_seen_at",
            "canonicalized_at",
            "transaction_b_committed_at",
            "decision_intent_durable_at",
            "order_submitted_at",
        )
    }
    blockers: list[str] = []
    if not correlation:
        blockers.append("fast_lane_correlation_id_missing")
    if parsed["first_actionable_seen_at"] is None:
        blockers.append("fast_lane_first_actionable_seen_at_missing")
    if parsed["canonicalized_at"] is None:
        blockers.append("fast_lane_canonicalized_at_missing")
    if parsed["transaction_b_committed_at"] is None:
        blockers.append("fast_lane_transaction_b_committed_at_missing")
    if parsed["decision_intent_durable_at"] is None:
        blockers.append("fast_lane_decision_intent_durable_at_missing")
    if _text(mode) == "live" and parsed["order_submitted_at"] is None:
        blockers.append("fast_lane_order_submitted_at_missing")
    ordered_keys = [
        "first_actionable_seen_at",
        "canonicalized_at",
        "transaction_b_committed_at",
        "decision_intent_durable_at",
    ]
    if _text(mode) == "live":
        ordered_keys.append("order_submitted_at")
    for previous, current in zip(ordered_keys, ordered_keys[1:]):
        if parsed[previous] is not None and parsed[current] is not None and parsed[current] < parsed[previous]:
            blockers.append(f"fast_lane_time_order_invalid:{previous}>{current}")
    segment_ms = {
        "canonicalization": _elapsed_ms(parsed["canonicalized_at"], parsed["first_actionable_seen_at"]),
        "strong_inference_and_transaction_b": _elapsed_ms(parsed["transaction_b_committed_at"], parsed["canonicalized_at"]),
        "decision_and_durable_intent": _elapsed_ms(parsed["decision_intent_durable_at"], parsed["transaction_b_committed_at"]),
        "sign_and_submit": _elapsed_ms(parsed["order_submitted_at"], parsed["decision_intent_durable_at"]),
    }
    for segment, budget in SEGMENT_BUDGET_MS.items():
        elapsed = segment_ms[segment]
        if _text(mode) != "live" and segment == "sign_and_submit":
            continue
        if elapsed is None:
            blockers.append(f"fast_lane_segment_timestamp_missing:{segment}")
        elif elapsed > budget:
            blockers.append(f"fast_lane_segment_budget_exceeded:{segment}")
    terminal = parsed["order_submitted_at"] if _text(mode) == "live" else parsed["decision_intent_durable_at"]
    total_elapsed = _elapsed_ms(terminal, parsed["first_actionable_seen_at"])
    if total_elapsed is None:
        blockers.append("fast_lane_total_elapsed_missing")
    elif total_elapsed > TOTAL_SLO_MS:
        blockers.append("fast_lane_total_slo_exceeded")
    topology_result = validate_observation_fast_lane_topology(
        topology,
        require_execution_readiness=_text(mode) == "live",
    )
    blockers.extend(topology_result["blockers"])
    return {
        "schema": SCHEMA_VERSION,
        "mode": _text(mode) or "shadow",
        "correlation_id": correlation,
        "ok": not blockers,
        "state": "within_slo" if not blockers else "blocked",
        "blockers": sorted(set(blockers)),
        "timestamps": {key: _text(values.get(key)) for key in parsed},
        "segment_elapsed_ms": segment_ms,
        "segment_budget_ms": dict(SEGMENT_BUDGET_MS),
        "total_elapsed_ms": total_elapsed,
        "total_slo_ms": TOTAL_SLO_MS,
        "topology": topology_result,
        "boundary": {
            "transaction_b_not_bypassed": True,
            "outbox_not_bypassed": True,
            "generic_queue_not_allowed_on_hot_path": True,
            "sync_forecast_refresh_not_allowed_on_hot_path": True,
            "release_delay_not_allowed_on_hot_path": True,
        },
        "read_only": True,
    }


__all__ = [
    "SCHEMA_VERSION",
    "HARD_EXECUTION_DEADLINE_MS",
    "SEGMENT_BUDGET_MS",
    "TOTAL_SLO_MS",
    "build_observation_fast_lane_contract",
    "validate_observation_fast_lane_topology",
]

"""Shadow-only canonical-observation to durable-decision fast-lane wiring.

The orchestrator owns no posterior mathematics and no trading policy.  It
only composes the unique P8 public-SSOT candidate path with the isolated P9
outbox consumer, then records the immutable P10 latency packet.  It cannot
call a signer, CLOB client, or a production archive database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
from typing import Any, Callable, Mapping

from observation_fast_lane_contract import (
    HARD_EXECUTION_DEADLINE_MS,
    build_observation_fast_lane_contract,
    validate_observation_fast_lane_topology,
)
from observation_fast_lane_shadow_telemetry_writer import (
    append_observation_fast_lane_shadow_latency_packet,
)
from resident_posterior_commit_outbox_shadow_worker import (
    ResidentPosteriorCommitOutboxShadowWorker,
)
from sequential_bayes_shadow_pipeline import run_canonical_event_public_ssot_shadow_update


SCHEMA_VERSION = "observation_fast_lane_shadow_orchestrator_v1"
HARD_T1_TO_DISPATCH_MS = HARD_EXECUTION_DEADLINE_MS


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_dt(value: Any) -> datetime | None:
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


def _iso_plus_ms(value: datetime, milliseconds: int) -> str:
    return (value + timedelta(milliseconds=milliseconds)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def run_observation_fast_lane_shadow(
    conn: sqlite3.Connection,
    *,
    first_actionable_seen_at: str,
    canonicalized_at: str,
    topology: Mapping[str, Any],
    posterior_transaction_inputs: Mapping[str, Any],
    decision_cycle_inputs: Mapping[str, Any],
    commit_timestamp_observer: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Run the one isolated P8→P9 path and return a P10 shadow latency packet.

    T1/T2 are upstream physical/canonical facts. T3 and T4 are captured here
    only after their respective Transaction-B and intent transactions return;
    caller-supplied ``completed_at``/``claimed_at`` remain logical inputs and
    can never act as P10 latency telemetry. A bad topology is rejected before
    any shadow posterior or decision mutation.
    """

    topology_result = validate_observation_fast_lane_topology(
        topology,
        require_execution_readiness=False,
    )
    posterior_inputs = _mapping(posterior_transaction_inputs)
    decision_inputs = _mapping(decision_cycle_inputs)
    timestamp_observer = commit_timestamp_observer or _utc_now_iso
    correlation_id = _text(posterior_inputs.get("correlation_id"))
    if not topology_result.get("ok"):
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "fast_lane_topology_blocked_before_write",
            "fast_lane": build_observation_fast_lane_contract(
                correlation_id=correlation_id,
                timestamps={
                    "first_actionable_seen_at": first_actionable_seen_at,
                    "canonicalized_at": canonicalized_at,
                    "transaction_b_committed_at": "",
                    "decision_intent_durable_at": "",
                },
                topology=topology,
                mode="shadow",
            ),
            "shadow_only": True,
            "places_orders": False,
        }
    if not correlation_id:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "fast_lane_correlation_id_missing_before_write",
            "fast_lane": build_observation_fast_lane_contract(
                correlation_id=correlation_id,
                timestamps={
                    "first_actionable_seen_at": first_actionable_seen_at,
                    "canonicalized_at": canonicalized_at,
                    "transaction_b_committed_at": "",
                    "decision_intent_durable_at": "",
                },
                topology=topology,
                mode="shadow",
            ),
            "shadow_only": True,
            "places_orders": False,
        }
    if posterior_inputs.get("shadow_only") is not True:
        raise ValueError("fast_lane_shadow_only_posterior_input_required")
    transaction_started_at = _text(timestamp_observer())
    t1 = _parse_dt(first_actionable_seen_at)
    transaction_started = _parse_dt(transaction_started_at)
    if (
        t1 is None
        or transaction_started is None
        or transaction_started < t1
        or (transaction_started - t1).total_seconds() * 1000.0 > HARD_T1_TO_DISPATCH_MS
    ):
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "fast_lane_observation_hard_deadline_or_clock_invalid_before_transaction_b",
            "fast_lane": build_observation_fast_lane_contract(
                correlation_id=correlation_id,
                timestamps={
                    "first_actionable_seen_at": first_actionable_seen_at,
                    "canonicalized_at": canonicalized_at,
                    "transaction_b_committed_at": "",
                    "decision_intent_durable_at": "",
                },
                topology=topology,
                mode="shadow",
            ),
            "shadow_only": True,
            "places_orders": False,
        }
    deadline_at = _iso_plus_ms(t1, HARD_T1_TO_DISPATCH_MS)
    posterior_inputs["decision_as_of"] = transaction_started_at
    posterior_inputs["started_at"] = transaction_started_at
    posterior_inputs["completed_at"] = transaction_started_at
    posterior_inputs["public_fact_as_of"] = transaction_started_at
    posterior_inputs["deadline_at"] = deadline_at
    posterior = run_canonical_event_public_ssot_shadow_update(conn, **posterior_inputs)
    if not posterior.get("ok"):
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "fast_lane_posterior_transaction_blocked",
            "posterior": posterior,
            "shadow_only": True,
            "places_orders": False,
        }
    transaction_b_committed_at = _text(timestamp_observer())
    public_fact = _mapping(posterior.get("public_fact_row"))
    if _text(public_fact.get("correlation_id")) != correlation_id:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "fast_lane_posterior_correlation_binding_mismatch",
            "posterior": posterior,
            "shadow_only": True,
            "places_orders": False,
        }
    if _text(public_fact.get("state")) == "physical_only_market_projection_na":
        probability_latency = build_observation_fast_lane_contract(
            correlation_id=correlation_id,
            timestamps={
                "first_actionable_seen_at": first_actionable_seen_at,
                "canonicalized_at": canonicalized_at,
                "transaction_b_committed_at": transaction_b_committed_at,
                "decision_intent_durable_at": "",
            },
            topology=topology,
            mode="shadow",
        )
        latency_timestamp_sources = {
            "first_actionable_seen_at": "canonical_observation_event",
            "canonicalized_at": "resident_canonical_append_observer",
            "transaction_b_committed_at": "post_physical_posterior_ssot_return_observer",
            "decision_intent_durable_at": "not_applicable_market_projection_na",
            "caller_completed_at_not_used_for_latency": True,
            "caller_claimed_at_not_used_for_latency": True,
            "logical_transaction_times_generated_by_orchestrator": True,
        }
        telemetry_packet = {
            "correlation_id": correlation_id,
            "canonical_observation_event_id": _text(posterior.get("canonical_observation_event_id")),
            "event_date": _text(posterior_inputs.get("public_event_date")),
            "posterior_run_id": _text(_mapping(posterior.get("checkpoint")).get("posterior_run_id")),
            "fact_snapshot_id": _text(public_fact.get("fact_snapshot_id")),
            "fast_lane": probability_latency,
            "latency_timestamp_sources": latency_timestamp_sources,
            "created_at": transaction_b_committed_at,
        }
        try:
            telemetry = append_observation_fast_lane_shadow_latency_packet(
                conn,
                packet=telemetry_packet,
            )
        except Exception as exc:
            telemetry = {
                "ok": False,
                "state": "blocked",
                "reason": "physical_posterior_ssot_latency_telemetry_append_failed",
                "error_class": type(exc).__name__,
                "shadow_only": True,
            }
        return {
            "schema": SCHEMA_VERSION,
            "ok": True,
            "state": "physical_posterior_ssot_committed_market_projection_na",
            "shadow_only": True,
            "places_orders": False,
            "correlation_id": correlation_id,
            "posterior": posterior,
            "decision": {
                "ok": True,
                "state": "not_applicable",
                "reason": "x_to_s_to_b_bridge_unavailable",
            },
            "outbox_dispatch": {
                "ok": True,
                "state": "not_applicable",
                "reason": "physical_posterior_has_no_market_outbox",
            },
            "fast_lane": probability_latency,
            "latency_timestamp_sources": latency_timestamp_sources,
            "telemetry": telemetry,
            "boundary": {
                "physical_posterior_committed": True,
                "market_projection_na_does_not_block_probability_ssot": True,
                "creates_trade_outbox": False,
            },
        }
    expected_outbox = _text(_mapping(posterior_inputs).get("outbox_event_id"))
    if _text(decision_inputs.get("outbox_event_id")) != expected_outbox:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "fast_lane_decision_outbox_binding_mismatch",
            "posterior": posterior,
            "shadow_only": True,
            "places_orders": False,
        }
    lease_owner = _text(decision_inputs.get("lease_owner"))
    if not lease_owner:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "fast_lane_outbox_lease_owner_missing_after_posterior_commit",
            "posterior": posterior,
            "shadow_only": True,
            "places_orders": False,
        }
    decision_started_at = _text(timestamp_observer())
    decision_started = _parse_dt(decision_started_at)
    if decision_started is None:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "fast_lane_decision_clock_invalid_after_posterior_commit",
            "posterior": posterior,
            "shadow_only": True,
            "places_orders": False,
        }
    decision_inputs["claimed_at"] = decision_started_at
    decision_inputs["lease_until"] = _iso_plus_ms(decision_started, 30_000)
    outbox_consumer = ResidentPosteriorCommitOutboxShadowWorker(
        conn,
        lease_owner=lease_owner,
    )

    def decision_inputs_for(outbox: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "slug": _text(outbox.get("slug")),
            "event_date": _text(outbox.get("event_date")),
            "market_snapshot": _mapping(decision_inputs.get("market_snapshot")),
            "capital_plan": _mapping(decision_inputs.get("capital_plan")),
            "settlement_window_policy": _mapping(decision_inputs.get("settlement_window_policy")),
            "strategy_artifact_sha256": _text(decision_inputs.get("strategy_artifact_sha256")),
            "top_yes_wave_entry_plan": _mapping(decision_inputs.get("top_yes_wave_entry_plan")),
        }

    outbox_dispatch = outbox_consumer.process_ready_latest_outbox(
        decision_as_of=_text(decision_inputs.get("claimed_at")),
        decision_inputs_for=decision_inputs_for,
        max_events=1,
    )
    outcome = next(
        (
            _mapping(item)
            for item in outbox_dispatch.get("outcomes") or []
            if _text(_mapping(item).get("outbox").get("outbox_event_id")) == expected_outbox
        ),
        {},
    )
    decision = _mapping(outcome.get("result"))
    if not outcome:
        decision = {
            "ok": False,
            "state": "blocked",
            "reason": "fast_lane_expected_outbox_not_dispatched",
        }
    durable_at = _text(timestamp_observer()) if decision.get("ok") else ""
    latency = build_observation_fast_lane_contract(
        correlation_id=correlation_id,
        timestamps={
            "first_actionable_seen_at": first_actionable_seen_at,
            "canonicalized_at": canonicalized_at,
            "transaction_b_committed_at": transaction_b_committed_at,
            "decision_intent_durable_at": durable_at,
        },
        topology=topology,
        mode="shadow",
    )
    latency_timestamp_sources = {
        "first_actionable_seen_at": "canonical_observation_event",
        "canonicalized_at": "resident_canonical_append_observer",
        "transaction_b_committed_at": "post_transaction_b_return_observer",
        "decision_intent_durable_at": "post_decision_intent_return_observer" if durable_at else "not_committed",
        "caller_completed_at_not_used_for_latency": True,
        "caller_claimed_at_not_used_for_latency": True,
        "logical_transaction_times_generated_by_orchestrator": True,
    }
    telemetry_packet = {
        "correlation_id": correlation_id,
        "canonical_observation_event_id": _text(posterior.get("canonical_observation_event_id")),
        "event_date": _text(posterior_inputs.get("public_event_date")),
        "posterior_run_id": _text(_mapping(posterior.get("checkpoint")).get("posterior_run_id")),
        "fact_snapshot_id": _text(public_fact.get("fact_snapshot_id")),
        "fast_lane": latency,
        "latency_timestamp_sources": latency_timestamp_sources,
        "created_at": durable_at or transaction_b_committed_at,
    }
    try:
        telemetry = append_observation_fast_lane_shadow_latency_packet(
            conn,
            packet=telemetry_packet,
        )
    except Exception as exc:
        telemetry = {
            "ok": False,
            "state": "blocked",
            "reason": "fast_lane_shadow_latency_telemetry_append_failed",
            "error_class": type(exc).__name__,
            "shadow_only": True,
        }
    success = bool(decision.get("ok") and latency.get("ok") and telemetry.get("ok"))
    return {
        "schema": SCHEMA_VERSION,
        "ok": success,
        "state": "shadow_committed_within_slo" if success else "shadow_committed_or_decision_but_slo_or_telemetry_blocked",
        "shadow_only": True,
        "places_orders": False,
        "correlation_id": correlation_id,
        "posterior": posterior,
        "decision": decision,
        "outbox_dispatch": outbox_dispatch,
        "fast_lane": latency,
        "latency_timestamp_sources": latency_timestamp_sources,
        "telemetry": telemetry,
    }


__all__ = ["SCHEMA_VERSION", "run_observation_fast_lane_shadow"]

"""Canonical-event inference and the isolated v2 shadow commit adapters.

``build_canonical_event_transaction_b_candidate`` is pure: it turns an
already persisted canonical observation plus frozen evidence into an immutable
Transaction-B bundle.  It performs no SQLite mutation, market read, decision,
intent, or order action.  Shadow and production callers share that one
mathematical path, but must use different explicit commit boundaries.

The commit helpers below remain shadow-only.  Production publication is owned
only by ``ProductionSequentialBayesTransactionBHost``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping

from resolution_bridge_runtime import PHYSICAL_ONLY_STATE, build_physical_only_resolution_bridge, build_resolution_bridge
from peak_timing_state_runtime import build_peak_timing_state
from realization_state_projection_runtime import (
    build_realization_state_projection,
    materialize_projected_realization_state_row,
)
from strong_observation_lr_artifact_contract import build_strong_observation_lr_input
from strong_physical_evidence_projection_runtime import build_strong_physical_evidence_projection
from sequential_bayes_filter_shadow_runtime import (
    build_next_checkpoint,
    build_strong_observation_update,
)
from sequential_bayes_shadow_writer import (
    commit_shadow_transaction_b,
    commit_shadow_physical_posterior_transaction,
    materialize_checkpoint_row,
    materialize_outbox_row,
    materialize_outbox_execution_contract_row,
    materialize_strong_update_row,
)
from sequential_bayes_public_ssot_shadow_writer import (
    commit_public_physical_ssot_shadow_transaction,
    commit_public_ssot_shadow_transaction_b,
    materialize_public_physical_posterior_fact_row,
    materialize_public_posterior_fact_row,
)
from temperature_trajectory_feature_runtime import (
    build_empty_pre_event_temperature_trajectory_feature_state,
    build_temperature_trajectory_feature_state,
)


SCHEMA_VERSION = "sequential_bayes_candidate_pipeline_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _checkpoint_physical_high_distribution(checkpoint: Mapping[str, Any]) -> Any:
    """Read or deterministically marginalize p(X) from one checkpoint."""

    value = _mapping(checkpoint)
    existing = value.get("physical_high_distribution")
    if isinstance(existing, (Mapping, list)) and existing:
        return existing
    joint = value.get("joint_posterior")
    if not isinstance(joint, list):
        return existing
    mass: dict[float, float] = {}
    for raw in joint:
        row = _mapping(raw)
        try:
            temperature = float(row.get("temperature_c"))
            probability = float(row.get("probability"))
        except (TypeError, ValueError):
            return existing
        mass[temperature] = mass.get(temperature, 0.0) + probability
    return {f"{temperature:g}": probability for temperature, probability in sorted(mass.items())}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json_object(value: Any) -> dict[str, Any] | None:
    try:
        parsed = json.loads(_text(value))
    except (TypeError, ValueError):
        return None
    return dict(parsed) if isinstance(parsed, Mapping) else None


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value.lower())


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


def _canonical_event_order_key(event: Mapping[str, Any]) -> tuple[datetime, datetime, str] | None:
    valid_time = _parse_time(event.get("valid_time"))
    actionable_time = _parse_time(event.get("first_actionable_seen_at"))
    event_id = _text(event.get("canonical_observation_event_id"))
    if valid_time is None or actionable_time is None or not event_id:
        return None
    return valid_time, actionable_time, event_id


def _with_runtime_dependency_lineage(
    path_auxiliary: Mapping[str, Any],
    *,
    resident_fast_lane_prewarm_dependency_identity: str,
) -> dict[str, Any] | None:
    """Attach runtime provenance without changing the Bayesian evidence model.

    A resident hot-path prewarm is an execution dependency, never a prior or
    likelihood input.  It is nevertheless part of the durable Transaction-B
    lineage whenever the resident worker supplied it, so retries cannot claim
    the same posterior was produced under a different dependency set.
    """

    auxiliary = dict(_mapping(path_auxiliary))
    supplied = _text(resident_fast_lane_prewarm_dependency_identity)
    existing = _text(auxiliary.get("resident_fast_lane_prewarm_dependency_identity"))
    if supplied and not _is_sha256(supplied):
        return None
    if existing and not _is_sha256(existing):
        return None
    if supplied and existing and supplied != existing:
        return None
    if supplied:
        auxiliary["resident_fast_lane_prewarm_dependency_identity"] = supplied
    return auxiliary


def load_canonical_observation_event(
    conn: sqlite3.Connection,
    *,
    canonical_observation_event_id: str,
) -> dict[str, Any]:
    """Load exactly one accepted canonical event from the append-only P1 store."""

    event_id = _text(canonical_observation_event_id)
    row = conn.execute(
        """
        SELECT canonical_observation_event_id, target_identity, station_id,
               report_type, valid_time, revision_no, selected_source_id,
               selection_role, first_transport_seen_at,
               first_actionable_seen_at, accepted_at, state,
               parsed_observation_json, quality_flags_json
        FROM canonical_observation_events
        WHERE canonical_observation_event_id=?
        """,
        (event_id,),
    ).fetchone()
    if row is None:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "canonical_observation_event_not_found",
            "read_only": True,
        }
    parsed = _json_object(row[12])
    try:
        quality_flags = json.loads(_text(row[13]))
    except (TypeError, ValueError):
        quality_flags = None
    if parsed is None or not isinstance(quality_flags, list):
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "canonical_observation_event_payload_invalid",
            "read_only": True,
        }
    event = {
        "canonical_observation_event_id": _text(row[0]),
        "target_identity": _text(row[1]),
        "station_id": _text(row[2]),
        "report_type": _text(row[3]),
        "valid_time": _text(row[4]),
        "revision_no": int(row[5]),
        "selected_source_id": _text(row[6]),
        "selection_role": _text(row[7]),
        "first_transport_seen_at": _text(row[8]),
        "first_actionable_seen_at": _text(row[9]),
        "accepted_at": _text(row[10]),
        "state": _text(row[11]),
        "parsed_observation": parsed,
        "quality_flags": quality_flags,
    }
    if event["state"] not in {"accepted_primary", "accepted_substitute"}:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "canonical_observation_event_not_accepted",
            "canonical_observation_event_id": event_id,
            "read_only": True,
        }
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "available",
        "canonical_observation_event": event,
        "read_only": True,
    }


def load_canonical_observation_sequence_asof(
    conn: sqlite3.Connection,
    *,
    target_identity: str,
    decision_as_of: str,
) -> dict[str, Any]:
    """Load the only legal trajectory input: canonical facts available at T0.

    The query is intentionally local and indexed by target/actionable time;
    it neither reaches a provider nor accepts a caller-precomputed trajectory.
    This makes the P8 hot path fresh without allowing a second evidence source
    or a future canonical event to leak into a D0 likelihood update.
    """

    as_of = _parse_time(decision_as_of)
    if not _text(target_identity) or as_of is None:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "canonical_observation_sequence_target_or_asof_missing",
            "read_only": True,
        }
    rows = conn.execute(
        """
        SELECT canonical_observation_event_id, canonical_observation_key,
               target_identity, station_id, report_type, valid_time,
               revision_no, selected_source_id, selection_role,
               first_transport_seen_at, first_actionable_seen_at, accepted_at,
               state, parsed_observation_json, quality_flags_json
        FROM canonical_observation_events
        WHERE target_identity=?
          AND state IN ('accepted_primary', 'accepted_substitute')
        ORDER BY valid_time, first_actionable_seen_at,
                 canonical_observation_event_id
        """,
        (_text(target_identity),),
    ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        action_time = _parse_time(row[10])
        if action_time is None:
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked",
                "reason": "canonical_observation_sequence_actionable_time_invalid",
                "read_only": True,
            }
        # RFC3339 values with and without fractional seconds do not compare
        # lexically in SQLite ("...00Z" sorts after "...00.001Z").  Filter
        # on parsed UTC instants, otherwise the just-persisted half-hour
        # report can disappear from the T0 sequence and lose the 3-second
        # path despite being legally actionable.
        if action_time > as_of:
            continue
        parsed = _json_object(row[13])
        try:
            quality_flags = json.loads(_text(row[14]))
        except (TypeError, ValueError):
            quality_flags = None
        if parsed is None or not isinstance(quality_flags, list):
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked",
                "reason": "canonical_observation_sequence_payload_invalid",
                "read_only": True,
            }
        events.append(
            {
                "canonical_observation_event_id": _text(row[0]),
                "canonical_observation_key": _text(row[1]),
                "target_identity": _text(row[2]),
                "station_id": _text(row[3]),
                "report_type": _text(row[4]),
                "valid_time": _text(row[5]),
                "revision_no": int(row[6] or 0),
                "selected_source_id": _text(row[7]),
                "selection_role": _text(row[8]),
                "first_transport_seen_at": _text(row[9]),
                "first_actionable_seen_at": _text(row[10]),
                "accepted_at": _text(row[11]),
                "state": _text(row[12]),
                "parsed_observation": parsed,
                "quality_flags": quality_flags,
            }
        )
    if not events:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "canonical_observation_sequence_empty_asof",
            "read_only": True,
        }
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "available",
        "canonical_observation_events": events,
        "read_only": True,
    }


def build_canonical_event_transaction_b_candidate(
    conn: sqlite3.Connection,
    *,
    candidate_mode: str,
    previous_checkpoint: Mapping[str, Any],
    canonical_observation_event_id: str,
    forecast_evidence_snapshot_id: str,
    prior_context_id: str,
    approved_lr_artifact: Mapping[str, Any],
    resolution_contract: Mapping[str, Any],
    realization_policy: Mapping[str, Any],
    temperature_quantization_c: float,
    fact_snapshot_id: str,
    correlation_id: str,
    outbox_event_id: str,
    decision_as_of: str,
    deadline_at: str,
    started_at: str,
    completed_at: str,
    source_reliability_before: Mapping[str, Any],
    source_reliability_after: Mapping[str, Any],
    path_auxiliary: Mapping[str, Any],
    resident_fast_lane_prewarm_dependency_identity: str = "",
    speci_soft_evidence_only: bool = False,
) -> dict[str, Any]:
    """Build one immutable Transaction-B candidate without mutation.

    ``candidate_mode`` is an explicit boundary marker, not evidence.  The
    returned rows are identical for a given frozen input; only the downstream
    commit boundary differs.  A production candidate is still orderless and
    cannot be published except by the production host under its frozen
    activation contract.
    """

    mode = _text(candidate_mode)
    if mode not in {"shadow", "production"}:
        raise ValueError("candidate_mode_must_be_shadow_or_production")
    shadow_only = mode == "shadow"
    # The current v2 artifact has a trained latent thermal post-state proposal,
    # but no separately approved source-reliability transition head yet.
    # Until that head and its OOS evidence exist, a caller must not smuggle an
    # arbitrary W_t^+ into the next checkpoint.  Retain the pre-state exactly;
    # a future approved head must replace this guard explicitly, never bypass
    # it via prepared-cycle configuration.
    if dict(_mapping(source_reliability_after)) != dict(_mapping(source_reliability_before)):
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "external_source_reliability_post_state_update_not_allowed",
            "read_only": True,
        }
    auxiliary_with_lineage = _with_runtime_dependency_lineage(
        path_auxiliary,
        resident_fast_lane_prewarm_dependency_identity=resident_fast_lane_prewarm_dependency_identity,
    )
    if auxiliary_with_lineage is None:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "resident_fast_lane_prewarm_dependency_identity_invalid_or_mismatched",
            "read_only": True,
        }
    previous = _mapping(previous_checkpoint)
    previous_cumulative_raw_log_score: list[Mapping[str, Any]] = []
    previous_update_id = _text(previous.get("last_strong_update_id"))
    if previous_update_id:
        row = conn.execute(
            "SELECT raw_log_score_json FROM strong_observation_updates "
            "WHERE strong_observation_update_id=?",
            (previous_update_id,),
        ).fetchone()
        if row is None:
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked",
                "reason": "previous_cumulative_strong_likelihood_missing",
                "read_only": True,
            }
        try:
            parsed_previous_score = json.loads(_text(row[0]))
        except (TypeError, ValueError):
            parsed_previous_score = None
        if not isinstance(parsed_previous_score, list):
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked",
                "reason": "previous_cumulative_strong_likelihood_invalid",
                "read_only": True,
            }
        previous_cumulative_raw_log_score = [
            _mapping(item) for item in parsed_previous_score
        ]
    loaded = load_canonical_observation_event(
        conn,
        canonical_observation_event_id=canonical_observation_event_id,
    )
    if not loaded.get("ok"):
        return dict(loaded)
    event = _mapping(loaded.get("canonical_observation_event"))
    if _text(event.get("target_identity")) != _text(previous.get("target_identity")):
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "canonical_event_target_identity_mismatch",
            "read_only": True,
        }
    loaded_sequence = load_canonical_observation_sequence_asof(
        conn,
        target_identity=_text(event.get("target_identity")),
        decision_as_of=decision_as_of,
    )
    if loaded_sequence.get("ok") is not True:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "canonical_observation_sequence_asof_blocked",
            "canonical_sequence": loaded_sequence,
            "read_only": True,
        }
    sequence_events = list(loaded_sequence.get("canonical_observation_events") or [])
    event_key = _canonical_event_order_key(event)
    if event_key is None:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "canonical_event_order_identity_invalid",
            "read_only": True,
        }
    pre_event_sequence: list[Mapping[str, Any]] = []
    found_current = False
    for candidate in sequence_events:
        candidate_key = _canonical_event_order_key(_mapping(candidate))
        if candidate_key is None:
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked",
                "reason": "canonical_observation_sequence_order_identity_invalid",
                "read_only": True,
            }
        if _text(_mapping(candidate).get("canonical_observation_event_id")) == _text(event.get("canonical_observation_event_id")):
            found_current = True
        elif candidate_key < event_key:
            pre_event_sequence.append(_mapping(candidate))
    if not found_current:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "canonical_observation_sequence_missing_current_event",
            "read_only": True,
        }
    if pre_event_sequence:
        pre_event_trajectory = build_temperature_trajectory_feature_state(
            canonical_events=pre_event_sequence,
            decision_as_of=decision_as_of,
            temperature_quantization_c=temperature_quantization_c,
            temporal_role="pre_event",
        )
    else:
        pre_event_trajectory = build_empty_pre_event_temperature_trajectory_feature_state(
            target_identity=_text(event.get("target_identity")),
            station_id=_text(event.get("station_id")),
            decision_as_of=decision_as_of,
            temperature_quantization_c=temperature_quantization_c,
        )
    if pre_event_trajectory.get("ok") is not True:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "canonical_pre_event_temperature_trajectory_blocked",
            "trajectory_feature_state": pre_event_trajectory,
            "read_only": True,
        }
    strong_observation_input = build_strong_observation_lr_input(
        canonical_observation_event=event,
        trajectory_feature_state=pre_event_trajectory,
        forecast_evidence_snapshot_id=forecast_evidence_snapshot_id,
        prior_context_id=prior_context_id,
        source_reliability_pre_state=source_reliability_before,
    )
    if strong_observation_input.get("ok") is not True:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "strong_observation_input_blocked",
            "strong_observation_input": strong_observation_input,
            "read_only": True,
        }
    update = build_strong_observation_update(
        previous_checkpoint=previous,
        canonical_observation_event=event,
        forecast_evidence_snapshot_id=forecast_evidence_snapshot_id,
        prior_context_id=prior_context_id,
        approved_lr_artifact=approved_lr_artifact,
        strong_observation_input=strong_observation_input,
        decision_as_of=decision_as_of,
        deadline_at=deadline_at,
        speci_soft_evidence_only=speci_soft_evidence_only,
        previous_cumulative_raw_log_score=previous_cumulative_raw_log_score,
    )
    if not update.get("ok"):
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "strong_observation_update_blocked",
            "strong_observation_update": update,
            "read_only": True,
        }
    if update.get("no_posterior_change"):
        return {
            "schema": SCHEMA_VERSION,
            "ok": True,
            "state": "duplicate_or_confirmation",
            "candidate_mode": mode,
            "strong_observation_update": update,
            "shadow_only": shadow_only,
            "wrote_transaction_b": False,
        }
    post_event_trajectory = build_temperature_trajectory_feature_state(
        canonical_events=[*pre_event_sequence, event],
        decision_as_of=decision_as_of,
        temperature_quantization_c=temperature_quantization_c,
        temporal_role="post_event",
    )
    if post_event_trajectory.get("ok") is not True:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "canonical_post_event_temperature_trajectory_blocked",
            "trajectory_feature_state": post_event_trajectory,
            "read_only": True,
        }
    checkpoint = build_next_checkpoint(
        previous_checkpoint=previous,
        strong_update=update,
        fact_snapshot_id=fact_snapshot_id,
        resolution_contract_id=_text(_mapping(resolution_contract).get("resolution_contract_id")),
        committed_at=completed_at,
        replay_generation=int(previous.get("replay_generation") or 0),
    )
    if not checkpoint.get("ok") or checkpoint.get("reused_previous"):
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "next_checkpoint_blocked_or_reused",
            "checkpoint": checkpoint,
            "read_only": True,
        }
    contract = _mapping(resolution_contract)
    if contract.get("bridge_closed") is True and contract.get("validated") is True:
        bridge = build_resolution_bridge(
            physical_high_distribution=checkpoint.get("physical_high_distribution"),
            resolution_contract=resolution_contract,
        )
    else:
        bridge = build_physical_only_resolution_bridge(
            physical_high_distribution=checkpoint.get("physical_high_distribution"),
            resolution_contract=resolution_contract,
        )
    if not bridge.get("ok"):
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "resolution_bridge_blocked",
            "resolution_bridge": bridge,
            "read_only": True,
        }
    raw_peak_timing_state = build_peak_timing_state(
        forecast_peak_timing_prior=_mapping(
            auxiliary_with_lineage.get("forecast_peak_timing_prior")
        ),
        strong_observation_update=update,
        post_event_trajectory=post_event_trajectory,
        canonical_observation_event=event,
    )
    if raw_peak_timing_state.get("ok") is True:
        peak_timing_state = dict(raw_peak_timing_state)
    else:
        # Peak timing controls only second-YES admission.  A structural timing
        # failure must remain visible but cannot suppress the physical-X
        # posterior, AMSC/SPECI ingestion, top YES, or safe NO.
        peak_timing_state = {
            "schema": "peak_timing_state_l3_v1",
            "ok": True,
            "state": "na",
            "na_reason": "peak_timing_integrity_unavailable",
            "canonical_observation_event_id": _text(
                event.get("canonical_observation_event_id")
            ),
            "strong_observation_update_id": _text(
                update.get("strong_observation_update_id")
            ),
            "forecast_evidence_snapshot_id": _text(
                update.get("forecast_evidence_snapshot_id")
            ),
            "entry_peak_at": "",
            "source_block_reasons": list(
                raw_peak_timing_state.get("block_reasons") or []
            ),
            "does_not_update_physical_x": True,
            "timing_failure_does_not_block_probability": True,
            "read_only": True,
        }
    update_row = materialize_strong_update_row(
        update,
        source_reliability_before=source_reliability_before,
        source_reliability_after=source_reliability_after,
        path_auxiliary={
            **auxiliary_with_lineage,
            "remaining_warming_marginal": list(update.get("remaining_warming_marginal") or []),
            "termination_risk": dict(_mapping(update.get("termination_risk"))),
            "peak_timing_state": dict(peak_timing_state),
        },
        started_at=started_at,
        completed_at=completed_at,
    )
    checkpoint_row = materialize_checkpoint_row(
        checkpoint,
        resolution_bridge=bridge,
        trajectory_feature_state=post_event_trajectory,
        source_reliability_state=source_reliability_after,
    )
    if _text(bridge.get("state")) == PHYSICAL_ONLY_STATE:
        return {
            "schema": SCHEMA_VERSION,
            "ok": True,
            "state": "physical_only_candidate_ready",
            "candidate_mode": mode,
            "shadow_only": shadow_only,
            "wrote_transaction_b": False,
            "canonical_observation_event_id": _text(event.get("canonical_observation_event_id")),
            "strong_observation_update": update,
            "checkpoint": checkpoint,
            "resolution_bridge": bridge,
            "strong_update_row": update_row,
            "checkpoint_row": checkpoint_row,
            "peak_timing_state": peak_timing_state,
            "market_bucket_probabilities_na_reason": _text(bridge.get("market_bucket_probabilities_na_reason")),
            "resident_fast_lane_prewarm_dependency_identity": _text(
                auxiliary_with_lineage.get("resident_fast_lane_prewarm_dependency_identity")
            ),
        }
    previous_bridge = build_resolution_bridge(
        physical_high_distribution=_checkpoint_physical_high_distribution(previous),
        resolution_contract=resolution_contract,
    )
    if previous_bridge.get("ok") is not True:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "previous_resolution_bridge_blocked_for_strong_evidence",
            "previous_resolution_bridge": previous_bridge,
            "read_only": True,
        }
    policy = _mapping(realization_policy)
    strong_physical_evidence = build_strong_physical_evidence_projection(
        previous_market_bucket_probabilities=_mapping(previous_bridge.get("market_bucket_probabilities")),
        posterior_market_bucket_probabilities=_mapping(bridge.get("market_bucket_probabilities")),
        bucket_contract=_mapping(_mapping(resolution_contract).get("bucket_contract")),
        settlement_exclusion_certificates=_mapping(bridge.get("settlement_exclusion_certificates")),
        canonical_observation_event=event,
        strong_observation_update=update,
        observed_daily_high_c=_mapping(post_event_trajectory.get("path_state_evidence")).get("observed_peak_display_c"),
        observed_max_lower_bound_c=update.get("observed_max_lower_bound_c"),
        realization_policy_artifact_sha256=_text(policy.get("artifact_sha256")),
        strong_support_log_probability_ratio_floor=policy.get("strong_support_log_probability_ratio_floor"),
        strong_exclusion_probability_ceiling=policy.get("strong_exclusion_probability_ceiling"),
        strong_exclusion_log_probability_ratio_ceiling=policy.get("strong_exclusion_log_probability_ratio_ceiling"),
    )
    if strong_physical_evidence.get("ok") is not True:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "strong_physical_evidence_projection_blocked",
            "strong_physical_evidence": strong_physical_evidence,
            "read_only": True,
        }
    realization_projection = build_realization_state_projection(
        checkpoint=checkpoint,
        resolution_bridge=bridge,
        resolution_contract=resolution_contract,
        latest_canonical_observation_event=event,
        strong_observation_update=update,
        strong_physical_evidence=strong_physical_evidence,
        peak_timing_state=peak_timing_state,
        realization_policy=realization_policy,
        temperature_quantization_c=temperature_quantization_c,
    )
    if not realization_projection.get("ok"):
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "realization_state_projection_blocked",
            "realization_projection": realization_projection,
            "read_only": True,
        }
    realization_row = materialize_projected_realization_state_row(
        checkpoint_row=checkpoint_row,
        realization_projection=realization_projection,
        fact_snapshot_id=fact_snapshot_id,
        created_at=_text(checkpoint_row.get("created_at")),
        committed_at=_text(checkpoint_row.get("committed_at")),
        candidate_mode=mode,
    )
    outbox_row = materialize_outbox_row(
        checkpoint_row,
        fact_snapshot_id=fact_snapshot_id,
        correlation_id=correlation_id,
        outbox_event_id=outbox_event_id,
    )
    outbox_execution_contract_row = materialize_outbox_execution_contract_row(
        outbox_row=outbox_row,
        checkpoint_row=checkpoint_row,
        canonical_observation_event=event,
        inference_decision_deadline_at=deadline_at,
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "candidate_ready",
        "candidate_mode": mode,
        "shadow_only": shadow_only,
        "wrote_transaction_b": False,
        "canonical_observation_event_id": _text(event.get("canonical_observation_event_id")),
        "strong_observation_update": update,
        "checkpoint": checkpoint,
        "resolution_bridge": bridge,
        "realization_projection": realization_projection,
        "peak_timing_state": peak_timing_state,
        "strong_update_row": update_row,
        "checkpoint_row": checkpoint_row,
        "realization_state_row": realization_row,
        "outbox_row": outbox_row,
        "outbox_execution_contract_row": outbox_execution_contract_row,
        "resident_fast_lane_prewarm_dependency_identity": _text(
            auxiliary_with_lineage.get("resident_fast_lane_prewarm_dependency_identity")
        ),
    }


def run_canonical_event_shadow_update(
    conn: sqlite3.Connection,
    *,
    shadow_only: bool,
    previous_checkpoint: Mapping[str, Any],
    canonical_observation_event_id: str,
    forecast_evidence_snapshot_id: str,
    prior_context_id: str,
    approved_lr_artifact: Mapping[str, Any],
    resolution_contract: Mapping[str, Any],
    realization_policy: Mapping[str, Any],
    temperature_quantization_c: float,
    fact_snapshot_id: str,
    correlation_id: str,
    outbox_event_id: str,
    decision_as_of: str,
    deadline_at: str,
    started_at: str,
    completed_at: str,
    source_reliability_before: Mapping[str, Any],
    source_reliability_after: Mapping[str, Any],
    path_auxiliary: Mapping[str, Any],
    resident_fast_lane_prewarm_dependency_identity: str = "",
) -> dict[str, Any]:
    """Commit one candidate to the four private P1 shadow tables only."""

    if shadow_only is not True:
        raise ValueError("shadow_only_required")
    candidate = build_canonical_event_transaction_b_candidate(
        conn,
        candidate_mode="shadow",
        previous_checkpoint=previous_checkpoint,
        canonical_observation_event_id=canonical_observation_event_id,
        forecast_evidence_snapshot_id=forecast_evidence_snapshot_id,
        prior_context_id=prior_context_id,
        approved_lr_artifact=approved_lr_artifact,
        resolution_contract=resolution_contract,
        realization_policy=realization_policy,
        temperature_quantization_c=temperature_quantization_c,
        fact_snapshot_id=fact_snapshot_id,
        correlation_id=correlation_id,
        outbox_event_id=outbox_event_id,
        decision_as_of=decision_as_of,
        deadline_at=deadline_at,
        started_at=started_at,
        completed_at=completed_at,
        source_reliability_before=source_reliability_before,
        source_reliability_after=source_reliability_after,
        path_auxiliary=path_auxiliary,
        resident_fast_lane_prewarm_dependency_identity=resident_fast_lane_prewarm_dependency_identity,
    )
    if candidate.get("state") == "physical_only_candidate_ready":
        committed = commit_shadow_physical_posterior_transaction(
            conn,
            expected_previous_checkpoint_id=_text(previous_checkpoint.get("posterior_checkpoint_id")),
            expected_previous_posterior_run_id=_text(previous_checkpoint.get("posterior_run_id")),
            strong_update_row=_mapping(candidate.get("strong_update_row")),
            checkpoint_row=_mapping(candidate.get("checkpoint_row")),
        )
        return {
            **candidate,
            "state": "shadow_committed_physical_only",
            "wrote_transaction_b": False,
            "wrote_physical_posterior_transaction": True,
            "transaction_b": committed,
        }
    if candidate.get("state") != "candidate_ready":
        return candidate
    committed = commit_shadow_transaction_b(
        conn,
        expected_previous_checkpoint_id=_text(previous_checkpoint.get("posterior_checkpoint_id")),
        expected_previous_posterior_run_id=_text(previous_checkpoint.get("posterior_run_id")),
        strong_update_row=_mapping(candidate.get("strong_update_row")),
        checkpoint_row=_mapping(candidate.get("checkpoint_row")),
        realization_state_row=_mapping(candidate.get("realization_state_row")),
        outbox_row=_mapping(candidate.get("outbox_row")),
        outbox_execution_contract_row=_mapping(candidate.get("outbox_execution_contract_row")),
    )
    return {
        **candidate,
        "state": "shadow_committed",
        "wrote_transaction_b": True,
        "transaction_b": committed,
    }


def run_canonical_event_public_ssot_shadow_update(
    conn: sqlite3.Connection,
    *,
    public_slug: str,
    public_event_date: str,
    public_fact_as_of: str,
    **candidate_inputs: Any,
) -> dict[str, Any]:
    """Commit the same candidate to an explicitly isolated public-SSOT shadow DB.

    The public writer itself requires the isolation sentinel and fails before
    P1 mutation on ordinary archive databases.  This is source/test-only and
    never makes a production public fact, decision, intent, or order.
    """

    candidate_inputs = dict(candidate_inputs)
    if candidate_inputs.pop("shadow_only", None) is not True:
        raise ValueError("shadow_only_required")
    candidate = build_canonical_event_transaction_b_candidate(
        conn,
        candidate_mode="shadow",
        **candidate_inputs,
    )
    if candidate.get("state") == "physical_only_candidate_ready":
        correlation_id = _text(candidate_inputs.get("correlation_id"))
        fact_snapshot_id = _text(candidate_inputs.get("fact_snapshot_id"))
        public_row = materialize_public_physical_posterior_fact_row(
            checkpoint_row=_mapping(candidate.get("checkpoint_row")),
            peak_timing_state=_mapping(candidate.get("peak_timing_state")),
            fact_snapshot_id=fact_snapshot_id,
            slug=public_slug,
            event_date=public_event_date,
            fact_as_of=public_fact_as_of,
            correlation_id=correlation_id,
            market_bucket_probabilities_na_reason=_text(
                candidate.get("market_bucket_probabilities_na_reason")
            ),
            resident_fast_lane_prewarm_dependency_identity=_text(
                candidate.get("resident_fast_lane_prewarm_dependency_identity")
            ),
        )
        committed = commit_public_physical_ssot_shadow_transaction(
            conn,
            expected_previous_checkpoint_id=_text(_mapping(candidate_inputs.get("previous_checkpoint")).get("posterior_checkpoint_id")),
            expected_previous_posterior_run_id=_text(_mapping(candidate_inputs.get("previous_checkpoint")).get("posterior_run_id")),
            strong_update_row=_mapping(candidate.get("strong_update_row")),
            checkpoint_row=_mapping(candidate.get("checkpoint_row")),
            public_fact_row=public_row,
        )
        return {
            **candidate,
            "state": "public_ssot_shadow_physical_only_committed",
            "wrote_transaction_b": False,
            "wrote_physical_posterior_transaction": True,
            "public_ssot_shadow_only": True,
            "public_fact_row": public_row,
            "transaction_b": committed,
        }
    if candidate.get("state") != "candidate_ready":
        return candidate
    checkpoint_row = _mapping(candidate.get("checkpoint_row"))
    realization_row = _mapping(candidate.get("realization_state_row"))
    outbox_row = _mapping(candidate.get("outbox_row"))
    outbox_execution_contract_row = _mapping(candidate.get("outbox_execution_contract_row"))
    public_row = materialize_public_posterior_fact_row(
        checkpoint_row=checkpoint_row,
        realization_state_row=realization_row,
        slug=public_slug,
        event_date=public_event_date,
        fact_as_of=public_fact_as_of,
        correlation_id=_text(outbox_row.get("correlation_id")),
        resident_fast_lane_prewarm_dependency_identity=_text(
            candidate.get("resident_fast_lane_prewarm_dependency_identity")
        ),
    )
    committed = commit_public_ssot_shadow_transaction_b(
        conn,
        expected_previous_checkpoint_id=_text(_mapping(candidate_inputs.get("previous_checkpoint")).get("posterior_checkpoint_id")),
        expected_previous_posterior_run_id=_text(_mapping(candidate_inputs.get("previous_checkpoint")).get("posterior_run_id")),
        strong_update_row=_mapping(candidate.get("strong_update_row")),
        checkpoint_row=checkpoint_row,
        realization_state_row=realization_row,
        outbox_row=outbox_row,
        outbox_execution_contract_row=outbox_execution_contract_row,
        public_fact_row=public_row,
    )
    return {
        **candidate,
        "state": "public_ssot_shadow_committed",
        "wrote_transaction_b": True,
        "public_ssot_shadow_only": True,
        "public_fact_row": public_row,
        "transaction_b": committed,
    }

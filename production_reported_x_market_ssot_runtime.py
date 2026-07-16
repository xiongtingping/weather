"""Pure successor-native SSOT/outbox materializer for a closed market bridge."""

from __future__ import annotations

import json
from typing import Any, Mapping

from production_probability_successor_activation_contract import PUBLIC_SSOT_SCHEMA, validate_successor_activation
from reported_x_successor_checkpoint_runtime import build_successor_public_ssot_surfaces
from sequential_bayes_shadow_writer import materialize_outbox_execution_contract_row, materialize_outbox_row


SCHEMA_VERSION = "production_reported_x_market_ssot_runtime_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _parse(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(_text(value) or "{}")
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def materialize_reported_x_market_public_fact_row(
    *,
    activation: Mapping[str, Any],
    checkpoint_row: Mapping[str, Any],
    strong_update_row: Mapping[str, Any],
    market_realization_candidate: Mapping[str, Any],
    fact_snapshot_id: str,
    slug: str,
    event_date: str,
    fact_as_of: str,
    correlation_id: str,
) -> dict[str, Any]:
    authorized = validate_successor_activation(activation)
    checkpoint = _mapping(checkpoint_row)
    surfaces = build_successor_public_ssot_surfaces(checkpoint)
    candidate = _mapping(market_realization_candidate)
    market = _mapping(candidate.get("market_projection"))
    bridge = _mapping(market.get("resolution_bridge"))
    realization_row = _mapping(candidate.get("realization_state_row"))
    realization = _mapping(candidate.get("realization_projection"))
    if surfaces.get("ok") is not True:
        raise ValueError("reported_x_market_ssot_checkpoint_surface_invalid")
    if (
        candidate.get("ok") is not True
        or _text(candidate.get("state")) != "market_realization_candidate_ready"
        or candidate.get("reported_x_unchanged") is not True
        or bridge.get("bridge_closed") is not True
    ):
        raise ValueError("reported_x_market_ssot_candidate_invalid")
    if (
        _text(candidate.get("posterior_checkpoint_id")) != _text(checkpoint.get("posterior_checkpoint_id"))
        or _text(candidate.get("posterior_run_id")) != _text(checkpoint.get("posterior_run_id"))
        or _text(realization_row.get("fact_snapshot_id")) != _text(fact_snapshot_id)
        or _text(realization_row.get("posterior_run_id")) != _text(checkpoint.get("posterior_run_id"))
    ):
        raise ValueError("reported_x_market_ssot_lineage_mismatch")
    auxiliary = _parse(_mapping(strong_update_row).get("path_auxiliary_json"))
    termination = _mapping(auxiliary.get("termination_risk"))
    timing = _mapping(auxiliary.get("peak_timing_state"))
    likelihood_identity = _mapping(auxiliary.get("strong_likelihood_update_identity"))
    if (
        _text(termination.get("schema")) != "termination_risk_marginal_v2_reported_x"
        or _text(termination.get("canonical_observation_event_id")) != _text(checkpoint.get("last_canonical_event_id"))
        or termination.get("updates_posterior") is not False
    ):
        raise ValueError("reported_x_market_ssot_termination_invalid")
    if (
        _text(timing.get("schema")) != "peak_timing_state_l3_v1"
        or _text(timing.get("strong_observation_update_id")) != _text(checkpoint.get("last_strong_update_id"))
    ):
        raise ValueError("reported_x_market_ssot_peak_timing_invalid")
    if _text(realization.get("current_report_bucket")):
        raise ValueError("reported_x_market_ssot_direct_report_bucket_must_be_retired")
    required = (
        fact_snapshot_id,
        slug,
        event_date,
        fact_as_of,
        correlation_id,
        checkpoint.get("posterior_checkpoint_id"),
        checkpoint.get("posterior_run_id"),
        bridge.get("resolution_contract_id"),
        market.get("bridge_artifact_sha256"),
        market.get("target_contract_sha256"),
        realization_row.get("realization_state_id"),
    )
    if any(not _text(value) for value in required):
        raise ValueError("reported_x_market_ssot_identity_missing")
    ssot = {
        "schema": PUBLIC_SSOT_SCHEMA,
        "state": "available",
        "writer_mode": "production",
        "fact_snapshot_id": _text(fact_snapshot_id),
        "posterior_run_id": _text(checkpoint.get("posterior_run_id")),
        "posterior_checkpoint_id": _text(checkpoint.get("posterior_checkpoint_id")),
        "realization_state_id": _text(realization_row.get("realization_state_id")),
        "probability_target_type": surfaces["probability_target_type"],
        "reported_physical_x_distribution": surfaces["reported_physical_x_distribution"],
        "reported_x_lower_bound_c": surfaces["reported_x_lower_bound_c"],
        "reported_x_termination": surfaces["reported_x_termination"],
        "physical_resolution_contract_id": _text(checkpoint.get("resolution_contract_id")),
        "resolution_contract_id": _text(bridge.get("resolution_contract_id")),
        "target_contract_sha256": _text(market.get("target_contract_sha256")),
        "bridge_artifact_sha256": _text(market.get("bridge_artifact_sha256")),
        "settlement_scalar_distribution": list(bridge.get("settlement_scalar_distribution") or []),
        "market_bucket_probabilities": dict(_mapping(bridge.get("market_bucket_probabilities"))),
        "settlement_exclusion_certificates": dict(_mapping(bridge.get("settlement_exclusion_certificates"))),
        "joint_posterior_sha256": _text(checkpoint.get("joint_posterior_sha256")),
        "last_canonical_event_id": _text(checkpoint.get("last_canonical_event_id")),
        "last_strong_update_id": _text(checkpoint.get("last_strong_update_id")),
        "committed_at": _text(checkpoint.get("committed_at")),
        "remaining_warming_marginal": list(auxiliary.get("remaining_warming_marginal") or []),
        "termination_risk": dict(termination),
        "peak_timing_state": dict(timing),
        "strong_likelihood_update_identity": dict(likelihood_identity),
        "current_report_bucket": "",
        "current_report_bucket_na_reason": "physical_x_must_not_be_directly_mapped_to_market_bucket_b",
        "latent_probability_public": False,
        "probability_runtime_identity": {
            "activation_contract_sha256": _text(authorized.get("activation_contract_sha256")),
            "runtime_manifest_sha256": _text(authorized.get("runtime_manifest_sha256")),
            "bayes_formula_id": _text(authorized.get("bayes_formula_id")),
            "composer_schema": _text(authorized.get("composer_schema")),
            "checkpoint_schema": _text(authorized.get("checkpoint_schema")),
            "strong_update_schema": _text(authorized.get("strong_update_schema")),
            "projection_schema": _text(authorized.get("projection_schema")),
            "target_type": _text(authorized.get("target_type")),
            "prior_model_artifact_sha256": _text(authorized.get("prior_model_artifact_sha256")),
            "weak_likelihood_artifact_sha256": _text(authorized.get("weak_likelihood_artifact_sha256")),
            "strong_lr_artifact_sha256": _text(authorized.get("strong_lr_artifact_sha256")),
        },
        "boundary": {
            "reported_x_is_unique_physical_posterior": True,
            "market_b_is_projection_not_second_weather_posterior": True,
            "physical_report_to_market_bucket_direct_mapping_retired": True,
            "market_price_consumed": False,
        },
    }
    return {
        "run_date": _text(event_date),
        "slug": _text(slug),
        "event_date": _text(event_date),
        "fact_as_of": _text(fact_as_of),
        "composer_version": "sequential_bayes_v3_reported_x_production",
        "fact_json": _json({"posterior_ssot_fact": ssot}),
        "created_at": _text(fact_as_of),
        "correlation_id": _text(correlation_id),
        "fact_snapshot_id": _text(fact_snapshot_id),
        "posterior_run_id": _text(checkpoint.get("posterior_run_id")),
    }


def materialize_reported_x_market_handoff_rows(
    *,
    checkpoint_row: Mapping[str, Any],
    market_realization_candidate: Mapping[str, Any],
    canonical_observation_event: Mapping[str, Any],
    fact_snapshot_id: str,
    correlation_id: str,
    outbox_event_id: str,
    inference_decision_deadline_at: str,
) -> dict[str, Any]:
    checkpoint = _mapping(checkpoint_row)
    realization = dict(_mapping(_mapping(market_realization_candidate).get("realization_state_row")))
    if _text(realization.get("fact_snapshot_id")) != _text(fact_snapshot_id):
        raise ValueError("reported_x_market_handoff_fact_snapshot_mismatch")
    outbox = materialize_outbox_row(
        checkpoint,
        fact_snapshot_id=fact_snapshot_id,
        correlation_id=correlation_id,
        outbox_event_id=outbox_event_id,
    )
    execution = materialize_outbox_execution_contract_row(
        outbox_row=outbox,
        checkpoint_row=checkpoint,
        canonical_observation_event=canonical_observation_event,
        inference_decision_deadline_at=inference_decision_deadline_at,
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "handoff_rows_ready",
        "realization_state_row": realization,
        "outbox_row": outbox,
        "outbox_execution_contract_row": execution,
        "places_orders": False,
    }


__all__ = [
    "SCHEMA_VERSION",
    "materialize_reported_x_market_handoff_rows",
    "materialize_reported_x_market_public_fact_row",
]

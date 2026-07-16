"""Successor-native L3 compiler from one reported-X checkpoint to market B."""

from __future__ import annotations

from typing import Any, Mapping

from posterior_market_projection_runtime import build_posterior_market_projection
from realization_state_projection_runtime import (
    build_realization_state_projection,
    materialize_projected_realization_state_row,
)
from resolution_bridge_artifact_runtime import materialize_closed_resolution_contract
from strong_physical_evidence_projection_runtime import build_strong_physical_evidence_projection


SCHEMA_VERSION = "reported_x_market_realization_compiler_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _distribution(checkpoint: Mapping[str, Any]) -> dict[str, Any]:
    return dict(_mapping(checkpoint.get("reported_physical_x_distribution")))


def _blocked(*reasons: str, **extra: Any) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "ok": False,
        "state": "blocked",
        "block_reasons": sorted({_text(reason) for reason in reasons if _text(reason)}),
        "reported_x_unchanged": True,
        "places_orders": False,
        **extra,
    }


def compile_reported_x_market_realization(
    *,
    previous_checkpoint: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
    physical_resolution_contract: Mapping[str, Any],
    market_target_contract: Mapping[str, Any],
    bridge_artifact: Mapping[str, Any],
    canonical_observation_event: Mapping[str, Any],
    strong_observation_update: Mapping[str, Any],
    peak_timing_state: Mapping[str, Any],
    realization_policy: Mapping[str, Any],
    fact_snapshot_id: str,
    created_at: str,
    committed_at: str,
    candidate_mode: str = "shadow",
) -> dict[str, Any]:
    """Compile B/L3 without recalculating or mutating the physical posterior."""

    previous = _mapping(previous_checkpoint)
    current = _mapping(checkpoint)
    previous_distribution = _distribution(previous)
    current_distribution = _distribution(current)
    issues: list[str] = []
    if not previous_distribution or not current_distribution:
        issues.append("reported_x_market_realization_distribution_missing")
    if _text(current.get("previous_checkpoint_id")) != _text(previous.get("posterior_checkpoint_id")):
        issues.append("reported_x_market_realization_predecessor_mismatch")
    if _text(current.get("last_canonical_event_id")) != _text(
        _mapping(canonical_observation_event).get("canonical_observation_event_id")
    ):
        issues.append("reported_x_market_realization_event_mismatch")
    if _text(current.get("last_strong_update_id")) != _text(
        _mapping(strong_observation_update).get("strong_observation_update_id")
    ):
        issues.append("reported_x_market_realization_strong_update_mismatch")
    if issues:
        return _blocked(*issues)
    previous_market = build_posterior_market_projection(
        reported_physical_x_distribution=previous_distribution,
        physical_resolution_contract=physical_resolution_contract,
        market_target_contract=market_target_contract,
        bridge_artifact=bridge_artifact,
    )
    current_market = build_posterior_market_projection(
        reported_physical_x_distribution=current_distribution,
        physical_resolution_contract=physical_resolution_contract,
        market_target_contract=market_target_contract,
        bridge_artifact=bridge_artifact,
    )
    previous_bridge = _mapping(previous_market.get("resolution_bridge"))
    current_bridge = _mapping(current_market.get("resolution_bridge"))
    if (
        previous_market.get("ok") is not True
        or current_market.get("ok") is not True
        or previous_bridge.get("bridge_closed") is not True
        or current_bridge.get("bridge_closed") is not True
    ):
        return _blocked(
            "reported_x_market_projection_unavailable",
            previous_market_projection=previous_market,
            current_market_projection=current_market,
        )
    try:
        closed_contract = materialize_closed_resolution_contract(
            target_contract=market_target_contract,
            bridge_artifact=bridge_artifact,
            physical_support_c=[
                *[float(key) for key in previous_distribution],
                *[float(key) for key in current_distribution],
            ],
        )
    except (TypeError, ValueError) as exc:
        return _blocked(f"reported_x_closed_resolution_contract_invalid:{exc}")
    policy = _mapping(realization_policy)
    update = _mapping(strong_observation_update)
    strong_evidence = build_strong_physical_evidence_projection(
        previous_market_bucket_probabilities=_mapping(previous_bridge.get("market_bucket_probabilities")),
        posterior_market_bucket_probabilities=_mapping(current_bridge.get("market_bucket_probabilities")),
        bucket_contract=_mapping(closed_contract.get("bucket_contract")),
        settlement_exclusion_certificates=_mapping(current_bridge.get("settlement_exclusion_certificates")),
        canonical_observation_event=canonical_observation_event,
        strong_observation_update=update,
        observed_daily_high_c=current.get("reported_x_lower_bound_c"),
        observed_max_lower_bound_c=current.get("reported_x_lower_bound_c"),
        realization_policy_artifact_sha256=_text(policy.get("artifact_sha256")),
        strong_support_log_probability_ratio_floor=float(policy.get("strong_support_log_probability_ratio_floor")),
        strong_exclusion_probability_ceiling=float(policy.get("strong_exclusion_probability_ceiling")),
        strong_exclusion_log_probability_ratio_ceiling=float(policy.get("strong_exclusion_log_probability_ratio_ceiling")),
    )
    if strong_evidence.get("ok") is not True:
        return _blocked("reported_x_strong_market_evidence_unavailable", strong_physical_evidence=strong_evidence)
    projection_checkpoint = {
        **dict(current),
        # The checkpoint remains the unique physical-X posterior.  L3 receives
        # a separate immutable market projection contract below.
        "resolution_contract_id": _text(current.get("resolution_contract_id")),
    }
    realization = build_realization_state_projection(
        checkpoint=projection_checkpoint,
        resolution_bridge=current_bridge,
        resolution_contract=closed_contract,
        latest_canonical_observation_event=canonical_observation_event,
        strong_observation_update=update,
        strong_physical_evidence=strong_evidence,
        peak_timing_state=peak_timing_state,
        realization_policy=policy,
        temperature_quantization_c=1.0,
    )
    if realization.get("ok") is not True:
        return _blocked("reported_x_market_realization_projection_failed", realization_projection=realization)
    row = materialize_projected_realization_state_row(
        checkpoint_row=projection_checkpoint,
        realization_projection=realization,
        fact_snapshot_id=fact_snapshot_id,
        created_at=created_at,
        committed_at=committed_at,
        candidate_mode=candidate_mode,
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "market_realization_candidate_ready",
        "reported_x_unchanged": True,
        "posterior_checkpoint_id": _text(current.get("posterior_checkpoint_id")),
        "posterior_run_id": _text(current.get("posterior_run_id")),
        "physical_resolution_contract_id": _text(current.get("resolution_contract_id")),
        "market_resolution_contract": closed_contract,
        "market_projection": current_market,
        "strong_physical_evidence": strong_evidence,
        "realization_projection": realization,
        "realization_state_row": row,
        "places_orders": False,
        "boundary": {
            "recalculates_physical_x": False,
            "direct_physical_report_to_market_bucket": False,
            "market_price_consumed": False,
            "writes_database": False,
            "places_orders": False,
        },
    }


__all__ = ["SCHEMA_VERSION", "compile_reported_x_market_realization"]

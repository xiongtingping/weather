"""Pure v6 market branch for one atomic Transaction-B bundle.

The compiler is production-native and orderless.  It may run only with an
approved X→S bridge and immutable market target contract.  Missing market
context is handled by the caller's physical-only branch and never blocks X.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from posterior_market_projection_runtime import build_posterior_market_projection
from production_probability_v6_path_auxiliary_runtime import (
    inject_v6_peak_timing_into_public_fact,
)
from production_probability_v6_public_ssot_writer import (
    materialize_v6_public_physical_fact_row,
)
from production_probability_v6_strong_physical_projection_runtime import (
    build_v6_strong_physical_evidence_projection,
)
from realization_state_projection_runtime import (
    build_realization_state_projection,
    materialize_projected_realization_state_row,
)
from resolution_bridge_artifact_runtime import materialize_closed_resolution_contract
from production_probability_v6_outbox_contract_runtime import (
    materialize_v6_outbox_execution_contract_row,
    materialize_v6_outbox_row,
)


SCHEMA_VERSION = "production_probability_v6_market_bundle_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _parse(value: Any, default: Any) -> Any:
    if isinstance(value, (Mapping, list)):
        return value
    try:
        return json.loads(_text(value))
    except (TypeError, ValueError):
        return default


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _checkpoint_view(value: Mapping[str, Any]) -> dict[str, Any]:
    checkpoint = dict(_mapping(value))
    distribution = _parse(
        checkpoint.get("reported_physical_x_distribution_json"), {}
    )
    if not isinstance(distribution, Mapping) or not distribution:
        raise ValueError("v6_market_bundle_reported_x_distribution_missing")
    checkpoint["reported_physical_x_distribution"] = {
        str(key): float(probability) for key, probability in distribution.items()
    }
    return checkpoint


def _validate_physical_resolution_binding(
    *,
    physical_resolution_contract: Mapping[str, Any],
    market_target_contract: Mapping[str, Any],
    bridge_artifact: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
    event_date: str,
) -> None:
    physical = _mapping(physical_resolution_contract)
    target = _mapping(market_target_contract)
    bridge = _mapping(bridge_artifact)
    issues: list[str] = []
    if _text(physical.get("resolution_contract_id")) != _text(
        checkpoint.get("resolution_contract_id")
    ):
        issues.append("physical_resolution_checkpoint_id_mismatch")
    if _text(physical.get("authority_contract_id")) != _text(
        target.get("authority_contract_id")
    ) or _text(physical.get("authority_contract_id")) != _text(
        bridge.get("authority_contract_id")
    ):
        issues.append("physical_resolution_authority_id_mismatch")
    if _text(physical.get("authority_contract_sha256")) != _text(
        target.get("authority_contract_sha256")
    ) or _text(physical.get("authority_contract_sha256")) != _text(
        bridge.get("authority_contract_sha256")
    ):
        issues.append("physical_resolution_authority_sha_mismatch")
    if _text(physical.get("resolution_source_id")) != _text(
        target.get("resolution_source_id")
    ) or _text(physical.get("resolution_source_id")) != _text(
        bridge.get("settlement_source_id")
    ):
        issues.append("physical_resolution_source_mismatch")
    day = _text(event_date)[:10]
    if (
        _text(physical.get("resolution_local_date"))[:10] != day
        or _text(target.get("resolution_local_date"))[:10] != day
    ):
        issues.append("physical_resolution_date_mismatch")
    if (
        _text(physical.get("settlement_unit")).upper() != "C"
        or physical.get("bridge_closed") is not False
        or physical.get("validated") is not False
    ):
        issues.append("physical_resolution_open_contract_invalid")
    if issues:
        raise ValueError(
            "v6_market_bundle_physical_resolution_binding_invalid:"
            + ",".join(sorted(set(issues)))
        )


def _market_public_fact(
    *,
    activation: Mapping[str, Any],
    candidate: Mapping[str, Any],
    realization_candidate: Mapping[str, Any],
    fact_snapshot_id: str,
    slug: str,
    event_date: str,
    fact_as_of: str,
    correlation_id: str,
) -> dict[str, Any]:
    update = _mapping(candidate.get("strong_update_row"))
    checkpoint = _mapping(candidate.get("checkpoint_row"))
    public = materialize_v6_public_physical_fact_row(
        activation=activation,
        strong_update_row=update,
        checkpoint_row=checkpoint,
        fact_snapshot_id=fact_snapshot_id,
        slug=slug,
        event_date=event_date,
        fact_as_of=fact_as_of,
        correlation_id=correlation_id,
        market_bucket_probabilities_na_reason="market_projection_materializing",
    )
    public = inject_v6_peak_timing_into_public_fact(
        public_fact_row=public,
        candidate=candidate,
    )
    fact = dict(_mapping(_parse(public.get("fact_json"), {})))
    ssot = dict(_mapping(fact.get("posterior_ssot_fact")))
    market = _mapping(realization_candidate.get("market_projection"))
    bridge = _mapping(market.get("resolution_bridge"))
    realization = _mapping(realization_candidate.get("realization_state_row"))
    if (
        market.get("ok") is not True
        or bridge.get("bridge_closed") is not True
        or not _text(realization.get("realization_state_id"))
    ):
        raise ValueError("v6_market_bundle_public_projection_invalid")
    ssot.update(
        {
            "state": "available",
            "realization_state_id": _text(realization.get("realization_state_id")),
            "physical_resolution_contract_id": _text(
                checkpoint.get("resolution_contract_id")
            ),
            "resolution_contract_id": _text(bridge.get("resolution_contract_id")),
            "target_contract_sha256": _text(market.get("target_contract_sha256")),
            "bridge_artifact_sha256": _text(market.get("bridge_artifact_sha256")),
            "settlement_scalar_distribution": list(
                bridge.get("settlement_scalar_distribution") or []
            ),
            "market_bucket_probabilities": dict(
                _mapping(bridge.get("market_bucket_probabilities"))
            ),
            "settlement_exclusion_certificates": dict(
                _mapping(bridge.get("settlement_exclusion_certificates"))
            ),
            "market_bucket_probabilities_na_reason": "",
            "current_report_bucket": "",
            "current_report_bucket_na_reason": (
                "physical_x_must_not_be_directly_mapped_to_market_bucket_b"
            ),
            "market_projection_boundary": {
                "reported_x_unchanged": True,
                "market_b_is_projection_not_second_weather_posterior": True,
                "market_price_consumed": False,
            },
        }
    )
    fact["posterior_ssot_fact"] = ssot
    public["fact_json"] = _json(fact)
    return public


def build_v6_market_transaction_bundle(
    *,
    activation: Mapping[str, Any],
    previous_checkpoint: Mapping[str, Any],
    candidate: Mapping[str, Any],
    physical_resolution_contract: Mapping[str, Any],
    market_target_contract: Mapping[str, Any],
    bridge_artifact: Mapping[str, Any],
    canonical_observation_event: Mapping[str, Any],
    realization_policy: Mapping[str, Any],
    fact_snapshot_id: str,
    slug: str,
    event_date: str,
    fact_as_of: str,
    correlation_id: str,
    outbox_event_id: str,
    inference_decision_deadline_at: str,
) -> dict[str, Any]:
    value = _mapping(candidate)
    if (
        value.get("ok") is not True
        or _text(value.get("candidate_mode")) != "production_successor"
        or value.get("shadow_only") is not False
    ):
        raise ValueError("v6_market_bundle_candidate_not_production_ready")
    previous = _checkpoint_view(previous_checkpoint)
    checkpoint = _checkpoint_view(_mapping(value.get("checkpoint_row")))
    update = dict(_mapping(value.get("strong_update_row")))
    event = dict(_mapping(canonical_observation_event))
    _validate_physical_resolution_binding(
        physical_resolution_contract=physical_resolution_contract,
        market_target_contract=market_target_contract,
        bridge_artifact=bridge_artifact,
        checkpoint=checkpoint,
        event_date=event_date,
    )
    previous_market = build_posterior_market_projection(
        reported_physical_x_distribution=previous["reported_physical_x_distribution"],
        physical_resolution_contract=physical_resolution_contract,
        market_target_contract=market_target_contract,
        bridge_artifact=bridge_artifact,
    )
    current_market = build_posterior_market_projection(
        reported_physical_x_distribution=checkpoint[
            "reported_physical_x_distribution"
        ],
        physical_resolution_contract=physical_resolution_contract,
        market_target_contract=market_target_contract,
        bridge_artifact=bridge_artifact,
    )
    if (
        previous_market.get("ok") is not True
        or current_market.get("ok") is not True
        or _mapping(current_market.get("resolution_bridge")).get("bridge_closed")
        is not True
    ):
        raise ValueError("v6_market_bundle_closed_projection_unavailable")
    closed = materialize_closed_resolution_contract(
        target_contract=market_target_contract,
        bridge_artifact=bridge_artifact,
        physical_support_c=[
            *[float(key) for key in previous["reported_physical_x_distribution"]],
            *[float(key) for key in checkpoint["reported_physical_x_distribution"]],
        ],
    )
    previous_bridge = _mapping(previous_market.get("resolution_bridge"))
    current_bridge = _mapping(current_market.get("resolution_bridge"))
    strong_evidence = build_v6_strong_physical_evidence_projection(
        previous_market_bucket_probabilities=_mapping(
            previous_bridge.get("market_bucket_probabilities")
        ),
        posterior_market_bucket_probabilities=_mapping(
            current_bridge.get("market_bucket_probabilities")
        ),
        bucket_contract=_mapping(closed.get("bucket_contract")),
        settlement_exclusion_certificates=_mapping(
            current_bridge.get("settlement_exclusion_certificates")
        ),
        canonical_observation_event=event,
        strong_observation_update=update,
        checkpoint=checkpoint,
        realization_policy=realization_policy,
    )
    if strong_evidence.get("ok") is not True:
        raise ValueError(
            "v6_market_bundle_strong_projection_blocked:"
            + ",".join(strong_evidence.get("block_reasons") or [])
        )
    auxiliary = _mapping(_parse(update.get("path_auxiliary_json"), {}))
    timing = _mapping(auxiliary.get("peak_timing_state"))
    realization_projection = build_realization_state_projection(
        checkpoint=checkpoint,
        resolution_bridge=current_bridge,
        resolution_contract=closed,
        latest_canonical_observation_event=event,
        strong_observation_update=update,
        strong_physical_evidence=strong_evidence,
        peak_timing_state=timing,
        realization_policy=realization_policy,
        temperature_quantization_c=1.0,
    )
    if realization_projection.get("ok") is not True:
        raise ValueError(
            "v6_market_bundle_realization_blocked:"
            + ",".join(realization_projection.get("block_reasons") or [])
        )
    realization_row = materialize_projected_realization_state_row(
        checkpoint_row=checkpoint,
        realization_projection=realization_projection,
        fact_snapshot_id=fact_snapshot_id,
        created_at=fact_as_of,
        committed_at=fact_as_of,
        candidate_mode="production",
    )
    realization_candidate = {
        "ok": True,
        "state": "market_realization_candidate_ready",
        "reported_x_unchanged": True,
        "posterior_checkpoint_id": _text(checkpoint.get("posterior_checkpoint_id")),
        "posterior_run_id": _text(checkpoint.get("posterior_run_id")),
        "market_projection": current_market,
        "strong_physical_evidence": strong_evidence,
        "realization_projection": realization_projection,
        "realization_state_row": realization_row,
    }
    public = _market_public_fact(
        activation=activation,
        candidate=value,
        realization_candidate=realization_candidate,
        fact_snapshot_id=fact_snapshot_id,
        slug=slug,
        event_date=event_date,
        fact_as_of=fact_as_of,
        correlation_id=correlation_id,
    )
    outbox = materialize_v6_outbox_row(
        checkpoint,
        fact_snapshot_id=fact_snapshot_id,
        correlation_id=correlation_id,
        outbox_event_id=outbox_event_id,
    )
    execution = materialize_v6_outbox_execution_contract_row(
        outbox_row=outbox,
        checkpoint_row=checkpoint,
        canonical_observation_event=event,
        inference_decision_deadline_at=inference_decision_deadline_at,
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "production_market_transaction_bundle_ready",
        "strong_update_row": update,
        "checkpoint_row": dict(_mapping(value.get("checkpoint_row"))),
        "realization_state_row": realization_row,
        "outbox_row": outbox,
        "outbox_execution_contract_row": execution,
        "public_fact_row": public,
        "market_projection": current_market,
        "strong_physical_evidence": strong_evidence,
        "realization_projection": realization_projection,
        "reported_x_unchanged": True,
        "places_orders": False,
        "clob": False,
    }


__all__ = ["SCHEMA_VERSION", "build_v6_market_transaction_bundle"]

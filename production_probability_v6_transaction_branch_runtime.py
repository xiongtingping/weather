"""Choose the unique v6 physical or approved market Transaction-B bundle.

The branch is fail-open only for physical X: market context, bridge, policy or
projection failures become an auditable market NA reason.  They never select
an old probability model and never authorize an order.
"""

from __future__ import annotations

from typing import Any, Mapping

from production_probability_v6_market_bundle_runtime import (
    build_v6_basket_market_projection_bundle,
    build_v6_market_transaction_bundle,
)
from production_probability_v6_path_auxiliary_runtime import (
    inject_v6_peak_timing_into_public_fact,
)
from production_probability_v6_public_ssot_writer import (
    materialize_v6_public_physical_fact_row,
)
from resolution_bridge_activation_runtime import (
    validate_resolution_bridge_activation,
)
from production_probability_v6_activation_contract import (
    IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA,
)


SCHEMA_VERSION = "production_probability_v6_transaction_branch_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _physical_bundle(
    *,
    activation: Mapping[str, Any],
    candidate: Mapping[str, Any],
    fact_snapshot_id: str,
    slug: str,
    event_date: str,
    fact_as_of: str,
    correlation_id: str,
    na_reason: str,
    market_block_reasons: list[str] | None = None,
) -> dict[str, Any]:
    public = materialize_v6_public_physical_fact_row(
        activation=activation,
        strong_update_row=_mapping(candidate.get("strong_update_row")),
        checkpoint_row=_mapping(candidate.get("checkpoint_row")),
        fact_snapshot_id=fact_snapshot_id,
        slug=slug,
        event_date=event_date,
        fact_as_of=fact_as_of,
        correlation_id=correlation_id,
        market_bucket_probabilities_na_reason=na_reason,
    )
    if _text(activation.get("composer_schema")) != IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA:
        public = inject_v6_peak_timing_into_public_fact(
            public_fact_row=public,
            candidate=candidate,
        )
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "physical_only_bundle_ready",
        "writer_route": "physical_only",
        "strong_update_row": dict(_mapping(candidate.get("strong_update_row"))),
        "checkpoint_row": dict(_mapping(candidate.get("checkpoint_row"))),
        "public_fact_row": public,
        "realization_state_row": None,
        "outbox_row": None,
        "outbox_execution_contract_row": None,
        "market_projection_available": False,
        "market_projection_na_reason": na_reason,
        "market_block_reasons": list(market_block_reasons or []),
        "reported_x_unchanged": True,
        "blocks_physical_x": False,
        "creates_outbox": False,
        "places_orders": False,
        "clob": False,
    }


def build_v6_transaction_branch_bundle(
    *,
    activation: Mapping[str, Any],
    previous_checkpoint: Mapping[str, Any],
    candidate: Mapping[str, Any],
    canonical_observation_event: Mapping[str, Any],
    market_context_result: Mapping[str, Any],
    bridge_activation: Mapping[str, Any] | None,
    bridge_artifact: Mapping[str, Any] | None,
    physical_resolution_contract: Mapping[str, Any] | None,
    realization_policy: Mapping[str, Any] | None,
    fact_snapshot_id: str,
    slug: str,
    event_date: str,
    fact_as_of: str,
    correlation_id: str,
    outbox_event_id: str,
    inference_decision_deadline_at: str,
) -> dict[str, Any]:
    context_result = _mapping(market_context_result)
    if (
        context_result.get("ok") is not True
        or context_result.get("market_projection_available") is not True
        or _text(context_result.get("state")) != "available"
    ):
        return _physical_bundle(
            activation=activation,
            candidate=candidate,
            fact_snapshot_id=fact_snapshot_id,
            slug=slug,
            event_date=event_date,
            fact_as_of=fact_as_of,
            correlation_id=correlation_id,
            na_reason=_text(context_result.get("na_reason"))
            or "market_projection_context_unavailable",
            market_block_reasons=list(context_result.get("block_reasons") or []),
        )

    context = _mapping(context_result.get("market_context"))
    try:
        bridge = validate_resolution_bridge_activation(bridge_activation or {})
        artifact = _mapping(bridge_artifact)
        if (
            _text(bridge.get("state")) != "production_enabled"
            or _text(bridge.get("artifact_sha256"))
            != _text(context.get("bridge_artifact_sha256"))
            or _text(artifact.get("artifact_sha256"))
            != _text(context.get("bridge_artifact_sha256"))
            or _text(_mapping(context.get("market_target_contract")).get("target_contract_sha256"))
            != _text(context.get("target_contract_sha256"))
        ):
            raise ValueError("v6_market_branch_content_binding_mismatch")
        identifiable_physical = (
            _text(activation.get("composer_schema"))
            == IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA
        )
        if identifiable_physical:
            bundle = build_v6_basket_market_projection_bundle(
                activation=activation,
                candidate=candidate,
                physical_resolution_contract=physical_resolution_contract or {},
                market_target_contract=_mapping(context.get("market_target_contract")),
                bridge_artifact=artifact,
                fact_snapshot_id=fact_snapshot_id,
                slug=_text(context.get("slug")) or slug,
                event_date=event_date,
                fact_as_of=fact_as_of,
                correlation_id=correlation_id,
            )
        else:
            bundle = build_v6_market_transaction_bundle(
                activation=activation,
                previous_checkpoint=previous_checkpoint,
                candidate=candidate,
                physical_resolution_contract=physical_resolution_contract or {},
                market_target_contract=_mapping(context.get("market_target_contract")),
                bridge_artifact=artifact,
                canonical_observation_event=canonical_observation_event,
                realization_policy=realization_policy or {},
                fact_snapshot_id=fact_snapshot_id,
                slug=_text(context.get("slug")) or slug,
                event_date=event_date,
                fact_as_of=fact_as_of,
                correlation_id=correlation_id,
                outbox_event_id=outbox_event_id,
                inference_decision_deadline_at=inference_decision_deadline_at,
            )
    except (KeyError, TypeError, ValueError) as exc:
        return _physical_bundle(
            activation=activation,
            candidate=candidate,
            fact_snapshot_id=fact_snapshot_id,
            slug=slug,
            event_date=event_date,
            fact_as_of=fact_as_of,
            correlation_id=correlation_id,
            na_reason=f"market_projection_branch_blocked:{type(exc).__name__}",
            market_block_reasons=[_text(exc) or type(exc).__name__],
        )
    writer_route = (
        "basket_probability_transaction_b"
        if _text(activation.get("composer_schema"))
        == IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA
        else "market_transaction_b"
    )
    return {
        **bundle,
        "schema": SCHEMA_VERSION,
        "state": (
            "basket_probability_transaction_bundle_ready"
            if writer_route == "basket_probability_transaction_b"
            else "market_transaction_bundle_ready"
        ),
        "writer_route": writer_route,
        "market_projection_available": True,
        "market_projection_na_reason": "",
        "blocks_physical_x": False,
        "creates_outbox": writer_route == "market_transaction_b",
        "places_orders": False,
        "clob": False,
    }


__all__ = ["SCHEMA_VERSION", "build_v6_transaction_branch_bundle"]

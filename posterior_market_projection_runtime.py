"""Pure projection of the unique reported-X posterior into market bucket B."""

from __future__ import annotations

from typing import Any, Mapping

from resolution_bridge_artifact_runtime import materialize_closed_resolution_contract
from resolution_bridge_runtime import build_physical_only_resolution_bridge, build_resolution_bridge


SCHEMA_VERSION = "posterior_market_projection_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def build_posterior_market_projection(
    *,
    reported_physical_x_distribution: Mapping[str, Any],
    physical_resolution_contract: Mapping[str, Any],
    market_target_contract: Mapping[str, Any] | None,
    bridge_artifact: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return B only when both the target rule and approved bridge exist.

    The function never changes p(X).  Missing bridge inputs produce an
    explicit NA surface, not a second probability model and not an exception
    that can block AMSC ingestion.
    """

    distribution = dict(_mapping(reported_physical_x_distribution))
    if not market_target_contract or not bridge_artifact:
        physical_only = build_physical_only_resolution_bridge(
            physical_high_distribution=distribution,
            resolution_contract=physical_resolution_contract,
            market_bucket_na_reason=(
                "x_to_s_to_b_bridge_disabled_or_unapproved"
                if not bridge_artifact
                else "market_target_contract_unavailable"
            ),
        )
        return {
            **physical_only,
            "schema": SCHEMA_VERSION,
            "probability_target_type": "amsc_scheduled_regular_metar_reported_daily_max_c",
            "market_probability_target_type": None,
            "reported_x_unchanged": True,
            "resolution_bridge": dict(physical_only),
            "places_orders": False,
        }
    try:
        closed = materialize_closed_resolution_contract(
            target_contract=market_target_contract,
            bridge_artifact=bridge_artifact,
            physical_support_c=[float(key) for key in distribution],
        )
    except (TypeError, ValueError) as exc:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "market_projection_blocked",
            "block_reasons": [f"closed_resolution_contract_unavailable:{exc}"],
            "reported_x_unchanged": True,
            "market_bucket_probabilities": {},
            "market_bucket_probabilities_na_reason": "x_to_s_to_b_projection_contract_invalid",
            "places_orders": False,
        }
    bridge = build_resolution_bridge(
        physical_high_distribution=distribution,
        resolution_contract=closed,
    )
    if bridge.get("ok") is not True or bridge.get("bridge_closed") is not True:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "market_projection_blocked",
            "block_reasons": list(bridge.get("block_reasons") or ["closed_resolution_bridge_invalid"]),
            "reported_x_unchanged": True,
            "market_bucket_probabilities": {},
            "market_bucket_probabilities_na_reason": "x_to_s_to_b_projection_failed",
            "places_orders": False,
        }
    baseline = _mapping(_mapping(bridge_artifact).get("operational_baseline"))
    probabilities = _mapping(bridge.get("market_bucket_probabilities"))
    if (
        baseline.get("soft_projection_exact_zero_forbidden_without_hard_certificate") is True
        and any(float(probability) <= 0.0 for probability in probabilities.values())
    ):
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "market_projection_blocked",
            "block_reasons": ["soft_operational_projection_contains_false_exact_zero"],
            "reported_x_unchanged": True,
            "market_bucket_probabilities": {},
            "market_bucket_probabilities_na_reason": "x_to_s_to_b_soft_tail_contract_violated",
            "places_orders": False,
        }
    return {
        **bridge,
        "schema": SCHEMA_VERSION,
        "state": "market_projection_available",
        "probability_target_type": "amsc_scheduled_regular_metar_reported_daily_max_c",
        "market_probability_target_type": "frozen_market_bucket_b",
        "reported_x_unchanged": True,
        "resolution_bridge": dict(bridge),
        "target_contract_sha256": _text(_mapping(market_target_contract).get("target_contract_sha256")),
        "bridge_artifact_sha256": _text(_mapping(bridge_artifact).get("artifact_sha256")),
        "places_orders": False,
    }


__all__ = ["SCHEMA_VERSION", "build_posterior_market_projection"]

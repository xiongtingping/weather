from __future__ import annotations

from resident_fast_lane_prewarm_contract import (
    validate_resident_execution_prewarm,
    validate_resident_fast_lane_prewarm,
    validate_resident_market_projection_prewarm,
)


def _components() -> dict:
    return {
        "forecast_snapshot": {"ready": True, "forecast_snapshot_id": "forecast_1", "artifact_sha256": "forecast_artifact_1", "valid_until": "2026-07-03T07:00:00Z"},
        "prior_context": {"ready": True, "prior_context_id": "prior_1", "artifact_sha256": "prior_artifact_1", "valid_until": "2026-07-03T07:00:00Z"},
        "strong_lr_artifact": {"ready": True, "approved": True, "strong_lr_artifact_sha256": "strong_1", "valid_until": "2026-07-03T07:00:00Z"},
        "market_context": {"ready": True, "market_snapshot_id": "market_1", "fresh_until": "2026-07-03T07:00:00Z"},
        "resident_signer_connection_pool": {"ready": True, "pool_id": "pool_1", "valid_until": "2026-07-03T07:00:00Z"},
    }


def test_prewarm_requires_real_runtime_identity_not_boolean_claims() -> None:
    result = validate_resident_fast_lane_prewarm(
        {
            "forecast_snapshot": True,
            "prior_context": True,
            "strong_lr_artifact": True,
            "market_context": True,
            "resident_signer_connection_pool": True,
        }
    )

    assert result["ok"] is False
    assert "resident_prewarm_forecast_snapshot_not_ready" in result["blockers"]
    assert "resident_prewarm_strong_lr_artifact_not_ready" in result["blockers"]


def test_probability_prewarm_identity_is_stable_and_stale_market_cannot_block_physical_posterior() -> None:
    components = _components()
    for name in ("forecast_snapshot", "prior_context", "strong_lr_artifact"):
        components[name]["valid_until"] = "2026-07-03T08:00:00Z"
    first = validate_resident_fast_lane_prewarm(components)
    second = validate_resident_fast_lane_prewarm(components)
    stale = validate_resident_fast_lane_prewarm(
        components,
        decision_as_of="2026-07-03T07:00:00.001Z",
    )

    assert first["ok"] is True
    assert first["dependency_identity_sha256"] == second["dependency_identity_sha256"]
    assert stale["ok"] is True
    assert "market_context" not in stale["component_ids"]

    market = validate_resident_market_projection_prewarm(
        components,
        decision_as_of="2026-07-03T07:00:00.001Z",
    )
    assert market["ok"] is False
    assert "resident_prewarm_market_context_stale_at_dispatch" in market["blockers"]


def test_p8_p9_prewarm_does_not_require_an_execution_signer() -> None:
    components = _components()
    components.pop("resident_signer_connection_pool")

    result = validate_resident_fast_lane_prewarm(components)

    assert result["ok"] is True
    assert result["purpose"] == "probability_ssot_inference"
    assert "resident_signer_connection_pool" not in result["component_ids"]


def test_l5_execution_prewarm_checks_the_signer_separately() -> None:
    missing = validate_resident_execution_prewarm({})
    ready = validate_resident_execution_prewarm(_components())

    assert missing["ok"] is False
    assert "resident_prewarm_resident_signer_connection_pool_not_ready" in missing["blockers"]
    assert ready["ok"] is True
    assert ready["purpose"] == "l5_execution"

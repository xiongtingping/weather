from __future__ import annotations

from datetime import date, timedelta

from posterior_market_projection_runtime import build_posterior_market_projection
from resolution_bridge_artifact_runtime import artifact_sha256
from resolution_bridge_calibration_runtime import fit_resolution_bridge_candidate


def _pairs() -> list[dict]:
    rows = []
    start = date(2025, 1, 1)
    for index in range(120):
        day = (start + timedelta(days=index)).isoformat()
        x = 20 + index % 15
        s = x + (-1, 0, 0, 0, 1)[index % 5]
        rows.append({
            "state": "available_for_bridge_calibration_only",
            "event_date": day,
            "authority_contract_id": "zuuu_amsc_wunderground_daily_high_v4",
            "final_physical_high_c": x,
            "settlement_scalar_c": s,
            "observed_x_s_pair_sha256": f"{index:064x}",
            "physical_source": {"source_id": "amsc_zuuu_scheduled_metar_daily_max_v2"},
            "settlement_source": {"source_id": "wunderground_zuuu_historical_observation_daily_high_final_v3"},
        })
    return rows


def _physical() -> dict:
    return {
        "resolution_contract_id": "physical-only",
        "resolution_source_id": "wunderground_zuuu_historical_observation_daily_high_final_v3",
        "resolution_local_date": "2025-04-30",
        "settlement_unit": "C",
        "bridge_closed": False,
        "validated": False,
    }


def _target() -> dict:
    return {
        "schema": "market_resolution_target_contract_v1",
        "authority_contract_id": "zuuu_amsc_wunderground_daily_high_v4",
        "authority_contract_sha256": "a" * 64,
        "resolution_source_id": "wunderground_zuuu_historical_observation_daily_high_final_v3",
        "resolution_local_date": "2025-04-30",
        "target_contract_sha256": "b" * 64,
        "rounding_rule": "whole_degree_half_step_v1",
        "bucket_contract": {
            "29C or below": {"lower_inclusive": None, "upper_exclusive": 29.5},
            "30C": {"lower_inclusive": 29.5, "upper_exclusive": 30.5},
            "31C or higher": {"lower_inclusive": 30.5, "upper_exclusive": None},
        },
        "bridge_closed": False,
        "validated": False,
    }


def _approved() -> dict:
    candidate = fit_resolution_bridge_candidate(
        _pairs(), authority_contract_sha256="a" * 64
    )["candidate_artifact"]
    core = {
        **candidate,
        "state": "production_approved",
        "bridge_closed": True,
        "validated": True,
        "production_runtime_eligible": True,
        "shadow_only": False,
        "approval_id": "ADR-bridge-test",
        "approval_receipt_sha256": "c" * 64,
    }
    core.pop("artifact_sha256")
    return {**core, "artifact_sha256": artifact_sha256(core)}


def test_open_bridge_returns_explicit_na_without_changing_x() -> None:
    result = build_posterior_market_projection(
        reported_physical_x_distribution={"30": 0.6, "31": 0.4},
        physical_resolution_contract=_physical(),
        market_target_contract=None,
        bridge_artifact=None,
    )
    assert result["ok"] is True
    assert result["reported_x_unchanged"] is True
    assert result["market_bucket_probabilities"] == {}
    assert result["bridge_closed"] is False


def test_approved_bridge_maps_x_to_b_without_market_price_inputs() -> None:
    result = build_posterior_market_projection(
        reported_physical_x_distribution={"30": 0.6, "31": 0.4},
        physical_resolution_contract=_physical(),
        market_target_contract=_target(),
        bridge_artifact=_approved(),
    )
    assert result["ok"] is True
    assert result["state"] == "market_projection_available"
    assert result["reported_x_unchanged"] is True
    assert sum(result["market_bucket_probabilities"].values()) == 1.0

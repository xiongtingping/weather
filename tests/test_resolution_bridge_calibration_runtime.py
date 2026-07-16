from __future__ import annotations

from datetime import date, timedelta

import pytest

from resolution_bridge_artifact_runtime import (
    artifact_sha256,
    materialize_closed_resolution_contract,
    residual_pmf_for_date,
    validate_resolution_bridge_artifact,
)
from resolution_bridge_calibration_runtime import fit_resolution_bridge_candidate
from resolution_bridge_runtime import build_resolution_bridge


AUTHORITY_SHA = "a" * 64


def _pairs(count: int = 120) -> list[dict]:
    start = date(2025, 1, 1)
    rows = []
    for index in range(count):
        day = (start + timedelta(days=index)).isoformat()
        x = 20 + index % 15
        residual = (-1, 0, 0, 0, 1)[index % 5]
        s = x + residual
        rows.append(
            {
                "state": "available_for_bridge_calibration_only",
                "event_date": day,
                "authority_contract_id": "zuuu_amsc_wunderground_daily_high_v4",
                "final_physical_high_c": x,
                "settlement_scalar_c": s,
                "observed_x_s_pair_sha256": f"{index:064x}",
                "physical_source": {
                    "source_id": "amsc_zuuu_scheduled_metar_daily_max_v2",
                },
                "settlement_source": {
                    "source_id": "wunderground_zuuu_historical_observation_daily_high_final_v3",
                },
            }
        )
    return rows


def _target() -> dict:
    return {
        "schema": "market_resolution_target_contract_v1",
        "authority_contract_id": "zuuu_amsc_wunderground_daily_high_v4",
        "authority_contract_sha256": AUTHORITY_SHA,
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


def test_calibration_builds_unique_open_candidate_with_prequential_oos() -> None:
    result = fit_resolution_bridge_candidate(
        _pairs(),
        authority_contract_sha256=AUTHORITY_SHA,
    )
    assert result["ok"] is True
    artifact = result["candidate_artifact"]
    assert artifact["bridge_closed"] is False
    assert artifact["production_runtime_eligible"] is False
    assert artifact["oos_evaluation"]["evaluated_event_days"] >= 18
    assert sum(artifact["global_residual_pmf"].values()) == pytest.approx(1.0)
    assert validate_resolution_bridge_artifact(artifact)["artifact_sha256"] == artifact["artifact_sha256"]


def test_insufficient_pairs_accumulate_without_candidate_or_live_block_authority() -> None:
    result = fit_resolution_bridge_candidate(
        _pairs(20),
        authority_contract_sha256=AUTHORITY_SHA,
    )
    assert result["ok"] is False
    assert result["state"] == "accumulating"
    assert result["candidate_artifact"] is None
    assert result["places_orders"] is False


def test_only_approved_artifact_can_materialize_closed_target_bound_kernel() -> None:
    candidate = fit_resolution_bridge_candidate(
        _pairs(), authority_contract_sha256=AUTHORITY_SHA
    )["candidate_artifact"]
    with pytest.raises(ValueError):
        materialize_closed_resolution_contract(
            target_contract=_target(), bridge_artifact=candidate, physical_support_c=[30, 31]
        )
    approved_core = {
        **candidate,
        "state": "production_approved",
        "bridge_closed": True,
        "validated": True,
        "production_runtime_eligible": True,
        "shadow_only": False,
        "approval_id": "ADR-bridge-test",
        "approval_receipt_sha256": "c" * 64,
    }
    approved_core.pop("artifact_sha256")
    approved = {**approved_core, "artifact_sha256": artifact_sha256(approved_core)}
    pmf = residual_pmf_for_date(approved, event_date="2025-04-30")
    assert sum(pmf.values()) == pytest.approx(1.0)
    contract = materialize_closed_resolution_contract(
        target_contract=_target(), bridge_artifact=approved, physical_support_c=[30, 31]
    )
    bridge = build_resolution_bridge(
        physical_high_distribution={30: 0.6, 31: 0.4}, resolution_contract=contract
    )
    assert bridge["ok"] is True
    assert bridge["bridge_closed"] is True
    assert sum(bridge["market_bucket_probabilities"].values()) == pytest.approx(1.0)

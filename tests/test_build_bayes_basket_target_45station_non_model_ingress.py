from __future__ import annotations

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.station_registry import coordinate_registry_manifest
from scripts.build_bayes_basket_target_45station_non_model_ingress import (
    MODEL_FIELDS,
    build_non_model_packet,
)


def _packet() -> dict:
    stations = coordinate_registry_manifest()["station_ids"]
    prior = "prior-v1"
    identity = {
        "schema": "bayes_basket_target_artifact_ingress_v1",
        "station_ids": stations,
        "global_priors": [{"global_prior_id": prior}],
        "policy_artifacts": [
            {
                "station_id": station,
                "artifact_id": f"{station}-{family}",
            }
            for station in stations
            for family in ("target", "reserve", "cadence")
        ],
        "policy_activations": [
            {
                "station_id": station,
                "stage": stage,
                "global_prior_id": prior,
                "target_artifact_id": f"{station}-target",
                "reserve_artifact_id": f"{station}-reserve",
                "cadence_artifact_id": f"{station}-cadence",
            }
            for station in stations
            for stage in ("P0", "P0-M", "P1")
        ],
        "cash_phase_policies": [
            {"station_id": station} for station in stations
        ],
        "cash_phase_authority_manifests": [
            {"station_id": station} for station in stations
        ],
        "calibration_artifacts": [{"invalid": "old-model-contract"}],
        "ensemble_selection_evidence": [
            {"method_version": "incompatible"}
        ],
        "ensemble_artifacts": [{"invalid": "old-model-contract"}],
        "calibration_activations": [{"invalid": "old-model-contract"}],
        "calibration_promotions": [],
        "policy_promotions": [],
        "execution_mode": "production_no_live",
        "policy_authority_scope": "PRODUCTION_NO_LIVE_CANDIDATE",
        "promoted": False,
        "live_order_authorized": False,
        "live_submit_authorized": False,
        "places_orders": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    return {**identity, "packet_sha256": content_sha256(identity)}


def test_non_model_packet_strips_all_model_rows_and_keeps_policy_scope() -> None:
    result = build_non_model_packet(_packet())

    assert len(result["station_ids"]) == 45
    assert len(result["policy_artifacts"]) == 135
    assert len(result["policy_activations"]) == 135
    assert all(result[field] == [] for field in MODEL_FIELDS)
    assert result["live_submit_authorized"] is False
    assert result["calls_order_gateway"] is False
    identity = dict(result)
    supplied = identity.pop("packet_sha256")
    assert supplied == content_sha256(identity)


def test_non_model_packet_rejects_broken_policy_binding() -> None:
    packet = _packet()
    packet["policy_activations"][0]["target_artifact_id"] = "missing"
    packet["packet_sha256"] = content_sha256(
        {key: value for key, value in packet.items() if key != "packet_sha256"}
    )

    with pytest.raises(
        ContractError,
        match="non_model_ingress_policy_binding_invalid",
    ):
        build_non_model_packet(packet)

from __future__ import annotations

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from scripts.slice_bayes_basket_target_artifact_packet import slice_packet


def _packet() -> dict[str, object]:
    identity: dict[str, object] = {
        "schema": "bayes_basket_target_artifact_ingress_v1",
        "execution_mode": "production_no_live",
        "station_ids": ["RKPK", "EGLC"],
        "global_priors": [
            {"global_prior_id": "global-v1"},
        ],
        "policy_artifacts": [
            {
                "station_id": station,
                "artifact_id": f"{station}-{stage}",
            }
            for station in ("RKPK", "EGLC")
            for stage in ("P0", "P0-M", "P1")
        ],
        "policy_activations": [
            {
                "station_id": station,
                "activation_id": f"{station}-{stage}",
                "global_prior_id": "global-v1",
            }
            for station in ("RKPK", "EGLC")
            for stage in ("P0", "P0-M", "P1")
        ],
        "cash_phase_policies": [
            {"station_id": "RKPK", "policy_version": "cash-rkpk"},
            {"station_id": "EGLC", "policy_version": "cash-eglc"},
        ],
        "cash_phase_authority_manifests": [
            {"station_id": "RKPK", "manifest_sha256": "a" * 64},
            {"station_id": "EGLC", "manifest_sha256": "b" * 64},
        ],
        "calibration_artifacts": [],
        "ensemble_selection_evidence": [],
        "ensemble_artifacts": [],
        "calibration_activations": [],
        "calibration_promotions": [],
        "policy_promotions": [],
        "live_order_authorized": False,
        "live_submit_authorized": False,
        "places_orders": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    return {**identity, "packet_sha256": content_sha256(identity)}


def test_slice_packet_keeps_only_exact_station_and_shared_prior() -> None:
    result = slice_packet(_packet(), station_id="RKPK")

    assert result["station_ids"] == ["RKPK"]
    assert {
        row["station_id"]
        for field in (
            "policy_artifacts",
            "policy_activations",
            "cash_phase_policies",
            "cash_phase_authority_manifests",
        )
        for row in result[field]
    } == {"RKPK"}
    assert result["global_priors"] == [{"global_prior_id": "global-v1"}]
    assert result["vertical_slice"]["batch_expansion_authorized"] is False
    identity = dict(result)
    supplied = identity.pop("packet_sha256")
    assert supplied == content_sha256(identity)


def test_slice_packet_rejects_tampered_source_packet() -> None:
    packet = _packet()
    packet["station_ids"] = ["RKPK"]
    with pytest.raises(ValueError, match="packet_sha256_mismatch"):
        slice_packet(packet, station_id="RKPK")

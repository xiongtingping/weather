from __future__ import annotations

from copy import deepcopy

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from scripts.merge_bayes_basket_target_no_live_artifact_packets import merge_packets


def _cash() -> dict:
    identity = {
        "schema": "bayes_basket_target_artifact_ingress_v1",
        "producer_schema": "bayes_basket_target_no_live_candidate_artifacts_v1",
        "policy_authority_scope": "PRODUCTION_NO_LIVE_CANDIDATE",
        "execution_mode": "production_no_live",
        "station_ids": ["ZBAA"],
        "global_priors": [],
        "policy_artifacts": [],
        "policy_activations": [],
        "cash_phase_policies": [],
        "cash_phase_authority_manifests": [],
        "policy_promotions": [],
        "calibration_artifacts": [],
        "ensemble_selection_evidence": [],
        "ensemble_artifacts": [],
        "calibration_activations": [],
        "calibration_promotions": [],
        "promoted": False,
        "live_order_authorized": False,
        "live_submit_authorized": False,
        "places_orders": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    return {**identity, "packet_sha256": content_sha256(identity)}


def _calibration() -> dict:
    return {
        "schema": "bayes_basket_target_artifact_ingress_v1",
        "authority_receipt": {
            "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
            "formal_promotion_created": False,
            "live_order_authorized": False,
        },
        "calibration_artifacts": [{"calibration_artifact_id": "cal-1"}],
        "ensemble_selection_evidence": [{"selection_evidence_id": "sel-1"}],
        "ensemble_artifacts": [{"ensemble_artifact_id": "ens-1"}],
        "calibration_activations": [{"activation_id": "act-1"}],
        "calibration_promotions": [],
        "global_priors": [],
        "policy_artifacts": [],
        "policy_activations": [],
        "cash_phase_policies": [],
        "cash_phase_authority_manifests": [],
        "policy_promotions": [],
    }


def test_merge_is_content_addressed_and_keeps_all_live_boundaries_closed() -> None:
    first = merge_packets(_cash(), _calibration())
    second = merge_packets(_cash(), _calibration())

    assert first == second
    assert first["packet_sha256"] == content_sha256({
        key: value for key, value in first.items() if key != "packet_sha256"
    })
    assert first["calibration_artifacts"] == [{"calibration_artifact_id": "cal-1"}]
    assert first["calibration_promotions"] == []
    assert first["live_submit_authorized"] is False
    assert first["calls_order_gateway"] is False
    assert first["orders_submitted"] == 0


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda packet: packet["authority_receipt"].update(authority_scope="LIVE"), "authority_scope_invalid"),
        (lambda packet: packet["authority_receipt"].update(formal_promotion_created=True), "formal_promotion_forbidden"),
        (lambda packet: packet["calibration_promotions"].append({"promotion_id": "x"}), "forbidden_policy_or_promotion"),
        (lambda packet: packet["cash_phase_policies"].append({"policy_version": "x"}), "forbidden_policy_or_promotion"),
    ],
)
def test_merge_rejects_authority_expansion_and_cross_domain_rows(mutation, reason: str) -> None:
    calibration = deepcopy(_calibration())
    mutation(calibration)

    with pytest.raises(ContractError, match=reason):
        merge_packets(_cash(), calibration)


def test_merge_rejects_tampered_cash_packet() -> None:
    cash = _cash()
    cash["station_ids"] = ["ZBAA", "ZUUU"]

    with pytest.raises(ContractError, match="packet_sha256_mismatch"):
        merge_packets(cash, _calibration())

from __future__ import annotations

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from scripts.merge_bayes_basket_probability_evidence_packets import (
    merge_packets,
)


def _packet(suffix: str) -> dict[str, object]:
    artifact_id = f"artifact-{suffix}"
    identity = {
        "schema": "bayes_basket_probability_evidence_ingress_v1",
        "artifacts": [{
            "artifact_id": artifact_id,
            "artifact_kind": "P0_HISTORICAL_META",
            "station_id": "RKPK",
        }],
        "promotions": [{
            "promotion_id": f"promotion-{suffix}",
            "artifact_id": artifact_id,
            "artifact_kind": "P0_HISTORICAL_META",
        }],
        "activations": [{
            "activation_id": f"activation-{suffix}",
            "artifact_id": artifact_id,
            "artifact_kind": "P0_HISTORICAL_META",
            "station_id": "RKPK",
        }],
    }
    return {
        **identity,
        "packet_sha256": content_sha256(identity),
    }


def test_merge_is_deterministic_and_content_addressed() -> None:
    first = merge_packets(
        [_packet("a"), _packet("b")],
        station_id="RKPK",
    )
    replay = merge_packets(
        [_packet("b"), _packet("a")],
        station_id="RKPK",
    )
    assert first == replay
    assert len(first["artifacts"]) == 2
    assert first["packet_sha256"] == content_sha256({
        key: value
        for key, value in first.items()
        if key != "packet_sha256"
    })


def test_merge_rejects_duplicate_or_cross_station_rows() -> None:
    with pytest.raises(
        ContractError,
        match="probability_evidence_merge_artifacts_duplicate",
    ):
        merge_packets(
            [_packet("a"), _packet("a")],
            station_id="RKPK",
        )
    cross = _packet("x")
    cross["artifacts"][0]["station_id"] = "KORD"
    with pytest.raises(
        ContractError,
        match="probability_evidence_merge_station_scope_mismatch",
    ):
        merge_packets([cross], station_id="RKPK")

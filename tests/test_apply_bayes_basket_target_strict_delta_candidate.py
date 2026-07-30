from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from bayes_basket_target.contracts import ContractError
from scripts.apply_bayes_basket_target_strict_delta_candidate import (
    ROOT,
    build_authority,
)


def _protocol() -> dict[str, object]:
    path = (
        ROOT
        / "config/trading/bayes_basket_target_strict_delta_RKPK_forward_candidate_v1.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def _sha(value: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def test_candidate_packet_is_pre_registered_and_never_claims_oos() -> None:
    protocol = _protocol()
    artifact, promotion, activation = build_authority(
        protocol,
        protocol_sha256=_sha(protocol),
    )

    assert artifact["authority_scope"] == "PRODUCTION_NO_LIVE_CANDIDATE"
    assert artifact["oos_gate_passed"] is False
    assert artifact["sample_n"] == artifact["holdout_n"] == 0
    assert artifact["oos_metrics"] == {}
    assert promotion["decision"] == "CANDIDATE"
    assert activation["authority_scope"] == "PRODUCTION_NO_LIVE_CANDIDATE"
    assert protocol["authorizes_order"] is False
    assert protocol["live_submit_authorized"] is False
    assert protocol["calls_order_gateway"] is False


def test_candidate_packet_fails_on_negative_evidence_hash_drift() -> None:
    protocol = _protocol()
    protocol["negative_evidence_file_sha256"] = "0" * 64

    with pytest.raises(
        ContractError,
        match="negative_evidence_drift",
    ):
        build_authority(
            protocol,
            protocol_sha256=_sha(protocol),
        )


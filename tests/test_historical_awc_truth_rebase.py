from __future__ import annotations

import pytest

from bayes_basket_target.canonical_contract import CONTRACT_VERSION, SCHEMA
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.historical_awc_truth_rebase import (
    rebase_historical_awc_truth_contract,
)


def _truth(event_date: str, *, value: float, committed: str, seed: str) -> dict[str, object]:
    identity = {
        "truth_id": f"awc-historical-truth-{seed[:32]}",
        "station_id": "KJFK",
        "event_date": event_date,
        "truth_revision_no": 0,
        "truth_value": value,
        "truth_content_sha256": seed,
        "canonical_committed_at_utc": committed,
        "station_day_end_utc": f"{event_date}T23:59:59Z",
        "closed_day": True,
        "source_lane": "AWC",
        "source_role": "AWC_HISTORICAL_REPLAY_INGRESS",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "settlement_timezone": "America/New_York",
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _contract(truths: list[dict[str, object]]) -> dict[str, object]:
    identity = {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "as_of_utc": "2026-08-04T00:00:00Z",
        "builder_id": "truth.test",
        "model_facts": [],
        "ensemble_member_facts": [],
        "native_arrival_facts": [],
        "ensemble_member_receipt_manifest_facts": [],
        "ensemble_member_arrival_binding_facts": [],
        "truth_facts": truths,
        "residual_facts": [],
        "activation_facts": [],
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def test_rebase_preserves_existing_identity_and_appends_only_new_scope() -> None:
    existing = _truth(
        "2026-07-01", value=31.0, committed="2026-08-03T12:00:00Z", seed="a" * 64
    )
    proposed_existing = _truth(
        "2026-07-01", value=31.0, committed="2026-08-03T20:59:00Z", seed="b" * 64
    )
    proposed_new = _truth(
        "2026-07-02", value=32.0, committed="2026-08-03T20:59:00Z", seed="c" * 64
    )
    contract, receipt = rebase_historical_awc_truth_contract(
        _contract([proposed_existing, proposed_new]),
        existing_truth_facts=[existing],
    )
    assert contract["truth_facts"][0] == existing
    assert contract["truth_facts"][1] == proposed_new
    assert receipt["preserved_existing_truth_fact_count"] == 1
    assert receipt["append_candidate_truth_fact_count"] == 1
    assert receipt["boundary"]["overwrites_existing_truth"] is False


def test_rebase_rejects_existing_scope_semantic_change() -> None:
    existing = _truth(
        "2026-07-01", value=31.0, committed="2026-08-03T12:00:00Z", seed="a" * 64
    )
    proposed = _truth(
        "2026-07-01", value=32.0, committed="2026-08-03T20:59:00Z", seed="b" * 64
    )
    with pytest.raises(ContractError, match="semantic_conflict"):
        rebase_historical_awc_truth_contract(
            _contract([proposed]), existing_truth_facts=[existing]
        )


def test_rebase_rejects_dropping_active_existing_scope() -> None:
    existing = _truth(
        "2026-07-01", value=31.0, committed="2026-08-03T12:00:00Z", seed="a" * 64
    )
    proposed = _truth(
        "2026-07-02", value=32.0, committed="2026-08-03T20:59:00Z", seed="b" * 64
    )
    with pytest.raises(ContractError, match="existing_scope_missing"):
        rebase_historical_awc_truth_contract(
            _contract([proposed]), existing_truth_facts=[existing]
        )

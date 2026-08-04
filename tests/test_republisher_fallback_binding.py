from __future__ import annotations

from copy import deepcopy

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.republisher_fallback_binding import (
    AUTHORITY_MODE,
    SOURCE_ROLE,
    TEMPORAL_SCOPE,
    build_republisher_fallback_binding,
    validate_republisher_fallback_binding,
)


def _prediction(contract_id: str | None) -> dict[str, object]:
    return {
        "receipt_class": "HISTORICAL_ASOF_REPUBLISHER_RECEIPT",
        "temporal_scope": TEMPORAL_SCOPE,
        "historical_oos_contract_id": contract_id,
    }


def _candidate(
    station_id: str,
    *,
    train_n: int = 20,
    holdout_n: int = 8,
    future_event_count: int = 0,
    state: str = "PRODUCTION_NO_LIVE_OOS_PASS",
    contract_ids: list[str] | None = None,
    predictions: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    ids = contract_ids if contract_ids is not None else [
        f"oos-{station_id}-{index}" for index in range(holdout_n)
    ]
    preds = predictions if predictions is not None else [
        _prediction(contract_id) for contract_id in ids
    ]
    identity: dict[str, object] = {
        "station_id": station_id,
        "state": state,
        "train_n": train_n,
        "holdout_n": holdout_n,
        "future_event_count": future_event_count,
        "oos_event_temporal_scope": TEMPORAL_SCOPE,
        "native_arrival_index_eligible": False,
        "p1_eligible": False,
        "formal_p0_eligible": False,
        "train_start_date": "2026-06-01",
        "train_end_date": "2026-06-20",
        "holdout_start_date": "2026-06-21",
        "holdout_end_date": "2026-06-28",
        "historical_oos_contract_ids": ids,
        "predictions": preds,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _report(
    model_id: str,
    candidate: dict[str, object],
    *,
    p1_authority_written: bool = False,
) -> dict[str, object]:
    identity: dict[str, object] = {
        "schema": "deterministic_republisher_strict_oos_v1",
        "as_of_utc": "2026-08-02T12:00:00Z",
        "model_id": model_id,
        "provider_id": f"OPEN_METEO_{model_id}",
        "source_role": SOURCE_ROLE,
        "transport_provider_id": "OPEN_METEO",
        "formal_p0_eligible": False,
        "state": "READY_CANDIDATES",
        "station_count": 1,
        "candidate_count": 1,
        "truth_count": 8,
        "curve_count": 8,
        "missing_truth_scopes": [],
        "shortfalls": [],
        "capture_receipt_sha256s": [],
        "candidates": [candidate],
        "oos_policy": {
            "temporal_scope": TEMPORAL_SCOPE,
            "future_event_count": 0,
            "native_arrival_index_written": False,
            "p1_authority_written": p1_authority_written,
            "formal_live_authority_written": False,
            "production_database_written": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "min_train_days": 20,
            "min_holdout_days": 8,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _regional_audit() -> dict[str, object]:
    identity: dict[str, object] = {
        "schema": "regional_model_candidate_closure_audit_v1",
        "generated_at_utc": "2026-08-02T12:00:00Z",
        "fail_closed": True,
        "icon_scope_classification": {},
        "input_evidence": [],
        "lineage_policy": {},
        "readiness": "candidate_only",
        "models": [
            {
                "model_key": "ICON-EU",
                "configured_model_parameter": "icon_eu",
                "source_class": SOURCE_ROLE,
                "fail_closed_reasons": ["strict_oos_absent"],
                "totals": {
                    "strict_oos_pass": 0,
                    "open_meteo_republisher_member_runs": 0,
                },
            }
        ],
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _build(*reports: dict[str, object]) -> dict[str, object]:
    return build_republisher_fallback_binding(
        deterministic_reports=list(reports),
        regional_audit=_regional_audit(),
        as_of_utc="2026-08-02T12:00:00Z",
    )


def test_pass_reports_bind_and_regional_model_remains_candidate_only() -> None:
    binding = _build(
        _report("AIFS", _candidate("RKPK")),
        _report("ICON", _candidate("ZBAA")),
    )

    validate_republisher_fallback_binding(binding)
    assert binding["binding_count"] == 2
    assert binding["bound_model_ids"] == ["AIFS", "ICON"]
    assert binding["candidate_only_model_ids"] == ["ICON-EU"]
    assert binding["formal_p0_eligible"] is False
    assert binding["native_arrival_index_written"] is False
    assert binding["p1_authority_written"] is False
    assert all(row["activation_state"] == "BOUND_NO_LIVE_FALLBACK" for row in binding["bindings"])
    assert all(row["source_role"] == SOURCE_ROLE for row in binding["bindings"])


def test_report_boundary_cannot_claim_p1_authority() -> None:
    report = _report("AIFS", _candidate("RKPK"), p1_authority_written=True)
    with pytest.raises(ContractError, match="boundary"):
        _build(report)


def test_candidate_with_missing_prediction_contract_is_rejected() -> None:
    candidate = _candidate("RKPK")
    candidate["predictions"] = [
        _prediction(None) if index == 0 else prediction
        for index, prediction in enumerate(candidate["predictions"])
    ]
    candidate["content_sha256"] = content_sha256(
        {key: value for key, value in candidate.items() if key != "content_sha256"}
    )
    with pytest.raises(ContractError, match="prediction_lineage"):
        _build(_report("AIFS", candidate))


def test_short_and_future_candidates_never_bind() -> None:
    short = _report("AIFS", _candidate("RKPK", train_n=19))
    future = _report("ICON", _candidate("ZBAA", future_event_count=1))
    binding = _build(short, future)
    validate_republisher_fallback_binding(binding)
    assert binding["binding_count"] == 0
    assert binding["state"] == "PENDING_STRICT_OOS"
    assert binding["bound_model_ids"] == []


def test_emitted_pointer_tampering_is_detected() -> None:
    binding = _build(_report("AIFS", _candidate("RKPK")))
    tampered = deepcopy(binding)
    tampered["bindings"][0]["p1_eligible"] = True
    tampered["content_sha256"] = content_sha256(
        {key: value for key, value in tampered.items() if key != "content_sha256"}
    )
    with pytest.raises(ContractError, match="row_boundary"):
        validate_republisher_fallback_binding(tampered)


from __future__ import annotations

from datetime import date, timedelta

from resolution_bridge_calibration_runtime import fit_resolution_bridge_candidate
from resolution_bridge_promotion_runtime import approval_receipt_sha256, compile_resolution_bridge_promotion


def _candidate() -> dict:
    start = date(2025, 1, 1)
    pairs = []
    for index in range(120):
        x = 20 + index % 15
        pairs.append({
            "state": "available_for_bridge_calibration_only",
            "event_date": (start + timedelta(days=index)).isoformat(),
            "authority_contract_id": "zuuu_amsc_wunderground_daily_high_v4",
            "final_physical_high_c": x,
            "settlement_scalar_c": x + (-1, 0, 0, 0, 1)[index % 5],
            "observed_x_s_pair_sha256": f"{index:064x}",
            "physical_source": {"source_id": "amsc_zuuu_scheduled_metar_daily_max_v2"},
            "settlement_source": {"source_id": "wunderground_zuuu_historical_observation_daily_high_final_v3"},
        })
    return fit_resolution_bridge_candidate(pairs, authority_contract_sha256="a" * 64)["candidate_artifact"]


def _receipt(candidate: dict) -> dict:
    core = {
        "schema": "resolution_bridge_promotion_approval_v1",
        "state": "accepted",
        "candidate_artifact_sha256": candidate["artifact_sha256"],
        "coverage_review_accepted": True,
        "oos_review_accepted": True,
        "approval_id": "ADR-XSB-0001",
        "reviewer": "operator",
        "approved_at": "2026-08-01T00:00:00Z",
    }
    return {**core, "approval_receipt_sha256": approval_receipt_sha256(core)}


def test_reviewed_candidate_compiles_orderless_production_artifact_and_pointer() -> None:
    candidate = _candidate()
    result = compile_resolution_bridge_promotion(
        candidate_artifact=candidate,
        approval_receipt=_receipt(candidate),
        artifact_path="config/artifacts/resolution_bridge/approved.json",
    )
    assert result["ok"] is True
    assert result["production_artifact"]["bridge_closed"] is True
    assert result["production_activation"]["authorizes_clob"] is False
    assert result["boundary"]["writes_files"] is False


def test_candidate_cannot_self_promote_without_independent_review_receipt() -> None:
    candidate = _candidate()
    result = compile_resolution_bridge_promotion(
        candidate_artifact=candidate,
        approval_receipt={},
        artifact_path="config/artifacts/resolution_bridge/approved.json",
    )
    assert result["ok"] is False
    assert "bridge_promotion_approval_not_accepted" in result["block_reasons"]

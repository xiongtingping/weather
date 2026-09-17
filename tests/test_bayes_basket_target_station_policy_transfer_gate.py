from __future__ import annotations

from pathlib import Path

import scripts.audit_station_policy_transfer_gate as transfer_gate


def _historical_oos_ready_fixture() -> dict[str, object]:
    return {
        "bundle": {
            "bundle_sha256": "a" * 64,
            "promotion_permitted": False,
            "scope": {"stage": "P0-M"},
            "station_count": 45,
            "artifact_count": 135,
            "activation_count": 45,
            "activations": [{"station_id": "RKPK"}],
        },
        "transfer": {
            "stations": [{"station_id": f"S{index}"} for index in range(8)],
            "method": {"not_measured": ["strategy PnL", "settlement win rate"]},
            "non_shenzhen_summary": {},
        },
        "transfer_validation": {"status": "PASS", "checks": {}},
        "strict_asof": {
            "status": "PASS",
            "windows": {"reports": [{}, {}, {}, {}]},
            "crossseason": {
                "lineage_complete": True,
                "all_four_seasons_present": True,
            },
        },
        "formula_validation": {
            "metrics": {
                "target_policy_holdout": {"rows": 1},
                "reserve_holdout": {"rows": 1},
                "cadence_holdout": {"rows": 1},
                "module_activation": {"target_policy": {"audit_fallback_triggered": False}},
            },
        },
        "utility": {
            "schema": "station_policy_exact_stage_scorecard_v1",
            "checks": {
                "policy_utility_metric_binding_complete": True,
                "policy_utility_score_rows_emitted": True,
                "historical_utility_oos_closed_awc_only": True,
                "observational_candidate_not_oos_blocks_production_no_live": False,
            },
            "input_counts": {
                "score_rows": {
                    stage: {family: 1 for family in ("target", "reserve", "cadence")}
                    for stage in ("P0", "P0-M", "P1")
                },
            },
        },
    }


def test_missing_forward_scorecard_is_not_an_oos_or_promotion_gate(
    monkeypatch,
) -> None:
    """Forward samples are observational and cannot delay promotion."""

    fixture = _historical_oos_ready_fixture()
    paths = {
        Path("bundle"): fixture["bundle"],
        Path("transfer"): fixture["transfer"],
        Path("transfer_validation"): fixture["transfer_validation"],
        Path("strict_asof"): fixture["strict_asof"],
        Path("formula_validation"): fixture["formula_validation"],
        Path("utility"): fixture["utility"],
    }
    monkeypatch.setattr(transfer_gate, "_load", lambda path: paths[path])
    monkeypatch.setattr(transfer_gate, "_sha", lambda _path: "b" * 64)
    monkeypatch.setattr(
        transfer_gate, "assert_candidate_bundle_boundary", lambda _bundle: None
    )

    receipt = transfer_gate.build_gate(
        bundle_path=Path("bundle"),
        transfer_path=Path("transfer"),
        transfer_validation_path=Path("transfer_validation"),
        strict_asof_path=Path("strict_asof"),
        formula_validation_path=Path("formula_validation"),
        forward_scorecard_path=None,
        exact_stage_scorecard_path=Path("utility"),
    )

    assert receipt["status"] == "EVIDENCE_COMPLETE_NO_LIVE_REVIEW"
    assert receipt["production_no_live_historical_oos_permitted"] is True
    assert receipt["historical_oos_hard_gate_blockers"] == []
    assert receipt["formal_live_promotion_gate_passed"] is True
    assert "future_scorecard_missing_or_invalid_formal_live_promotion_only" not in receipt["blockers"]
    assert (
        "future_scorecard_missing_or_invalid_formal_live_promotion_only"
        not in receipt["historical_oos_hard_gate_blockers"]
    )


def test_valid_forward_scorecard_cannot_replace_historical_direct_utility(
    monkeypatch,
) -> None:
    fixture = _historical_oos_ready_fixture()
    utility = dict(fixture["utility"])
    utility["checks"] = dict(utility["checks"])
    utility["checks"]["policy_utility_score_rows_emitted"] = False
    paths = {
        Path("bundle"): fixture["bundle"],
        Path("transfer"): fixture["transfer"],
        Path("transfer_validation"): fixture["transfer_validation"],
        Path("strict_asof"): fixture["strict_asof"],
        Path("formula_validation"): fixture["formula_validation"],
        Path("utility"): utility,
        Path("forward"): {},
    }
    monkeypatch.setattr(transfer_gate, "_load", lambda path: paths[path])
    monkeypatch.setattr(transfer_gate, "_sha", lambda _path: "b" * 64)
    monkeypatch.setattr(
        transfer_gate, "assert_candidate_bundle_boundary", lambda _bundle: None
    )
    monkeypatch.setattr(
        transfer_gate,
        "validate_station_policy_forward_scorecard",
        lambda *_args, **_kwargs: {
            "valid": True,
            "checks": {},
            "scope": {},
            "families": {},
        },
    )

    receipt = transfer_gate.build_gate(
        bundle_path=Path("bundle"),
        transfer_path=Path("transfer"),
        transfer_validation_path=Path("transfer_validation"),
        strict_asof_path=Path("strict_asof"),
        formula_validation_path=Path("formula_validation"),
        forward_scorecard_path=Path("forward"),
        exact_stage_scorecard_path=Path("utility"),
    )

    assert receipt["checks"]["station_policy_scorecard_valid"] is True
    assert receipt["checks"]["forward_scorecard_observational_only"] is True
    assert receipt["checks"]["forward_scorecard_not_used_for_oos_or_promotion"] is True
    assert receipt["formal_live_promotion_gate_passed"] is False
    assert "utility_execution_evidence_unbound_formal_live_promotion_only" in receipt["blockers"]
    assert "future_scorecard_missing_or_invalid_formal_live_promotion_only" not in receipt["blockers"]

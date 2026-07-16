from scripts.forecast_source_weight_release_gate import build_forecast_source_weight_release_gate


def _artifact():
    return {
        "schema": "forecast_source_contextual_weight_artifact_v1",
        "source_data_policy": "external_only",
        "artifact_sha256": "artifact-1",
        "production_ready": False,
        "runtime_integrated": False,
        "gates": {"promotion_eligible": False},
        "guards": {"changes_parameters": False},
    }


def _shadow():
    return {
        "schema": "forecast_source_weight_shadow_evaluation_v1",
        "evaluation_manifest_sha256": "eval-1",
        "release_gate": {
            "strict_oos_evaluated": True,
            "nll_improved": True,
            "rps_improved": True,
            "statistical_gate_passed": True,
            "production_apply_allowed": False,
            "curve_incremental_statistical_gate_passed": True,
            "curve_production_promotion_proposal_eligible": True,
        },
        "guards": {
            "market_sources_excluded": True,
            "weak_sources_never_hard_lock": True,
            "provider_family_dedup_enabled": True,
            "champion_matches_production_legacy_runtime": True,
            "runtime_requests_never_change_parameters": True,
            "amsc_curve_reliability_consumed_once": True,
            "changes_parameters": False,
        },
    }


def _ablation():
    return {"schema": "forecast_source_contextual_ablation_v1", "selected_specification": "global"}


def test_not_configured_is_safe_and_does_not_block_static_champion():
    report = build_forecast_source_weight_release_gate()
    assert report["ok"] is True
    assert report["state"] == "not_configured"


def test_valid_contextual_candidate_can_only_continue_to_governed_proposal():
    report = build_forecast_source_weight_release_gate(artifact=_artifact(), shadow_evaluation=_shadow(), ablation=_ablation(), require_candidate=True, require_ablation=True)
    assert report["ok"] is True
    assert report["state"] == "ready_for_governed_proposal"
    assert report["next_boundary"] == "generate_proposal_without_apply"
    assert report["guards"]["changes_parameters"] is False


def test_curve_gate_is_independent_and_contract_enforced():
    shadow = _shadow()
    shadow["guards"]["amsc_curve_reliability_consumed_once"] = False
    report = build_forecast_source_weight_release_gate(
        artifact=_artifact(),
        shadow_evaluation=shadow,
        ablation=_ablation(),
        require_candidate=True,
        require_curve=True,
        require_ablation=True,
    )
    assert report["ok"] is False
    assert any(item["check"] == "curve_consumed_once" for item in report["blockers"])

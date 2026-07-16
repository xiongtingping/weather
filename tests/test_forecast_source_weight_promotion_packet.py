from scripts.forecast_source_weight_promotion_packet import build_promotion_packet


def _artifact(sha, weights):
    return {
        "schema": "forecast_source_contextual_weight_artifact_v1",
        "source_data_policy": "external_only",
        "artifact_sha256": sha,
        "global_weights": weights,
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
        },
        "guards": {
            "market_sources_excluded": True,
            "weak_sources_never_hard_lock": True,
            "provider_family_dedup_enabled": True,
            "champion_matches_production_legacy_runtime": True,
            "runtime_requests_never_change_parameters": True,
            "changes_parameters": False,
        },
    }


def _ablation():
    return {"schema": "forecast_source_contextual_ablation_v1", "selected_specification": "global"}


def test_packet_is_proposed_but_never_applies_and_has_rollback_bindings():
    packet = build_promotion_packet(
        champion_artifact=_artifact("old", {"a": 0.5, "b": 0.5}),
        challenger_artifact=_artifact("new", {"a": 0.54, "b": 0.46}),
        shadow_evaluation=_shadow(),
        ablation=_ablation(),
    )
    assert packet["state"] == "proposed"
    assert packet["promotion_proposal_eligible"] is True
    assert packet["production_apply_allowed"] is False
    request = packet["parameter_change_request"]
    assert request["state"] == "proposed"
    assert request["governance_state"]["can_apply"] is False
    assert request["artifact_bindings"]["parent_artifact_sha256"] == "old"
    assert request["rollback_plan"]["rollback_artifact_sha256"] == "old"
    assert request["rollback_plan"]["requires_retraining"] is False
    assert request["measurement_plan"]["windows_days"] == [7, 30, 90]


def test_packet_blocks_single_step_weight_jump_over_five_points():
    packet = build_promotion_packet(
        champion_artifact=_artifact("old", {"a": 0.5, "b": 0.5}),
        challenger_artifact=_artifact("new", {"a": 0.7, "b": 0.3}),
        shadow_evaluation=_shadow(),
        ablation=_ablation(),
    )
    assert packet["promotion_proposal_eligible"] is False
    assert "single_promotion_weight_change_exceeds_governance_cap" in packet["blockers"]


def test_packet_hash_is_deterministic():
    kwargs = {
        "champion_artifact": _artifact("old", {"a": 0.5, "b": 0.5}),
        "challenger_artifact": _artifact("new", {"a": 0.52, "b": 0.48}),
        "shadow_evaluation": _shadow(),
        "ablation": _ablation(),
    }
    assert build_promotion_packet(**kwargs)["packet_sha256"] == build_promotion_packet(**kwargs)["packet_sha256"]

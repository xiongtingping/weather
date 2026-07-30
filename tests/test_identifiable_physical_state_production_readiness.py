from scripts.identifiable_physical_state_production_readiness import (
    evaluate_release_readiness,
)


def _replay(*, gates: dict[str, bool], diagnostics: dict[str, bool]) -> dict:
    return {
        "ok": True,
        "promotion_gates": gates,
        "non_blocking_diagnostics": diagnostics,
    }


def test_nll_crps_cannot_block_identifiable_physical_release() -> None:
    result = evaluate_release_readiness(
        candidate_artifacts_valid=True,
        observation_identifiability_boundary_declared=True,
        state_parameter_draws_bound_to_calibration=True,
        additive_schema_plan_valid=True,
        calibration={"ok": True},
        replay_report=_replay(
            gates={
                "minimum_oos_aligned_events_100": True,
                "minimum_independent_replayed_days_30": True,
                "strict_positive_actual_support_rate_one": True,
                "p95_compose_under_3000ms": True,
            },
            diagnostics={
                "no_worse_mean_nll_than_legacy": False,
                "no_worse_mean_crps_than_legacy": False,
            },
        ),
        zero_order={"ok": True},
        runtime_closure={"ok": True},
        current_production={"ok": True},
    )
    assert result["promotion_ready"] is True
    assert not any(
        "nll" in key or "crps" in key for key in result["blocking_gates"]
    )
    assert result["non_blocking_diagnostics"]["no_worse_mean_nll_than_legacy"] is False
    assert result["non_blocking_diagnostics"]["no_worse_mean_crps_than_legacy"] is False
    assert result["promotion_policy"]["strict_oos_sample_volume"] == "14_training_days_4_strict_oos_days"
    assert result["promotion_policy"]["strict_oos_sample_volume_production_gate"] is False
    assert result["promotion_policy"]["production_unblocked_by_strict_oos_sample_volume"] is True
    assert result["non_blocking_diagnostics"]["research_evidence_gates"] == {
        "state_parameter_draws_bound_to_calibration": True,
        "strict_asof_parameter_calibration_ready": True,
        "canonical_replay_complete": True,
        "canonical_replay_blocking_gates_pass": True,
    }


def test_strict_asof_calibration_and_replay_volume_are_non_blocking() -> None:
    result = evaluate_release_readiness(
        candidate_artifacts_valid=True,
        observation_identifiability_boundary_declared=True,
        state_parameter_draws_bound_to_calibration=False,
        additive_schema_plan_valid=True,
        calibration={"ok": False},
        replay_report=_replay(
            gates={
                "minimum_oos_aligned_events_100": True,
                "minimum_independent_replayed_days_30": False,
                "strict_positive_actual_support_rate_one": True,
                "p95_compose_under_3000ms": True,
            },
            diagnostics={
                "no_worse_mean_nll_than_legacy": True,
                "no_worse_mean_crps_than_legacy": True,
            },
        ),
        zero_order={"ok": True},
        runtime_closure={"ok": True},
        current_production={"ok": True},
    )
    assert result["promotion_ready"] is True
    assert "strict_asof_parameter_calibration_ready" not in result["blocking_gates"]
    assert "state_parameter_draws_bound_to_calibration" not in result["blocking_gates"]
    assert "canonical_replay_blocking_gates_pass" not in result["blocking_gates"]
    assert result["research_evidence"] == {
        "state_parameter_draws_bound_to_calibration": False,
        "strict_asof_parameter_calibration_ready": False,
        "canonical_replay_complete": True,
        "canonical_replay_blocking_gates_pass": False,
    }

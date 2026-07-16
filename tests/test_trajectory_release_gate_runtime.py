from datetime import date, timedelta

from trajectory_release_gate_runtime import (
    FROZEN_REGISTRY_SCHEMA,
    build_release_gate_audit,
)


def _samples(start: date, count: int):
    return [
        {
            "event_date": (start + timedelta(days=index)).isoformat(),
            "quality_grade": "A",
            "evaluation_eligible": True,
        }
        for index in range(count)
    ]


def _all_true_integrity():
    return {
        "hard_mask_violation_zero": True,
        "duplicate_valid_time_zero": True,
        "future_observation_leak_zero": True,
        "late_arrival_observation_leak_zero": True,
        "missing_default_fact_zero": True,
        "single_hpa_pressure_cutoff_false_positive_zero": True,
        "development_counterfactuals_pass": True,
        "ssot_audit_pass": True,
        "production_chain_audit_pass": True,
        "posterior_consistency_audit_pass": True,
    }


def _all_true_performance():
    return {
        "rps_improvement_at_least_5pct": True,
        "frozen_rps_direction_consistent": True,
        "frozen_relative_degradation_upper_bound_at_most_2pct": True,
        "no_material_log_loss_brier_tail_or_cutoff_degradation": True,
        "event_day_weighted": True,
        "time_and_sufficient_weather_strata_no_catastrophic_degradation": True,
    }


def test_gate_excludes_development_evidence_and_keeps_live_accumulation_open_before_registration():
    samples = _samples(date(2026, 8, 1), 39) + [
        {"event_date": "2026-07-06", "quality_grade": "A", "evaluation_eligible": True},
        {"event_date": "2026-07-09", "quality_grade": "A", "evaluation_eligible": True},
    ]
    report = build_release_gate_audit(
        samples=samples,
        evaluation={"event_day_count": 39, "performance_conclusion_allowed": True},
        candidate_policy_version="candidate-v1",
        as_of="2026-09-09T00:00:00Z",
    )

    assert report["qualified_a_event_day_count"] == 39
    assert report["development_evidence_a_event_dates"] == ["2026-07-06", "2026-07-09"]
    assert report["frozen_registration"]["state"] == "not_ready"
    assert "a_class_event_days_below_60" in report["release_blockers"]
    assert report["live_rollout"]["allowed_by_this_read_only_audit"] is True
    assert report["process_accumulation"]["a_class_event_days_are_live_rollout_blocker"] is False
    assert report["process_accumulation"]["parameter_learning_allowed_to_propose"] is True


def test_gate_requires_valid_20_day_frozen_registry_and_all_explicit_quality_gates():
    samples = _samples(date(2026, 8, 1), 60)
    all_dates = [row["event_date"] for row in samples]
    registry = {
        "schema": FROZEN_REGISTRY_SCHEMA,
        "candidate_policy_version": "candidate-v1",
        "training_a_event_dates": all_dates[:40],
        "frozen_event_dates": all_dates[40:],
    }
    report = build_release_gate_audit(
        samples=samples,
        evaluation={"event_day_count": 60, "paired_baseline_event_day_count": 60, "performance_conclusion_allowed": True},
        frozen_registry=registry,
        integrity_gates=_all_true_integrity(),
        performance_gates=_all_true_performance(),
        candidate_policy_version="candidate-v1",
        as_of="2026-10-01T00:00:00Z",
    )

    assert report["qualified_a_event_day_count"] == 60
    assert report["frozen_registry_valid"] is True
    assert report["frozen_a_event_day_count"] == 20
    assert report["release_eligible"] is True
    assert report["full_evidence_maturity_complete"] is True
    assert report["process_accumulation"]["stage"] == "evidence_maturity_complete"
    assert report["guards"]["releases_production_probability"] is False


def test_gate_rejects_development_evidence_in_frozen_registry():
    samples = _samples(date(2026, 8, 1), 60) + [
        {"event_date": "2026-07-06", "quality_grade": "A", "evaluation_eligible": True}
    ]
    dates = [row["event_date"] for row in samples if row["event_date"] != "2026-07-06"]
    registry = {
        "schema": FROZEN_REGISTRY_SCHEMA,
        "candidate_policy_version": "candidate-v1",
        "training_a_event_dates": dates[:40],
        "frozen_event_dates": ["2026-07-06", *dates[41:60]],
    }
    report = build_release_gate_audit(
        samples=samples,
        evaluation={"event_day_count": 60, "performance_conclusion_allowed": True},
        frozen_registry=registry,
        integrity_gates=_all_true_integrity(),
        performance_gates=_all_true_performance(),
        candidate_policy_version="candidate-v1",
    )

    assert report["frozen_registry_valid"] is False
    assert "frozen_holdout_registry_missing_or_invalid" in report["release_blockers"]


def test_insufficient_a_days_block_parameter_fitting_but_not_guarded_live_rollout():
    report = build_release_gate_audit(
        samples=_samples(date(2026, 8, 1), 2),
        evaluation={"event_day_count": 2, "performance_conclusion_allowed": False},
        candidate_policy_version="candidate-v1",
    )

    assert report["qualified_a_event_day_count"] == 2
    assert report["process_accumulation"]["stage"] == "structural_live_accumulation_no_data_fitted_parameters"
    assert report["process_accumulation"]["parameter_learning_allowed_to_propose"] is False
    assert report["live_rollout"]["allowed_by_this_read_only_audit"] is True
    assert "a_class_event_days_below_60" in report["evidence_maturity_gaps"]

"""Read-only evidence-maturity governance for the ZUUU daily-high rollout."""

from __future__ import annotations

from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "zuuu_daily_high_release_gate_audit_v2"
POLICY_VERSION = "zuuu_daily_high_release_gate_policy_v2_live_process_accumulation"
FROZEN_REGISTRY_SCHEMA = "zuuu_daily_high_frozen_holdout_registry_v1"
DEVELOPMENT_EVIDENCE_DATES = frozenset({"2026-07-06", "2026-07-09"})
MIN_A_DAYS_FOR_REGULARIZED_SHADOW = 30
MIN_A_DAYS_FOR_FROZEN_REGISTRATION = 40
MIN_A_DAYS_FOR_RELEASE = 60
REQUIRED_FROZEN_A_DAYS = 20


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _a_event_dates(samples: Sequence[Mapping[str, Any]], *, development_evidence_dates: set[str]) -> tuple[list[str], list[str]]:
    qualified: set[str] = set()
    development: set[str] = set()
    for sample in samples:
        row = _mapping(sample)
        if row.get("quality_grade") != "A" or row.get("evaluation_eligible") is not True:
            continue
        event_date = str(row.get("event_date") or "")
        if not event_date:
            continue
        if event_date in development_evidence_dates:
            development.add(event_date)
        else:
            qualified.add(event_date)
    return sorted(qualified), sorted(development)


def build_frozen_holdout_registration_packet(
    *,
    as_of: str,
    candidate_policy_version: str,
    qualified_a_event_dates: Sequence[str],
    development_evidence_dates: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Produce a no-write packet that pre-registers the *next* 20 A-class days."""

    development = {str(value) for value in (development_evidence_dates or DEVELOPMENT_EVIDENCE_DATES)}
    qualified = sorted({str(value) for value in qualified_a_event_dates if str(value) and str(value) not in development})
    ready = len(qualified) >= MIN_A_DAYS_FOR_FROZEN_REGISTRATION and bool(candidate_policy_version)
    return {
        "schema": FROZEN_REGISTRY_SCHEMA,
        "state": "ready_for_explicit_registration" if ready else "not_ready",
        "as_of": str(as_of or ""),
        "candidate_policy_version": str(candidate_policy_version or ""),
        "training_a_event_dates": qualified,
        "development_evidence_dates": sorted(development),
        "frozen_selection_policy": "next_20_new_a_class_event_days_after_registration_as_of",
        "required_frozen_a_days": REQUIRED_FROZEN_A_DAYS,
        "next_action": "explicitly_persist_registry_after_human_review" if ready else "continue_accumulating_non_development_a_class_event_days",
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_posterior": False,
            "places_orders": False,
            "changes_parameters": False,
        },
    }


def build_release_gate_audit(
    *,
    samples: Sequence[Mapping[str, Any]],
    evaluation: Mapping[str, Any] | None,
    frozen_registry: Mapping[str, Any] | None = None,
    integrity_gates: Mapping[str, Any] | None = None,
    performance_gates: Mapping[str, Any] | None = None,
    candidate_policy_version: str = "",
    as_of: str = "",
    development_evidence_dates: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Report evidence maturity without making A-day counts a live rollout gate.

    The live path performs per-snapshot candidate-contract, phase, hard-mask and
    fallback checks.  Event-day counts govern only whether data-driven
    parameters may be proposed and whether the rollout has enough independent
    evidence to graduate from guarded accumulation.
    """

    development = {str(value) for value in (development_evidence_dates or DEVELOPMENT_EVIDENCE_DATES)}
    qualified_a_days, development_a_days = _a_event_dates(samples, development_evidence_dates=development)
    registry = _mapping(frozen_registry)
    frozen_dates = sorted({str(value) for value in registry.get("frozen_event_dates") or [] if str(value)})
    frozen_a_dates = sorted(set(frozen_dates) & set(qualified_a_days))
    registry_valid = bool(
        registry.get("schema") == FROZEN_REGISTRY_SCHEMA
        and registry.get("candidate_policy_version") == str(candidate_policy_version or "")
        and not (set(frozen_dates) & development)
        and not (set(frozen_dates) & set(registry.get("training_a_event_dates") or []))
    )
    integrity = _mapping(integrity_gates)
    performance = _mapping(performance_gates)
    required_integrity = (
        "hard_mask_violation_zero",
        "duplicate_valid_time_zero",
        "future_observation_leak_zero",
        "late_arrival_observation_leak_zero",
        "missing_default_fact_zero",
        "single_hpa_pressure_cutoff_false_positive_zero",
        "development_counterfactuals_pass",
        "ssot_audit_pass",
        "production_chain_audit_pass",
        "posterior_consistency_audit_pass",
    )
    required_performance = (
        "rps_improvement_at_least_5pct",
        "frozen_rps_direction_consistent",
        "frozen_relative_degradation_upper_bound_at_most_2pct",
        "no_material_log_loss_brier_tail_or_cutoff_degradation",
        "event_day_weighted",
        "time_and_sufficient_weather_strata_no_catastrophic_degradation",
    )
    evidence_maturity_gaps: list[str] = []
    if len(qualified_a_days) < MIN_A_DAYS_FOR_RELEASE:
        evidence_maturity_gaps.append("a_class_event_days_below_60")
    if not registry_valid:
        evidence_maturity_gaps.append("frozen_holdout_registry_missing_or_invalid")
    if len(frozen_a_dates) < REQUIRED_FROZEN_A_DAYS:
        evidence_maturity_gaps.append("frozen_a_class_event_days_below_20")
    if _mapping(evaluation).get("performance_conclusion_allowed") is not True:
        evidence_maturity_gaps.append("evaluation_not_ready_for_performance_conclusion")
    for key in required_integrity:
        if integrity.get(key) is not True:
            evidence_maturity_gaps.append(f"integrity_gate_missing_or_failed:{key}")
    for key in required_performance:
        if performance.get(key) is not True:
            evidence_maturity_gaps.append(f"performance_gate_missing_or_failed:{key}")

    registration = build_frozen_holdout_registration_packet(
        as_of=as_of,
        candidate_policy_version=candidate_policy_version,
        qualified_a_event_dates=qualified_a_days,
        development_evidence_dates=sorted(development),
    )
    parameter_learning_allowed = len(qualified_a_days) >= MIN_A_DAYS_FOR_REGULARIZED_SHADOW
    full_evidence_maturity_complete = not evidence_maturity_gaps
    if len(qualified_a_days) < MIN_A_DAYS_FOR_REGULARIZED_SHADOW:
        accumulation_stage = "structural_live_accumulation_no_data_fitted_parameters"
    elif len(qualified_a_days) < MIN_A_DAYS_FOR_FROZEN_REGISTRATION:
        accumulation_stage = "regularized_parameter_proposals_only"
    elif not full_evidence_maturity_complete:
        accumulation_stage = "frozen_holdout_accumulation_and_governed_evaluation"
    else:
        accumulation_stage = "evidence_maturity_complete"

    return {
        "schema": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "candidate_policy_version": str(candidate_policy_version or ""),
        "as_of": str(as_of or ""),
        "qualified_a_event_dates": qualified_a_days,
        "qualified_a_event_day_count": len(qualified_a_days),
        "development_evidence_a_event_dates": development_a_days,
        "development_evidence_excluded": sorted(development),
        "process_accumulation": {
            "stage": accumulation_stage,
            "a_class_event_days_are_live_rollout_blocker": False,
            "candidate_policy_is_live_guarded_by_runtime_contract": True,
            "parameter_learning_allowed_to_propose": parameter_learning_allowed,
            "parameter_learning_requires_human_review_and_explicit_apply": True,
            "weather_type_specific_parameter_learning_allowed": False,
            "full_evidence_maturity_complete": full_evidence_maturity_complete,
            "full_evidence_maturity_definition": "60_non_development_a_days_plus_valid_20_day_frozen_holdout_plus_integrity_and_performance_evidence",
            "runtime_rollback_mode": "set_PM_ZUUU_DAILY_HIGH_CANDIDATE_LIVE_MODE_to_shadow",
        },
        "regularized_shadow_allowed": parameter_learning_allowed,
        "frozen_registration": registration,
        "frozen_registry_valid": registry_valid,
        "frozen_a_event_dates": frozen_a_dates,
        "frozen_a_event_day_count": len(frozen_a_dates),
        # Backward-compatible fields: "release" now means evidence-maturity
        # graduation, not permission for the guarded live candidate to run.
        "release_eligible": full_evidence_maturity_complete,
        "release_blockers": sorted(set(evidence_maturity_gaps)),
        "full_evidence_maturity_complete": full_evidence_maturity_complete,
        "evidence_maturity_gaps": sorted(set(evidence_maturity_gaps)),
        "live_rollout": {
            "state": "not_blocked_by_sample_or_frozen_holdout_count",
            "allowed_by_this_read_only_audit": True,
            "not_a_permission_to_bypass_runtime_contract": True,
            "runtime_guard_owners": [
                "candidate_contract",
                "phase_lower_bound_guard",
                "posterior_hard_mask",
                "candidate_to_baseline_fallback",
            ],
        },
        "integrity_gates": {key: integrity.get(key) is True for key in required_integrity},
        "performance_gates": {key: performance.get(key) is True for key in required_performance},
        "evaluation_summary": {
            "event_day_count": _mapping(evaluation).get("event_day_count"),
            "paired_baseline_event_day_count": _mapping(evaluation).get("paired_baseline_event_day_count"),
            "performance_conclusion_allowed": _mapping(evaluation).get("performance_conclusion_allowed") is True,
        },
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_posterior": False,
            "places_orders": False,
            "changes_parameters": False,
            "releases_production_probability": False,
            "a_day_count_blocks_live_rollout": False,
        },
    }


__all__ = [
    "DEVELOPMENT_EVIDENCE_DATES",
    "FROZEN_REGISTRY_SCHEMA",
    "MIN_A_DAYS_FOR_FROZEN_REGISTRATION",
    "MIN_A_DAYS_FOR_RELEASE",
    "POLICY_VERSION",
    "REQUIRED_FROZEN_A_DAYS",
    "SCHEMA_VERSION",
    "build_frozen_holdout_registration_packet",
    "build_release_gate_audit",
]

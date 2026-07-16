"""Governed promotion packet for the unique source-set weak-likelihood artifact.

It cannot approve, write an active pointer, mutate a model or touch live
trading.  It only makes all evidence required for a later human/controlled
promotion explicit and rejects a candidate whose immutable identities differ.
"""

from __future__ import annotations

from typing import Any, Mapping

from joint_forecast_weak_likelihood_runtime import ARTIFACT_SCHEMA_VERSION, canonical_artifact_sha256


SCHEMA_VERSION = "joint_forecast_weak_likelihood_promotion_packet_v1"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def build_joint_weak_promotion_packet(
    *, candidate_artifact: Mapping[str, Any], oos_evaluation: Mapping[str, Any], shadow_comparison: Mapping[str, Any] | None = None, minimum_oos_samples: int = 30
) -> dict[str, Any]:
    """Prepare review evidence without applying the candidate."""

    candidate = _mapping(candidate_artifact)
    oos = _mapping(oos_evaluation)
    shadow = _mapping(shadow_comparison)
    blockers: list[str] = []
    candidate_sha = _text(candidate.get("artifact_sha256"))
    if candidate.get("schema") != ARTIFACT_SCHEMA_VERSION:
        blockers.append("candidate_artifact_schema_invalid")
    if candidate.get("approval_state") != "shadow_only":
        blockers.append("candidate_must_remain_shadow_only_until_approval")
    if not candidate_sha or candidate_sha != canonical_artifact_sha256(candidate):
        blockers.append("candidate_artifact_sha256_invalid")
    if candidate.get("source_data_policy") != "forecast_only_asof":
        blockers.append("candidate_source_data_policy_invalid")
    if oos.get("state") != "evaluated_shadow_only":
        blockers.append("strict_oos_evaluation_missing_or_invalid")
    if _text(_mapping(oos.get("training")).get("artifact_sha256")) != candidate_sha:
        blockers.append("oos_candidate_artifact_identity_mismatch")
    alpha_selection = _mapping(oos.get("bias_alpha_selection"))
    if alpha_selection.get("comparison_complete") is not True:
        blockers.append("bias_alpha_oos_comparison_missing_or_incomplete")
    if _text(alpha_selection.get("selected_artifact_sha256")) != candidate_sha:
        blockers.append("candidate_not_selected_by_bias_alpha_oos")
    if alpha_selection.get("candidate_is_selected") is not True:
        blockers.append("bias_alpha_selection_candidate_flag_missing_or_false")
    oos_safety = _mapping(oos.get("oos_safety"))
    if oos_safety.get("state") != "passed":
        blockers.append("oos_raw_baseline_harm_or_tail_protection_failed")
    if oos_safety.get("paired_event_day_bootstrap") is not True:
        blockers.append("oos_event_day_block_bootstrap_evidence_missing")
    try:
        sample_count = int(oos.get("sample_count") or 0)
    except (TypeError, ValueError):
        sample_count = 0
    if sample_count < int(minimum_oos_samples):
        blockers.append("oos_sample_count_below_promotion_minimum")
    for field in ("mae_c", "mean_negative_log_probability"):
        try:
            valid = float(oos.get(field)) >= 0.0
        except (TypeError, ValueError):
            valid = False
        if not valid:
            blockers.append(f"oos_{field}_missing_or_invalid")
    # A separate shadow comparison is mandatory only for execution promotion;
    # this packet records its absence instead of fabricating a result.
    if shadow and shadow.get("state") != "passed":
        blockers.append("shadow_comparison_not_passed")
    review = {
        "schema": SCHEMA_VERSION,
        "state": "proposed" if not blockers else "blocked",
        "candidate_artifact_sha256": candidate_sha,
        "oos_evaluation": {"state": oos.get("state"), "sample_count": sample_count, "mae_c": oos.get("mae_c"), "mean_negative_log_probability": oos.get("mean_negative_log_probability"), "bias_alpha_selection": alpha_selection, "safety": oos_safety},
        "shadow_comparison": shadow or {"state": "not_supplied", "na_reason": "not_yet_run"},
        "blockers": sorted(set(blockers)),
        "required_before_activation": ["approved_review_record", "p10_p8_p9_shadow_latency_evidence", "atomic_active_pointer_write", "rollback_artifact_reference"],
        "boundary": {"approval_state_written": False, "writes_runtime_artifact_pointer": False, "strict_asof_oos_runtime_gate": False, "writes_posterior": False, "places_orders": False},
    }
    return review


__all__ = ["SCHEMA_VERSION", "build_joint_weak_promotion_packet"]

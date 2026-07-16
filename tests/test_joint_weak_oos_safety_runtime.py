from __future__ import annotations

from joint_weak_oos_safety_runtime import build_joint_weak_oos_safety_evidence


def _rows(*, corrected_error: float, raw_error: float, corrected_nll: float, raw_nll: float) -> tuple[list[dict], list[dict], list[dict]]:
    candidates: list[dict] = []
    raw: list[dict] = []
    training: list[dict] = []
    for index in range(20):
        day = f"2025-06-{index + 1:02d}"
        target = 25.0 if index < 5 else 35.0 if index >= 15 else 30.0
        identity = {"event_id": f"event-{index}", "event_date": day, "decision_as_of": f"{day}T06:00:00Z", "target_physical_x_final_c": target}
        candidates.append({**identity, "absolute_error_c": corrected_error, "negative_log_probability": corrected_nll, "ranked_probability_score": 0.05})
        raw.append({**identity, "absolute_error_c": raw_error, "negative_log_probability": raw_nll, "ranked_probability_score": 0.12})
        training.append({"physical_x_final_c": target})
    return candidates, raw, training


def test_event_day_bootstrap_and_physical_tail_protection_pass_for_pairwise_improvement() -> None:
    candidate, raw, training = _rows(corrected_error=0.10, raw_error=0.35, corrected_nll=0.8, raw_nll=1.1)

    evidence = build_joint_weak_oos_safety_evidence(
        candidate_rows=candidate, raw_rows=raw, training_samples=training,
        candidate_artifact_sha256="candidate-a", bootstrap_replicates=200,
    )

    assert evidence["state"] == "passed"
    assert evidence["paired_event_day_bootstrap"] is True
    assert evidence["tails"]["low_physical_x"]["state"] == "passed"
    assert evidence["tails"]["high_physical_x"]["state"] == "passed"
    assert evidence["overall"]["mae"]["ci_upper"] < 0.0


def test_tail_harm_or_identity_drift_blocks_candidate_without_live_gate() -> None:
    candidate, raw, training = _rows(corrected_error=0.80, raw_error=0.20, corrected_nll=1.6, raw_nll=0.9)
    harmful = build_joint_weak_oos_safety_evidence(
        candidate_rows=candidate, raw_rows=raw, training_samples=training,
        candidate_artifact_sha256="candidate-b", bootstrap_replicates=200,
    )

    assert harmful["state"] == "blocked"
    assert "overall_raw_vs_corrected_harm_tolerance_failed" in harmful["blockers"]
    assert harmful["live_runtime_gate"] is False

    raw.pop()
    mismatch = build_joint_weak_oos_safety_evidence(
        candidate_rows=candidate, raw_rows=raw, training_samples=training,
        candidate_artifact_sha256="candidate-b", bootstrap_replicates=200,
    )
    assert mismatch["state"] == "blocked"
    assert "raw_and_corrected_oos_identity_sets_differ" in mismatch["blockers"]

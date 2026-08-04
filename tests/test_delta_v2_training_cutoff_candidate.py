from __future__ import annotations

from bayes_basket_target.delta_v2_training_cutoff_candidate import (
    assert_target_owned_training_cutoff_candidate,
    build_target_owned_training_cutoff_candidate,
)


def _row(
    event_date: str,
    decision_at_utc: str,
    closed_at_utc: str,
    phase: str,
) -> dict[str, str]:
    return {
        "fact_id": f"d2fact-{event_date}-{phase}",
        "station_id": "RKPK",
        "event_date": event_date,
        "decision_at_utc": decision_at_utc,
        "settlement_closed_at_utc": closed_at_utc,
        "phase": phase,
    }


def _rows() -> list[dict[str, str]]:
    return [
        _row("2026-07-29", "2026-07-29T09:00:00Z", "2026-07-29T15:00:00Z", "TAIL"),
        _row("2026-07-30", "2026-07-29T16:00:00Z", "2026-07-30T15:00:00Z", "HEAVY"),
        _row("2026-07-30", "2026-07-29T18:00:00Z", "2026-07-30T15:00:00Z", "STANDARD"),
        _row("2026-07-30", "2026-07-30T02:00:00Z", "2026-07-30T15:00:00Z", "TAIL"),
    ]


def test_causal_cutoff_binds_only_closed_training_and_later_evaluation() -> None:
    artifact = build_target_owned_training_cutoff_candidate(
        _rows(),
        as_of_utc="2026-08-01T00:00:00Z",
        min_unique_events_per_phase=1,
    )

    assert_target_owned_training_cutoff_candidate(artifact)
    assert artifact["state"] == "CANDIDATE_READY_NO_LIVE"
    assert artifact["policy_training_cutoffs"] == {"RKPK": "2026-07-29T15:00:00Z"}
    candidate = artifact["station_candidates"]["RKPK"]
    assert candidate["train_label_closed_no_later_than_cutoff"] is True
    assert candidate["evaluation_decision_not_before_cutoff"] is True
    assert candidate["evaluation_event_days"] == 1
    assert artifact["writes_production_db"] is False
    assert artifact["orders_submitted"] == 0


def test_phase_threshold_deficits_keep_candidate_accumulating() -> None:
    artifact = build_target_owned_training_cutoff_candidate(
        _rows(),
        as_of_utc="2026-08-01T00:00:00Z",
        min_unique_events_per_phase=30,
    )

    assert artifact["state"] == "ACCUMULATING"
    candidate = artifact["station_candidates"]["RKPK"]
    assert candidate["phase_unique_event_deficits"] == {
        "HEAVY": 29,
        "STANDARD": 29,
        "TAIL": 29,
    }
    assert "cash_phase_cutoff_sample_accumulating" in artifact["accumulation_blockers"]

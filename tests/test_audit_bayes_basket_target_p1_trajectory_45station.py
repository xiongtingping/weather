from __future__ import annotations

from scripts.audit_bayes_basket_target_p1_trajectory_45station import (
    classify_blockers,
)


def test_classifier_distinguishes_prefix_gap_from_oos_sample_and_artifact() -> None:
    blockers = classify_blockers(
        truth_row_count=30,
        observation_row_count=200,
        correction_inventory={
            "unreviewed_correction_count": 0,
            "active_halt_count": 0,
        },
        source_manifest={
            "eligible_fold_count": 0,
            "v3_eligible_fold_count": 0,
            "v3_incomplete_future_curve_fold_count": 0,
            "excluded": [
                {
                    "event_date": "2026-07-01",
                    "reason": "canonical_prefix_missing_at_bucket_start",
                }
            ],
        },
        training_error="ContractError:metar_trajectory_training_fold_count_invalid",
        formal_artifact_state="MISSING",
    )

    assert blockers == [
        "data_missing",
        "sample_insufficient",
        "trajectory_oos_not_ready",
        "trajectory_artifact_missing",
    ]


def test_classifier_keeps_manual_review_as_hard_blocker() -> None:
    blockers = classify_blockers(
        truth_row_count=30,
        observation_row_count=200,
        correction_inventory={
            "unreviewed_correction_count": 1,
            "active_halt_count": 1,
        },
        source_manifest={
            "eligible_fold_count": 24,
            "v3_eligible_fold_count": 24,
            "v3_incomplete_future_curve_fold_count": 0,
            "excluded": [],
        },
        formal_artifact_state="FORMAL_PROMOTED",
    )

    assert blockers == ["revision_unreviewed"]


def test_classifier_accepts_only_proven_quarantined_historical_days() -> None:
    """A historical correction cannot leak into a fit through station scope."""

    blockers = classify_blockers(
        truth_row_count=30,
        observation_row_count=200,
        correction_inventory={
            "unreviewed_correction_count": 1,
            "active_halt_count": 1,
            # The correction is known, but the exact-fold builder has
            # already quarantined that event date.  This does not clear a
            # runtime halt; it only proves the historical artifact does not
            # consume the bad day.
            "event_date_resolution_complete": True,
            "unreviewed_reachable_event_date_count": 0,
        },
        source_manifest={
            "eligible_fold_count": 24,
            "v3_eligible_fold_count": 24,
            "v3_incomplete_future_curve_fold_count": 0,
            "excluded": [],
        },
        formal_artifact_state="FORMAL_PROMOTED",
    )

    assert blockers == []


def test_classifier_blocks_unresolved_or_reachable_correction_day() -> None:
    blockers = classify_blockers(
        truth_row_count=30,
        observation_row_count=200,
        correction_inventory={
            "unreviewed_correction_count": 1,
            "active_halt_count": 0,
            "event_date_resolution_complete": False,
            "unreviewed_reachable_event_date_count": 0,
        },
        source_manifest={
            "eligible_fold_count": 24,
            "v3_eligible_fold_count": 24,
            "v3_incomplete_future_curve_fold_count": 0,
            "excluded": [],
        },
        formal_artifact_state="FORMAL_PROMOTED",
    )

    assert blockers == ["revision_unreviewed"]


def test_classifier_does_not_promote_candidate_artifact() -> None:
    blockers = classify_blockers(
        truth_row_count=30,
        observation_row_count=200,
        correction_inventory={
            "unreviewed_correction_count": 0,
            "active_halt_count": 0,
        },
        source_manifest={
            "eligible_fold_count": 24,
            "v3_eligible_fold_count": 24,
            "v3_incomplete_future_curve_fold_count": 0,
            "excluded": [],
        },
        formal_artifact_state="PRODUCTION_NO_LIVE_CANDIDATE",
    )

    assert blockers == ["trajectory_oos_not_ready"]

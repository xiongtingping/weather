from __future__ import annotations

import pytest

import hashlib
import json
from datetime import datetime, timedelta

from sequential_bayes_filter_shadow_runtime import (
    build_next_checkpoint,
    build_strong_observation_update,
    replay_canonical_sequence,
)
from strong_observation_lr_artifact_contract import build_strong_observation_lr_input
from temperature_trajectory_feature_runtime import build_empty_pre_event_temperature_trajectory_feature_state
from tests.strong_observation_artifact_fixtures import approved_strong_artifact


def _hash(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _checkpoint(*, consumed=()) -> dict:
    rows = [
        {"temperature_c": 29.0, "state": "warming", "probability": 0.25},
        {"temperature_c": 29.0, "state": "cooling", "probability": 0.25},
        {"temperature_c": 30.0, "state": "warming", "probability": 0.25},
        {"temperature_c": 30.0, "state": "cooling", "probability": 0.25},
    ]
    return {
        "posterior_checkpoint_id": "pchk_1234567890abcdef",
        "posterior_run_id": "post_1234567890abcdef",
        "target_identity": "highest-temperature-in-chengdu-on-july-3-2026",
        "event_date": "2026-07-03",
        "joint_posterior": rows,
        "joint_posterior_sha256": _hash(rows),
        "observed_max_lower_bound_c": 28.5,
        "consumed_event_ids": list(consumed),
        "source_event_ids": ["srcevt_1234567890abcdef"],
    }


def _event(*, event_id="canobs_1234567890abcdef", valid_time="2026-07-03T06:30:00Z", temp=30.0) -> dict:
    valid_at = datetime.fromisoformat(valid_time.replace("Z", "+00:00"))
    transport_at = valid_at + timedelta(seconds=30)
    actionable_at = valid_at + timedelta(minutes=1)
    return {
        "canonical_observation_event_id": event_id,
        "canonical_observation_key": f"key_{event_id}",
        "target_identity": "highest-temperature-in-chengdu-on-july-3-2026",
        "station_id": "ZUUU",
        "selected_source_id": "AMSC",
        "state": "accepted_primary",
        "selection_role": "primary",
        "valid_time": valid_time,
        "first_transport_seen_at": transport_at.isoformat().replace("+00:00", "Z"),
        "first_actionable_seen_at": actionable_at.isoformat().replace("+00:00", "Z"),
        "parsed_observation": {"temperature_c": temp, "dewpoint_c": 22.0, "cloud_text": "FEW020", "weather_text": ""},
        "quality_flags": [],
    }


def _artifact(**overrides) -> dict:
    artifact = approved_strong_artifact(
        model_artifact_sha256="approved-artifact-1",
        default_raw_log_score=0.0,
    )
    artifact.update(overrides)
    return artifact


def _strong_input(event: dict) -> dict:
    trajectory = build_empty_pre_event_temperature_trajectory_feature_state(
        target_identity=event["target_identity"],
        station_id=event["station_id"],
        decision_as_of="2026-07-03T07:00:00Z",
    )
    assert trajectory["ok"] is True
    input_row = build_strong_observation_lr_input(
        canonical_observation_event=event,
        trajectory_feature_state=trajectory,
        forecast_evidence_snapshot_id="forecast-snapshot-1",
        prior_context_id="prior-context-1",
        source_reliability_pre_state={"AMSC": {"state": "unknown"}},
    )
    assert input_row["ok"] is True
    return input_row


def _update(
    checkpoint: dict,
    event: dict,
    artifact: dict | None = None,
    previous_cumulative_raw_log_score: list[dict] | None = None,
) -> dict:
    return build_strong_observation_update(
        previous_checkpoint=checkpoint,
        canonical_observation_event=event,
        forecast_evidence_snapshot_id="forecast-snapshot-1",
        prior_context_id="prior-context-1",
        approved_lr_artifact=artifact or _artifact(),
        strong_observation_input=_strong_input(event),
        decision_as_of="2026-07-03T07:00:00Z",
        deadline_at="2026-07-03T07:00:03Z",
        previous_cumulative_raw_log_score=previous_cumulative_raw_log_score,
    )


def test_no_information_score_is_canonically_centered_to_zero_delta() -> None:
    update = _update(_checkpoint(), _event(temp=29.0))

    assert update["ok"] is True
    assert update["state"] == "neutral"
    assert update["normalizer_offset"] == 0.0
    assert all(row["posterior_delta"] == 0.0 for row in update["posterior_delta"])
    assert sum(row["probability"] for row in update["candidate_post_joint"]) == 1.0


def test_hard_condition_is_applied_before_normalization_and_cannot_be_tempered() -> None:
    update = _update(_checkpoint(), _event(temp=30.0), _artifact(default_raw_log_score=0.7))

    assert update["ok"] is True
    assert update["observed_max_lower_bound_c"] == 29.5
    assert update["hard_condition_trace"]["masked_cell_count"] == 2
    assert all(row["temperature_c"] >= 29.5 for row in update["candidate_post_joint"])
    assert any(row["posterior_delta"] is None for row in update["posterior_delta"])
    assert update["termination_risk"]["state"] == "available"
    assert update["termination_risk"]["same_strong_update_marginal"] is True
    assert update["termination_risk"]["updates_posterior"] is False
    expected = sum(
        row["probability"] for row in update["candidate_post_joint"]
        if row["temperature_c"] - 30.0 < 0.5
    )
    assert update["termination_risk"]["probability_no_material_new_daily_high"] == pytest.approx(expected)


def test_score_and_state_transition_are_consumed_once_by_the_composer() -> None:
    artifact = _artifact(
        raw_log_score_by_pre_state={"warming": 0.4, "cooling": -0.4},
        post_state_transition={
            "warming": {"warming": 0.8, "temporary_platform": 0.2},
            "cooling": {"cooling": 1.0},
        },
    )
    update = _update(_checkpoint(), _event(), artifact)

    assert update["ok"] is True
    assert update["state"] == "applied"
    assert {row["state"] for row in update["candidate_post_joint"]} == {
        "warming",
        "temporary_platform",
        "cooling",
    }
    assert update["raw_log_score_sha256"]
    assert update["posterior_delta_sha256"]
    assert update["normalizer_offset"] != 0.0


def test_unchanged_cumulative_evidence_produces_zero_conditional_innovation() -> None:
    artifact = _artifact(raw_log_score_by_pre_state={"warming": 0.4, "cooling": -0.4})
    first = _update(_checkpoint(), _event(temp=29.0), artifact)
    repeated = _update(
        _checkpoint(),
        _event(temp=29.0),
        artifact,
        previous_cumulative_raw_log_score=first["raw_log_score"],
    )

    assert repeated["ok"] is True
    assert repeated["likelihood_update_kind"] == "cumulative_evidence_replacement_delta_log_lr_v1"
    assert all(row["log_lr"] == 0.0 for row in repeated["conditional_innovation_log_score"])
    assert all(row["posterior_delta"] == 0.0 for row in repeated["posterior_delta"])


def test_duplicate_canonical_event_does_not_create_a_new_posterior_update() -> None:
    event = _event()
    update = _update(_checkpoint(consumed=[event["canonical_observation_event_id"]]), event)

    assert update["ok"] is True
    assert update["state"] == "neutral"
    assert update["no_posterior_change"] is True
    next_checkpoint = build_next_checkpoint(
        previous_checkpoint=_checkpoint(consumed=[event["canonical_observation_event_id"]]),
        strong_update=update,
        fact_snapshot_id="fact_1234567890abcdef",
        resolution_contract_id="resolution-v1",
        committed_at="2026-07-03T07:00:00Z",
    )
    assert next_checkpoint["reused_previous"] is True


def test_strong_update_fails_closed_for_invalid_canonical_temporal_identity() -> None:
    event = _event()
    event["valid_time"] = "2026-07-03T06:32:00Z"

    update = _update(_checkpoint(), event)

    assert update["ok"] is False
    assert set(update["block_reasons"]) >= {
        "canonical_valid_time_after_first_transport",
        "canonical_valid_time_after_first_actionable",
    }


def test_strong_update_fails_closed_when_canonical_transport_time_is_missing() -> None:
    event = _event()
    event["first_transport_seen_at"] = "not-a-time"

    update = _update(_checkpoint(), event)

    assert update["ok"] is False
    assert "canonical_first_transport_seen_at_missing_or_invalid" in update["block_reasons"]


def test_replay_is_order_invariant_after_canonical_sorting() -> None:
    first = _event(event_id="canobs_aaaaaaaaaaaaaaaa", valid_time="2026-07-03T06:30:00Z")
    second = _event(event_id="canobs_bbbbbbbbbbbbbbbb", valid_time="2026-07-03T07:00:00Z")
    artifact = _artifact(raw_log_score_by_pre_state={"warming": 0.2, "cooling": -0.1})
    common = {
        "initial_checkpoint": _checkpoint(),
        "forecast_evidence_snapshot_id": "forecast-snapshot-1",
        "prior_context_id": "prior-context-1",
        "approved_lr_artifact": artifact,
        "decision_as_of": "2026-07-03T07:30:00Z",
        "deadline_at": "2026-07-03T07:31:00Z",
        "fact_snapshot_id_prefix": "fact-replay",
        "resolution_contract_id": "resolution-v1",
    }

    ordered = replay_canonical_sequence(canonical_events=[first, second], **common)
    reordered = replay_canonical_sequence(canonical_events=[second, first], **common)

    assert ordered["ok"] is True
    assert reordered["ok"] is True
    assert ordered["canonical_sequence_sha256"] == reordered["canonical_sequence_sha256"]
    assert ordered["checkpoint"]["joint_posterior_sha256"] == reordered["checkpoint"]["joint_posterior_sha256"]


def test_incremental_checkpoint_identity_retains_full_canonical_sequence_after_restart() -> None:
    first_event = _event(event_id="canobs_aaaaaaaaaaaaaaaa", valid_time="2026-07-03T06:30:00Z")
    second_event = _event(event_id="canobs_bbbbbbbbbbbbbbbb", valid_time="2026-07-03T06:45:00Z")
    initial = _checkpoint()
    first_update = _update(initial, first_event)
    first_checkpoint = build_next_checkpoint(
        previous_checkpoint=initial,
        strong_update=first_update,
        fact_snapshot_id="fact_sequence_first",
        resolution_contract_id="resolution-v1",
        committed_at="2026-07-03T07:00:00Z",
    )
    first = dict(first_checkpoint)
    second_update = _update(first, second_event)
    second = build_next_checkpoint(
        previous_checkpoint=first,
        strong_update=second_update,
        fact_snapshot_id="fact_sequence_second",
        resolution_contract_id="resolution-v1",
        committed_at="2026-07-03T07:00:01Z",
    )

    resumed = dict(first)
    resumed.pop("source_event_ids", None)
    resumed_update = _update(resumed, second_event)
    resumed_second = build_next_checkpoint(
        previous_checkpoint=resumed,
        strong_update=resumed_update,
        fact_snapshot_id="fact_sequence_second",
        resolution_contract_id="resolution-v1",
        committed_at="2026-07-03T07:00:01Z",
    )

    assert first["source_event_ids"] == [first_event["canonical_observation_event_id"]]
    assert second["source_event_ids"] == [
        first_event["canonical_observation_event_id"],
        second_event["canonical_observation_event_id"],
    ]
    assert second["posterior_run_id"] == resumed_second["posterior_run_id"]
    assert second["posterior_checkpoint_id"] == resumed_second["posterior_checkpoint_id"]


def test_market_or_legacy_edge_input_in_strong_artifact_fails_closed() -> None:
    update = _update(_checkpoint(), _event(), _artifact(market_price=0.55))

    assert update["ok"] is False
    assert update["state"] == "blocked"
    assert "strong_lr_artifact_contains_forbidden_market_or_composer_surface" in update["block_reasons"]

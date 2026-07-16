from __future__ import annotations

from peak_timing_state_runtime import (
    build_peak_timing_state,
    validate_peak_timing_state_contract,
)
from joint_weak_live_evidence_runtime import build_forecast_peak_timing_prior
from second_yes_entry_window_runtime import build_second_yes_entry_window_state


def _peak_state(*, peak_already: float = 0.2) -> dict:
    return build_peak_timing_state(
        forecast_peak_timing_prior={
            "schema": "forecast_peak_timing_prior_v1",
            "state": "available",
            "forecast_evidence_snapshot_id": "forecast-1",
            "distribution": [
                {"peak_at": "2026-07-14T08:00:00Z", "probability": 0.25},
                {"peak_at": "2026-07-14T08:30:00Z", "probability": 0.75},
            ],
        },
        strong_observation_update={
            "ok": True,
            "strong_observation_update_id": "update-1",
            "canonical_observation_event_id": "event-1",
            "forecast_evidence_snapshot_id": "forecast-1",
            "termination_risk": {
                "probability_no_material_new_daily_high": peak_already,
            },
        },
        post_event_trajectory={
            "ok": True,
            "temporal_role": "post_event",
            "latest_canonical_observation_event_id": "event-1",
            "latest_valid_time": "2026-07-14T07:30:00Z",
            "path_state_evidence": {
                "observed_peak_valid_time": "2026-07-14T07:00:00Z",
            },
        },
        canonical_observation_event={"canonical_observation_event_id": "event-1"},
    )


def _realization(timing: dict) -> dict:
    return {
        "posterior_run_id": "posterior-1",
        "realization_state_id": "realization-1",
        "peak_timing_state": timing,
    }


def test_forecast_timing_prior_uses_same_joint_weak_information_weights() -> None:
    prior = build_forecast_peak_timing_prior(
        forecast_evidence={
            "event_date": "2026-07-14",
            "forecast_evidence_snapshot_id": "forecast-1",
            "source_revisions": [
                {"source_id": "ecmwf", "source_revision_id": "e1", "forecast_peak_valid_time": "2026-07-14T08:00:00Z"},
                {"source_id": "gfs", "source_revision_id": "g1", "forecast_peak_valid_time": "2026-07-14T09:00:00Z"},
            ],
        },
        joint_weak_likelihood={
            "source_expert_trace": [
                {"source_id": "ecmwf", "source_revision_id": "e1", "information_weight": 3.0},
                {"source_id": "gfs", "source_revision_id": "g1", "information_weight": 1.0},
            ],
        },
    )

    assert prior["state"] == "available"
    assert prior["mode_peak_at"] == "2026-07-14T08:00:00Z"
    assert [row["probability"] for row in prior["distribution"]] == [0.75, 0.25]
    assert prior["does_not_update_physical_x"] is True


def test_strong_termination_marginal_updates_forecast_peak_timing() -> None:
    timing = _peak_state(peak_already=0.2)

    assert timing["ok"] is True
    assert timing["entry_peak_at"] == "2026-07-14T08:30:00Z"
    assert timing["peak_already_probability"] == 0.2
    assert timing["future_peak_probability"] == 0.8
    assert timing["does_not_update_physical_x"] is True
    evidence, issues = validate_peak_timing_state_contract(
        timing,
        expected_event_id="event-1",
        expected_update_id="update-1",
    )
    assert issues == []
    assert evidence["entry_peak_at"] == "2026-07-14T08:30:00Z"


def test_peak_timing_contract_rejects_lineage_and_future_slot_drift() -> None:
    timing = _peak_state()
    timing["strong_observation_update_id"] = "update-old"
    timing["entry_peak_at"] = "2026-07-14T09:00:00Z"

    _evidence, issues = validate_peak_timing_state_contract(
        timing,
        expected_event_id="event-1",
        expected_update_id="update-1",
    )

    assert "public_peak_timing_update_binding_mismatch" in issues
    assert "public_peak_timing_entry_not_forecast_supported" in issues


def test_peak_already_mode_closes_second_yes_without_faking_future_clock() -> None:
    timing = _peak_state(peak_already=0.8)

    assert timing["entry_peak_at"] == ""
    assert timing["entry_peak_at_na_reason"] == "daily_peak_already_is_timing_mode"


def test_expired_forecast_slots_preserve_unresolved_future_timing_mass() -> None:
    timing = build_peak_timing_state(
        forecast_peak_timing_prior={
            "schema": "forecast_peak_timing_prior_v1",
            "state": "available",
            "forecast_evidence_snapshot_id": "forecast-1",
            "distribution": [
                {"peak_at": "2026-07-14T08:00:00Z", "probability": 1.0},
            ],
        },
        strong_observation_update={
            "ok": True,
            "strong_observation_update_id": "update-1",
            "canonical_observation_event_id": "event-1",
            "forecast_evidence_snapshot_id": "forecast-1",
            "termination_risk": {
                "probability_no_material_new_daily_high": 0.9,
            },
        },
        post_event_trajectory={
            "ok": True,
            "temporal_role": "post_event",
            "latest_canonical_observation_event_id": "event-1",
            "latest_valid_time": "2026-07-14T09:00:00Z",
            "path_state_evidence": {
                "observed_peak_valid_time": "2026-07-14T08:30:00Z",
            },
        },
        canonical_observation_event={"canonical_observation_event_id": "event-1"},
    )

    assert timing["state"] == "available"
    assert timing["entry_peak_at"] == ""
    assert timing["entry_peak_at_na_reason"] == "forecast_supported_future_peak_slot_missing"
    assert timing["distribution"][-1]["time_state"] == "future_peak_time_unresolved"
    assert abs(sum(row["probability"] for row in timing["distribution"]) - 1.0) < 1e-12
    _evidence, issues = validate_peak_timing_state_contract(
        timing,
        expected_event_id="event-1",
        expected_update_id="update-1",
    )
    assert issues == []


def test_second_yes_window_is_exactly_final_half_hour_before_predicted_peak() -> None:
    realization = _realization(_peak_state())
    before = build_second_yes_entry_window_state(
        realization_state=realization, decision_as_of="2026-07-14T07:59:59.999Z"
    )
    start = build_second_yes_entry_window_state(
        realization_state=realization, decision_as_of="2026-07-14T08:00:00Z"
    )
    end = build_second_yes_entry_window_state(
        realization_state=realization, decision_as_of="2026-07-14T08:30:00Z"
    )

    assert before["entry_allowed"] is False
    assert start["entry_allowed"] is True
    assert end["entry_allowed"] is False
    assert start["entry_window_minutes"] == 30
    assert start["uses_ev_edge_lcb"] is False

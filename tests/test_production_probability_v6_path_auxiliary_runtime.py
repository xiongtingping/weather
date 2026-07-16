import json

from production_probability_v6_path_auxiliary_runtime import (
    enrich_v6_candidate_with_post_event_auxiliary,
    inject_v6_peak_timing_into_public_fact,
)


def _event(identity: str, valid: str, seen: str, temperature: float) -> dict:
    return {
        "canonical_observation_event_id": identity,
        "canonical_observation_key": identity + "-key",
        "target_identity": "physical_weather_day_v1:ZUUU:2026-07-16",
        "station_id": "ZUUU",
        "state": "accepted_primary",
        "valid_time": valid,
        "first_actionable_seen_at": seen,
        "parsed_observation": {"temperature_c": temperature},
    }


def _candidate() -> dict:
    termination = {
        "schema": "termination_risk_marginal_v3_physical_state_bayes_v6",
        "state": "available",
        "probability_no_material_new_daily_high": 0.25,
        "updates_posterior": False,
        "canonical_observation_event_id": "event-2",
    }
    return {
        "ok": True,
        "strong_update_row": {
            "strong_observation_update_id": "strong-v6-2",
            "canonical_observation_event_id": "event-2",
            "forecast_evidence_snapshot_id": "forecast-1",
            "decision_as_of": "2026-07-16T07:30:20Z",
            "completed_at": "2026-07-16T07:30:21Z",
            "path_auxiliary_json": json.dumps({"termination_risk": termination}),
        },
        "checkpoint_row": {
            "posterior_checkpoint_id": "checkpoint-2",
            "posterior_run_id": "posterior-2",
            "last_canonical_event_id": "event-2",
            "last_strong_update_id": "strong-v6-2",
            "duration_clocks_json": "{}",
        },
    }


def test_enrichment_adds_bound_peak_timing_without_changing_posterior_identity():
    events = [
        _event("event-1", "2026-07-16T07:00:00Z", "2026-07-16T07:00:20Z", 36),
        _event("event-2", "2026-07-16T07:30:00Z", "2026-07-16T07:30:20Z", 37),
    ]
    candidate = _candidate()
    enriched = enrich_v6_candidate_with_post_event_auxiliary(
        candidate=candidate,
        canonical_events_through_current=events,
        forecast_peak_timing_prior={
            "forecast_evidence_snapshot_id": "forecast-1",
            "distribution": [
                {"peak_at": "2026-07-16T08:00:00Z", "probability": 0.4},
                {"peak_at": "2026-07-16T09:00:00Z", "probability": 0.6},
            ],
        },
    )

    assert enriched["checkpoint_row"]["posterior_run_id"] == "posterior-2"
    assert enriched["reported_physical_x_unchanged"] is True
    timing = enriched["peak_timing_state"]
    assert timing["canonical_observation_event_id"] == "event-2"
    assert timing["strong_observation_update_id"] == "strong-v6-2"
    assert timing["entry_peak_at"] == "2026-07-16T09:00:00Z"
    assert timing["peak_already_probability"] == 0.25
    assert timing["future_peak_probability"] == 0.75


def test_public_injection_uses_same_checkpoint_and_update_timing():
    events = [
        _event("event-1", "2026-07-16T07:00:00Z", "2026-07-16T07:00:20Z", 36),
        _event("event-2", "2026-07-16T07:30:00Z", "2026-07-16T07:30:20Z", 37),
    ]
    enriched = enrich_v6_candidate_with_post_event_auxiliary(
        candidate=_candidate(),
        canonical_events_through_current=events,
        forecast_peak_timing_prior={
            "forecast_evidence_snapshot_id": "forecast-1",
            "distribution": [{"peak_at": "2026-07-16T08:30:00Z", "probability": 1.0}],
        },
    )
    public = {
        "posterior_run_id": "posterior-2",
        "fact_json": json.dumps(
            {"posterior_ssot_fact": {"posterior_checkpoint_id": "checkpoint-2"}}
        ),
    }
    projected = inject_v6_peak_timing_into_public_fact(
        public_fact_row=public,
        candidate=enriched,
    )
    ssot = json.loads(projected["fact_json"])["posterior_ssot_fact"]

    assert ssot["peak_timing_state"]["entry_peak_at"] == "2026-07-16T08:30:00Z"
    assert (
        ssot["temperature_trajectory_feature_state"][
            "latest_canonical_observation_event_id"
        ]
        == "event-2"
    )

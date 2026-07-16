from __future__ import annotations

import hashlib

from joint_forecast_weak_training_event_runtime import (
    CONTEXT_SCHEMA_VERSION,
    build_joint_weak_training_events,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _physical_source() -> dict:
    return {
        "source_observation_id": "physical-20260703",
        "source_id": "amsc_zuuu_scheduled_metar_daily_max_v2",
        "source_class": "settlement_source",
        "role": "physical_daily_high_final_truth",
        "station_id": "ZUUU",
        "event_date": "2026-07-03",
        "fetched_at": "2026-07-03T18:00:00Z",
        "raw_ref": "raw://physical",
        "raw_sha256": _sha("physical"),
        "payload": {"final_physical_high_c": 30.0, "label_finality": "final", "strict_asof_training_eligible": True},
    }


def _context(**overrides: object) -> dict:
    value = {
        "schema": CONTEXT_SCHEMA_VERSION,
        "context_capture_id": "forecast-context-1",
        "context_raw_sha256": _sha("forecast context"),
        "context_source_class": "forecast_weak_context",
        "target_identity": "highest-temperature-in-chengdu-on-july-3-2026",
        "event_date": "2026-07-03",
        "decision_as_of": "2026-07-03T06:30:00Z",
        "context_available_at": "2026-07-03T06:00:00Z",
        "forecast_context": {"season": "summer", "weather_regime": "clear", "lead_bucket": "0_6h"},
    }
    value.update(overrides)
    return value


def _forecast(*, source_id: str = "gfs", fetched_at: str = "2026-07-03T06:00:00Z", **overrides: object) -> dict:
    value = {
        "source_event_id": f"{source_id}-revision-1",
        "source_observation_id": f"{source_id}-observation-1",
        "source_id": source_id,
        "source_class": "forecast_weak_source",
        "hard_eligible": False,
        "event_date": "2026-07-03",
        "valid_time": "2026-07-03T15:00:00+08:00",
        "fetched_at": fetched_at,
        "forecast_peak_c": 30.5,
        "raw_sha256": _sha(source_id),
        "raw_ref": f"raw://{source_id}",
        "payload": {
            "evidence_tier": "A_live_observed_transport",
            "response_headers_observed": True,
            "future_training_proposal_only": True,
            "capture_time_provenance": "observed_transport",
            "first_system_seen_at": fetched_at,
            "available_at": fetched_at,
        },
    }
    value.update(overrides)
    return value


def test_event_assembly_binds_only_pre_cutoff_tier_a_forecast_to_closed_physical_x() -> None:
    result = build_joint_weak_training_events(
        context_captures=[_context()],
        strict_forecast_observations=[_forecast(), _forecast(source_id="taf", fetched_at="2026-07-03T06:45:00Z")],
        final_physical_source_observations=[_physical_source()],
    )

    assert result["state"] == "available"
    assert len(result["events"]) == 1
    event = result["events"][0]
    assert event["physical_x_final"]["temperature_c"] == 30.0
    assert [row["source_id"] for row in event["source_revisions"]] == ["gfs"]
    assert event["source_revisions"][0]["strict_asof_tier_a"] is True
    assert result["boundary"]["live_amsc_ingest_blocked"] is False


def test_event_assembly_rejects_context_leakage_and_ambiguous_physical_x_sources() -> None:
    leaky = _context(forecast_context={"season": "summer", "weather_regime": "clear", "lead_bucket": "0_6h", "market_price": "0.5"})
    leaked = build_joint_weak_training_events(
        context_captures=[leaky], strict_forecast_observations=[_forecast()], final_physical_source_observations=[_physical_source()]
    )
    assert leaked["state"] == "blocked"
    assert "forecast_context_capture_fields_drift" in leaked["quarantined_contexts"][0]["reasons"]
    assert "forecast_context_capture_contains_forbidden_observation_or_market_input" in leaked["quarantined_contexts"][0]["reasons"]

    ambiguous = build_joint_weak_training_events(
        context_captures=[_context()], strict_forecast_observations=[_forecast()], final_physical_source_observations=[_physical_source(), _physical_source()]
    )
    assert ambiguous["state"] == "blocked"
    assert ambiguous["quarantined_contexts"][0]["reasons"] == ["final_physical_x_source_ambiguous"]


def test_event_assembly_never_reinterprets_non_tier_a_or_future_forecasts() -> None:
    not_tier_a = _forecast()
    not_tier_a["payload"]["response_headers_observed"] = False
    result = build_joint_weak_training_events(
        context_captures=[_context()],
        strict_forecast_observations=[not_tier_a, _forecast(source_id="icon", fetched_at="2026-07-03T07:00:00Z")],
        final_physical_source_observations=[_physical_source()],
    )

    assert result["state"] == "blocked"
    assert "forecast_revision_not_tier_a_observed_transport" in result["quarantined_contexts"][0]["reasons"]


def test_event_assembly_quarantines_complete_but_non_strict_physical_x_label() -> None:
    physical = _physical_source()
    physical["payload"]["strict_asof_training_eligible"] = False

    result = build_joint_weak_training_events(
        context_captures=[_context()],
        strict_forecast_observations=[_forecast()],
        final_physical_source_observations=[physical],
    )

    assert result["state"] == "blocked"
    assert result["quarantined_contexts"][0]["reasons"] == [
        "final_physical_x_not_strict_asof_training_eligible"
    ]

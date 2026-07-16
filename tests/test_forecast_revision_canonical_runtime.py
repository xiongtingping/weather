from __future__ import annotations

from forecast_revision_canonical_runtime import canonicalize_forecast_revisions


TARGET = "highest-temperature-in-chengdu-on-july-3-2026"
EVENT_DATE = "2026-07-03"
AS_OF = "2026-07-03T07:00:00Z"


def _row(source_id: str, revision: str, peak: float, *, arrived_at: str, valid_time: str = "2026-07-03T12:00:00Z") -> dict:
    return {
        "source_id": source_id,
        "source_event_id": f"srcevt_{source_id}_{revision}",
        "source_observation_id": f"srcobs_{source_id}_{revision}",
        "source_class": "forecast_weak_source",
        "event_date": EVENT_DATE,
        "valid_time": valid_time,
        "fetched_at": arrived_at,
        "forecast_peak_c": peak,
        "raw_sha256": f"sha-{source_id}-{revision}",
    }


def test_latest_visible_revision_per_source_is_selected_by_system_arrival_not_provider_claim() -> None:
    early = _row("ecmwf", "early", 30.0, arrived_at="2026-07-03T06:00:00Z")
    late = _row("ecmwf", "late", 31.0, arrived_at="2026-07-03T06:30:00Z")
    future = _row("ecmwf", "future", 35.0, arrived_at="2026-07-03T07:01:00Z")
    future["payload"] = {"provider_available_at": "2026-07-03T05:00:00Z"}
    taf = _row("taf", "one", 32.0, arrived_at="2026-07-03T06:15:00Z")

    first = canonicalize_forecast_revisions(
        [future, taf, early, late],
        target_identity=TARGET,
        event_date=EVENT_DATE,
        decision_as_of=AS_OF,
    )
    second = canonicalize_forecast_revisions(
        [late, early, taf, future],
        target_identity=TARGET,
        event_date=EVENT_DATE,
        decision_as_of=AS_OF,
    )

    assert first["ok"] is True
    assert [row["source_revision_id"] for row in first["source_revisions"]] == ["srcevt_ecmwf_late", "srcevt_taf_one"]
    assert first["forecast_evidence_snapshot_id"] == second["forecast_evidence_snapshot_id"]
    assert any(row["exclusion_reason"] == "forecast_revision_after_asof" for row in first["source_trace"])
    assert first["boundary"]["provider_declared_availability_cannot_backdate_system_visibility"] is True


def test_missing_valid_time_or_nonforecast_input_becomes_explicit_empty_forecast_set() -> None:
    missing_valid = _row("ecmwf", "missing", 30.0, arrived_at="2026-07-03T06:00:00Z", valid_time="")
    hard = _row("amsc", "metar", 30.0, arrived_at="2026-07-03T06:00:00Z")
    hard["source_class"] = "hard_observation_candidate"
    result = canonicalize_forecast_revisions(
        [missing_valid, hard],
        target_identity=TARGET,
        event_date=EVENT_DATE,
        decision_as_of=AS_OF,
    )

    assert result["ok"] is True
    assert result["state"] == "available"
    assert result["source_revisions"] == []
    assert result["weak_evidence_state"] == "no_forecast_revisions_available"
    assert result["boundary"]["empty_forecast_source_set_is_explicit_model_input"] is True
    assert {row["exclusion_reason"] for row in result["source_trace"]} == {
        "forecast_revision_valid_time_missing",
        "source_class_not_forecast_weak",
    }

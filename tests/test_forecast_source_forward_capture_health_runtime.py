from __future__ import annotations

from forecast_source_forward_capture_health_runtime import build_forecast_source_forward_capture_health_audit


def _report(*, observed_at: str = "2026-07-11T23:00:00Z", event_date: str = "2026-07-12") -> dict:
    return {
        "schema": "current_forecast_source_forward_capture_v1",
        "source_data_policy": "external_only",
        "event_date": event_date,
        "observed_at": observed_at,
        "results": {
            source_id: {"ok": True, "state": "written", "response_sha256": f"sha-{source_id}"}
            for source_id in ("ecmwf_model_peak", "gfs_model_peak", "icon_model_peak")
        },
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
    }


def test_health_audit_accepts_current_complete_three_model_capture() -> None:
    audit = build_forecast_source_forward_capture_health_audit(
        _report(),
        now="2026-07-11T23:30:00Z",
        expected_event_date="2026-07-12",
    )

    assert audit["ok"] is True
    assert audit["age_seconds"] == 1800.0
    assert all(row["ok"] for row in audit["source_states"])
    assert audit["boundary"]["operational_health_not_training_readiness"] is True


def test_health_audit_fails_closed_for_stale_or_missing_required_model() -> None:
    report = _report(observed_at="2026-07-11T18:00:00Z")
    report["results"].pop("icon_model_peak")

    audit = build_forecast_source_forward_capture_health_audit(
        report,
        now="2026-07-11T23:30:00Z",
        expected_event_date="2026-07-12",
        max_age_seconds=7200,
    )

    assert audit["ok"] is False
    assert "forward_capture_report_stale" in audit["blockers"]
    assert "forward_capture_required_source_result_missing:icon_model_peak" in audit["blockers"]


def test_health_audit_never_accepts_wrong_target_day_or_production_side_effect_guard() -> None:
    report = _report(event_date="2026-07-11")
    report["guards"]["places_orders"] = True

    audit = build_forecast_source_forward_capture_health_audit(
        report,
        now="2026-07-11T23:30:00Z",
        expected_event_date="2026-07-12",
    )

    assert audit["ok"] is False
    assert "forward_capture_event_date_not_current" in audit["blockers"]
    assert "forward_capture_production_guard_invalid" in audit["blockers"]

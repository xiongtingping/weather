from __future__ import annotations

import json
from pathlib import Path

from joint_weak_baseline_artifact_runtime import build_joint_weak_operational_baseline_artifact
from joint_weak_live_evidence_runtime import (
    bind_live_joint_weak_likelihood_for_production,
    build_joint_weak_live_forecast_evidence,
    build_live_joint_weak_likelihood,
)


EVENT_DATE = "2026-07-13"
AS_OF = "2026-07-13T00:01:00Z"


def _revision(provider: str, *, peak: float, available_at: str, revision_id: str) -> dict:
    return {
        "schema": "official_nwp_daily_peak_revision_v1",
        "ok": True,
        "state": "available",
        "source_id": provider,
        "source_revision_id": revision_id,
        "event_date": EVENT_DATE,
        "available_at": available_at,
        "forecast_peak_c": peak,
        "raw_sha256": f"raw-{provider}-{revision_id}",
        "strict_asof_tier_a": provider != "ecmwf",
    }


def _write_revision(root: Path, provider: str, revision: dict) -> None:
    path = root / provider / "daily_peak_revisions" / f"{revision['source_revision_id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(revision), encoding="utf-8")


def _taf_source() -> dict:
    return {
        "source_id": "zuuu_taf_forecast_amsc",
        "source_event_id": "taf-immutable-1",
        "source_observation_id": "taf-obs-1",
        "source_class": "forecast_weak_source",
        "event_date": EVENT_DATE,
        "response_completed_at": "2026-07-12T21:04:00Z",
        "issue_time_utc": "2026-07-12T21:00:00Z",
        "forecast_peak_c": 35.0,
        "raw_sha256": "raw-taf-immutable-1",
        "payload": {
            "weak_source_id": "taf",
            "forecast_revision_kind": "original",
            "forecast_weather_regime": "clear_dry",
        },
    }


def _taf_amendment() -> dict:
    value = _taf_source()
    value.update({
        "source_event_id": "taf-immutable-amd-2",
        "source_observation_id": "taf-obs-amd-2",
        "response_completed_at": "2026-07-12T23:04:00Z",
        "issue_time_utc": "2026-07-12T22:30:00Z",
        "forecast_peak_c": 33.0,
        "raw_sha256": "raw-taf-amd-2",
    })
    value["payload"] = {
        "weak_source_id": "taf",
        "forecast_revision_kind": "amendment",
        "forecast_weather_regime": "wet_precipitation",
    }
    return value


def _late_discovered_older_taf_amendment() -> dict:
    value = _taf_amendment()
    value.update({
        "source_event_id": "taf-immutable-old-amd",
        "source_observation_id": "taf-obs-old-amd",
        "response_completed_at": "2026-07-12T23:30:00Z",
        "issue_time_utc": "2026-07-12T20:00:00Z",
        "forecast_peak_c": 31.0,
        "raw_sha256": "raw-taf-old-amd",
    })
    return value


def test_live_evidence_selects_only_latest_visible_forecasts_and_never_reads_observation_inputs(tmp_path: Path) -> None:
    _write_revision(tmp_path, "gfs", _revision("gfs", peak=32.8, available_at="2026-07-12T11:20:00Z", revision_id="old"))
    _write_revision(tmp_path, "gfs", _revision("gfs", peak=33.2, available_at="2026-07-12T22:20:00Z", revision_id="new"))
    _write_revision(tmp_path, "icon", _revision("icon", peak=37.6, available_at="2026-07-12T11:32:00Z", revision_id="icon"))
    _write_revision(tmp_path, "ecmwf", _revision("ecmwf", peak=35.4, available_at="2026-07-13T00:02:00Z", revision_id="future"))

    evidence = build_joint_weak_live_forecast_evidence(
        event_date=EVENT_DATE,
        decision_as_of=AS_OF,
        curve_root=tmp_path,
        source_observations=[_taf_source()],
    )

    assert evidence["ok"] is True
    assert [row["source_id"] for row in evidence["source_revisions"]] == ["icon", "gfs", "taf"]
    assert next(row for row in evidence["source_revisions"] if row["source_id"] == "gfs")["forecast_peak_c"] == 33.2
    assert evidence["forecast_context"] == {
        "season": "summer",
        "weather_regime": "clear_dry",
        "lead_bucket": "d0_0600_0859",
    }
    assert evidence["boundary"]["d0_observation_or_trajectory_not_consumed"] is True
    assert "current_temperature_c" not in json.dumps(evidence)


def test_live_joint_weak_likelihood_uses_the_new_baseline_not_legacy_v31(tmp_path: Path) -> None:
    for provider, peak in (("ecmwf", 35.4), ("icon", 37.6), ("gfs", 32.8)):
        _write_revision(tmp_path, provider, _revision(provider, peak=peak, available_at="2026-07-12T12:00:00Z", revision_id=provider))
    artifact_path = tmp_path / "joint_weak_baseline.json"
    artifact_path.write_text(json.dumps(build_joint_weak_operational_baseline_artifact()), encoding="utf-8")

    result = build_live_joint_weak_likelihood(
        temperature_grid_c=[28.0 + 0.25 * index for index in range(48)],
        event_date=EVENT_DATE,
        decision_as_of=AS_OF,
        curve_root=tmp_path,
        source_observations=[_taf_source()],
        artifact_path=artifact_path,
    )

    assert result["ok"] is True
    weak = result["joint_weak_likelihood"]
    assert weak["weak_source_set"]["source_set_key"] == "ecmwf+icon+gfs+taf"
    assert weak["weak_source_surface_selection"]["baseline_exact_surface_generated"] is True
    assert result["boundary"]["legacy_v31_or_open_meteo_used"] is False
    assert weak["boundary"]["d0_observation_or_trajectory_not_consumed"] is True
    background = weak["forecast_background_context"]
    assert background["forecast_evidence_snapshot_id"] == weak["forecast_evidence_snapshot_id"]
    assert background["transition_control_active"] is False
    assert background["changes_probability"] is False
    assert background["physical_weather_regime_consumed"] is False

    production = bind_live_joint_weak_likelihood_for_production(result)
    assert production["production_runtime_eligible"] is True
    assert production["shadow_only"] is False
    assert production["joint_weak_likelihood"]["shadow_only"] is False
    assert production["joint_weak_likelihood"]["artifact_sha256"] == weak["artifact_sha256"]
    assert production["joint_weak_likelihood"]["joint_forecast_weak_likelihood"] == weak["joint_forecast_weak_likelihood"]


def test_taf_amd_replaces_original_inside_new_weak_source_set() -> None:
    evidence = build_joint_weak_live_forecast_evidence(
        event_date=EVENT_DATE,
        decision_as_of=AS_OF,
        source_observations=[_taf_source(), _taf_amendment()],
    )

    taf_rows = [row for row in evidence["source_revisions"] if row["source_id"] == "taf"]
    assert len(taf_rows) == 1
    assert taf_rows[0]["source_revision_id"] == "taf-immutable-amd-2"
    assert taf_rows[0]["forecast_peak_c"] == 33.0
    assert any(
        row["source_revision_id"] == "taf-immutable-1"
        and row["exclusion_reason"] == "superseded_by_latest_visible_independent_revision"
        for row in evidence["source_trace"]
    )


def test_late_discovered_older_taf_issue_is_archived_but_does_not_replace_newer_issue() -> None:
    evidence = build_joint_weak_live_forecast_evidence(
        event_date=EVENT_DATE,
        decision_as_of=AS_OF,
        source_observations=[_taf_source(), _late_discovered_older_taf_amendment()],
    )

    taf_rows = [row for row in evidence["source_revisions"] if row["source_id"] == "taf"]
    assert len(taf_rows) == 1
    assert taf_rows[0]["source_revision_id"] == "taf-immutable-1"
    assert taf_rows[0]["forecast_peak_c"] == 35.0
    old_trace = next(
        row for row in evidence["source_trace"]
        if row["source_revision_id"] == "taf-immutable-old-amd"
    )
    assert old_trace["included"] is False
    assert old_trace["exclusion_reason"] == "superseded_by_latest_visible_independent_revision"


def test_live_evidence_auto_loads_independent_sources_without_p1_materialization(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "joint_weak_live_evidence_runtime.load_live_independent_weak_source_observations",
        lambda **_: {
            "ok": True,
            "state": "available",
            "source_observations": [_taf_source()],
            "boundary": {"p1_materialization_not_required_for_live_scoring": True},
        },
    )

    evidence = build_joint_weak_live_forecast_evidence(
        event_date=EVENT_DATE,
        decision_as_of=AS_OF,
        curve_root=tmp_path,
    )

    assert evidence["ok"] is True
    assert evidence["available_source_ids"] == ["taf"]
    assert evidence["independent_source_load"]["boundary"]["p1_materialization_not_required_for_live_scoring"] is True

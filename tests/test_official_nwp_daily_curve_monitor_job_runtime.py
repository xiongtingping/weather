from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import official_nwp_daily_curve_monitor_job_runtime as monitor_runtime
from official_nwp_daily_curve_monitor_job_runtime import build_official_nwp_daily_curve_monitor_job, select_latest_plannable_run, select_plannable_run_candidates


ROOT = Path(__file__).resolve().parents[1]
CURVE = json.loads((ROOT / "config" / "forecast_sources" / "zuuu_official_daily_curve_run_contract_v1.json").read_text(encoding="utf-8"))
JOB = json.loads((ROOT / "config" / "forecast_sources" / "official_nwp_daily_curve_monitor_job_v1.json").read_text(encoding="utf-8"))


def test_selects_latest_nominal_run_before_target_window() -> None:
    run = select_latest_plannable_run(curve_contract=CURVE, provider_id="gfs", target_event_date="2026-07-13", now_utc=datetime(2026, 7, 12, 10, 30, tzinfo=timezone.utc))
    assert run == "2026-07-12T06:00:00Z"


def test_run_candidates_are_newest_first_and_bounded() -> None:
    candidates = select_plannable_run_candidates(
        curve_contract=CURVE,
        provider_id="gfs",
        target_event_date="2026-07-13",
        now_utc=datetime(2026, 7, 12, 10, 30, tzinfo=timezone.utc),
        limit=2,
    )
    assert candidates == ["2026-07-12T06:00:00Z", "2026-07-12T00:00:00Z"]


def test_monitor_job_plans_only_and_isolated_from_fast_lane(tmp_path: Path) -> None:
    result = build_official_nwp_daily_curve_monitor_job(job_contract=JOB, curve_contract=CURVE, now_utc=datetime(2026, 7, 12, 10, 30, tzinfo=timezone.utc), execute=False, curve_source_root=tmp_path / "curve", catalog_path=ROOT / "config" / "forecast_sources" / "official_nwp_zuuu_v1.json")
    assert result["state"] == "planned"
    assert result["boundary"]["hot_path_network_acquisition"] is False
    assert len(result["provider_results"]) == 1
    assert result["provider_results"][0]["execute"] is False


def test_monitor_job_skips_only_a_confirmed_matching_provider_run(tmp_path: Path, monkeypatch) -> None:
    collector = lambda **_: {"ok": True, "state": "available"}
    monkeypatch.setattr(monitor_runtime, "_persist_complete_daily_peak_revision", lambda **_: {"ok": True, "state": "persisted"})
    result = build_official_nwp_daily_curve_monitor_job(job_contract=JOB, curve_contract=CURVE, now_utc=datetime(2026, 7, 12, 10, 30, tzinfo=timezone.utc), execute=True, curve_source_root=tmp_path / "curve", catalog_path=ROOT / "config" / "forecast_sources" / "official_nwp_zuuu_v1.json", completed_provider_runs={"gfs": "2026-07-12T06:00:00Z"}, collectors={"ecmwf": collector, "icon": collector, "gfs": collector})
    assert result["provider_results"][0]["provider_id"] == "icon"
    assert result["completed_provider_runs"] == {"gfs": "2026-07-12T06:00:00Z", "icon": "2026-07-12T06:00:00Z"}


def test_monitor_retries_one_older_complete_run_when_latest_transport_is_unavailable(tmp_path: Path, monkeypatch) -> None:
    job = deepcopy(JOB)
    job["providers"] = ["gfs"]
    calls: list[str] = []

    def collector(**kwargs):
        run = kwargs["run_at_utc"]
        calls.append(run)
        return (
            {"ok": False, "state": "blocked", "blockers": ["official_transport_not_published"]}
            if run == "2026-07-12T06:00:00Z"
            else {"ok": True, "state": "available"}
        )

    # This unit test isolates run fallback; persistence is separately tested
    # with real immutable point history.
    monkeypatch.setattr(monitor_runtime, "_persist_complete_daily_peak_revision", lambda **_: {"ok": True, "state": "persisted"})

    result = build_official_nwp_daily_curve_monitor_job(
        job_contract=job,
        curve_contract=CURVE,
        now_utc=datetime(2026, 7, 12, 10, 30, tzinfo=timezone.utc),
        execute=True,
        curve_source_root=tmp_path / "curve",
        catalog_path=ROOT / "config" / "forecast_sources" / "official_nwp_zuuu_v1.json",
        collectors={"gfs": collector},
    )

    item = result["provider_results"][0]
    assert result["ok"] is True
    assert calls[0] == "2026-07-12T06:00:00Z"
    assert calls[-1] == "2026-07-12T00:00:00Z"
    assert item["fallback_run_used"] is True
    assert item["attempted_run_at_utc"] == ["2026-07-12T06:00:00Z", "2026-07-12T00:00:00Z"]

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import official_nwp_live_snapshot_monitor_runtime as runtime


ROOT = Path(__file__).resolve().parents[1]
CURVE = json.loads((ROOT / "config/forecast_sources/zuuu_official_daily_curve_run_contract_v1.json").read_text(encoding="utf-8"))
JOB = json.loads((ROOT / "config/forecast_sources/official_nwp_live_snapshot_monitor_job_v1.json").read_text(encoding="utf-8"))


def test_live_snapshot_plan_is_separate_from_curve_and_hot_path(tmp_path: Path) -> None:
    result = runtime.build_official_nwp_live_snapshot_monitor_job(
        job_contract=JOB,
        curve_contract=CURVE,
        now_utc=datetime(2026, 7, 12, 10, 30, tzinfo=timezone.utc),
        source_root=tmp_path / "live",
        catalog_path=ROOT / "config/forecast_sources/official_nwp_zuuu_v1.json",
        prewarm_cache=tmp_path / "prewarm" / "current.json",
        execute=False,
    )
    assert result["ok"] is True
    assert result["state"] == "planned"
    assert result["target_peak_anchor_valid_time_utc"] == "2026-07-13T06:00:00Z"
    assert result["boundary"]["curve_root_unused"] is True
    assert result["boundary"]["hot_path_network_acquisition"] is False


def test_live_snapshot_retries_older_run_then_publishes_available_mask(tmp_path: Path, monkeypatch) -> None:
    job = deepcopy(JOB)
    job["providers"] = ["gfs"]
    calls: list[str] = []

    def collector(**kwargs):
        calls.append(kwargs["run_at_utc"])
        return {"ok": kwargs["run_at_utc"] == "2026-07-12T00:00:00Z", "state": "available"}

    monkeypatch.setattr(runtime, "_default_collectors", lambda: {"gfs": collector})
    result = runtime.build_official_nwp_live_snapshot_monitor_job(
        job_contract=job,
        curve_contract=CURVE,
        now_utc=datetime(2026, 7, 12, 10, 30, tzinfo=timezone.utc),
        source_root=tmp_path / "live",
        catalog_path=ROOT / "config/forecast_sources/official_nwp_zuuu_v1.json",
        prewarm_cache=tmp_path / "prewarm" / "current.json",
        execute=True,
    )
    assert calls == ["2026-07-12T06:00:00Z", "2026-07-12T00:00:00Z"]
    assert result["provider_results"][0]["attempted_run_at_utc"] == calls
    assert result["prewarm"]["ok"] is True
    assert result["boundary"]["curve_root_unused"] is True


def test_prewarm_publication_clock_is_after_planning_clock_for_new_receipts(tmp_path: Path, monkeypatch) -> None:
    job = deepcopy(JOB)
    job["providers"] = ["gfs"]
    captured = {}
    planning_clock = datetime(2000, 1, 1, tzinfo=timezone.utc)

    monkeypatch.setattr(runtime, "_default_collectors", lambda: {"gfs": lambda **_: {"ok": True, "state": "available"}})

    def build_prewarm(**kwargs):
        captured["as_of"] = kwargs["as_of"]
        return {"ok": True, "bundle": {}, "cache_path": str(kwargs["cache_path"]), "boundary": {}}

    monkeypatch.setattr(runtime, "build_official_nwp_prewarm_plan", build_prewarm)
    monkeypatch.setattr(runtime, "persist_official_nwp_prewarm", lambda *, plan: {"ok": True, "state": "ready"})
    result = runtime.build_official_nwp_live_snapshot_monitor_job(
        job_contract=job,
        curve_contract=CURVE,
        now_utc=planning_clock,
        source_root=tmp_path / "live",
        catalog_path=ROOT / "config/forecast_sources/official_nwp_zuuu_v1.json",
        prewarm_cache=tmp_path / "prewarm" / "current.json",
        execute=True,
    )
    assert result["ok"] is True
    assert captured["as_of"] > planning_clock


def test_provider_selection_rotates_by_collection_slot_not_utc_hour(tmp_path: Path, monkeypatch) -> None:
    job = deepcopy(JOB)
    job["providers"] = ["ecmwf", "icon", "gfs"]
    selected: list[str] = []

    monkeypatch.setattr(
        runtime,
        "_default_collectors",
        lambda: {provider: (lambda **_: {"ok": True, "state": "available"}) for provider in job["providers"]},
    )
    monkeypatch.setattr(runtime, "build_official_nwp_prewarm_plan", lambda **_: {"ok": True})
    monkeypatch.setattr(runtime, "persist_official_nwp_prewarm", lambda *, plan: {"ok": True, "state": "ready"})
    for minute in (0, 15, 30):
        result = runtime.build_official_nwp_live_snapshot_monitor_job(
            job_contract=job,
            curve_contract=CURVE,
            now_utc=datetime(2026, 7, 12, 10, minute, tzinfo=timezone.utc),
            source_root=tmp_path / f"live-{minute}",
            catalog_path=ROOT / "config/forecast_sources/official_nwp_zuuu_v1.json",
            prewarm_cache=tmp_path / f"prewarm-{minute}" / "current.json",
            execute=True,
        )
        selected.append(result["provider_results"][0]["provider_id"])
        assert result["snapshot_max_age_sec"] >= 3 * result["collection_interval_sec"]
    assert set(selected) == {"ecmwf", "icon", "gfs"}

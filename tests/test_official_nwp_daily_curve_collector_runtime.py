from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from official_nwp_daily_curve_collector_runtime import build_nomads_gfs_hourly_url, collect_official_daily_curve


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads((ROOT / "config" / "forecast_sources" / "zuuu_official_daily_curve_run_contract_v1.json").read_text(encoding="utf-8"))


def test_gfs_url_is_direct_official_hourly_filter() -> None:
    url = build_nomads_gfs_hourly_url(run_at_utc="2026-07-11T12:00:00Z", valid_time_utc="2026-07-12T03:00:00Z")

    assert url.startswith("https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25_1hr.pl?")
    assert "gfs.t12z.pgrb2.0p25.f015" in url
    assert "api.open-meteo.com" not in url
    query = parse_qs(urlsplit(url).query)
    # A station-centred sub-cell request can be rounded to a one-cell GRIB
    # whose sole coordinate is outside ZUUU.  The official filter area must
    # retain a full GFS-cell margin, so ecCodes has a valid nearest point.
    assert float(query["leftlon"][0]) < 103.95 < float(query["rightlon"][0])
    assert float(query["bottomlat"][0]) < 30.576 < float(query["toplat"][0])


def test_default_is_no_network_plan_and_uses_isolated_curve_root(tmp_path: Path) -> None:
    result = collect_official_daily_curve(contract=CONTRACT, provider_id="gfs", run_at_utc="2026-07-11T12:00:00Z", target_event_date="2026-07-12", curve_source_root=tmp_path / "curve", catalog_path=ROOT / "config" / "forecast_sources" / "official_nwp_zuuu_v1.json")

    assert result["ok"] is True
    assert result["state"] == "planned"
    assert result["point_results"] == []
    assert result["boundary"]["isolated_from_hot_prewarm_root"] is True


def test_explicit_execution_calls_only_declared_points_and_never_needs_hot_root(tmp_path: Path) -> None:
    calls: list[dict] = []
    def collector(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "state": "available"}

    result = collect_official_daily_curve(contract=CONTRACT, provider_id="gfs", run_at_utc="2026-07-11T12:00:00Z", target_event_date="2026-07-12", curve_source_root=tmp_path / "curve", catalog_path=ROOT / "config" / "forecast_sources" / "official_nwp_zuuu_v1.json", execute=True, collectors={"gfs": collector})

    assert result["ok"] is True
    assert len(calls) == result["plan"]["expected_point_count"]
    assert all(call["source_root"] == tmp_path / "curve" for call in calls)


def test_execution_resumes_exact_immutable_history_points_without_refetch(tmp_path: Path) -> None:
    run, valid = "2026-07-11T12:00:00Z", "2026-07-11T16:00:00Z"
    history = tmp_path / "curve" / "gfs" / "history"
    history.mkdir(parents=True)
    (history / "point.json").write_text(json.dumps({"schema": "official_nwp_normalized_snapshot_v1", "provider_id": "gfs", "run_at_utc": run, "valid_time_utc": valid, "raw_sha256": "raw", "snapshot_sha256": "snap"}), encoding="utf-8")
    called = []
    result = collect_official_daily_curve(contract=CONTRACT, provider_id="gfs", run_at_utc=run, target_event_date="2026-07-12", curve_source_root=tmp_path / "curve", catalog_path=ROOT / "config" / "forecast_sources" / "official_nwp_zuuu_v1.json", execute=True, collectors={"gfs": lambda **kwargs: called.append(kwargs) or {"ok": True, "state": "available"}})
    assert result["point_results"][0]["state"] == "already_collected"
    assert len(called) == result["plan"]["expected_point_count"] - 1


def test_execution_refuses_to_backfill_a_closed_target_day(tmp_path: Path) -> None:
    result = collect_official_daily_curve(contract=CONTRACT, provider_id="gfs", run_at_utc="2026-07-09T12:00:00Z", target_event_date="2026-07-10", curve_source_root=tmp_path / "curve", catalog_path=ROOT / "config" / "forecast_sources" / "official_nwp_zuuu_v1.json", execute=True, now_utc=datetime(2026, 7, 12, tzinfo=timezone.utc), collectors={"gfs": lambda **_: {"ok": True}})

    assert result["ok"] is False
    assert result["blockers"] == ["official_nwp_daily_curve_historical_target_execute_forbidden"]


def test_execution_can_use_bounded_parallel_workers_without_changing_curve_order(tmp_path: Path) -> None:
    result = collect_official_daily_curve(
        contract=CONTRACT,
        provider_id="gfs",
        run_at_utc="2026-07-11T12:00:00Z",
        target_event_date="2026-07-12",
        curve_source_root=tmp_path / "curve",
        catalog_path=ROOT / "config" / "forecast_sources" / "official_nwp_zuuu_v1.json",
        execute=True,
        max_point_workers=2,
        collectors={"gfs": lambda **kwargs: {"ok": True, "state": "available", "echo": kwargs["valid_time_utc"]}},
    )
    assert result["ok"] is True
    assert [item["valid_time_utc"] for item in result["point_results"]] == result["plan"]["expected_valid_times_utc"]
    assert all(item["echo"] == item["valid_time_utc"] for item in result["point_results"])

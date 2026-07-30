from __future__ import annotations

import json
from pathlib import Path

from official_nwp_daily_curve_plan_runtime import build_official_nwp_daily_curve_plan


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads((ROOT / "config" / "forecast_sources" / "zuuu_official_daily_curve_run_contract_v1.json").read_text(encoding="utf-8"))


def test_ecmwf_plan_uses_declared_3h_curve_and_local_target_day() -> None:
    plan = build_official_nwp_daily_curve_plan(contract=CONTRACT, provider_id="ecmwf", run_at_utc="2026-07-11T12:00:00Z", target_event_date="2026-07-12")

    assert plan["ok"] is True
    assert plan["curve_time_resolution_hours"] == 3
    assert plan["expected_valid_times_utc"][0] == "2026-07-11T18:00:00Z"
    assert plan["expected_valid_times_utc"][-1] == "2026-07-12T15:00:00Z"
    assert plan["boundary"]["interpolation_or_cross_run_fill_forbidden"] is True


def test_icon_hourly_and_jma_unconfigured_are_explicit() -> None:
    icon = build_official_nwp_daily_curve_plan(contract=CONTRACT, provider_id="icon", run_at_utc="2026-07-11T12:00:00Z", target_event_date="2026-07-12")
    jma = build_official_nwp_daily_curve_plan(contract=CONTRACT, provider_id="jma", run_at_utc="2026-07-11T12:00:00Z", target_event_date="2026-07-12")

    assert icon["ok"] is True
    assert icon["curve_time_resolution_hours"] == 1
    assert icon["expected_point_count"] == 24
    assert jma["ok"] is False
    assert "official_nwp_daily_curve_authorized_provider_not_configured" in jma["blockers"]

    late = build_official_nwp_daily_curve_plan(contract=CONTRACT, provider_id="icon", run_at_utc="2026-07-11T18:00:00Z", target_event_date="2026-07-12")
    assert late["ok"] is True
    assert late["expected_valid_times_utc"][0] == "2026-07-11T18:00:00Z"


def test_same_day_06z_run_is_eligible_for_remaining_local_day() -> None:
    plan = build_official_nwp_daily_curve_plan(
        contract=CONTRACT,
        provider_id="ecmwf",
        run_at_utc="2026-07-22T06:00:00Z",
        target_event_date="2026-07-22",
    )
    assert plan["ok"] is True
    assert plan["expected_valid_times_utc"] == [
        "2026-07-22T06:00:00Z",
        "2026-07-22T09:00:00Z",
        "2026-07-22T12:00:00Z",
        "2026-07-22T15:00:00Z",
    ]

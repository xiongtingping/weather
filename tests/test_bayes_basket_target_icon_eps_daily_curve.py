from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.icon_eps_daily_curve import build_icon_eps_daily_archive
from bayes_basket_target.station_registry import coordinate_registry_manifest
from scripts.run_bayes_basket_target_icon_eps_daily_curve import build_capture_plan


STATIONS = tuple(coordinate_registry_manifest()["station_ids"])
INDICES = {station: index * 100 for index, station in enumerate(STATIONS)}


def _cohort(lead: int) -> dict[str, object]:
    run = datetime(2026, 8, 1, tzinfo=timezone.utc)
    valid = run + timedelta(hours=lead)
    raw_sha = f"{lead + 1:064x}"
    records = [
        {
            "station_id": station,
            "member_id": member,
            "temperature_2m_c": 10.0 + lead / 10.0 + member / 100.0,
            "raw_sha256": raw_sha,
        }
        for member in range(1, 41)
        for station in sorted(STATIONS)
    ]
    identity = {
        "schema": "bayes_basket_target_icon_global_eps_station_member_cohort_v1",
        "state": "OFFICIAL_40_MEMBER_45_STATION_DECODED_CANDIDATE_ONLY",
        "provider_id": "icon_global_eps_official",
        "product_id": "DWD_ICON_GLOBAL_EPS",
        "independence_family_id": "DWD_ICON_EPS",
        "run_initialization_utc": run.isoformat().replace("+00:00", "Z"),
        "provider_published_at_utc": "2026-08-01T02:00:00Z",
        "response_completed_at_utc": "2026-08-01T03:00:00Z",
        "forecast_hour": lead,
        "valid_time_utc": valid.isoformat().replace("+00:00", "Z"),
        "raw_sha256": raw_sha,
        "member_count": 40,
        "station_count": 45,
        "member_record_count": 1800,
        "lineage": {
            "source_role": "official_native",
            "transport_provider_id": "DWD",
            "official_grid": {
                "source_grid_uuid": "ae487d14fe2e11e4af85e50a2a56a360",
                "source_grid_cell_count": 737_280,
                "station_nearest_cell_indices": INDICES,
            },
        },
        "member_station_records": records,
        "formal_p0_eligible": False,
        "hard_no_eligible": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def test_builds_45_by_40_complete_local_day_members_without_authority() -> None:
    archive = build_icon_eps_daily_archive(
        cohorts=[_cohort(lead) for lead in range(0, 24, 3)],
        event_date="2026-08-01",
        station_timezones={station: "UTC" for station in STATIONS},
        built_at_utc="2026-08-01T04:00:00Z",
    )

    assert archive["state"] == "OFFICIAL_MEMBER_DAILY_CURVES_READY_STRICT_OOS_PENDING"
    assert archive["station_count"] == 45
    assert archive["member_count"] == 40
    assert archive["capture_count"] == 45
    assert all(capture["daily_curve_complete"] for capture in archive["captures"])
    assert all(len(capture["members"]) == 40 for capture in archive["captures"])
    assert archive["holdout_eligible"] is True
    assert archive["formal_p0_eligible"] is False
    assert archive["hard_no_eligible"] is False
    assert archive["boundary"]["orders_submitted"] == 0


def test_missing_middle_lead_fails_closed() -> None:
    with pytest.raises(ContractError, match="curve_incomplete"):
        build_icon_eps_daily_archive(
            cohorts=[_cohort(lead) for lead in (0, 3, 6, 9, 15, 18, 21)],
            event_date="2026-08-01",
            station_timezones={station: "UTC" for station in STATIONS},
            built_at_utc="2026-08-01T04:00:00Z",
        )


def test_capture_plan_spans_all_station_local_days_at_three_hour_cadence() -> None:
    zones = {station: "Asia/Shanghai" for station in STATIONS}
    zones["NZWN"] = "Pacific/Auckland"
    zones["KLAX"] = "America/Los_Angeles"
    plan = build_capture_plan(
        run_initialization_utc="2026-08-01T00:00:00Z",
        event_date="2026-08-02",
        station_timezones=zones,
        as_of_utc="2026-08-01T08:00:00Z",
    )

    assert plan["forecast_hours"] == list(range(12, 55, 3))
    assert plan["lead_count"] == 15
    assert plan["raw_member_fanout"] is False
    assert plan["formal_p0_eligible"] is False

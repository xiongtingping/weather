from __future__ import annotations

from datetime import datetime, timezone

import pytest

from station_p1_preobservation_schedule import (
    load_schedule,
    next_preobservation_plan,
    select_scheduled_p1_source,
    validate_observation_against_plan,
)


def test_frozen_schedule_has_45_unique_stations_and_ten_asos_ready() -> None:
    schedule = load_schedule()
    assert len(schedule["stations"]) == 45
    assert len({row["station_id"] for row in schedule["stations"]}) == 45
    assert sum(row["asos_p1_oos_ready"] for row in schedule["stations"]) == 10


def test_us_station_plan_freezes_exact_and_prior_madis_slots() -> None:
    plan = next_preobservation_plan(
        load_schedule(),
        station_id="KLAX",
        as_of_utc=datetime(2026, 8, 5, 8, 49, tzinfo=timezone.utc),
    )
    assert plan["scheduled_metar_observation_at_utc"] == "2026-08-05T08:53:00Z"
    assert plan["exact_asos_observation_at_utc"] == "2026-08-05T08:53:00Z"
    assert plan["madis_preobservation_at_utc"] == "2026-08-05T08:50:00Z"
    assert plan["madis_lead_minutes"] == 3


def test_dual_minute_station_selects_next_slot_without_post_observation_choice() -> None:
    plan = next_preobservation_plan(
        load_schedule(),
        station_id="EHAM",
        as_of_utc=datetime(2026, 8, 5, 8, 26, tzinfo=timezone.utc),
    )
    assert plan["scheduled_metar_observation_at_utc"] == "2026-08-05T08:55:00Z"
    assert plan["source_priority"] == ["AWC_METAR_SPECI"]


def test_schedule_binding_rejects_wrong_madis_cycle_and_unfrozen_minute() -> None:
    schedule = load_schedule()
    with pytest.raises(ValueError, match="observation_slot_mismatch"):
        validate_observation_against_plan(
            load_schedule(),
            station_id="KLAX",
            source_product="MADIS_HFMETAR_5MIN",
            planned_metar_observation_at_utc="2026-08-05T08:53:00Z",
            observed_at_utc="2026-08-05T08:45:00Z",
        )


def test_source_selector_is_priority_ordered_not_value_ordered() -> None:
    selected = select_scheduled_p1_source(
        load_schedule(),
        station_id="KLAX",
        planned_metar_observation_at_utc="2026-08-05T08:53:00Z",
        candidates={
            "EXACT_ASOS_1MIN": {
                "observed_at_utc": "2026-08-05T08:53:00Z", "temperature_c": 20.0,
            },
            "MADIS_HFMETAR_5MIN": {
                "observed_at_utc": "2026-08-05T08:50:00Z", "temperature_c": 40.0,
            },
            "AWC_METAR_SPECI": {
                "observed_at_utc": "2026-08-05T08:53:00Z", "temperature_c": 50.0,
            },
        },
    )
    assert selected["source_product"] == "EXACT_ASOS_1MIN"


def test_source_selector_falls_back_to_frozen_madis_slot_only() -> None:
    selected = select_scheduled_p1_source(
        load_schedule(),
        station_id="KSFO",
        planned_metar_observation_at_utc="2026-08-05T08:56:00Z",
        candidates={
            "EXACT_ASOS_1MIN": None,
            "MADIS_HFMETAR_5MIN": {
                "observed_at_utc": "2026-08-05T08:55:00Z", "temperature_c": 21.0,
            },
        },
    )
    assert selected["source_product"] == "MADIS_HFMETAR_5MIN"
    assert selected["rejected_higher_priority_sources"] == [
        {"source_product": "EXACT_ASOS_1MIN", "reason": "DATA_MISSING"}
    ]
    with pytest.raises(ValueError, match="unfrozen_planned_minute"):
        validate_observation_against_plan(
            load_schedule(),
            station_id="KLAX",
            source_product="EXACT_ASOS_1MIN",
            planned_metar_observation_at_utc="2026-08-05T08:52:00Z",
            observed_at_utc="2026-08-05T08:52:00Z",
        )

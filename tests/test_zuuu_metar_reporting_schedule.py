from __future__ import annotations

from zuuu_metar_reporting_schedule import (
    EXPECTED_REGULAR_SLOT_COUNT,
    SCHEDULE_ID,
    expected_regular_metar_slots_utc,
    build_zuuu_primary_metar_schedule_contract,
    validate_regular_metar_slot_coverage,
)


def test_zuuu_schedule_is_44_slots_with_hourly_overnight_and_half_hour_daytime():
    slots = expected_regular_metar_slots_utc("2026-07-11")

    assert len(slots) == EXPECTED_REGULAR_SLOT_COUNT == 44
    assert slots[:5] == [
        "2026-07-10T16:00:00Z",
        "2026-07-10T17:00:00Z",
        "2026-07-10T18:00:00Z",
        "2026-07-10T19:00:00Z",
        "2026-07-10T20:00:00Z",
    ]
    assert "2026-07-10T16:30:00Z" not in slots
    assert slots[-1] == "2026-07-11T15:30:00Z"


def test_schedule_coverage_rejects_the_old_false_48_slot_grid():
    slots = expected_regular_metar_slots_utc("2026-07-11")
    complete = validate_regular_metar_slot_coverage(event_date="2026-07-11", observed_valid_times_utc=slots)
    old_grid = validate_regular_metar_slot_coverage(
        event_date="2026-07-11",
        observed_valid_times_utc=slots + ["2026-07-10T16:30:00Z"],
    )

    assert complete["schedule_id"] == SCHEDULE_ID
    assert complete["complete"] is True
    assert old_grid["complete"] is False
    assert old_grid["unexpected_regular_metar_slots_utc"] == ["2026-07-10T16:30:00Z"]


def test_speci_cannot_contribute_to_the_regular_metar_final_x_schedule():
    contract = build_zuuu_primary_metar_schedule_contract("2026-07-11")

    assert contract["specli_can_fill_regular_slot"] is False

from __future__ import annotations

from amsc_poll_slot_runtime import assess_plan_poll_slot, assess_zuuu_regular_metar_schedule, poll_slot_utc_for_attempt


def _plan(latest_at: str) -> dict:
    return {
        "effective_payload": {
            "weather_likelihood_features": {"zuuu_metar_raw_amsc_latest_at": latest_at}
        }
    }


def test_slot_is_bound_to_actual_attempt_not_launcher_start() -> None:
    assert poll_slot_utc_for_attempt("2026-07-12T00:00:24Z") == "2026-07-12T00:00:00Z"

    state = assess_plan_poll_slot(
        _plan("2026-07-11T23:30:00Z"),
        attempt_started_at="2026-07-12T00:00:24Z",
    )
    assert state["fresh"] is False
    assert state["reason"] == "latest_metar_before_poll_slot"
    assert state["expected_slot_at"] == "2026-07-12T00:00:00Z"


def test_only_exact_slot_is_accepted() -> None:
    exact = assess_plan_poll_slot(
        _plan("2026-07-12T00:30:00Z"),
        attempt_started_at="2026-07-12T00:30:54Z",
    )
    future = assess_plan_poll_slot(
        _plan("2026-07-12T01:00:00Z"),
        attempt_started_at="2026-07-12T00:30:54Z",
    )
    assert exact["fresh"] is True
    assert exact["reason"] == "exact_poll_slot_match"
    assert future["fresh"] is False
    assert future["reason"] == "latest_metar_after_poll_slot"


def test_overnight_local_half_slots_are_not_expected_regular_metar_events() -> None:
    unscheduled = assess_zuuu_regular_metar_schedule("2026-07-10T16:30:10Z")
    scheduled = assess_zuuu_regular_metar_schedule("2026-07-10T16:00:10Z")
    plan_state = assess_plan_poll_slot(
        _plan("2026-07-10T16:30:00Z"),
        attempt_started_at="2026-07-10T16:30:10Z",
    )

    assert unscheduled["scheduled_regular_metar_slot"] is False
    assert unscheduled["reason"] == "unscheduled_regular_metar_slot"
    assert scheduled["scheduled_regular_metar_slot"] is True
    assert plan_state["fresh"] is False
    assert plan_state["reason"] == "unscheduled_regular_metar_slot"

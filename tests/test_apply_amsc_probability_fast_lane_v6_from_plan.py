from __future__ import annotations

from datetime import datetime, timezone

from scripts import apply_amsc_probability_fast_lane_v6_from_plan as runtime


def test_current_zuuu_event_date_uses_station_day_not_et_run_date() -> None:
    instant = datetime(2026, 7, 15, 16, 30, tzinfo=timezone.utc)

    assert runtime.current_zuuu_event_date(instant) == "2026-07-16"


def test_current_zuuu_event_date_accepts_naive_utc_for_deterministic_replay() -> None:
    instant = datetime(2026, 7, 15, 23, 30)

    assert runtime.current_zuuu_event_date(instant) == "2026-07-16"

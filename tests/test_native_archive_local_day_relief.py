"""The archives' local-day grid bar is per station, not per UTC date."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bayes_basket_target import ecmwf_ifs_archive, noaa_gefs_archive
from bayes_basket_target.contracts import ContractError


RUN_18Z_D1 = datetime(2026, 9, 2, 18, tzinfo=timezone.utc)


def test_ecmwf_d1_18z_run_seals_a_shorter_grid_for_an_eastern_station() -> None:
    # 03:00 Tokyo on the event day: six period ends fall on 2026-09-03.
    steps = ecmwf_ifs_archive._period_end_steps(
        event_date="2026-09-03",
        settlement_timezone="Asia/Tokyo",
        run_init_utc=RUN_18Z_D1,
    )
    assert steps == [3, 6, 9, 12, 15, 18]
    # A western station's day has not begun: the full-day bar still holds
    # and is met.
    steps = ecmwf_ifs_archive._period_end_steps(
        event_date="2026-09-03",
        settlement_timezone="America/Los_Angeles",
        run_init_utc=RUN_18Z_D1,
    )
    assert len(steps) == 8


def test_gefs_d1_18z_run_seals_a_shorter_grid_for_an_eastern_station() -> None:
    hours = noaa_gefs_archive._forecast_hours(
        event_date="2026-09-03",
        settlement_timezone="Asia/Tokyo",
        run_init_utc=RUN_18Z_D1,
    )
    assert hours == [0, 3, 6, 9, 12, 15, 18]
    hours = noaa_gefs_archive._forecast_hours(
        event_date="2026-09-03",
        settlement_timezone="America/Los_Angeles",
        run_init_utc=RUN_18Z_D1,
    )
    assert len(hours) == 8


def test_a_run_preceding_the_station_day_keeps_the_full_day_bar() -> None:
    # A d2 run precedes every station's day; the bar is the full grid and a
    # 72h horizon cannot reach a day that starts 66h out, so it is refused.
    far = datetime(2026, 8, 31, 0, tzinfo=timezone.utc)
    with pytest.raises(ContractError, match="ecmwf_archive_local_day_grid_incomplete"):
        ecmwf_ifs_archive._period_end_steps(
            event_date="2026-09-03",
            settlement_timezone="America/Los_Angeles",
            run_init_utc=far,
        )
    with pytest.raises(ContractError, match="gefs_archive_local_day_grid_incomplete"):
        noaa_gefs_archive._forecast_hours(
            event_date="2026-09-03",
            settlement_timezone="America/Los_Angeles",
            run_init_utc=far,
        )

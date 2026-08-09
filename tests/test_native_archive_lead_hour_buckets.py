"""Training groups are keyed by lead in hours, not by the calendar-day label.

Measured over the retained archive, the two disagree badly::

    label   min     median   max     span
    d0     -12.0h    -2.0h    7.0h    19h
    d1      12.0h    22.0h   31.0h    19h
    d2      18.0h    35.0h   55.0h    37h

d1 and d2 overlap across 18-31h, so two samples issued the same number of hours
ahead landed in different training groups purely because of which calendar day
their run fell on; and d2 alone spans 37 hours, so its within-group spread was
wider than the gap between groups.  Both follow from the label being a property
of the run's UTC date while skill is a property of the lead to the station's
own local day.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bayes_basket_target.native_archive_lead import (
    LEAD_HOUR_BUCKETS,
    LONGEST_LEAD_HOUR_BUCKET,
    lead_hour_bucket,
    lead_hour_bucket_for_run,
    lead_hours_to_local_day,
)


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


# KMIA is UTC-4 in August, so its 2026-08-09 begins at 04:00Z.
KMIA_DAY_START = _utc("2026-08-09T04:00:00Z")


@pytest.mark.parametrize(
    "run_init,expected_hours,expected_bucket",
    [
        ("2026-08-07T00:00:00Z", 52.0, "h48_plus"),
        ("2026-08-07T18:00:00Z", 34.0, "h24_48"),
        ("2026-08-08T00:00:00Z", 28.0, "h24_48"),
        ("2026-08-08T18:00:00Z", 10.0, "h00_12"),
        # Issued after the local day began: the freshest information there is.
        ("2026-08-09T12:00:00Z", -8.0, "h00_12"),
    ],
)
def test_runs_bucket_by_their_lead_to_the_local_day(
    run_init: str, expected_hours: float, expected_bucket: str
) -> None:
    hours = lead_hours_to_local_day(
        day_start_utc=KMIA_DAY_START, run_init=_utc(run_init)
    )

    assert hours == expected_hours
    assert lead_hour_bucket_for_run(
        day_start_utc=KMIA_DAY_START, run_init=_utc(run_init)
    ) == expected_bucket


def test_the_same_run_buckets_differently_for_stations_on_different_clocks() -> None:
    """This is the whole point: the label cannot capture what the hours do.

    One 18Z run is 34 hours ahead of Miami's day and 22 hours ahead of
    Auckland's, because their local days begin twelve hours apart.  Both rows
    carry the calendar label ``d2``.
    """

    run_init = _utc("2026-08-07T18:00:00Z")
    # NZWN is UTC+12, so its 2026-08-09 begins at 2026-08-08T12:00Z.
    nzwn_day_start = _utc("2026-08-08T12:00:00Z")

    miami = lead_hour_bucket_for_run(
        day_start_utc=KMIA_DAY_START, run_init=run_init
    )
    auckland = lead_hour_bucket_for_run(
        day_start_utc=nzwn_day_start, run_init=run_init
    )

    assert lead_hours_to_local_day(
        day_start_utc=KMIA_DAY_START, run_init=run_init
    ) == 34.0
    assert lead_hours_to_local_day(
        day_start_utc=nzwn_day_start, run_init=run_init
    ) == 18.0
    assert miami == "h24_48"
    assert auckland == "h12_24"
    assert miami != auckland


def test_a_negative_lead_is_the_freshest_bucket_not_a_separate_one() -> None:
    # Half the d0 samples in the retained archive are negative -- a same-day
    # run issued after the local day opened.  That is the most recent
    # information available for the day, not a different regime.
    assert lead_hour_bucket(-12.0) == "h00_12"
    assert lead_hour_bucket(0.0) == "h00_12"


def test_boundaries_are_inclusive_upward() -> None:
    # A run exactly on a boundary belongs to the shorter lead, so a bucket
    # cannot claim a sample the next one down already covers.
    assert lead_hour_bucket(12.0) == "h00_12"
    assert lead_hour_bucket(12.01) == "h12_24"
    assert lead_hour_bucket(24.0) == "h12_24"
    assert lead_hour_bucket(48.0) == "h24_48"
    assert lead_hour_bucket(48.01) == LONGEST_LEAD_HOUR_BUCKET


def test_the_longest_bucket_is_open_ended() -> None:
    # Runs are bounded by UTC calendar day while the lead is measured to the
    # local day start, so a western station sees past 48h from the same run
    # set -- 55h in the retained archive.  A named ceiling the collection
    # window does not enforce would create a bucket that never fills.
    assert lead_hour_bucket(55.0) == LONGEST_LEAD_HOUR_BUCKET
    assert lead_hour_bucket(1000.0) == LONGEST_LEAD_HOUR_BUCKET
    assert LONGEST_LEAD_HOUR_BUCKET not in {name for name, _ in LEAD_HOUR_BUCKETS}


def test_buckets_are_ordered_and_non_overlapping() -> None:
    uppers = [upper for _name, upper in LEAD_HOUR_BUCKETS]

    assert uppers == sorted(uppers)
    assert len(set(uppers)) == len(uppers)
    assert len({name for name, _ in LEAD_HOUR_BUCKETS}) == len(LEAD_HOUR_BUCKETS)

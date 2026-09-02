"""A report on the wire seconds before its nominal minute is still that report."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bayes_basket_target.source_policy import (
    EARLY_TRANSMISSION_TOLERANCE,
    report_seen_within_early_transmission_tolerance,
)

NOMINAL = datetime(2026, 9, 2, 13, 0, tzinfo=timezone.utc)


def test_measured_early_transmissions_are_inside_the_tolerance() -> None:
    # 2026-09-02, thirty days, 50,169 AWC reports: max 38.8 s early, p99 34.5 s.
    assert EARLY_TRANSMISSION_TOLERANCE == timedelta(seconds=120)
    for early_seconds in (0.0, 12.2, 34.5, 38.8, 120.0):
        assert report_seen_within_early_transmission_tolerance(
            NOMINAL, NOMINAL - timedelta(seconds=early_seconds)
        )


def test_late_sightings_are_always_in_order() -> None:
    assert report_seen_within_early_transmission_tolerance(
        NOMINAL, NOMINAL + timedelta(minutes=7)
    )


def test_a_report_seen_a_whole_minute_past_the_tolerance_is_a_rewind() -> None:
    assert not report_seen_within_early_transmission_tolerance(
        NOMINAL, NOMINAL - EARLY_TRANSMISSION_TOLERANCE - timedelta(seconds=1)
    )
    # Stamped for the next hour, seen in this one: never a transmission habit.
    assert not report_seen_within_early_transmission_tolerance(
        NOMINAL + timedelta(hours=1), NOMINAL
    )

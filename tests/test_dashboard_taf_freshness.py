"""The 发布时间 lamp's TAF freshness reads each station against its own usual
wait (user 2026-10-16: 「发布时间」红了).

EGLC, London City, closes at night: its last TAF of the day lands ~10:55Z
and the next ~02:00Z, so the fleet's 9 h line called it stale every evening.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import calibration_dashboard as cd  # noqa: E402

EGLC = [
    "2026-10-12T02:02:00Z", "2026-10-12T04:53:00Z", "2026-10-12T08:03:00Z",
    "2026-10-12T10:54:00Z", "2026-10-13T01:53:00Z", "2026-10-13T04:57:00Z",
    "2026-10-13T07:56:00Z", "2026-10-13T11:00:00Z", "2026-10-14T02:32:00Z",
    "2026-10-14T04:54:00Z", "2026-10-14T07:55:00Z", "2026-10-14T10:56:00Z",
    "2026-10-15T02:02:00Z", "2026-10-15T04:59:00Z", "2026-10-15T07:58:00Z",
    "2026-10-15T10:54:00Z",
]


def test_a_station_closed_at_night_waits_its_own_overnight_gap() -> None:
    gap = cd._taf_usual_gap_hours(EGLC, "2026-10-15T10:54:00Z")
    assert gap is not None and 14.5 < gap < 15.7
    assert cd._taf_freshness(13.0, gap) == "fresh"
    # Past its own usual wait it still goes late, then stale.
    assert cd._taf_freshness(gap + 1.0, gap) == "late"
    assert cd._taf_freshness(gap + 4.0, gap) == "stale"


def test_the_daytime_issue_of_the_same_station_keeps_its_own_short_wait() -> None:
    # After the ~08Z issue the next follows in ~3 h: the fleet lines stand.
    gap = cd._taf_usual_gap_hours(EGLC[:-1], "2026-10-15T07:58:00Z")
    assert gap is not None and gap < 3.5
    assert cd._taf_freshness(7.0, gap) == "late"
    assert cd._taf_freshness(10.0, gap) == "stale"


def test_too_few_samples_fall_back_to_the_fleet_lines() -> None:
    assert cd._taf_usual_gap_hours(EGLC[-3:], EGLC[-1]) is None
    assert cd._taf_freshness(6.0, None) == "fresh"
    assert cd._taf_freshness(8.0, None) == "late"
    assert cd._taf_freshness(9.5, None) == "stale"
    assert cd._taf_freshness(None, None) == "stale"


def test_a_six_hourly_station_is_judged_as_before() -> None:
    hours = [f"2026-10-1{d}T{h:02d}:00:00Z" for d in range(2, 6) for h in (5, 11, 17, 23)]
    gap = cd._taf_usual_gap_hours(hours, "2026-10-15T23:00:00Z")
    assert gap == 6.0
    for age in (6.0, 7.0, 9.5):
        assert cd._taf_freshness(age, gap) == cd._taf_freshness(age, None)

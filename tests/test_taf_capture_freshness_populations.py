"""Never captured is not "three hours without a bulletin", and must not read as it.

``capture_age_seconds`` returns ``inf`` for a scope that has never received
one.  Every threshold comparison reads ``inf`` as over the threshold, which
is right for the SCHEDULER -- those are exactly the scopes to poll first --
and wrong for a report, which was folding them into the stale count.

Measured 2026-09-10, the card's own words:

    TAF 采集 ｜88/111 个 scope 超过 3 小时没有新报文
    （中位 0.5h，最久 1.1h）：SBGR|2026-09-10 1.1h、FACT|2026-09-10 1.1h、
    LEMD|2026-09-10 1.1h；88 个从未收到过

A sentence contradicted by its own parentheses: nothing was older than 1.1h.
All 88 were never-captured, 70 of them tomorrow and the day after -- days
that have never been captured by definition -- and the true count of scopes
that had gone three hours without a bulletin was ZERO.  The share put the
lane at 88/111, past the two-thirds mark, so the board reported a stopped
lane while every scope that had ever been served was under two hours old.

The two populations are counted apart here, and the share that colours the
strip is taken over the one it belongs to.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from scripts import weather_basket_yes_daemon as daemon  # noqa: E402

import calibration_dashboard as C  # noqa: E402

NOW = "2026-09-10T09:00:00Z"


def _catalog(*scopes: tuple[str, str]) -> list[dict[str, str]]:
    return [
        {"station_id": station, "event_date": day} for station, day in scopes
    ]


def _freshness(catalog, arrivals):
    return daemon._taf_capture_freshness(
        catalog, newest_capture_at=arrivals, as_of_utc=NOW,
    )


def test_a_scope_that_never_received_one_is_not_counted_as_stale():
    fresh = _freshness(
        _catalog(("KLGA", "2026-09-10"), ("KMIA", "2026-09-11")),
        {"KLGA|2026-09-10": "2026-09-10T08:30:00Z"},
    )

    assert fresh["stale_scope_count"] == 0
    assert fresh["never_captured_count"] == 1
    assert fresh["measured_scope_count"] == 1


def test_a_scope_that_had_bulletins_and_stopped_still_counts_as_stale():
    """The thing the card was built for, unchanged."""

    fresh = _freshness(
        _catalog(("KLGA", "2026-09-10")),
        {"KLGA|2026-09-10": "2026-09-10T02:00:00Z"},  # seven hours
    )

    assert fresh["stale_scope_count"] == 1
    assert fresh["measured_scope_count"] == 1
    assert fresh["max_age_hours"] == 7.0


def test_the_never_captured_are_broken_down_by_day_with_a_denominator():
    fresh = _freshness(
        _catalog(
            ("KLGA", "2026-09-10"), ("KMIA", "2026-09-10"),
            ("KLGA", "2026-09-11"), ("KMIA", "2026-09-11"),
        ),
        {"KLGA|2026-09-10": "2026-09-10T08:30:00Z"},
    )

    assert fresh["never_captured_by_event_date"] == {
        "2026-09-10": 1, "2026-09-11": 2,
    }
    assert fresh["scope_count_by_event_date"] == {
        "2026-09-10": 2, "2026-09-11": 2,
    }


def _card(fresh):
    return C._taf_capture_health(
        {"forecast_prefetch": {"forward_taf_capture": {
            "capture_freshness": fresh,
        }}}
    )


def test_the_day_that_has_just_entered_the_catalogue_does_not_redden_the_lane():
    """The 2026-09-10 shape: everything served is fresh, the far days are
    untouched by definition, and the lane is not stopped."""

    fresh = _freshness(
        _catalog(
            *[(f"K{i:02d}", "2026-09-10") for i in range(25)],
            *[(f"K{i:02d}", "2026-09-11") for i in range(35)],
            *[(f"K{i:02d}", "2026-09-12") for i in range(35)],
        ),
        {f"K{i:02d}|2026-09-10": "2026-09-10T08:00:00Z" for i in range(25)},
    )
    card = _card(fresh)

    assert fresh["stale_scope_count"] == 0
    assert fresh["never_captured_count"] == 70
    assert card["taf_capture_health"] == "green"
    # And the sentence no longer contradicts itself.  (Seven hours since
    # 2026-09-14: one scheduled six-hour issuance and an hour -- see
    # test_taf_freshness_reads_the_issuance_cycle.)
    assert "0 个超过 7 小时没有新的" in card["taf_capture_note"]
    assert "70 个未来日期的 scope 从未收到过" in card["taf_capture_note"]


def test_the_traded_day_going_blind_does_colour_it():
    """Never-captured on the nearest day is a supply fact, not a new day."""

    fresh = _freshness(
        _catalog(*[(f"K{i:02d}", "2026-09-10") for i in range(29)]),
        {f"K{i:02d}|2026-09-10": "2026-09-10T08:00:00Z" for i in range(15)},
    )
    card = _card(fresh)

    assert card["taf_capture_health"] == "amber"
    assert "2026-09-10 这一天 29 个 scope 里 14 个从未收到过报文" in (
        card["taf_capture_note"]
    )


def test_a_genuinely_silent_fleet_still_goes_red():
    """The share is taken over the scopes that have an age, so a lane that
    really did stop delivering is not diluted by whatever future days the
    catalogue happens to hold."""

    fresh = _freshness(
        _catalog(
            *[(f"K{i:02d}", "2026-09-10") for i in range(10)],
            *[(f"K{i:02d}", "2026-09-12") for i in range(90)],
        ),
        {f"K{i:02d}|2026-09-10": "2026-09-10T00:00:00Z" for i in range(10)},
    )
    card = _card(fresh)

    assert fresh["stale_scope_count"] == 10
    assert fresh["measured_scope_count"] == 10
    assert card["taf_capture_health"] == "red"


def test_an_older_receipt_without_the_new_fields_still_reads():
    """A page can render against a receipt written by the previous build."""

    card = _card({
        "scope_count": 111, "stale_scope_count": 4,
        "never_captured_count": 88, "stale_after_hours": 3.0,
        "median_age_hours": 0.5, "max_age_hours": 1.1, "worst_scopes": [],
    })

    # 111 - 88 = 23 measured; 4 of them stale.
    assert "23 个 scope 有过报文，其中 4 个" in card["taf_capture_note"]
    assert card["taf_capture_health"] == "green"

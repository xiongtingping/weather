"""The daily table's station count, and the fleet it is a fraction of.

"29" on its own could not tell a day the model had nothing to say about
from a day the market priced us out: both read as nineteen stations that
are simply not there.  The breakdown groups the ledger's own per-station-day
reason codes so the reader gets the ledger's word rather than the page's
guess (user ruling, 2026-09-02).

What is pinned here is the arithmetic -- opened plus not-opened is the whole
fleet, and the reasons account for every station that did not open -- and
the one thing that must stay outside it, the live bar.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import calibration_dashboard as C  # noqa: E402


def _entry(station, day, *, opened=False, reason=None):
    return {
        "station_id": station,
        "event_date": day,
        "stages": {"P0": {"stake_usdc": 1.0} if opened else None,
                   "P0-M": None, "P1": None},
        "absent_reason": reason,
    }


def test_every_station_is_either_opened_or_accounted_for() -> None:
    roster = [
        _entry("AAAA", "2026-08-30", opened=True),
        _entry("BBBB", "2026-08-30", opened=True),
        _entry("CCCC", "2026-08-30", reason="no_depth"),
        _entry("DDDD", "2026-08-30", reason="no_depth"),
        _entry("EEEE", "2026-08-30", reason="negative_edge_window"),
    ]
    day = C._station_breakdown(roster, set())["2026-08-30"]
    assert day["fleet"] == 5
    assert day["opened"] == 2
    assert day["not_opened"] == 3
    assert sum(item["count"] for item in day["reasons"]) == day["not_opened"]
    assert day["opened"] + day["not_opened"] == day["fleet"]


def test_the_reasons_are_ordered_by_how_many_stations_they_took() -> None:
    """A reader scanning the cell wants the day's largest blocker first."""

    roster = [
        _entry("AAAA", "2026-08-30", reason="negative_edge_window"),
        _entry("BBBB", "2026-08-30", reason="no_depth"),
        _entry("CCCC", "2026-08-30", reason="no_depth"),
        _entry("DDDD", "2026-08-30", reason="no_depth"),
    ]
    reasons = C._station_breakdown(roster, set())["2026-08-30"]["reasons"]
    assert [item["reason"] for item in reasons] == [
        "no_depth", "negative_edge_window",
    ]


def test_a_station_seated_at_any_stage_counts_as_opened() -> None:
    """A day topped up at P1 without a P0 seat still carried a position."""

    roster = [{
        "station_id": "AAAA", "event_date": "2026-08-30",
        "stages": {"P0": None, "P0-M": None, "P1": {"stake_usdc": 1.0}},
        "absent_reason": None,
    }]
    day = C._station_breakdown(roster, set())["2026-08-30"]
    assert day["opened"] == 1
    assert day["reasons"] == []


def test_a_station_with_no_recorded_reason_is_named_unknown_not_dropped(
) -> None:
    """Dropping it would make the two halves stop summing to the fleet, and
    a cell that does not add up is worse than one that says it does not
    know."""

    roster = [_entry("AAAA", "2026-08-30"), _entry("BBBB", "2026-08-30")]
    day = C._station_breakdown(roster, set())["2026-08-30"]
    assert day["reasons"] == [{"reason": "unknown", "count": 2}]
    assert day["not_opened"] == 2


def test_the_live_bar_is_counted_but_never_folded_into_the_reasons() -> None:
    """The bar says what the live lane may not trade.  The paper book seats
    a barred station like any other, so adding it to the not-opened total
    would double-count the barred stations that did open."""

    roster = [
        _entry("EDDM", "2026-08-30", opened=True),
        _entry("EHAM", "2026-08-30", reason="no_depth"),
        _entry("ZZZZ", "2026-08-30", opened=True),
    ]
    day = C._station_breakdown(roster, {"EDDM", "EHAM"})["2026-08-30"]
    assert day["live_barred"] == 2
    assert day["opened"] == 2
    assert day["not_opened"] == 1
    assert [item["reason"] for item in day["reasons"]] == ["no_depth"]


def test_each_event_day_is_counted_on_its_own() -> None:
    roster = [
        _entry("AAAA", "2026-08-30", opened=True),
        _entry("AAAA", "2026-08-31", reason="no_depth"),
    ]
    days = C._station_breakdown(roster, set())
    assert days["2026-08-30"]["opened"] == 1
    assert days["2026-08-31"]["opened"] == 0
    assert days["2026-08-31"]["fleet"] == 1


def test_the_barred_list_has_one_reader() -> None:
    """The live-readiness panel and this cell must not disagree about who is
    barred, so both call ``barred_station_set`` rather than reading the file
    themselves."""

    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(
        encoding="utf-8"
    )
    assert source.count('"excluded_stations", "auto_excluded_stations"') == 1
    assert source.count("barred_station_set()") >= 2


def test_the_barred_list_reads_the_live_exclusions_file() -> None:
    """Reads the real config: a set that silently comes back empty would
    make the cell claim nothing is barred."""

    barred = C.barred_station_set()
    assert barred, "no station read from the live exclusions file"
    assert all(name == name.upper() for name in barred)

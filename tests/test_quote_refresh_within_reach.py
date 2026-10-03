"""A mid-loop quote refresh restates the stations the loop will reach, not the day.

2026-10-03 13:22Z: four seconds a compile on a loaded host, so a renewed
60-second lease (about 26 s left when the publisher hands a book over, 14 s
above the 12-second threshold) covered three scopes -- while restating all 44
stations of the day took 17-57 s, longer than the lease it was renewing.
25 refreshes, about 1,100 station writes and 437 s for 119 compiles; the
cycle ran 1,113 s.  A refresh now restates the scope in hand and the day's
scopes in the positions the renewed lease is expected to reach.
"""

from __future__ import annotations

from pathlib import Path

from bayes_basket_target import production_worker as pw

ROOT = Path(__file__).resolve().parents[1]


def test_the_reach_is_the_lease_left_over_the_cost_of_a_position() -> None:
    assert pw.QUOTE_REFRESH_REACH_SECONDS == 14.0
    # Before any loop is measured: 0.45 s a position, 31 positions.
    assert pw.quote_refresh_reach_v1(seconds_per_position=None) == 31
    assert pw.quote_refresh_reach_v1(seconds_per_position=0.0) == 31
    # The loaded loop of 2026-10-03 13:22Z: three.
    assert pw.quote_refresh_reach_v1(seconds_per_position=4.5) == 3
    # Never less than the scope in hand.
    assert pw.quote_refresh_reach_v1(seconds_per_position=60.0) == 1
    assert pw.quote_refresh_reach_v1(seconds_per_position=0.25) == 56


def test_the_reach_takes_the_days_scopes_ahead_in_loop_order() -> None:
    # Listing rows interleave two days station by station.
    rows = [
        ("CYYZ", "2026-10-04"), ("CYYZ", "2026-10-05"),
        ("EGLC", "2026-10-04"), ("EGLC", "2026-10-05"),
        ("KLAX", "2026-10-04"), ("KLAX", "2026-10-05"),
        ("NZWN", "2026-10-04"),
    ]
    assert pw.quote_refresh_stations_v1(rows, position=0, reach=4) == ["CYYZ", "EGLC"]
    assert pw.quote_refresh_stations_v1(rows, position=1, reach=4) == ["CYYZ", "EGLC"]
    assert pw.quote_refresh_stations_v1(rows, position=2, reach=99) == ["EGLC", "KLAX", "NZWN"]
    # Scopes already compiled are not ahead of the loop: never behind it.
    assert pw.quote_refresh_stations_v1(rows, position=4, reach=99) == ["KLAX", "NZWN"]


def test_scopes_the_loop_will_not_compile_are_left_out_but_never_the_one_in_hand() -> None:
    rows = [("CYYZ", "d"), ("EGLC", "d"), ("KLAX", "d"), ("NZWN", "d")]
    skip = {("EGLC", "d"), ("CYYZ", "d")}
    assert pw.quote_refresh_stations_v1(rows, position=0, reach=4, skip=skip) == ["CYYZ", "KLAX", "NZWN"]
    assert pw.quote_refresh_stations_v1(rows, position=0, reach=1, skip=skip) == ["CYYZ"]


def test_the_loop_restates_the_reach_and_reports_what_it_wrote() -> None:
    source = (ROOT / "bayes_basket_target" / "production_worker.py").read_text(encoding="utf-8")
    loop = source[source.index("for position, (station_id, event_date) in enumerate(rows):"):]
    loop = loop[:loop.index('self._mark("decision_loop")')]
    assert "quote_refresh_reach_v1(" in loop and "quote_refresh_stations_v1(" in loop
    assert "stations=refresh_stations," in loop
    receipt = source.split('"quote_evidence_refresh": {', 1)[1].split('"receipts":', 1)[0]
    for field in ('"restated_station_count"', '"within_reach"', '"ineffective_scopes"'):
        assert field in receipt
    # "0" is the whole day again, as before.
    assert pw.QUOTE_REFRESH_WITHIN_REACH is True

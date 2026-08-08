"""Capture scopes whose backing run has published come first.

Both archives take the same run for a d2 lead -- 18Z two days before the event
date -- but a run existing is not a run being complete: members land on the
object store over the following hours.  Capture slots are scarce and a capture
costs minutes, so spending one on a scope whose fields cannot resolve yet
delays every scope that was actually ready.

Readiness outranks the timezone order; inside a readiness group the
east-to-west day order still holds, so the station whose event day opens first
is still served first.
"""

from datetime import datetime, timedelta, timezone

import pytest

from bayes_basket_target.official_nwp_capture_scheduler import (
    D2_PUBLICATION_MARGIN_HOURS,
    d2_model_ready_at_utc,
    registry_capture_scopes,
)


def _registry(rows):
    return {"rows": rows}


def _row(station, event_date, tz, lat=0.0, lon=0.0):
    return {
        "station_id": station,
        "event_date": event_date,
        "settlement_timezone": tz,
        "outcome": f"{station}-{event_date}-yes",
        "source_eligible": 1,
        "market_active": 1,
        "market_accepting_orders": 1,
        "latitude": lat,
        "longitude": lon,
    }


def test_ready_at_is_the_d2_run_plus_a_publication_margin() -> None:
    # d2 run for 2026-08-09 is 2026-08-07 18Z.
    ready = d2_model_ready_at_utc("2026-08-09")

    assert ready == datetime(
        2026, 8, 7, 18, tzinfo=timezone.utc
    ) + timedelta(hours=D2_PUBLICATION_MARGIN_HOURS)


def test_margin_is_non_zero_so_a_just_initialised_run_is_not_ready() -> None:
    # The pre-existing guard only rejected `query_at <= run_init`, which treats
    # a run as usable the instant it initialises.
    assert D2_PUBLICATION_MARGIN_HOURS > 0
    assert d2_model_ready_at_utc("2026-08-09") > datetime(
        2026, 8, 7, 18, tzinfo=timezone.utc
    )


@pytest.mark.parametrize(
    "event_date,expected",
    [("2026-08-07", True), ("2026-08-08", True), ("2026-08-09", False)],
)
def test_readiness_flag_tracks_the_clock(event_date: str, expected: bool) -> None:
    as_of = datetime(2026, 8, 7, 6, tzinfo=timezone.utc)

    assert (d2_model_ready_at_utc(event_date) <= as_of) is expected


def test_ready_scopes_sort_ahead_of_unready_ones() -> None:
    # KMIA's event day opens earlier in UTC than NZWN's, but its run has not
    # published; the ready scope must still be served first.
    rows = [
        _row("KMIA", "2026-08-09", "America/New_York"),
        _row("NZWN", "2026-08-08", "Pacific/Auckland"),
    ]

    scopes = registry_capture_scopes(
        _registry(rows), as_of_utc="2026-08-07T06:00:00Z", max_scopes=50
    )
    order = [item["station_ids"][0] for item in scopes]

    assert order.index("NZWN") < order.index("KMIA")
    by_station = {item["station_ids"][0]: item for item in scopes}
    assert by_station["NZWN"]["d2_model_ready"] is True
    assert by_station["KMIA"]["d2_model_ready"] is False


def test_timezone_order_is_preserved_within_a_readiness_group() -> None:
    rows = [
        _row("KLGA", "2026-08-08", "America/New_York"),
        _row("NZWN", "2026-08-08", "Pacific/Auckland"),
        _row("ZBAA", "2026-08-08", "Asia/Shanghai"),
    ]

    scopes = registry_capture_scopes(
        _registry(rows), as_of_utc="2026-08-07T06:00:00Z", max_scopes=50
    )
    order = [item["station_ids"][0] for item in scopes]

    # All ready, so the east-to-west day-start order decides: +12, +8, -4.
    assert order == ["NZWN", "ZBAA", "KLGA"]
    assert all(item["d2_model_ready"] for item in scopes)


def test_every_scope_publishes_its_readiness_timestamp() -> None:
    rows = [_row("WSSS", "2026-08-08", "Asia/Singapore")]

    scopes = registry_capture_scopes(
        _registry(rows), as_of_utc="2026-08-07T06:00:00Z", max_scopes=50
    )

    # Operators need to see why a scope was deprioritised, not just that it was.
    # d2 run for 08-08 is 08-06 18Z; +8h margin lands on 08-07 02:00Z.
    assert scopes[0]["d2_model_ready_at_utc"] == "2026-08-07T02:00:00Z"


def test_unready_scopes_are_deprioritised_not_dropped() -> None:
    # Dropping them would make the catalogue shrink and grow across polls and
    # would starve a scope whose run publishes mid-sweep.
    rows = [_row("KMIA", "2026-08-09", "America/New_York")]

    scopes = registry_capture_scopes(
        _registry(rows), as_of_utc="2026-08-07T06:00:00Z", max_scopes=50
    )

    assert len(scopes) == 1
    assert scopes[0]["d2_model_ready"] is False

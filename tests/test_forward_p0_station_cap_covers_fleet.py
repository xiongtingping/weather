"""The forward P0 station cap is the fleet, not the founding 45.

2026-10-04 the fleet went 45 -> 48.  The forward P0 compile picks the
stations with a live cross activation ``ORDER BY station_id LIMIT
MAX_FORWARD_P0_STATIONS``; once LLBG, LTFM and UUWW held activations the
last three alphabetically -- ZSQD, ZUCK, ZUUU -- fell off the end, and their
D+1/D+2 P0 stopped at the 12Z run (10-04 17:45-17:59Z) while every other
station took the 18Z one.
"""

from __future__ import annotations

from bayes_basket_target import production_worker as pw
from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.station_registry import coordinate_registry_manifest


def test_the_station_cap_admits_every_fleet_station() -> None:
    assert pw.MAX_FORWARD_P0_STATIONS == expected_station_count()
    fleet = sorted(coordinate_registry_manifest()["station_ids"])
    # The exact cut the LIMIT makes: alphabetical, so the tail is what a
    # short cap drops.
    assert fleet[: pw.MAX_FORWARD_P0_STATIONS] == fleet
    assert pw.MAX_FORWARD_P0_STATION_DAYS == expected_station_count() * 4

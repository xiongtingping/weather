"""Shared fixtures for the test suite."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _map_the_fixture_station_season(monkeypatch: pytest.MonkeyPatch) -> None:
    """Map the fixture station ``TEST`` like a northern mid-latitude station.

    Station-local seasons (charter 62) fail closed on an unmapped station
    (``station_season_station_unmapped``), and many pipeline fixtures build
    station ``TEST``.  Several files grew their own copy of this fixture while
    others (the AIFS ingress and historical republisher contracts) were simply
    red on it; one copy here covers every file.  A test that needs ``TEST``
    unmapped can ``monkeypatch.delitem`` it back out.
    """

    from bayes_basket_target import station_season

    monkeypatch.setitem(station_season._SEASON_ANCHOR_MONTH, "TEST", 7)

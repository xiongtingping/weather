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


@pytest.fixture(autouse=True)
def _no_forward_physics_capture_from_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """The resident's forecast refresh cycle starts a real forward capture.

    Three files drive that cycle with a stand-in worker, and until
    2026-09-14 each run launched ``capture_station_forecast_physics.py``
    against the production SSOT from inside pytest.  The resident now passes
    the worker's own database and skips a worker with none; this switch keeps
    a stand-in that does carry a path from reaching the network as well.  A
    test about the capture itself sets it back to ``on``.
    """

    monkeypatch.setenv("PM_FORECAST_PHYSICS_REFRESH", "off")

"""Shared fixtures for the test suite."""
from __future__ import annotations

import os

import pytest

# Charter 141: the capital dials are an operator file the board writes while
# the book trades.  Tests size with the built-in defaults, never with whatever
# the operator last clicked -- set before any test module imports the kernel,
# which reads the file at import.  A test of the file itself points the path
# at its own copy.
os.environ.setdefault(
    "PM_CAPITAL_LAYER_CONFIG",
    os.path.join(os.path.dirname(__file__), "_no_capital_layer_config.json"),
)


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


@pytest.fixture(autouse=True)
def _no_test_reaches_the_real_order_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may talk to the order gateway running on this machine.

    The worker is built for ``production_live``, and a compiled plan carries
    that authority into ``commit_production_candidate_atomic``, which then
    delivers through ``_default_order_transport`` -- the real loopback
    gateway.  Found 2026-09-15: the post-fill e2e committed a live plan and
    was stopped from posting only because its fixture station (ZUUU) happens
    to be operator-barred in the live config.  Every request primitive of the
    client is its ``_post``; a test that exercises the client subclasses or
    replaces it and is unaffected, and anything else gets a 4xx-shaped
    refusal instead of the network.
    """

    import sys
    from pathlib import Path

    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    import clob_order_service_client

    def refuse(self, path, payload):  # noqa: ARG001
        return {
            "ok": False,
            "error": "test_suite_may_not_reach_the_order_gateway",
            "transport_ambiguous": False,
        }

    monkeypatch.setattr(
        clob_order_service_client.ClobOrderServiceClient, "_post", refuse
    )


@pytest.fixture
def ledger_evidence_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read the claim lanes' population off the paper candidate table.

    Charter 140 moved the lambda, beta and valuation lanes onto production's
    own evidence (``basket_candidate_window_scored``) and the truth lane's
    labels.  The estimator suites build their populations in the paper
    table's shape, and what they test -- bands, hurdles, clamps, the
    activation guard, the walk-forward cuts -- does not depend on which
    table the rows came from, so they run on the ledger arm by asking for
    this fixture.  The production path itself is pinned end to end in
    ``test_charter_140_claim_lane_production_evidence.py``.
    """

    from bayes_basket_target import (
        beta_edge_store,
        candidate_window_evidence,
        effective_edge_store,
    )

    ledger = candidate_window_evidence.LEDGER_TABLE
    monkeypatch.setattr(candidate_window_evidence, "EVIDENCE_SOURCE", "ledger")
    monkeypatch.setattr(candidate_window_evidence, "CANDIDATE_SOURCE", ledger)
    monkeypatch.setattr(effective_edge_store, "EVIDENCE_SOURCE", "ledger")
    monkeypatch.setattr(effective_edge_store, "CANDIDATE_SOURCE", ledger)
    monkeypatch.setattr(beta_edge_store, "CANDIDATE_SOURCE", ledger)

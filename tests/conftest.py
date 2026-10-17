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
# Charter 146: the strategy-lane switches are an operator file too (the
# basket paused, the tiebreak lane live).  Tests run every lane as designed
# -- a missing file reads as the defaults -- so an operator's pause does
# not turn the submit-path tests red.  A test of the switch itself points
# the path at its own copy.
os.environ.setdefault(
    "PM_STRATEGY_LANES_CONFIG",
    os.path.join(os.path.dirname(__file__), "_no_strategy_lanes_config.json"),
)
# Charter 199: every P0 compile reads the fusion blend's training file.  A
# compile test must not move with today's settled days, so the suite reads a
# file that does not exist -- the receipt then records the seat weights over
# SEAT_TRAINING_UNAVAILABLE.  A test of the blend hands in its own file.
os.environ.setdefault(
    "P0_FUSION_BLEND_TRAINING_DB",
    os.path.join(os.path.dirname(__file__), "_no_fusion_blend_training.db"),
)
# Charter 230: production ships ``PM_POSTERIOR_LANE=on`` -- the L4 posterior
# is the basket's one belief, and a row without an admitted walk-forward
# verdict buys nothing.  The mechanism tests (seat rules, completions, the
# capital layer, execution) exercise arithmetic that is the same whatever
# vector it is fed, and their fixtures carry no fitted posterior, so the suite
# runs them on the weather vector (``shadow``).  The lane's own tests switch
# ``on`` explicitly; the shipped default is asserted in a subprocess.
os.environ.setdefault("PM_POSTERIOR_LANE", "shadow")


@pytest.fixture(autouse=True)
def _realisation_layer_reads_no_live_artifact(monkeypatch: pytest.MonkeyPatch, tmp_path_factory) -> None:
    """Charter 254: the lanes' shadow reads ``data/realisation_layer/active.json``.
    A test reads an empty directory of its own, so a lane suite never prices
    its fixtures on the live fit; a test that wants a fit writes one there."""

    from bayes_basket_target import realisation_store

    monkeypatch.setattr(realisation_store, "ARTIFACT_DIR", tmp_path_factory.mktemp("realisation_layer"))
    realisation_store._ACTIVE_CACHE.clear()
    # The population's input cache likewise (2026-10-17): a fixture's
    # decisions never land in, or are read from, the live cache.
    monkeypatch.setattr(realisation_store, "POPULATION_CACHE_DIR", tmp_path_factory.mktemp("realisation_population_cache"))


@pytest.fixture(autouse=True)
def _no_test_reads_the_benchmark_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    """Charter 258: the Proper lane reads the benchmark cluster's fills off the
    public data API on every pass.  A suite reads none -- an empty tape, so
    nothing is sealed -- and a test of the seal hands in its own fetch."""

    from bayes_basket_target import benchmark_cluster

    monkeypatch.setattr(benchmark_cluster, "DEFAULT_FETCH", lambda path, params: [])


@pytest.fixture(autouse=True)
def _no_test_reads_the_account_position_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """Charter 262: the 0.998 recycle lane reads the account's own position
    list off the public data API.  A suite reads an empty list (and no cached
    one); a test of that source hands in its own reader."""

    from bayes_basket_target import certain_win_recycle

    monkeypatch.setattr(certain_win_recycle, "_read_account_positions", lambda: {})
    monkeypatch.setitem(certain_win_recycle._ACCOUNT_POSITIONS, "at", -1e18)
    monkeypatch.setitem(certain_win_recycle._TOKEN_LEGS, "at", -1e18)


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
def _no_taf_raw_archive_from_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Captures archive every TAF they see (「存原文」, 2026-09-19).

    A stand-in fetch would append its made-up bulletins to the real archive
    the next TAF test reads.  A test of the archive itself points the
    directory at its own ``tmp_path``.
    """

    monkeypatch.setenv("PM_TAF_RAW_ARCHIVE_DIR", "off")
    # Charter 162: the TGFTP fallback reaches the network; a test that makes
    # AWC fail is testing that failure, not what TGFTP answers today.
    monkeypatch.setenv("PM_TGFTP_FALLBACK", "off")


@pytest.fixture(autouse=True)
def _no_nwp_object_store_from_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Charter 195 (三): ICON/ECCC objects go through a real on-disk store.

    A stand-in fetch would file its made-up bytes under a real run's object
    key (2026-09-22: a follow test left a 39-byte "ICON 2026092218" body the
    next production capture of that run would have been served).  Off in
    tests; a test of the store points it at its own ``tmp_path``.
    """

    monkeypatch.setenv("ICON_EPS_OBJECT_STORE", "")
    monkeypatch.setenv("ECCC_GEPS_OBJECT_STORE", "")


@pytest.fixture(autouse=True)
def _no_member_trajectory_store_from_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Captures built in tests must not write the live member trajectory
    store (``member_trajectory_store``); a test that wants it points the
    variable at its own file."""

    monkeypatch.setenv("PM_MEMBER_TRAJECTORY_DB", "off")


@pytest.fixture(autouse=True)
def _no_live_supply_watch_in_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Charter 197 amendment: the send gate reads the live watch file.

    A test that sends would otherwise hold its orders whenever the real
    tunnel is down, and stamp a stale-watch audit on its events whenever the
    real sweep has not run -- the outcome of a test would be the state of
    this machine's network.  Off in tests; a test of the gate points it at
    its own ``tmp_path``.
    """

    monkeypatch.setenv("PM_SUPPLY_WATCH_PATH", "off")


@pytest.fixture(autouse=True)
def _no_transport_breaker_carryover_from_tests():
    """Charter 195 (四): the capture's transport breaker is process-global.

    Stand-in fetches that fail on purpose would otherwise add up across tests
    and open it, and the next test's request would sleep 30-60s and probe the
    real network.  Each test starts with a breaker that never opens; a test of
    the breaker builds its own.
    """

    from bayes_basket_target import transport_breaker

    transport_breaker.reset_for_tests(threshold=10**9)
    yield
    transport_breaker.reset_for_tests(threshold=10**9)


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


@pytest.fixture(autouse=True)
def _no_test_launches_the_residents_scripts(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may start one of the resident's own scripts for real.

    The production worker launches nine scripts from ``scripts/`` -- the
    Open-Meteo d2 capture, the AWC observation sweep, the TAF backfill and
    ingest, the METAR trajectory fit, the correction-review clearer -- each
    with ``--commit`` and most of them over the network.  Found 2026-09-17:
    seven worker tests started ``capture_open_meteo_d2_ensemble_daily.py``
    through the local proxy into pytest's temp directories, the capture pool
    kept pytest from exiting until they were killed, and one batch went from
    270s to 547s.

    Only the worker module's ``subprocess`` is replaced, by a forwarding
    namespace whose ``run`` and ``Popen`` refuse a command that names a file
    under ``scripts/``; the global module is untouched, so every test that runs a
    CLI still can, and a worker test that stubs
    ``production_worker.subprocess.run`` stubs it on this namespace and wins.
    """

    import subprocess
    import sys
    import types
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from bayes_basket_target import production_worker

    scripts = str(root / "scripts") + "/"
    real_run = subprocess.run

    def refuse_scripts(args, *pargs, **kwargs):
        parts = [args] if isinstance(args, (str, bytes, Path)) else list(args or ())
        if any(str(part).startswith(scripts) for part in parts):
            return subprocess.CompletedProcess(
                args, 97, stdout="", stderr="test_suite_may_not_launch_resident_scripts",
            )
        return real_run(args, *pargs, **kwargs)

    real_popen = subprocess.Popen

    class _RefusedPopen:
        """What a refused launch looks like to a ``Popen`` caller: it ran
        nothing and exited 97, and nothing is left for a deadline to kill."""

        def __init__(self, args) -> None:
            self.args = args
            self.pid = -1
            self.returncode = 97

        def communicate(self, input=None, timeout=None):  # noqa: A002
            return "", "test_suite_may_not_launch_resident_scripts"

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

        def kill(self) -> None:
            return None

        terminate = kill

    def refuse_scripts_popen(args, *pargs, **kwargs):
        # 2026-09-24: the identity pass moved to ``Popen`` (its deadline has
        # to kill the script's whole process group), and a worker test whose
        # fake still sat on ``subprocess.run`` launched the real
        # ``pr12_awc_observation_sweep.sh`` against the live stores until it
        # was killed.  Both entry points are guarded now.
        parts = [args] if isinstance(args, (str, bytes, Path)) else list(args or ())
        if any(str(part).startswith(scripts) for part in parts):
            return _RefusedPopen(args)
        return real_popen(args, *pargs, **kwargs)

    guarded = types.SimpleNamespace(
        **{name: getattr(subprocess, name) for name in dir(subprocess) if not name.startswith("__")}
    )
    guarded.run = refuse_scripts
    guarded.Popen = refuse_scripts_popen
    monkeypatch.setattr(production_worker, "subprocess", guarded)

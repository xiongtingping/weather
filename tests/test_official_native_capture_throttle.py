"""Captures run off the rotation thread, bounded by concurrency.

Official-native coverage is what gates P0 -- a republisher packet is filtered
out of the P0 inbox by design -- but one capture takes minutes.  Running it
inline was wrong in both directions: uncapped it stalled the rotation at roughly
one scope per six hours; throttled hard enough to protect the rotation, coverage
crawled.  Captures now submit to a small pool and the caller returns
immediately.
"""

from pathlib import Path

import pytest

from bayes_basket_target.production_worker import (
    OFFICIAL_NATIVE_CAPTURE_MAX_CONCURRENCY,
    OFFICIAL_NATIVE_CAPTURE_RETRY_SEC,
    TargetProductionWorker,
)


@pytest.fixture
def worker(tmp_path: Path):
    instance = object.__new__(TargetProductionWorker)
    instance.nwp_runtime_root = tmp_path
    instance._official_native_capture_pool = None
    yield instance
    pool = getattr(instance, "_official_native_capture_pool", None)
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


def _capture(worker, station="KMIA", event_date="2026-08-07", *, tmp_path):
    return worker._capture_official_native_packet(
        station_id=station,
        event_date=event_date,
        as_of_utc="2026-08-06T15:00:00Z",
        inbox_path=tmp_path / "inbox.json",
    )


def test_missing_registry_short_circuits_before_any_work(
    worker, tmp_path: Path
) -> None:
    # No forward registry in this tmp root, so nothing is submitted -- which is
    # what makes the remaining assertions safe to run offline.
    result = _capture(worker, tmp_path=tmp_path)

    assert result["state"] == "SKIPPED"
    assert result["reason"] == "forward_market_registry_missing"
    assert result["calls_order_gateway"] is False


def test_repeat_scope_is_throttled_by_its_own_floor(worker, tmp_path: Path) -> None:
    _capture(worker, tmp_path=tmp_path)

    second = _capture(worker, tmp_path=tmp_path)

    assert second["state"] == "THROTTLED"
    assert 0 < second["retry_after_sec"] <= OFFICIAL_NATIVE_CAPTURE_RETRY_SEC


def test_other_scopes_are_not_blocked_by_a_throttled_one(
    worker, tmp_path: Path
) -> None:
    _capture(worker, tmp_path=tmp_path)

    # Coverage must keep growing across stations while one backs off.  This is
    # the behaviour the old global floor destroyed.
    other_station = _capture(worker, station="KATL", tmp_path=tmp_path)
    other_date = _capture(worker, event_date="2026-08-08", tmp_path=tmp_path)

    assert other_station["state"] != "THROTTLED"
    assert other_date["state"] != "THROTTLED"


def test_concurrency_is_bounded(worker, tmp_path: Path) -> None:
    # Simulate saturated slots without touching the network.
    class _Done:
        def done(self):
            return False

    worker._official_native_capture_inflight = {
        (f"ST{index}", "2026-08-07"): _Done()
        for index in range(OFFICIAL_NATIVE_CAPTURE_MAX_CONCURRENCY)
    }

    result = _capture(worker, tmp_path=tmp_path)

    assert result["state"] == "DEFERRED_CAPTURE_SLOTS_BUSY"
    assert result["in_flight"] == OFFICIAL_NATIVE_CAPTURE_MAX_CONCURRENCY


def test_a_scope_already_in_flight_is_not_resubmitted(
    worker, tmp_path: Path
) -> None:
    class _Running:
        def done(self):
            return False

    worker._official_native_capture_inflight = {("KMIA", "2026-08-07"): _Running()}

    result = _capture(worker, tmp_path=tmp_path)

    assert result["state"] == "IN_FLIGHT"


def test_finished_captures_free_their_slot_and_surface_the_outcome(
    worker, tmp_path: Path
) -> None:
    class _Finished:
        def done(self):
            return True

        def result(self):
            return {"state": "CAPTURED", "inbox_packet_count": 7}

    worker._official_native_capture_inflight = {("KATL", "2026-08-07"): _Finished()}

    _capture(worker, tmp_path=tmp_path)

    assert ("KATL", "2026-08-07") not in worker._official_native_capture_inflight
    last = worker._official_native_capture_last_result
    assert last["state"] == "CAPTURED"
    assert last["station_id"] == "KATL"


def test_a_failed_capture_does_not_wedge_its_slot(worker, tmp_path: Path) -> None:
    class _Raising:
        def done(self):
            return True

        def result(self):
            raise RuntimeError("archive unreachable")

    worker._official_native_capture_inflight = {("WSSS", "2026-08-07"): _Raising()}

    _capture(worker, tmp_path=tmp_path)

    assert ("WSSS", "2026-08-07") not in worker._official_native_capture_inflight
    assert worker._official_native_capture_last_result["state"] == "FAILED"


def test_capture_never_touches_the_order_path(worker, tmp_path: Path) -> None:
    result = _capture(worker, tmp_path=tmp_path)

    assert result["calls_order_gateway"] is False
    assert result["places_orders"] is False

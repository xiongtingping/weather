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


def test_another_event_date_is_not_blocked_by_a_throttled_one(
    worker, tmp_path: Path
) -> None:
    _capture(worker, tmp_path=tmp_path)

    # Coverage must keep growing across event dates while one backs off.  This
    # is the behaviour the old global floor destroyed.
    other_date = _capture(worker, event_date="2026-08-08", tmp_path=tmp_path)

    assert other_date["state"] != "THROTTLED"


def test_a_second_station_of_a_captured_day_costs_no_second_capture(
    worker, tmp_path: Path
) -> None:
    # The download is a model run, not a station: one pass reads every station
    # the day lists.  A sibling station arriving later in the rotation must
    # therefore ride the first pass rather than start a second identical one --
    # asking per station is exactly what made one run cost forty-five
    # downloads.
    _capture(worker, tmp_path=tmp_path)

    sibling = _capture(worker, station="KATL", tmp_path=tmp_path)

    assert sibling["state"] == "THROTTLED"


def test_concurrency_is_bounded(worker, tmp_path: Path) -> None:
    # Simulate saturated slots without touching the network.
    class _Done:
        def done(self):
            return False

    worker._official_native_capture_inflight = {
        (f"2026-09-{index + 1:02d}",): _Done()
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

    worker._official_native_capture_inflight = {("2026-08-07",): _Running()}

    result = _capture(worker, tmp_path=tmp_path)

    assert result["state"] == "IN_FLIGHT"


def test_finished_captures_free_their_slot_and_surface_the_outcome(
    worker, tmp_path: Path
) -> None:
    class _Finished:
        def done(self):
            return True

        def result(self):
            return {
                "state": "CAPTURED",
                "inbox_packet_count": 7,
                "captured_run_init_utc": "2026-08-05T18:00:00Z",
            }

    worker._official_native_capture_inflight = {("2026-08-07",): _Finished()}

    _capture(worker, tmp_path=tmp_path)

    assert ("2026-08-07",) not in worker._official_native_capture_inflight
    last = worker._official_native_capture_last_result_by_date["2026-08-07"]
    assert last["state"] == "CAPTURED"
    assert last["event_date"] == "2026-08-07"
    assert last["completed_at_utc"] == "2026-08-06T15:00:00Z"


def test_a_run_already_captured_is_not_downloaded_again(
    worker, tmp_path: Path
) -> None:
    # These families publish on a fixed cycle and their objects are immutable,
    # so a run that has been read once has nothing further to give.  Re-reading
    # it on the next poll would re-download the same bytes; the date waits for
    # the next cycle instead.
    def _finished(run_init: str):
        class _Finished:
            def done(self):
                return True

            def result(self):
                return {
                    "state": "CAPTURED",
                    "inbox_packet_count": 7,
                    "captured_run_init_utc": run_init,
                }

        return _Finished()

    # At 2026-08-06T15:00Z the newest initialised run covering 08-07 is that
    # day's 12Z -- the ladder follows every six-hourly cycle the publishers
    # issue, so holding 12Z means nothing newer exists to fetch.
    worker._official_native_capture_inflight = {
        ("2026-08-07",): _finished("2026-08-06T12:00:00Z")
    }

    result = _capture(worker, tmp_path=tmp_path)

    assert result["state"] == "RUN_ALREADY_CAPTURED"
    assert result["captured_run_init_utc"] == "2026-08-06T12:00:00Z"


def test_a_superseded_run_does_not_suppress_the_next_capture(
    worker, tmp_path: Path
) -> None:
    # The point of keying on the run is to stop re-reading the *same* one, not
    # to stop reading at all.  Once a fresher cycle publishes, the date is due
    # again -- this is the case that would silently freeze coverage if the key
    # were the event date alone.
    class _Finished:
        def done(self):
            return True

        def result(self):
            return {
                "state": "CAPTURED",
                "inbox_packet_count": 7,
                "captured_run_init_utc": "2026-08-05T18:00:00Z",
            }

    worker._official_native_capture_inflight = {("2026-08-07",): _Finished()}

    result = _capture(worker, tmp_path=tmp_path)

    assert result["state"] != "RUN_ALREADY_CAPTURED"


def test_a_failed_capture_does_not_wedge_its_slot(worker, tmp_path: Path) -> None:
    class _Raising:
        def done(self):
            return True

        def result(self):
            raise RuntimeError("archive unreachable")

    worker._official_native_capture_inflight = {("2026-08-07",): _Raising()}

    _capture(worker, tmp_path=tmp_path)

    assert ("2026-08-07",) not in worker._official_native_capture_inflight
    assert (
        worker._official_native_capture_last_result_by_date["2026-08-07"]["state"]
        == "FAILED"
    )


def test_a_throttled_scope_reports_its_own_dates_outcome_not_the_last_one(
    worker, tmp_path: Path
) -> None:
    """One outcome per event date, so a date's failure cannot be overwritten.

    The worker used to keep a single slot for the newest outcome of any
    capture.  On 2026-09-05 the 2026-09-07 pass exited after two minutes with
    no staging inbox, and before any receipt could be read the slot already
    held 2026-09-06's and then 2026-09-05's outcome -- the one date whose
    failure mattered was the one date with no record.  A THROTTLED receipt
    now carries the attempt it is holding down; another date finishing later
    does not displace it.
    """
    class _Finished:
        def __init__(self, outcome):
            self._outcome = outcome

        def done(self):
            return True

        def result(self):
            return self._outcome

    # Attempt 08-07 (no registry in this root, so it is throttled and nothing
    # is submitted), then let two passes finish: 08-07 failed, 08-08 captured
    # afterwards.
    _capture(worker, tmp_path=tmp_path)
    worker._official_native_capture_inflight = {
        ("2026-08-07",): _Finished({
            "state": "FAILED",
            "reason": "run_ladder_exhausted:capture_exit_1:404",
        }),
        ("2026-08-08",): _Finished({
            "state": "CAPTURED",
            "inbox_packet_count": 45,
            "captured_run_init_utc": "2026-08-06T06:00:00Z",
        }),
    }

    throttled = _capture(worker, tmp_path=tmp_path)

    assert throttled["state"] == "THROTTLED"
    assert throttled["last_completed"]["event_date"] == "2026-08-07"
    assert throttled["last_completed"]["state"] == "FAILED"
    assert throttled["last_completed"]["reason"].endswith(":404")
    by_date = worker._official_native_capture_last_result_by_date
    assert set(by_date) == {"2026-08-07", "2026-08-08"}
    assert by_date["2026-08-08"]["state"] == "CAPTURED"
    assert not hasattr(worker, "_official_native_capture_last_result")


def test_outcomes_older_than_the_open_window_are_not_retained(
    worker, tmp_path: Path
) -> None:
    class _Finished:
        def done(self):
            return True

        def result(self):
            return {"state": "FAILED", "reason": "late"}

    worker._official_native_capture_inflight = {
        ("2026-07-01",): _Finished(),
        ("2026-08-03",): _Finished(),
        ("2026-08-07",): _Finished(),
    }

    _capture(worker, tmp_path=tmp_path)

    # The clock is 2026-08-06: three days back is 08-03, which stays; July
    # is closed everywhere and answers nothing.
    assert set(worker._official_native_capture_last_result_by_date) == {
        "2026-08-03", "2026-08-07",
    }


def test_a_failed_fresh_rung_does_not_redownload_the_held_run(
    worker, tmp_path: Path
) -> None:
    # The newest run is the last to finish uploading, so failing on it is
    # ordinary.  What must not happen is the ladder then falling back onto the
    # run this date already holds and fetching it a second time -- the fallback
    # exists to find *older* evidence when there is none, not to re-buy it.
    class _Finished:
        def done(self):
            return True

        def result(self):
            return {
                "state": "CAPTURED",
                "inbox_packet_count": 7,
                "captured_run_init_utc": "2026-08-06T00:00:00Z",
            }

    worker._official_native_capture_inflight = {("2026-08-07",): _Finished()}

    # 2026-08-06T18:00Z has published by this clock and is newer than what the
    # date holds, so the scope is due -- but every older rung is already held.
    result = worker._capture_official_native_packet(
        station_id="KMIA",
        event_date="2026-08-07",
        as_of_utc="2026-08-06T19:00:00Z",
        inbox_path=tmp_path / "inbox.json",
    )

    assert result["state"] != "RUN_ALREADY_CAPTURED"


def test_every_rung_held_means_nothing_left_to_fetch(
    worker, tmp_path: Path
) -> None:
    class _Finished:
        def done(self):
            return True

        def result(self):
            return {
                "state": "CAPTURED",
                "inbox_packet_count": 7,
                "captured_run_init_utc": "2026-08-06T12:00:00Z",
            }

    worker._official_native_capture_inflight = {("2026-08-07",): _Finished()}

    # At 15:00Z the 18Z run has not initialised, so 12Z is both the newest
    # rung and the one already held: there is nothing to download.
    result = _capture(worker, tmp_path=tmp_path)

    assert result["state"] == "RUN_ALREADY_CAPTURED"


def test_capture_never_touches_the_order_path(worker, tmp_path: Path) -> None:
    result = _capture(worker, tmp_path=tmp_path)

    assert result["calls_order_gateway"] is False
    assert result["places_orders"] is False


def test_packet_run_identity_is_read_from_its_systems() -> None:
    # The rotation decides "has a newer cycle published since this packet was
    # built" from this value, so an unreadable field must not read as "old" --
    # that would force a download on every poll.
    from bayes_basket_target.production_worker import _native_packet_run_init

    assert _native_packet_run_init({}) is None
    assert _native_packet_run_init({"run_init_utc_by_system": {}}) is None
    assert _native_packet_run_init(
        {"run_init_utc_by_system": {"A": "not-a-time"}}
    ) is None
    # A mixed packet has advanced only as far as the slower of its two
    # required systems.  Judged by the newest, GEFS 06Z over ECMWF 00Z read
    # as "already on 06Z": the probe reused it while ECMWF -- 51 of the
    # members -- sat a run behind the one published, and the merge would
    # have kept it over a fresh capture carrying ECMWF 06Z (2026-09-03,
    # twenty-nine 2026-09-04 scopes).
    slower = _native_packet_run_init({
        "run_init_utc_by_system": {
            "ECMWF_IFS_ENS_OPEN_PF50": "2026-08-13T00:00:00Z",
            "NOAA_GEFS_05": "2026-08-13T06:00:00Z",
            "ECCC_GEM_GLOBAL_ENS": "2026-08-12T12:00:00Z",
        }
    })
    assert slower is not None
    assert slower.isoformat().replace("+00:00", "Z") == "2026-08-13T00:00:00Z"
    # Neither required system named: the newest of what is there.
    fallback = _native_packet_run_init({
        "run_init_utc_by_system": {"A": "2026-08-13T00:00:00Z", "B": "2026-08-13T06:00:00Z"}
    })
    assert fallback.isoformat().replace("+00:00", "Z") == "2026-08-13T06:00:00Z"


def _inbox_with_packet(path: Path, *, event_date: str, received_at_utc: str) -> None:
    import json

    path.write_text(json.dumps({
        "packets": [{
            "schema": "official_native_forward_packet_v1",
            "station_id": "KMIA",
            "event_date": event_date,
            "received_at_utc": received_at_utc,
        }],
    }), encoding="utf-8")


def _held_newest_run(worker) -> None:
    class _Finished:
        def done(self):
            return True

        def result(self):
            return {
                "state": "CAPTURED",
                "inbox_packet_count": 7,
                "captured_run_init_utc": "2026-08-06T12:00:00Z",
            }

    worker._official_native_capture_inflight = {("2026-08-07",): _Finished()}


def test_stale_packets_reopen_a_held_run(worker, tmp_path: Path) -> None:
    """A held run is retired only while its packets are still usable.

    2026-09-02: the day's packets aged past the compile window at
    mid-afternoon, the next run was eight hours away, the fallback quota
    was spent, and the held-run guard refused to re-read the run whose
    archives it already had -- no P0 fleet-wide all night.
    """

    _held_newest_run(worker)
    _inbox_with_packet(
        tmp_path / "inbox.json",
        event_date="2026-08-07",
        received_at_utc="2026-08-05T15:00:00Z",
    )

    result = _capture(worker, tmp_path=tmp_path)

    assert result["state"] != "RUN_ALREADY_CAPTURED"


def test_fresh_packets_keep_the_held_run_retired(worker, tmp_path: Path) -> None:
    _held_newest_run(worker)
    _inbox_with_packet(
        tmp_path / "inbox.json",
        event_date="2026-08-07",
        received_at_utc="2026-08-06T14:50:00Z",
    )

    result = _capture(worker, tmp_path=tmp_path)

    assert result["state"] == "RUN_ALREADY_CAPTURED"



# --------------------------------------------------------------------------
# Charter 193 (三): what a date holds survives a restart; a failed capture's
# stdout reason reaches the attempt log.
# --------------------------------------------------------------------------
def _archive_file(root: Path, name: str, event_date: str, run: str) -> None:
    import json

    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(
        json.dumps({"event_date": event_date, "run_init_utc": run}),
        encoding="utf-8",
    )


def test_a_restarted_worker_reads_the_held_run_from_the_archives(
    worker, tmp_path: Path
) -> None:
    # A fresh process (empty memory) for a date whose files hold 12Z -- the
    # newest run covering 08-07 at 15:00Z -- does not walk below it.
    capture_root = tmp_path / "official_native_capture"
    _archive_file(capture_root, "gefs_2026-08-07_d1.json", "2026-08-07", "2026-08-06T12:00:00Z")
    _archive_file(capture_root, "ecmwf_2026-08-07_d2.json", "2026-08-07", "2026-08-05T18:00:00Z")
    _archive_file(capture_root, "ecmwf_2026-08-07_d1.json", "2026-08-07", "2026-08-06T12:00:00Z")

    result = _capture(worker, tmp_path=tmp_path)

    assert result["state"] == "RUN_ALREADY_CAPTURED"
    assert result["captured_run_init_utc"] == "2026-08-06T12:00:00Z"


def test_the_restored_run_is_the_slower_required_family(worker, tmp_path: Path) -> None:
    from bayes_basket_target.production_worker import _held_run_from_capture_archives

    capture_root = tmp_path / "official_native_capture"
    _archive_file(capture_root, "gefs_2026-08-07_d1.json", "2026-08-07", "2026-08-06T12:00:00Z")
    _archive_file(capture_root, "ecmwf_2026-08-07_d1.json", "2026-08-07", "2026-08-06T06:00:00Z")
    _archive_file(capture_root, "ecmwf_2026-08-07_d2_18z.json", "2026-08-07", "2026-08-06T18:00:00Z")
    assert _held_run_from_capture_archives(capture_root, "2026-08-07") == "2026-08-06T06:00:00Z"
    # the 06Z held leaves the 12Z rung to try: not retired
    assert _capture(worker, tmp_path=tmp_path)["state"] != "RUN_ALREADY_CAPTURED"
    # one family missing: nothing is claimed
    (capture_root / "gefs_2026-08-07_d1.json").unlink()
    assert _held_run_from_capture_archives(capture_root, "2026-08-07") is None


def test_a_failed_capture_names_its_stdout_reason() -> None:
    import json

    from bayes_basket_target.production_worker import _capture_stdout_reason

    receipt = json.dumps({
        "schema": "official_native_ensemble_forward_capture_v1",
        "state": "BLOCKED_FAIL_CLOSED",
        "reason": "ContractError:official_native_forward_archive_would_regress:x",
    })
    assert _capture_stdout_reason("noise\n" + receipt + "\n") == (
        "ContractError:official_native_forward_archive_would_regress:x"
    )
    assert _capture_stdout_reason("") == ""

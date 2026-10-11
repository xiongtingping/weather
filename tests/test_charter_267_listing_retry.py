"""Charter 267 (一): a listing pass that fails only because a required family's
newest run is not published yet re-arms the day's listing trigger.

The ~05:00Z batch lists D+2 while ECMWF's run of that morning is hours from
publication and the day's D-3 18Z bank is still being topped up by the follow
job queued beside the pass; the first pass failed whole on
``official_native_forward_required_family_unavailable`` every morning
(2026-10-09/10/11) and the stations waited for an unrelated later listing.
"""
from __future__ import annotations

import bayes_basket_target.production_worker as pw

AS_OF = "2026-10-11T04:58:30Z"
DAY = "2026-10-13"
FAILED = {
    "state": "FAILED",
    "trigger": "listing:EDDM",
    "reason": "run_ladder_exhausted:capture_exit_2:e:ECMWF_OPEN_DATA_S3:...404...",
    "rung_trace": [{
        "lead_days": 2, "cycle_hour": 0, "returncode": 2,
        "stdout_reason": "ContractError:official_native_forward_required_family_unavailable:"
                         "ECMWF_OPEN_DATA_S3:ECMWF_OPEN_DATA_S3=HTTPStatusError:Client error '404 Not Found'",
    }],
}


def _worker(tmp_path):
    worker = object.__new__(pw.TargetProductionWorker)
    worker.nwp_runtime_root = tmp_path
    worker.station_allowlist = ()
    worker._enqueue_listing_follow = lambda *, event_date: "QUEUED"
    (tmp_path / "market_registry_forward.json").write_bytes(b"{}")
    (tmp_path / "official_native_ensemble_forward_inbox.json").write_bytes(b"{}")
    return worker


def _wire(monkeypatch, worker, rows):
    registry = {"filters": {"include_not_started_for_p0": True}, "rows": rows}
    monkeypatch.setattr(pw, "load_market_registry_snapshot", lambda path: registry)
    monkeypatch.setattr(pw, "_load_native_forward_inbox", lambda path, error_code: [])
    calls = []

    def fake_capture(**kwargs):
        calls.append(kwargs)
        return {"state": "SUBMITTED"}

    monkeypatch.setattr(worker, "_capture_official_native_packet", fake_capture)
    return calls


ROWS = [
    {"station_id": "EDDM", "event_date": DAY, "settlement_timezone": "Europe/Berlin"},
    {"station_id": "EFHK", "event_date": DAY, "settlement_timezone": "Europe/Helsinki"},
]


def test_only_a_listing_failure_on_the_unpublished_required_family_re_arms() -> None:
    assert pw._listing_pass_needs_retry(FAILED)
    assert not pw._listing_pass_needs_retry({**FAILED, "state": "CAPTURED"})
    assert not pw._listing_pass_needs_retry({**FAILED, "trigger": "rotation"})
    assert not pw._listing_pass_needs_retry({**FAILED, "trigger": "arrival:ECMWF|x"})
    assert not pw._listing_pass_needs_retry({
        **FAILED, "rung_trace": [{"stdout_reason": "native_family_temperature_curve_point_count_invalid"}],
    })
    # the outcome's own reason keeps only the tail; the mark is read off the rungs
    assert pw.LISTING_RETRY_FAILURE_MARK not in FAILED["reason"]


def test_a_failed_listing_pass_puts_the_day_back_after_the_floor(monkeypatch, tmp_path) -> None:
    worker = _worker(tmp_path)
    calls = _wire(monkeypatch, worker, ROWS)
    clock = [1000.0]
    monkeypatch.setattr(pw.time, "monotonic", lambda: clock[0])
    worker.run_listing_triggered_captures(as_of_utc=AS_OF)
    assert len(calls) == 1
    assert {(DAY, "EDDM"), (DAY, "EFHK")} <= worker._official_native_listing_attempts
    # the next refresh: recorded as attempted, nothing to do
    worker.run_listing_triggered_captures(as_of_utc=AS_OF)
    assert len(calls) == 1
    # the pass comes back failed on ECMWF's unpublished run
    worker._arm_listing_retry(DAY)
    assert not any(item[0] == DAY for item in worker._official_native_listing_attempts)
    receipt = worker.run_listing_triggered_captures(as_of_utc=AS_OF)
    assert len(calls) == 1
    assert receipt["outcomes"][0]["state"] == "LISTING_RETRY_WAIT"
    clock[0] += pw.LISTING_RETRY_AFTER_SEC + 1
    worker.run_listing_triggered_captures(as_of_utc=AS_OF)
    assert len(calls) == 2
    assert calls[-1]["listing_station"] in {"EDDM", "EFHK"}


def test_the_re_arm_is_bounded(monkeypatch, tmp_path) -> None:
    worker = _worker(tmp_path)
    worker._official_native_listing_attempts = {(DAY, "EDDM")}
    for _ in range(pw.LISTING_RETRY_MAX):
        worker._official_native_listing_attempts.add((DAY, "EDDM"))
        worker._arm_listing_retry(DAY)
        assert (DAY, "EDDM") not in worker._official_native_listing_attempts
    worker._official_native_listing_attempts.add((DAY, "EDDM"))
    worker._arm_listing_retry(DAY)
    assert (DAY, "EDDM") in worker._official_native_listing_attempts
    assert worker._listing_retry_map()[DAY]["count"] == pw.LISTING_RETRY_MAX

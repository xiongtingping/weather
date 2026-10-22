"""User ruling 2026-10-22 「按照建议做前三条」: the ECMWF replica's outage falls back to S3.

2026-10-20 05:39-07:49Z every capture pass for the day just listed waited out
the breaker's 300 s on ``nwp_transport_outage:storage.googleapis.com`` and the
day's first P0 came three hours after listing.  The child reports the outage
on its stdout receipt with rc 2, so the worker read an ordinary failure.  Now
the same rung reruns on ECMWF's own S3 at once, and every capture reads S3
while the replica is held out.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import bayes_basket_target.production_worker as pw
from bayes_basket_target.ecmwf_ifs_archive import ALLOWED_TRANSPORT_BASE_URLS
from tests.test_charter_195_nwp_arrival import _Completed, _capture, _finish, _pass_worker

OUTAGE = json.dumps({
    "state": "BLOCKED_FAIL_CLOSED",
    "reason": "ContractError:official_native_forward_required_family_unavailable:ECMWF_OPEN_DATA_S3:"
              "ECMWF_OPEN_DATA_S3=ContractError:nwp_transport_outage:storage.googleapis.com:300s",
})
OK = json.dumps({"run_init_utc_by_family": {"ECMWF_OPEN_DATA_S3": "2026-09-21T12:00:00Z",
                                             "NOAA_GEFS_PDS_S3": "2026-09-21T12:00:00Z"}})
RUNG = [(2, 12, datetime(2026, 9, 21, 12, tzinfo=timezone.utc))]


def test_the_s3_base_is_one_the_archive_accepts_and_the_outage_text_is_recognised():
    assert pw.ECMWF_S3_BASE_URL in ALLOWED_TRANSPORT_BASE_URLS
    assert pw._gcs_replica_outage(OUTAGE)
    assert not pw._gcs_replica_outage("HTTPStatusError:Client error '404 Not Found' for url 'https://storage.googleapis.com/x'")
    assert not pw._gcs_replica_outage("nwp_transport_outage:noaa-gefs-pds.s3.amazonaws.com:300s")


def test_a_replica_outage_reruns_the_rung_on_s3_and_holds_the_replica_out(monkeypatch, tmp_path):
    envs = []
    worker, calls = _pass_worker(monkeypatch, tmp_path, [_Completed(2, stdout=OUTAGE), _Completed(0, stdout=OK)])
    real_run = pw.subprocess.run

    def recording(command, **kwargs):
        envs.append(kwargs["env"]["ECMWF_ARCHIVE_TRANSPORT_BASE_URL"])
        return real_run(command, **kwargs)

    monkeypatch.setattr(pw.subprocess, "run", recording)
    try:
        assert worker._ecmwf_capture_env()["ECMWF_ARCHIVE_TRANSPORT_BASE_URL"] == pw.ECMWF_GCS_REPLICA_BASE_URL
        _capture(worker, tmp_path, forced_rungs=RUNG)
        outcome = _finish(worker)
        assert envs == [pw.ECMWF_GCS_REPLICA_BASE_URL, pw.ECMWF_S3_BASE_URL]
        assert len(calls) == 2 and calls[0] == calls[1]          # the same rung, twice
        assert outcome["state"] != "FAILED"
        trace = outcome.get("rung_trace") or []
        assert any(r.get("transport") == "ecmwf_s3_after_gcs_outage" for r in trace)
        # Held out: the next capture (and the follow jobs) read S3 straight away.
        assert worker._ecmwf_capture_env()["ECMWF_ARCHIVE_TRANSPORT_BASE_URL"] == pw.ECMWF_S3_BASE_URL
        worker._ecmwf_gcs_outage_until_mono = 0.0
        assert worker._ecmwf_capture_env()["ECMWF_ARCHIVE_TRANSPORT_BASE_URL"] == pw.ECMWF_GCS_REPLICA_BASE_URL
    finally:
        worker._official_native_capture_pool.shutdown(wait=True)


def test_an_ordinary_failure_is_not_rerun_on_s3(monkeypatch, tmp_path):
    not_published = json.dumps({"reason": "ContractError:official_native_forward_required_family_unavailable:"
                                          "ECMWF_OPEN_DATA_S3=HTTPStatusError:Client error '404 Not Found' for url "
                                          "'https://storage.googleapis.com/ecmwf-open-data/x'"})
    worker, calls = _pass_worker(monkeypatch, tmp_path, [_Completed(2, stdout=not_published)])
    try:
        _capture(worker, tmp_path, forced_rungs=RUNG)
        _finish(worker)
        assert len(calls) == 1
        assert worker._ecmwf_capture_env()["ECMWF_ARCHIVE_TRANSPORT_BASE_URL"] == pw.ECMWF_GCS_REPLICA_BASE_URL
    finally:
        worker._official_native_capture_pool.shutdown(wait=True)


def test_every_capture_env_goes_through_the_one_chooser():
    import inspect
    source = inspect.getsource(pw.TargetProductionWorker)
    assert source.count('"ECMWF_ARCHIVE_TRANSPORT_BASE_URL": ECMWF_GCS_REPLICA_BASE_URL') == 0
    assert source.count("self._ecmwf_capture_env()") >= 4

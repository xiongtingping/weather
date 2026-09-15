"""The observation-identity pass runs when the archive has a new report.

It re-derives a three-day window every production cycle -- 33s at the median,
16% of a cycle on 2026-09-15 -- while most cycles fall between one hour's
METARs and the next.  The archive's observation table is append-only (a
correction is a new row), so the newest rowid of what the pass reads -- the
AWC lane and the ASOS substitute fills -- says whether anything the last pass
did not see has arrived; the window's own start moves once a day.  The madis
high-frequency lane is read by neither and must not move it.
"""

from __future__ import annotations

import sqlite3
import subprocess
from datetime import datetime, timezone

import bayes_basket_target.production_worker as pw


def _worker(monkeypatch) -> tuple[pw.TargetProductionWorker, sqlite3.Connection, list]:
    worker = object.__new__(pw.TargetProductionWorker)
    archive = sqlite3.connect(":memory:")
    archive.execute(
        "CREATE TABLE reported_trajectory_observations("
        " observation_id TEXT PRIMARY KEY, source_lane TEXT, station_id TEXT,"
        " valid_at TEXT)"
    )
    archive.execute("INSERT INTO reported_trajectory_observations VALUES ('awc_o1','awc','KMIA','2026-09-15T06:53:00Z')")
    archive.commit()
    worker.canonical_source_conn = archive
    runs: list = []

    def staged(command, **_kw):
        runs.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(pw.subprocess, "run", staged)
    monkeypatch.setattr(pw, "load_bounded_json", lambda *_a, **_kw: {})
    return worker, archive, runs


def test_a_second_cycle_without_a_new_report_reuses_the_staged_contract(monkeypatch) -> None:
    worker, _archive, runs = _worker(monkeypatch)
    first = worker._identify_live_observations()
    assert first["state"] == "STAGED"
    second = worker._identify_live_observations()
    assert second["state"] == "SKIPPED_NO_NEW_REPORTS"
    assert second["duration_sec"] == 0.0
    assert len(runs) == 1


def test_a_new_report_runs_the_pass_again(monkeypatch) -> None:
    worker, archive, runs = _worker(monkeypatch)
    worker._identify_live_observations()
    archive.execute("INSERT INTO reported_trajectory_observations VALUES ('awc_o2','awc','EGLC','2026-09-15T07:50:00Z')")
    archive.commit()
    assert worker._identify_live_observations()["state"] == "STAGED"
    assert len(runs) == 2


def test_a_new_utc_day_runs_the_pass_again(monkeypatch) -> None:
    worker, _archive, runs = _worker(monkeypatch)
    worker._identify_live_observations()
    awc, substitute, _day = worker._live_observation_identity_watermark
    worker._live_observation_identity_watermark = (awc, substitute, "2026-09-14")
    assert worker._identify_live_observations()["state"] == "STAGED"
    assert len(runs) == 2


def test_a_failed_pass_is_retried_next_cycle(monkeypatch) -> None:
    worker, _archive, runs = _worker(monkeypatch)

    def failed(command, **_kw):
        runs.append(command)
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="boom")

    monkeypatch.setattr(pw.subprocess, "run", failed)
    assert worker._identify_live_observations()["state"] == "FAILED"
    assert worker._identify_live_observations()["state"] == "FAILED"
    assert len(runs) == 2


def test_an_archive_that_cannot_be_asked_runs_the_pass_as_before(monkeypatch) -> None:
    worker, _archive, runs = _worker(monkeypatch)
    worker.canonical_source_conn = None
    assert worker._identify_live_observations()["state"] == "STAGED"
    assert worker._identify_live_observations()["state"] == "STAGED"
    assert len(runs) == 2


def test_the_watermark_is_the_newest_row_and_todays_date(monkeypatch) -> None:
    worker, _archive, _runs = _worker(monkeypatch)
    awc, substitute, day = pw._live_observation_watermark(worker.canonical_source_conn)
    assert (awc, substitute) == (1, 0)
    assert day == datetime.now(timezone.utc).date().isoformat()


def test_a_high_frequency_row_the_pass_does_not_read_does_not_rerun_it(monkeypatch) -> None:
    worker, archive, runs = _worker(monkeypatch)
    worker._identify_live_observations()
    archive.execute("INSERT INTO reported_trajectory_observations VALUES ('asos_soft_1','madis_hfmetar','KLAX','2026-09-15T08:05:00Z')")
    archive.commit()
    assert worker._identify_live_observations()["state"] == "SKIPPED_NO_NEW_REPORTS"
    assert len(runs) == 1


def test_a_substitute_fill_reruns_the_pass(monkeypatch) -> None:
    worker, archive, runs = _worker(monkeypatch)
    worker._identify_live_observations()
    archive.execute("INSERT INTO reported_trajectory_observations VALUES ('asos_p1_KLAX_1','madis_hfmetar','KLAX','2026-09-15T08:05:00Z')")
    archive.commit()
    assert worker._identify_live_observations()["state"] == "STAGED"
    assert len(runs) == 2

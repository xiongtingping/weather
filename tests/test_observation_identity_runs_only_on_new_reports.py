"""The observation-identity pass runs when the archive has a new report.

It re-derives a three-day window every production cycle -- 33s at the median,
16% of a cycle on 2026-09-15 -- while most cycles fall between one hour's
METARs and the next.  The archive's observation table is append-only (a
correction is a new row), so its newest rowid says whether anything the last
pass did not see has arrived; the window's own start moves once a day.
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
        " observation_id TEXT PRIMARY KEY, station_id TEXT, valid_at TEXT)"
    )
    archive.execute("INSERT INTO reported_trajectory_observations VALUES ('o1','KMIA','2026-09-15T06:53:00Z')")
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
    archive.execute("INSERT INTO reported_trajectory_observations VALUES ('o2','EGLC','2026-09-15T07:50:00Z')")
    archive.commit()
    assert worker._identify_live_observations()["state"] == "STAGED"
    assert len(runs) == 2


def test_a_new_utc_day_runs_the_pass_again(monkeypatch) -> None:
    worker, _archive, runs = _worker(monkeypatch)
    worker._identify_live_observations()
    newest, _day = worker._live_observation_identity_watermark
    worker._live_observation_identity_watermark = (newest, "2026-09-14")
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
    newest, day = pw._live_observation_watermark(worker.canonical_source_conn)
    assert newest == 1
    assert day == datetime.now(timezone.utc).date().isoformat()

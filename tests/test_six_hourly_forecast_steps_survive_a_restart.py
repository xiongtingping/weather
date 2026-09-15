"""A restart does not make the forecast turn's six-hourly steps due again.

The physics capture and the calibration audit ran every six hours by a due
time kept only on the worker, so each restart made both due at once and the
first forecast turn of every generation spent 108-178s and 32-46s on them
while holding the resident's single writer -- six generations by 06:18Z on
2026-09-15.
"""

from __future__ import annotations

import json
import subprocess
import time
from types import SimpleNamespace

from scripts import weather_basket_yes_daemon as daemon


def _worker(tmp_path):
    return SimpleNamespace(nwp_runtime_root=tmp_path, db_path=tmp_path / "t.db")


def test_a_worker_without_a_runtime_root_keeps_the_in_memory_clock(tmp_path) -> None:
    worker = SimpleNamespace()
    assert daemon.forecast_lane_due_mono(
        worker, attr="_next_x", clock_name="x", interval_sec=60.0
    ) == 0.0
    daemon.record_forecast_lane_attempt(worker, clock_name="x")
    assert not list(tmp_path.iterdir())


def test_an_attempt_survives_into_the_next_generation(tmp_path) -> None:
    daemon.record_forecast_lane_attempt(_worker(tmp_path), clock_name="x")

    reborn = _worker(tmp_path)
    due = daemon.forecast_lane_due_mono(
        reborn, attr="_next_x", clock_name="x", interval_sec=3600.0
    )
    assert due > time.monotonic() + 3500.0
    # Asked again, the generation answers from memory.
    assert daemon.forecast_lane_due_mono(
        reborn, attr="_next_x", clock_name="x", interval_sec=3600.0
    ) == due


def test_an_attempt_older_than_the_interval_is_due_now(tmp_path) -> None:
    (tmp_path / daemon.FORECAST_LANE_CLOCKS_FILENAME).write_text(
        json.dumps({"x": {"attempted_at_utc": "2026-09-15T00:00:00Z"}}),
        encoding="utf-8",
    )
    assert daemon.forecast_lane_due_mono(
        _worker(tmp_path), attr="_next_x", clock_name="x", interval_sec=60.0
    ) == 0.0


def test_an_unreadable_clock_file_is_due_now(tmp_path) -> None:
    (tmp_path / daemon.FORECAST_LANE_CLOCKS_FILENAME).write_text(
        "not json", encoding="utf-8"
    )
    assert daemon.forecast_lane_due_mono(
        _worker(tmp_path), attr="_next_x", clock_name="x", interval_sec=60.0
    ) == 0.0


def test_the_physics_capture_is_not_rerun_by_a_restart(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PM_FORECAST_PHYSICS_REFRESH", "on")
    runs: list[list[str]] = []

    def fake_run(command, **_kw):
        runs.append(command)
        return subprocess.CompletedProcess(
            command, 0,
            stdout=json.dumps({"station_count": 45, "captured": 45}).encode(),
            stderr=b"",
        )

    monkeypatch.setattr(daemon.subprocess, "run", fake_run)
    first = daemon._refresh_forecast_physics(_worker(tmp_path))
    assert first["state"] == "captured"

    after_restart = daemon._refresh_forecast_physics(_worker(tmp_path))
    assert after_restart["state"] == "not_due"
    assert len(runs) == 1


def test_the_calibration_audit_is_not_rerun_by_a_restart(tmp_path) -> None:
    # The first generation runs it (against no database here, so it reports
    # a failure) -- the attempt is what is remembered, as for the physics.
    first = daemon._audit_calibration_health(_worker(tmp_path))
    assert first["state"] != "not_due"

    after_restart = daemon._audit_calibration_health(_worker(tmp_path))
    assert after_restart["state"] == "not_due"

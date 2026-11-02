"""The forward physics capture writes the worker's database, never a default.

2026-09-14: ``_refresh_forecast_physics`` started
``capture_station_forecast_physics.py --mode forward`` with no ``--db``, so the
script wrote its default, the production SSOT.  Three test files drive the
resident's forecast refresh cycle with a stand-in worker, and a regression run
was caught with that capture holding the 45GB live store open under pytest --
a writer outside the resident's schedule, which is exactly what the schedule
exists to prevent.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def daemon():
    spec = importlib.util.spec_from_file_location(
        "_physics_isolation_daemon_under_test",
        ROOT / "scripts" / "weather_basket_yes_daemon.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _no_subprocess(*_args, **_kwargs):
    raise AssertionError("a subprocess was started")


def test_the_suite_turns_the_capture_off(daemon, monkeypatch):
    assert os.environ.get("PM_FORECAST_PHYSICS_REFRESH") == "off"
    monkeypatch.setattr(daemon.subprocess, "run", _no_subprocess)

    class Worker:
        db_path = Path("/somewhere/that/must/not/be/written.db")

    assert daemon._refresh_forecast_physics(Worker())["state"] == "disabled"


def test_a_worker_without_a_database_gets_no_capture(daemon, monkeypatch):
    monkeypatch.setenv("PM_FORECAST_PHYSICS_REFRESH", "on")
    monkeypatch.setattr(daemon.subprocess, "run", _no_subprocess)

    class Worker:
        pass

    state = daemon._refresh_forecast_physics(Worker())
    assert state["state"] == "skipped_worker_has_no_database"


def test_the_capture_is_pointed_at_the_workers_own_database(daemon, monkeypatch, tmp_path):
    monkeypatch.setenv("PM_FORECAST_PHYSICS_REFRESH", "on")
    seen: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        seen.append([str(item) for item in argv])
        return subprocess.CompletedProcess(
            argv, 0,
            stdout=json.dumps({"station_count": 1, "captured": 1, "table_rows": 1}).encode(),
            stderr=b"",
        )

    monkeypatch.setattr(daemon.subprocess, "run", fake_run)

    class Worker:
        db_path = tmp_path / "ssot.db"

    state = daemon._refresh_forecast_physics(Worker())
    assert state["state"] == "captured"
    assert len(seen) == 1
    argv = seen[0]
    assert argv[argv.index("--db") + 1] == str(tmp_path / "ssot.db")


def test_the_resident_keeps_the_failure_reasons(daemon, monkeypatch, tmp_path):
    """2026-10-07 15:31Z/21:31Z: 48 of 48 failed in ~3 s each and the receipt
    said only ``captured: 0`` -- the reasons were dropped."""

    monkeypatch.setenv("PM_FORECAST_PHYSICS_REFRESH", "on")
    receipt = {
        "station_count": 3, "captured": 1, "table_rows": 9,
        "failed": [
            {"station_id": "A", "state": "FAILED", "reason": "OSError:forecast_physics_http_status_429"},
            {"station_id": "B", "state": "FAILED", "reason": "OSError:forecast_physics_http_status_429"},
        ],
        "deferred_past_deadline": ["C", "D"],
    }
    monkeypatch.setattr(
        daemon.subprocess, "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(receipt).encode(), stderr=b""),
    )

    class Worker:
        db_path = tmp_path / "ssot.db"

    state = daemon._refresh_forecast_physics(Worker())
    assert state["failed_count"] == 2
    assert state["failed_reasons"] == {"OSError:forecast_physics_http_status_429": 2}
    assert state["deferred_past_deadline"] == 2


def test_forecast_physics_is_captured_hourly(daemon) -> None:
    # user 2026-10-17 「都取最新的」: P0 (recompiled every one to two hours)
    # must read a radiation total no older than an hour's capture.
    assert daemon.FORECAST_PHYSICS_REFRESH_SECONDS == 60 * 60

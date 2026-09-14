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

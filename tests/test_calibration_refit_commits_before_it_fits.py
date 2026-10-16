"""calibration_refit starts when due and holds no writer turn, because it
never holds a write transaction across its work.

2026-10-16 01:12-01:28Z: the resident counted the sweep as holding the
writer for its whole subprocess, so production waited out its centre/width
and per-station width fits -- ~15 minutes a day, growing with the evidence
window -- until the 900 s timeout killed it, for writes that each take a few
milliseconds.  Each store function that writes commits what it wrote; these
tests hold that the fits run outside any transaction, which is what the
resident's SELF_COMMITTING_SSOT_SWEEPS rests on.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target import center_width_store as store

ROOT = Path(__file__).resolve().parents[1]


def _daemon():
    spec = importlib.util.spec_from_file_location(
        "_refit_turn_daemon_under_test", ROOT / "scripts" / "weather_basket_yes_daemon.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_fit_starts_outside_a_write_transaction(tmp_path, monkeypatch) -> None:
    conn = sqlite3.connect(tmp_path / "ssot.db")
    store.ensure_center_width_schema(conn)
    conn.commit()
    seen: list[tuple[str, bool]] = []

    def fit_center_width(conn_, *, stage, min_decision_at_utc):
        seen.append(("center_width", conn_.in_transaction))
        return {
            "delta_c": 0.0, "width_lambda": 0.75, "forward_nll_gain": -0.01,
            "accumulating": False, "admitted": True, "vetoed": False,
            "evidence": {}, "serving_day_count": 10, "serving_cell_count": 100,
        }

    def fit_station_width(conn_, *, stage, min_decision_at_utc):
        seen.append(("station_width", conn_.in_transaction))
        return {
            "station_count": 0, "forward_nll_gain": 0.0, "accumulating": True,
            "admitted": False, "vetoed": False, "scales": {}, "evidence": {},
        }

    monkeypatch.setattr(store, "fit_center_width", fit_center_width)
    monkeypatch.setattr(store, "fit_station_width", fit_station_width)
    results = store.refit_all_center_width(conn, commit=True, min_decision_at_utc=None)
    served = [stage for stage in results if stage in store.CENTER_WIDTH_SERVED_STAGES]
    assert served, results
    for stage in served:
        assert "error" not in results[stage], results[stage]
        assert results[stage]["outcome"] == "ACTIVATED", results[stage]
    # The station fit runs after the stage's activation and journal were
    # written: they are committed, not held open across it.
    assert ("station_width", False) in seen
    assert all(in_transaction is False for _, in_transaction in seen), seen
    assert conn.in_transaction is False


@pytest.fixture(scope="module")
def daemon():
    return _daemon()


def test_the_resident_starts_it_when_due_and_lets_production_run(daemon) -> None:
    name = "calibration_refit"
    assert name in daemon.SELF_COMMITTING_SSOT_SWEEPS
    # It does write the SSOT, and says so.
    assert daemon.hosted_sweep_write_domain(name) == "ssot"
    # It holds no writer turn: production, the prefetch and WAL maintenance
    # are not held out while it runs ...
    assert daemon.writer_excluding_sweeps([name]) == []
    # ... it claims nothing an SSOT sweep or a capture needs ...
    due = [
        (5.0, 0, "settlement_truth", "s.sh", 900.0),
        (4.0, 1, "capture_noaa_conus_forward", "c.sh", 900.0),
    ]
    picked = [item[2] for item in daemon.admissible_hosted_sweeps(due, running_names=[name])]
    assert "settlement_truth" in picked
    assert "capture_noaa_conus_forward" in [
        item[2] for item in daemon.admissible_hosted_sweeps(due[1:], running_names=[name])
    ]
    # ... it is started on the when-due path, never inside a writer turn ...
    mine = [(9.0, 2, name, "r.sh", 3600.0)]
    assert daemon.admissible_hosted_sweeps(mine, running_names=[]) == []
    assert daemon.writer_independent_admissions(mine, running_names=[]) == mine
    assert daemon.writer_independent_admissions(mine, running_names=[name]) == []
    # ... and its finish serves no writer turn, nor does it owe a lease.
    assert daemon.sweep_finish_serves_writer_turn(name) is False
    assert name not in daemon.HOSTED_SWEEP_LEASE_CONTRACT

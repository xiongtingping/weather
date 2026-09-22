"""The forecast turn says which of its steps held the writer, and for how long.

2026-09-15: the board reported production waiting 742s behind
``forecast_prefetch`` and could not say more.  Traced by hand from file write
times, the rotation advance took three and a half minutes at about 3.9s a
call, while the same call on a read-only replica took 0.14s -- and nothing in
the receipt could tell the two apart.
"""

from __future__ import annotations

import time
from typing import Any

import bayes_basket_target.production_worker as pw
from scripts import weather_basket_yes_daemon as daemon


class _LappingWorker:
    """Advances one scope a call and marks its segments like the real call."""

    skip_nwp_refresh = False
    commit_conn = None
    taf_inbox_path = None
    _nwp_lap = pw.TargetProductionWorker._nwp_lap
    _add_span = pw.TargetProductionWorker._add_span

    def __init__(self) -> None:
        self.calls = 0
        self._nwp_rotation_state = {
            "catalog_scope_keys": [f"scope-{index}" for index in range(90)],
        }

    def refresh_resident_nwp(self, *, as_of_utc: str) -> dict[str, Any]:
        self.calls += 1
        self._nwp_lap(None)
        time.sleep(0.001)
        self._nwp_lap("nwp_registry")
        self._nwp_lap("nwp_official_compile")
        return {"state": "official_native_reused"}


def test_the_poll_publishes_each_segment_once_per_call_and_the_call_total() -> None:
    worker = _LappingWorker()
    receipt = daemon.run_target_forecast_refresh_cycle(worker)

    spans = receipt["resident_nwp_capture"]["call_spans_ms"]
    assert spans["nwp_call_total"]["count"] == worker.calls
    assert spans["nwp_registry"]["count"] == worker.calls
    assert spans["nwp_official_compile"]["count"] == worker.calls
    assert spans["nwp_registry"]["total_ms"] >= worker.calls * 1.0
    assert spans["nwp_call_total"]["total_ms"] >= spans["nwp_registry"]["total_ms"]


def test_a_new_poll_starts_its_spans_from_zero() -> None:
    worker = _LappingWorker()
    daemon.run_target_forecast_refresh_cycle(worker)
    first_calls = worker.calls
    worker._next_official_nwp_poll_mono = 0.0          # due again
    receipt = daemon.run_target_forecast_refresh_cycle(worker)

    spans = receipt["resident_nwp_capture"]["call_spans_ms"]
    assert spans["nwp_call_total"]["count"] == worker.calls - first_calls


def test_the_turn_names_every_step_it_ran() -> None:
    receipt = daemon.run_target_forecast_refresh_cycle(_LappingWorker())
    # Charter 195: the arrival-triggered captures start the turn, off the poll.
    assert set(receipt["step_timings_ms"]) == {
        "nwp_arrival_triggers",
        "resident_nwp", "forward_taf", "forecast_physics", "calibration_health",
    }
    assert all(value >= 0.0 for value in receipt["step_timings_ms"].values())


def test_a_lap_only_records_after_a_call_has_started() -> None:
    worker = _LappingWorker()
    worker._nwp_lap("nwp_registry")                    # no start mark yet
    assert not getattr(worker, "_stage_spans", None)
    worker._nwp_lap(None)
    worker._nwp_lap("nwp_registry")
    assert worker._stage_spans["nwp_registry"]["count"] == 1

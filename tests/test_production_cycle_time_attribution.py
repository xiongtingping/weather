"""A cycle's profile must account for the whole cycle, not part of it.

Measured 2026-09-07, the first cycle after a restart ran 4284.6s against a
720s ceiling.  Its published profile named 1768.2s of stages -- 41% -- and
said nothing at all about the other 2516.4s, because ``_mark`` records the
span *since the previous mark* and the per-scope decision loop, the last
650 lines of ``_run_stages``, had no mark after it.  That loop is where the
cycle lives: 90 candidates, 86 of them refused on ``production_quote_expired``
because the loop outran the 60-second quote lease it compiles against.

Three weeks of "fixed it again" came out of that gap.  With a timeline
covering a minority of the cycle, the only other evidence was the
point-in-time statement probe, which names whichever query is running at the
instant of the snapshot -- so each round found a different slow statement,
fixed it, and met the same overrun on the next.

Two guards, so the gap cannot reopen quietly:
  * the residual is published, so an unmarked stretch shows up as
    ``unattributed`` growing rather than as a timeline that stops accounting;
  * the loop and the settlement lane after it keep their marks.
"""

from __future__ import annotations

import importlib.util
import inspect
import sqlite3
import time
from pathlib import Path

from bayes_basket_target.production_worker import TargetProductionWorker

ROOT = Path(__file__).resolve().parents[1]


def _worker(tmp_path: Path) -> TargetProductionWorker:
    db = tmp_path / "target-read.db"
    with sqlite3.connect(db) as source:
        source.execute("CREATE TABLE marker(value TEXT)")
    runtime = tmp_path / "nwp"
    runtime.mkdir()
    return TargetProductionWorker(db, commit_conn=None, nwp_runtime_root=runtime)


def _dashboard():
    spec = importlib.util.spec_from_file_location(
        "calibration_dashboard_under_test",
        ROOT / "scripts" / "calibration_dashboard.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_time_no_mark_claims_is_published_not_dropped(tmp_path: Path) -> None:
    """The exact 2026-09-07 shape: one named stage, most of the cycle after it."""

    worker = _worker(tmp_path)

    def _stages() -> dict[str, object]:
        worker._mark("named_stage")
        # Stands in for the decision loop: real work, no mark behind it.
        time.sleep(0.05)
        return {"ok": True}

    worker._run_stages = _stages  # type: ignore[method-assign]
    result = worker.run()

    timings = result["stage_timings_ms"]
    assert "unattributed" in timings, (
        "an unmarked stretch must be reported, not silently absent"
    )
    assert timings["unattributed"] >= 40.0, timings
    # And it is the arithmetic residual, not an estimate.
    named = sum(
        value
        for key, value in timings.items()
        if key not in {"cycle_total", "unattributed"}
    )
    assert abs(named + timings["unattributed"] - timings["cycle_total"]) <= 1.0


def test_a_fully_marked_cycle_reports_no_residual(tmp_path: Path) -> None:
    """The guard must not cry wolf over the ordinary case."""

    worker = _worker(tmp_path)

    def _stages() -> dict[str, object]:
        time.sleep(0.02)
        worker._mark("first")
        time.sleep(0.02)
        worker._mark("last")
        return {"ok": True}

    worker._run_stages = _stages  # type: ignore[method-assign]
    timings = worker.run()["stage_timings_ms"]
    assert timings["unattributed"] <= 2.0, timings


def test_the_decision_loop_and_its_settlement_lane_stay_marked() -> None:
    """Removing either mark reopens the blind spot this file exists for."""

    source = inspect.getsource(TargetProductionWorker._run_stages)
    for stage in ("decision_loop", "settlement_lane"):
        assert f'self._mark("{stage}")' in source, stage


def test_per_scope_spans_separate_refusal_from_compile_from_refresh() -> None:
    """"The loop is slow" is not a finding; which of its four costs is."""

    source = inspect.getsource(TargetProductionWorker._run_stages)
    for span in (
        "decision_loop_quote_refresh",
        "decision_loop_compile",
        "decision_loop_commit",
        "decision_loop_blocked_commit",
        "decision_loop_lease_probe",
    ):
        assert span in source, span


def test_an_overrun_names_where_the_seconds_went() -> None:
    """The board reports the total and the top consumers in the same breath."""

    dash = _dashboard()
    from datetime import datetime, timezone

    now = (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    health = dash._production_health({"production": {
        "state": "cycle_committed_partial",
        "ok": True,
        "committed_scope_count": 11,
        "blocked_scope_count": 90,
        "blocked_scope_reasons": {"production_quote_expired": 86},
        "worker_checked_at": now,
        "stage_timings_ms": {
            "cycle_total": 4_284_617.5,
            "decision_loop": 2_516_400.0,
            "probability_p0m": 830_354.4,
            "market_ingress": 325_547.3,
            "unattributed": 0.0,
        },
    }})
    note = health["production_note"]
    assert "单轮 4285 秒" in note, note
    assert "decision_loop 2516s" in note, note
    assert "probability_p0m 830s" in note, note

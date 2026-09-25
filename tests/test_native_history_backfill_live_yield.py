"""The native history backfill stands aside for live SSOT lanes and paces itself.

Measured 2026-09-13: while this lane ran, the SSOT write lock was free on 0 of
32 probes over thirty seconds, and on 74 of 74 once it stopped.  The live
capture lanes wait 120 seconds and then raise, so jma_msm and dwd_icon_eu each
failed their only attempt in five hours with "database is locked".  These
tests hold the two behaviours that end that: yield while a live lane is late,
and leave the lock idle between packets.  They test the decision, not the
clock -- sleep and now are injected.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


driver = _load("_run_native_history_backfill_under_test", "scripts/run_native_history_backfill.py")

NOW = datetime(2026, 9, 13, 2, 0, tzinfo=timezone.utc)


def _stamp(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


def test_a_lane_is_late_only_past_its_own_interval():
    intervals = {"capture_jma_msm_forward": 10800.0, "market_registry_retention": 3600.0}
    clocks = {
        # 338 minutes on a 180-minute contract: 1.9 rounds behind.
        "capture_jma_msm_forward": {"attempted_at_utc": _stamp(338)},
        # 33 minutes on a 60-minute contract: on time.
        "market_registry_retention": {"attempted_at_utc": _stamp(33)},
    }

    late = driver.overdue_ssot_lanes(intervals, clocks, NOW.timestamp(), rounds=1.0)

    assert [name for name, _ in late] == ["capture_jma_msm_forward"]
    assert round(late[0][1], 2) == round(338 / 180, 2)


def test_the_threshold_is_rounds_of_the_lanes_own_interval():
    intervals = {"pr12_awc_observation": 10800.0}
    clocks = {"pr12_awc_observation": {"attempted_at_utc": _stamp(270)}}  # 1.5 rounds

    assert driver.overdue_ssot_lanes(intervals, clocks, NOW.timestamp(), rounds=1.0)
    assert not driver.overdue_ssot_lanes(intervals, clocks, NOW.timestamp(), rounds=2.0)


def test_a_lane_that_never_ran_is_not_contention_but_an_unreadable_clock_is():
    intervals = {"never_ran": 3600.0, "garbled": 3600.0}
    clocks = {"garbled": {"attempted_at_utc": "not-a-time"}}

    late = dict(driver.overdue_ssot_lanes(intervals, clocks, NOW.timestamp(), rounds=1.0))

    assert "never_ran" not in late
    assert late["garbled"] == float("inf")


def test_the_yield_reads_the_residents_sweep_table_not_a_copy():
    """Restating the intervals here is how an audit starts disagreeing with
    production.  The contract must be the daemon's own table, SSOT members
    only, since only they contend for this write lock."""

    daemon = _load("_daemon_contract_under_test", "scripts/weather_basket_yes_daemon.py")
    expected = {
        str(name): float(interval)
        for name, _script, interval, _timeout in daemon.HOSTED_SWEEPS
        if daemon.HOSTED_SWEEP_WRITE_DOMAIN.get(str(name)) == "ssot"
    }

    intervals, clocks_path = driver._hosted_sweep_contract()

    assert intervals == expected
    assert all(daemon.HOSTED_SWEEP_WRITE_DOMAIN[name] == "ssot" for name in intervals)
    assert clocks_path == Path(daemon.HOSTED_SWEEP_CLOCKS_PATH)


def test_standing_aside_ends_the_moment_the_resident_serves_the_late_lane():
    intervals = {"capture_dwd_icon_eu_forward": 10800.0}
    clock = {"now": NOW.timestamp()}
    reads = {"n": 0}
    slept: list[float] = []

    def read_clocks():
        reads["n"] += 1
        # Late for the first two polls; the resident then stamps the lane.
        minutes_ago = 313 if reads["n"] <= 2 else 0
        return {"capture_dwd_icon_eu_forward": {"attempted_at_utc": _stamp(minutes_ago)}}

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["now"] += seconds

    result = driver.stand_aside_for_live_lanes(
        intervals, read_clocks, rounds=1.0, max_seconds=900.0,
        sleep=sleep, now=lambda: NOW.timestamp(), poll_seconds=15.0,
    )

    assert result["ended"] == "clear"
    assert slept == [15.0, 15.0]
    assert result["waited_seconds"] == 30.0
    assert [name for name, _ in result["late"]] == ["capture_dwd_icon_eu_forward"]


def test_standing_aside_is_capped_so_a_stuck_lane_cannot_stall_the_walk():
    intervals = {"station_onboarding": 3600.0}
    slept: list[float] = []

    result = driver.stand_aside_for_live_lanes(
        intervals,
        lambda: {"station_onboarding": {"attempted_at_utc": _stamp(60 * 50)}},
        rounds=1.0, max_seconds=60.0,
        sleep=slept.append, now=lambda: NOW.timestamp(), poll_seconds=15.0,
    )

    assert result["ended"] == "cap_reached"
    assert sum(slept) == 60.0


def test_nothing_late_costs_nothing():
    slept: list[float] = []

    result = driver.stand_aside_for_live_lanes(
        {"settlement_truth": 10800.0},
        lambda: {"settlement_truth": {"attempted_at_utc": _stamp(5)}},
        rounds=1.0, max_seconds=900.0,
        sleep=slept.append, now=lambda: NOW.timestamp(),
    )

    assert result == {"waited_seconds": 0.0, "ended": "not_needed", "late": []}
    assert slept == []


def test_an_unreadable_clocks_file_does_not_block_the_walk():
    def read_clocks():
        raise ValueError("truncated json")

    result = driver.stand_aside_for_live_lanes(
        {"settlement_truth": 10800.0}, read_clocks,
        rounds=1.0, max_seconds=900.0, sleep=lambda s: None, now=lambda: NOW.timestamp(),
    )

    assert result["ended"] == "not_needed"


def test_pacing_leaves_the_lock_idle_as_long_as_it_was_held():
    slept: list[float] = []

    held = driver.pace_after_commit(40.0, ratio=1.0, max_seconds=120.0, sleep=slept.append)

    assert held == 40.0 and slept == [40.0]


def test_pacing_is_capped_and_can_be_switched_off():
    slept: list[float] = []

    assert driver.pace_after_commit(900.0, ratio=1.0, max_seconds=120.0, sleep=slept.append) == 120.0
    assert driver.pace_after_commit(40.0, ratio=0.0, max_seconds=120.0, sleep=slept.append) == 0.0
    assert slept == [120.0]


def test_the_drain_loop_yields_before_ingest_and_paces_after_commit():
    """Order matters: standing aside after taking the lock would be no yield
    at all.  Read the drain body as code, not as text."""

    import ast

    tree = ast.parse((ROOT / "scripts" / "run_native_history_backfill.py").read_text())
    drain = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "drain"
    )
    calls = [
        (node.lineno, node.func.id)
        for node in ast.walk(drain)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id in {"stand_aside", "_run", "pace"}
    ]
    order = [name for _, name in sorted(calls)]

    assert order == ["stand_aside", "_run", "pace"]


def test_the_scratch_prune_keeps_queued_and_young_packets_and_drops_old_raw(tmp_path):
    import os
    import time

    raw = tmp_path / "raw_ecmwf"
    packets = tmp_path / "packets" / "ecmwf"
    raw.mkdir()
    packets.mkdir(parents=True)
    old = time.time() - 30 * 3600

    def make(path: Path, *, aged: bool) -> Path:
        path.write_bytes(b"x" * 10)
        if aged:
            os.utime(path, (old, old))
        return path

    old_raw = make(raw / "field_old.grib2", aged=True)
    young_raw = make(raw / "field_young.grib2", aged=False)
    committed = make(packets / "ecmwf_2026-01-01_l1_c12.json", aged=True)
    queued = make(packets / "ecmwf_2026-01-02_l1_c12.json", aged=True)
    young_packet = make(packets / "ecmwf_2026-01-03_l1_c12.json", aged=False)

    removed, freed = driver.prune_scratch_store(
        [raw], [packets], {queued.resolve()}, raw_keep_hours=1.5,
    )

    assert (removed, freed) == (2, 20)
    assert not old_raw.exists() and not committed.exists()
    assert young_raw.exists() and queued.exists() and young_packet.exists()


def test_every_exit_of_the_walk_prunes_the_scratch_store():
    """The per-day prune sits after the max_seconds break, so a `--max-seconds`
    loop never reached it and 10.8 GB of raw piled up (2026-09-14).  A prune
    must also run after the walk, outside the loop, on every way out."""

    import ast

    tree = ast.parse((ROOT / "scripts" / "run_native_history_backfill.py").read_text())
    main = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    walk = next(node for node in main.body if isinstance(node, ast.While))

    def prunes(node) -> bool:
        return any(
            isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
            and call.func.id == "prune_scratch_store"
            for call in ast.walk(node)
        )

    after_walk = main.body[main.body.index(walk) + 1:]
    assert prunes(walk)
    assert any(prunes(stmt) for stmt in after_walk)

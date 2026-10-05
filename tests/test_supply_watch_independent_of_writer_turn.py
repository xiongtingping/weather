"""The supply watch starts when due, whoever holds the writer.

The send gate reads the watch's file and fails OPEN once it is fifteen
minutes old, and every hosted sweep -- write domain ``none`` included -- was
admitted only in a sweeps writer turn with no production cycle in flight.
2026-10-05 03:0xZ, a restart and a 735 s first cycle later, the board read
「守望扫描 16 分钟没有跑」: the egress guard was off while production was busy.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def daemon():
    spec = importlib.util.spec_from_file_location(
        "weather_basket_yes_daemon",
        ROOT / "scripts" / "weather_basket_yes_daemon.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(daemon, name, overdue=1.0):
    for index, (member, script, _interval, timeout) in enumerate(daemon.HOSTED_SWEEPS):
        if member == name:
            return (overdue, index, member, script, timeout)
    raise AssertionError(name)


def test_independent_sweeps_write_no_database(daemon) -> None:
    assert "supply_watch" in daemon.WRITER_INDEPENDENT_SWEEPS
    hosted = {name for name, *_ in daemon.HOSTED_SWEEPS}
    for name in daemon.WRITER_INDEPENDENT_SWEEPS:
        assert name in hosted
        assert daemon.hosted_sweep_write_domain(name) == "none"


def test_the_watch_is_admitted_beside_a_running_writer(daemon) -> None:
    due = [_row(daemon, "supply_watch"), _row(daemon, "settlement_truth", 5.0)]
    # A production cycle and an SSOT sweep are running: only the watch starts.
    admitted = daemon.writer_independent_admissions(
        due, running_names=["settlement_truth"],
    )
    assert [item[2] for item in admitted] == ["supply_watch"]
    # Already running, or no room under the total cap: nothing.
    assert daemon.writer_independent_admissions(due, running_names=["supply_watch"]) == []
    full = ["a", "b", "c", "d"][: daemon.HOSTED_SWEEP_MAX_CONCURRENCY]
    assert daemon.writer_independent_admissions(due, running_names=full) == []


def test_the_watch_takes_no_writer_turn_and_serves_none(daemon) -> None:
    far = 10.0**9
    only_watch_due = {name: far for name, *_ in daemon.HOSTED_SWEEPS}
    only_watch_due["supply_watch"] = 0.0
    assert daemon.writer_turn_sweeps_due(now_mono=1.0, next_due=only_watch_due) is False
    only_watch_due["settlement_truth"] = 0.0
    assert daemon.writer_turn_sweeps_due(now_mono=1.0, next_due=only_watch_due) is True
    assert daemon.sweep_finish_serves_writer_turn("supply_watch") is False
    assert daemon.sweep_finish_serves_writer_turn("settlement_truth") is True

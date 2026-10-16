"""One number says what a sweep slot may cost production: the writer lease.

Between production cycles the writer goes to a hosted sweep for as long as
its pass runs.  Every pass budget so far was the lane's own constant (900s,
1200s, 1500s), each sized after the incident that lane caused; on
2026-09-15 the paper ledger's 1500s pass kept a due production cycle
waiting 1,025s and the maker probe's seven-minute pass 422s.  The resident
now grants the lease in the sweep's environment, sliced lanes clamp to it
and continue on the next pass, and the record names any lane that ran past
it.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sqlite3
import sys
from pathlib import Path

import pytest

import bayes_basket_target.pass_deadline as pd
from scripts import weather_basket_yes_daemon as daemon

ROOT = Path(__file__).resolve().parents[1]


# --- the helper every lane reads ------------------------------------------

def test_no_lease_keeps_the_lanes_own_budget(monkeypatch) -> None:
    monkeypatch.delenv(pd.WRITER_LEASE_ENV, raising=False)
    assert pd.writer_lease_seconds(900.0) == 900.0
    for junk in ("", "  ", "abc", "-5", "0"):
        monkeypatch.setenv(pd.WRITER_LEASE_ENV, junk)
        assert pd.writer_lease_seconds(900.0) == 900.0


def test_a_lease_clamps_a_larger_budget_and_leaves_a_smaller_one(monkeypatch) -> None:
    monkeypatch.setenv(pd.WRITER_LEASE_ENV, "240")
    assert pd.writer_lease_seconds(900.0) == 240.0
    assert pd.writer_lease_seconds(60.0) == 60.0


# --- the resident grants it -----------------------------------------------

def test_every_hosted_sweep_is_started_with_the_lease(tmp_path) -> None:
    script = tmp_path / "sweep.sh"
    script.write_text(
        f'#!/bin/bash\necho "lease=${{{pd.WRITER_LEASE_ENV}}}"\n', encoding="utf-8"
    )
    result = daemon.run_hosted_sweep(str(script), 30.0)
    assert result["state"] == "ok"
    assert f"lease={daemon.WRITER_LEASE_SEC:.0f}" in result["output_tail"]


def test_the_lease_keeps_production_inside_its_own_contract() -> None:
    """A cycle at its warn line plus one lease stays under the overdue line."""

    assert daemon.WRITER_LEASE_SEC < daemon.PRODUCTION_CYCLE_OVERDUE_SEC
    assert daemon.WRITER_LEASE_SEC + 450.0 < 2 * daemon.PRODUCTION_CYCLE_OVERDUE_SEC


# --- the record names a lane that ran past it -----------------------------

def test_the_clock_entry_records_duration_and_overrun_for_a_writer_holder() -> None:
    entry = daemon.hosted_sweep_clock_entry(
        {"completed_at_utc": "2026-09-15T01:00:00Z"},
        {"state": "ok", "duration_sec": 400.0, "continue": True},
        name="settlement_truth", now_utc="2026-09-15T02:00:00Z",
        lease_sec=240.0,
    )
    assert entry["completed_at_utc"] == "2026-09-15T02:00:00Z"
    assert entry["attempted_at_utc"] == "2026-09-15T02:00:00Z"
    assert entry["last_duration_sec"] == 400.0
    assert entry["lease_overrun_sec"] == 160.0
    assert entry["continue_requested"] is True


def test_a_failed_run_keeps_the_completion_stamp_and_records_no_continuation() -> None:
    entry = daemon.hosted_sweep_clock_entry(
        {"completed_at_utc": "2026-09-15T01:00:00Z"},
        {"state": "timeout_killed", "duration_sec": 1800.0, "continue": True},
        name="settlement_truth", now_utc="2026-09-15T02:00:00Z",
    )
    assert entry["completed_at_utc"] == "2026-09-15T01:00:00Z"
    assert entry["last_state"] == "timeout_killed"
    assert entry["continue_requested"] is False
    assert entry["lease_overrun_sec"] > 0


def test_a_download_capture_never_overruns_the_lease() -> None:
    entry = daemon.hosted_sweep_clock_entry(
        None, {"state": "ok", "duration_sec": 1150.0},
        name="capture_noaa_conus_forward", now_utc="2026-09-15T02:00:00Z",
    )
    assert entry["lease_overrun_sec"] == 0.0


# --- every writer-holding sweep is under contract --------------------------

def _writer_holding_sweeps() -> list[str]:
    return sorted(
        name for name, _script, _interval, _timeout in daemon.HOSTED_SWEEPS
        if daemon.hosted_sweep_write_domain(name) in daemon._EXCLUSIVE_WRITE_DOMAINS
        and name not in daemon.WRITER_TURN_EXEMPT_SWEEPS
    )


def _entry_script(sweep_name: str) -> Path:
    wrapper = next(
        ROOT / script for name, script, _i, _t in daemon.HOSTED_SWEEPS
        if name == sweep_name
    )
    text = wrapper.read_text(encoding="utf-8")
    return ROOT / re.search(r"scripts/[A-Za-z0-9_]+\.py", text).group(0)


def _imports_lease_helper(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "bayes_basket_target.pass_deadline"
        and any(alias.name == "writer_lease_seconds" for alias in node.names)
        for node in ast.walk(tree)
    )


def test_every_writer_holding_sweep_is_classified() -> None:
    holders = _writer_holding_sweeps()
    assert holders, "no writer-holding sweep discovered"
    assert set(holders) == set(daemon.HOSTED_SWEEP_LEASE_CONTRACT), (
        set(holders) ^ set(daemon.HOSTED_SWEEP_LEASE_CONTRACT)
    )


@pytest.mark.parametrize("sweep_name", _writer_holding_sweeps())
def test_a_sliced_sweep_reads_the_lease_and_a_one_shot_is_rare_past_it(sweep_name) -> None:
    kind, bound = daemon.HOSTED_SWEEP_LEASE_CONTRACT[sweep_name]
    interval = next(
        i for name, _s, i, _t in daemon.HOSTED_SWEEPS if name == sweep_name
    )
    if kind == "sliced":
        assert bound == daemon.WRITER_LEASE_SEC
        assert _imports_lease_helper(_entry_script(sweep_name)), sweep_name
    else:
        assert kind == "one_shot"
        if bound > daemon.WRITER_LEASE_SEC:
            assert interval >= 6 * 3600.0, (
                f"{sweep_name} may run {bound:.0f}s past a {daemon.WRITER_LEASE_SEC:.0f}s "
                f"lease and is due every {interval:.0f}s"
            )


# --- the sliced lanes stop on it -------------------------------------------

def _fresh(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_truth_sweep_stops_between_scopes_and_says_so(monkeypatch, tmp_path, capsys) -> None:
    truth = _fresh(ROOT / "scripts" / "settlement_truth_sweep.py", "_truth_sweep_under_test")
    target = tmp_path / "ssot.db"
    canonical = tmp_path / "archive.db"
    for path in (target, canonical):
        sqlite3.connect(path).close()
    monkeypatch.setattr(truth, "_backlog", lambda conn, start: [("KMIA", "2026-09-10"), ("EGLC", "2026-09-10")])
    monkeypatch.setattr(truth, "_coverage", lambda conn, start: [])
    calls: list = []
    monkeypatch.setattr(
        truth, "materialize_settled_station_truth",
        lambda *a, **kw: calls.append(kw) or {"state": "committed", "wrote": False},
    )
    monkeypatch.setattr(truth, "writer_lease_seconds", lambda default: 0.0)
    monkeypatch.setattr(truth, "STATUS_JSON", tmp_path / "status.json")
    monkeypatch.setattr(sys, "argv", [
        "settlement_truth_sweep.py", "--db", str(target), "--canonical", str(canonical),
        "--status-out", str(tmp_path / "status.json"),
    ])
    assert truth.main() == 0
    out = capsys.readouterr().out.splitlines()
    assert pd.HOSTED_SWEEP_CONTINUE_MARKER in out
    assert calls == []


def test_the_settled_day_close_stops_between_cycles_and_says_so(monkeypatch, tmp_path, capsys) -> None:
    close = _fresh(ROOT / "scripts" / "settled_day_cycle_close_sweep.py", "_day_close_under_test")
    db = tmp_path / "ssot.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE basket_target_capital_pool(pool_id INTEGER, balance_usdc REAL, reserved_usdc REAL)")
    conn.execute("INSERT INTO basket_target_capital_pool VALUES (1, 100.0, 5.0)")
    conn.commit(); conn.close()
    ready = [{"cycle_id": "c1", "settlement_day_end_utc": "2026-09-10T00:00:00Z", "amount_usdc": 1.0}]
    monkeypatch.setattr(close, "_candidates", lambda conn, *, as_of, limit: (ready, {}, 0.0, 0))
    closed: list = []
    monkeypatch.setattr(close, "close_settled_day_cycle", lambda conn, **kw: closed.append(kw) or {"state": "closed", "released_usdc": 1.0, "cancelled_intent_count": 0})
    monkeypatch.setattr(close, "writer_lease_seconds", lambda default: 0.0)
    monkeypatch.setattr(close, "STATUS_JSON", tmp_path / "status.json")
    monkeypatch.setattr(sys, "argv", ["settled_day_cycle_close_sweep.py", "--db", str(db)])
    assert close.main() == 0
    assert pd.HOSTED_SWEEP_CONTINUE_MARKER in capsys.readouterr().out.splitlines()
    assert closed == []

    monkeypatch.setattr(close, "writer_lease_seconds", lambda default: 600.0)
    assert close.main() == 0
    assert pd.HOSTED_SWEEP_CONTINUE_MARKER not in capsys.readouterr().out.splitlines()
    assert len(closed) == 1


# --- the ledger's scheduled walk stops on the budget too --------------------



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
import time
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
        name="paper_trading_ledger", now_utc="2026-09-15T02:00:00Z",
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
        name="paper_trading_ledger", now_utc="2026-09-15T02:00:00Z",
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


def test_the_ledger_pass_budget_is_clamped_by_the_lease(monkeypatch) -> None:
    monkeypatch.setenv(pd.WRITER_LEASE_ENV, "150")
    ledger = _fresh(ROOT / "scripts" / "paper_trading_ledger.py", "_ledger_under_lease")
    assert ledger.REDERIVE_PASS_BUDGET_SEC == 150.0
    monkeypatch.delenv(pd.WRITER_LEASE_ENV)
    ledger = _fresh(ROOT / "scripts" / "paper_trading_ledger.py", "_ledger_without_lease")
    assert ledger.REDERIVE_PASS_BUDGET_SEC == 420.0


@pytest.fixture()
def probe():
    return _fresh(ROOT / "scripts" / "maker_shadow_probe.py", "_maker_probe_under_test")


def _book_db(probe) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    probe.ensure_schema(conn)
    conn.execute(
        "CREATE TABLE basket_official_ws_snapshot("
        " station_id TEXT, event_date TEXT, state TEXT,"
        " quote_observed_at TEXT, buckets_json TEXT)"
    )
    return conn


def test_the_probe_places_nothing_once_its_clock_is_spent(probe, monkeypatch) -> None:
    conn = _book_db(probe)
    now = probe._now()
    book = {"31C": {"ask": 0.4, "bid": 0.38, "tick": 0.01, "queue_ahead": None,
                    "queue_known": False, "depth": 10.0}}
    monkeypatch.setattr(
        probe, "_latest_books",
        lambda c, **kw: {("KMIA", "2026-09-16"): (now, book), ("EGLC", "2026-09-16"): (now, book)},
    )
    monkeypatch.setattr(probe, "_model_probabilities", lambda c, s, d: {"31C": 0.5})
    monkeypatch.setattr(probe, "load_effective_edge_tables", lambda c: None)

    spent = probe.place(conn, deadline=time.monotonic() - 1.0)
    assert spent["stopped_for_time"] == 1
    assert conn.execute("SELECT COUNT(*) FROM maker_shadow_order").fetchone()[0] == 0

    whole = probe.place(conn, deadline=None)
    assert "stopped_for_time" not in whole
    assert whole["scopes_with_fresh_book"] == 2
    assert conn.execute("SELECT COUNT(*) FROM maker_shadow_order").fetchone()[0] > 0


def test_the_probe_resolves_nothing_once_its_clock_is_spent(probe, monkeypatch) -> None:
    conn = _book_db(probe)
    now = probe._now()
    book = {"31C": {"ask": 0.4, "bid": 0.38, "tick": 0.01, "queue_ahead": None,
                    "queue_known": False, "depth": 10.0}}
    monkeypatch.setattr(
        probe, "_latest_books", lambda c, **kw: {("KMIA", "2026-09-16"): (now, book)},
    )
    monkeypatch.setattr(probe, "_model_probabilities", lambda c, s, d: {"31C": 0.5})
    monkeypatch.setattr(probe, "load_effective_edge_tables", lambda c: None)
    probe.place(conn)
    open_before = conn.execute(
        "SELECT COUNT(*) FROM maker_shadow_order WHERE state='OPEN'"
    ).fetchone()[0]
    assert open_before > 0

    spent = probe.resolve(conn, deadline=time.monotonic() - 1.0)
    assert spent["stopped_for_time"] == 1
    assert spent["still_open"] == open_before


def test_the_probe_asks_for_the_next_pass_when_any_phase_stopped(probe, monkeypatch, tmp_path, capsys) -> None:
    db = tmp_path / "book.db"
    sqlite3.connect(db).close()
    monkeypatch.setattr(probe, "place", lambda conn, *, deadline=None: {"stopped_for_time": 1})
    monkeypatch.setattr(probe, "resolve", lambda conn, *, deadline=None: {"open": 0})
    monkeypatch.setattr(probe, "settle", lambda conn, *, deadline=None: {"unsettled_fills": 0})
    monkeypatch.setattr(probe, "claim_cap_reading", lambda conn: "cap")
    monkeypatch.setattr(sys, "argv", ["maker_shadow_probe.py", "--db", str(db)])
    assert probe.main() == 0
    assert pd.HOSTED_SWEEP_CONTINUE_MARKER in capsys.readouterr().out.splitlines()

    monkeypatch.setattr(probe, "place", lambda conn, *, deadline=None: {"intents_written": 3})
    assert probe.main() == 0
    assert pd.HOSTED_SWEEP_CONTINUE_MARKER not in capsys.readouterr().out.splitlines()


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

def _ledger_walk_harness(monkeypatch, tmp_path):
    ledger = _fresh(ROOT / "scripts" / "paper_trading_ledger.py", "_ledger_walk_under_test")
    conn = sqlite3.connect(":memory:")
    ledger.ensure_schema(conn)
    conn.execute(
        "CREATE TABLE basket_probability_bridge_history("
        " station_id TEXT, event_date TEXT, stage TEXT, state TEXT)"
    )
    conn.executemany(
        "INSERT INTO basket_probability_bridge_history VALUES (?,?,'P0','COMMITTED')",
        [("KMIA", "2026-09-14"), ("EGLC", "2026-09-14"), ("KMIA", "2026-09-15")],
    )
    conn.commit()
    # Everything before the walk, stubbed: the question is only whether the
    # walk starts a scope once its clock is spent.
    monkeypatch.setattr(ledger, "ensure_schema", lambda c: False)
    monkeypatch.setattr(ledger, "_purge_below_floor", lambda c: 0)
    monkeypatch.setattr(ledger, "_positions_under_other_rule_sets", lambda c: 0)
    monkeypatch.setattr(ledger, "_repair_slice_through", lambda c, *, recent_days: None)
    monkeypatch.setattr(ledger, "_drop_superseded_positions", lambda c, **kw: (0, 0))
    monkeypatch.setattr(ledger, "_truth", lambda c: {})
    monkeypatch.setattr(ledger, "_station_peak_cutoffs", lambda tz: {})
    monkeypatch.setattr(ledger, "_rescore_settlements", lambda c, t: {})
    monkeypatch.setattr(ledger, "_opening_equity", lambda c, *, earliest: 1000.0)
    conn.execute(
        "CREATE TABLE calibration_station_market_timezone_binding("
        " station_id TEXT, settlement_timezone TEXT)"
    )
    return ledger, conn


def test_the_scheduled_walk_answers_no_scope_once_its_budget_is_spent(monkeypatch, tmp_path) -> None:
    ledger, conn = _ledger_walk_harness(monkeypatch, tmp_path)
    counts = ledger.backfill(conn, recent_days=4, budget_sec=0.0)
    assert counts["stopped_for_time"] == 1
    assert counts.get("already_recorded", 0) == 0
    assert counts.get("chain_replayed", 0) == 0


def test_a_cut_walk_asks_for_the_next_pass_and_takes_no_repair_slice(monkeypatch, tmp_path, capsys) -> None:
    ledger = _fresh(ROOT / "scripts" / "paper_trading_ledger.py", "_ledger_main_under_test")
    db = tmp_path / "book.db"
    db.write_bytes(b"")
    monkeypatch.setattr(ledger, "_connect", lambda *_a, **_kw: sqlite3.connect(":memory:"))
    calls: list[dict] = []

    def cut_walk(conn, **kwargs):
        calls.append(kwargs)
        # A plain incremental pass, no repair in sight, cut for time: the
        # marker is owed for the scopes it did not reach.
        return {"stopped_for_time": 1}

    monkeypatch.setattr(ledger, "backfill", cut_walk)
    monkeypatch.setattr(ledger, "_repair_slice_through", lambda *_a, **_kw: None)
    monkeypatch.setattr(sys, "argv", ["paper_trading_ledger.py", "--backfill", "--db", str(db)])
    assert ledger.main() == 0
    assert len(calls) == 1
    assert calls[0]["budget_sec"] == ledger.REDERIVE_PASS_BUDGET_SEC
    assert pd.HOSTED_SWEEP_CONTINUE_MARKER in capsys.readouterr().out.splitlines()


def test_a_rebuild_by_hand_walks_to_the_end(monkeypatch, tmp_path) -> None:
    ledger = _fresh(ROOT / "scripts" / "paper_trading_ledger.py", "_ledger_rebuild_under_test")
    db = tmp_path / "book.db"
    db.write_bytes(b"")
    conn = sqlite3.connect(":memory:")
    ledger.ensure_schema(conn)
    monkeypatch.setattr(ledger, "_connect", lambda *_a, **_kw: conn)
    calls: list[dict] = []
    monkeypatch.setattr(ledger, "backfill", lambda c, **kw: calls.append(kw) or {})
    monkeypatch.setattr(sys, "argv", ["paper_trading_ledger.py", "--rebuild", "--db", str(db)])
    assert ledger.main() == 0
    assert calls[0]["budget_sec"] is None

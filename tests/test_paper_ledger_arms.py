"""Arms on the paper ledger: a production knob per arm, read off the run.

The old ledger's ``PAPER_LEDGER_*`` arms left with it on 2026-09-25.  An arm is
now a ``PM_*`` knob production itself reads; these pin the parsing, the
command each arm runs, and the table that refuses to compare runs made on
different code or windows.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import paper_ledger_aligned as ledger  # noqa: E402
import paper_ledger_arms as arms  # noqa: E402


def test_an_arm_is_a_name_and_its_production_knobs() -> None:
    assert arms.parse_arm("base") == ("base", {})
    assert arms.parse_arm("dwell:PM_RESTING_REPRICE_DWELL_SECONDS=3600,PM_X=a") == (
        "dwell", {"PM_RESTING_REPRICE_DWELL_SECONDS": "3600", "PM_X": "a"},
    )
    # Only production's own knobs: the ledger has no switches of its own.
    with pytest.raises(ValueError, match="arm_knob_invalid"):
        arms.parse_arm("old:PAPER_LEDGER_EXECUTION=taker")
    with pytest.raises(ValueError, match="arm_knob_invalid"):
        arms.parse_arm("bare:PM_X")
    with pytest.raises(ValueError, match="arm_name_invalid"):
        arms.parse_arm(":PM_X=1")


def test_each_arm_walks_the_same_window_under_its_own_label() -> None:
    args = argparse.Namespace(
        capital="paper", opening_cash_usdc=120.0, fill_model="empirical",
        ledger_db="/tmp/x.db", from_event_date="2026-09-21",
        to_event_date="2026-09-24", stations="",
    )
    command = arms.arm_command(args, "dwell")
    assert command[1] == str(arms.LEDGER_SCRIPT)
    assert command[command.index("--label") + 1] == "arm:dwell"
    assert command[command.index("--from-event-date") + 1] == "2026-09-21"
    assert command[command.index("--to-event-date") + 1] == "2026-09-24"
    assert "--stations" not in command


def test_the_run_id_is_read_off_the_printed_summary() -> None:
    assert arms._run_id(json.dumps({"run_id": "run_a", "orders": 3}, indent=2)) == "run_a"
    with pytest.raises(RuntimeError):
        arms._run_id("{}")


def _book(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    for ddl in ledger.LEDGER_DDL:
        conn.execute(ddl)
    for run_id, head, env in (
        ("run_base", "aaaa1111", {}),
        ("run_arm", "aaaa1111", {"PM_RESTING_REPRICE_DWELL_SECONDS": "3600"}),
        ("run_other", "bbbb2222", {}),
    ):
        params = {"label": f"arm:{run_id}", "from_event_date": "2026-09-21",
                  "to_event_date": "2026-09-24", "capital": "paper",
                  "pm_environment": env}
        summary = {"fill_model": "empirical", "maker_fill_curves_sha256": "c" * 64,
                   "settled_station_days": 1, "settled_cost_usdc": 0.9,
                   "settled_pnl_usdc": 0.1, "settled_return_on_cost": 0.111111,
                   "orders_by_state": {"filled": 2}}
        conn.execute(
            "INSERT INTO runs VALUES(?,?,?,?,?,?,?)",
            (run_id, ledger.SCHEMA, "t0", "t1", head, json.dumps(params), json.dumps(summary)),
        )
        # One maker fill on the winner and one taker fill on a loser.
        for order_id, bucket, candidate, filled, cost, stage in (
            ("o1", "30°C", "MAKER", 2.0, 0.6, "P0"),
            ("o2", "31°C", "TAKER_FOK", 1.0, 0.3, "P0-M"),
        ):
            conn.execute(
                "INSERT INTO orders(run_id,order_id,station_id,event_date,bucket,candidate,"
                "limit_price,size,placed_at,expires_at,state,filled_size,filled_cost_usdc,"
                "ended_at,end_reason,events_json,stage) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, order_id, "ZUUU", "2026-09-22", bucket, candidate, cost / filled,
                 filled, "t", None, "filled", filled, cost, "t", "", "[]", stage),
            )
        conn.execute(
            "INSERT INTO settlements VALUES(?,?,?,?,?,?,?,?)",
            (run_id, "ZUUU", "2026-09-22", "30°C", 0.9, 2.0, 1.1, "{}"),
        )
    conn.commit()
    return conn


def test_the_table_prices_each_fill_against_the_settled_bucket(tmp_path) -> None:
    conn = _book(tmp_path / "l.db")
    table = arms.compare(conn, ["run_base", "run_arm"])
    assert table["comparable"] is True
    base, armed = table["runs"]
    assert armed["pm_environment"] == {"PM_RESTING_REPRICE_DWELL_SECONDS": "3600"}
    assert base["by_stage"] == {
        "P0 MAKER": {"fills": 1, "cost_usdc": 0.6, "pnl_usdc": 1.4},
        "P0-M TAKER_FOK": {"fills": 1, "cost_usdc": 0.3, "pnl_usdc": -0.3},
    }


def test_runs_on_different_code_are_not_called_comparable(tmp_path) -> None:
    conn = _book(tmp_path / "l.db")
    table = arms.compare(conn, ["run_base", "run_other"])
    assert table["comparable"] is False
    assert table["same"]["code_head"] is False
    with pytest.raises(ValueError, match="run_not_found"):
        arms.compare(conn, ["run_missing"])

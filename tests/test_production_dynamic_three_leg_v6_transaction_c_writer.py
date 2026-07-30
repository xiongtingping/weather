import sqlite3

import pytest

from dynamic_three_leg_v6_schema_contract import initialize_dynamic_three_leg_v6_schema
from production_dynamic_three_leg_v6_transaction_c_writer import (
    commit_target_realization_v6_transaction_c,
)
from target_realization_v6_runtime import build_target_realization_v6_plan


NOW = "2026-07-16T08:00:00Z"
ARTIFACT = "a" * 64
POLICY = "b" * 64


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE posterior_commit_outbox(
            outbox_event_id TEXT PRIMARY KEY,posterior_run_id TEXT,fact_snapshot_id TEXT,
            correlation_id TEXT,state TEXT,claimed_at TEXT,completed_at TEXT,attempt_count INTEGER,
            claim_generation INTEGER,lease_owner TEXT,lease_until TEXT,completed_decision_id TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE auto_trade_decisions(
            id INTEGER PRIMARY KEY,run_id TEXT,run_date TEXT,slug TEXT,event_date TEXT,outcome TEXT,
            side TEXT,action TEXT,decided_state TEXT,will_order INTEGER,reason_codes_json TEXT,
            reasons_json TEXT,details_json TEXT,created_at TEXT,correlation_id TEXT,fact_snapshot_id TEXT,
            posterior_run_id TEXT,decision_id TEXT UNIQUE,order_intent_id TEXT
        )"""
    )
    conn.execute("CREATE TABLE posterior_execution_intents(order_intent_id TEXT PRIMARY KEY)")
    initialize_dynamic_three_leg_v6_schema(conn)
    conn.execute(
        "INSERT INTO posterior_commit_outbox VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("o1", "p1", "f1", "c1", "claimed", NOW, None, 1, 7, "worker", NOW, None),
    )
    return conn


def _plan():
    return build_target_realization_v6_plan(
        posterior_run_id="p1",
        realization_state_id="r1",
        canonical_observation_event_id="e1",
        posterior_buckets=[
            {"bucket": "35C", "ordinal": 35, "probability": 0.55},
            {"bucket": "36C", "ordinal": 36, "probability": 0.30},
            {"bucket": "34C", "ordinal": 34, "probability": 0.15},
        ],
        peak_timing_state={"entry_peak_at": "2026-07-16T09:00:00Z"},
        future_warming_or_rebound_risk=True,
        second_yes_entry_allowed=True,
        position_exposure_snapshot={"complete": True, "exposures": []},
        coverage_snapshot={"complete": True, "all_conflicting_sell_and_cancel_reconciled": True},
        prior_main_yes_cycle=None,
        available_capital_usdc=0.0,
        capital_snapshot_id="cap1",
        risk_snapshot_id="risk1",
        capital_epoch_id="epoch1",
        strategy_artifact_sha256=ARTIFACT,
        decision_as_of=NOW,
    )


def _activation():
    return {
        "schema": "target_realization_v6_production_activation_v1",
        "state": "enabled",
        "strategy_artifact_sha256": ARTIFACT,
        "exact_exit_reason_policy_sha256": POLICY,
        "cutover_artifact_sha256": "c" * 64,
        "live_decision_write_authorized": True,
        "live_submit_authorized": True,
    }


def _decision():
    return {
        "decision_id": "d1",
        "run_date": "2026-07-16",
        "slug": "zuuu",
        "event_date": "2026-07-16",
        "outcome": "portfolio",
        "side": "",
        "action": "hold",
        "decided_state": "zero_order_drill",
        "will_order": False,
        "reason_codes": ["v6_zero_order_drill"],
        "reasons": [{"reason_code": "v6_zero_order_drill"}],
        "details": {"places_orders": False},
    }


def _commit(conn, **overrides):
    args = dict(
        outbox_event_id="o1",
        lease_owner="worker",
        claim_generation=7,
        plan=_plan(),
        decisions=[_decision()],
        intents=[],
        production_activation=_activation(),
        exact_exit_reason_policy_sha256=POLICY,
        committed_at=NOW,
    )
    args.update(overrides)
    return commit_target_realization_v6_transaction_c(conn, **args)


def test_v6_transaction_c_commits_policy_state_and_outbox_atomically_without_orders():
    conn = _conn()
    result = _commit(conn)
    assert result["ok"] is True
    assert result["intent_count"] == 0
    assert conn.execute("SELECT state FROM posterior_commit_outbox WHERE outbox_event_id='o1'").fetchone()[0] == "completed"
    assert conn.execute("SELECT COUNT(*) FROM target_realization_v6_plans").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM dynamic_three_leg_v6_main_yes_cycles").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM dynamic_three_leg_v6_capital_allocations").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM dynamic_three_leg_v6_risk_reservations").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0] == 1


@pytest.mark.parametrize("fault_after", ["policy_state", "decisions", "intents", "outbox"])
def test_v6_transaction_c_fault_injection_rolls_back_every_row(fault_after):
    conn = _conn()
    with pytest.raises(RuntimeError):
        _commit(conn, fault_after=fault_after)
    for table in (
        "target_realization_v6_plans",
        "dynamic_three_leg_v6_main_yes_cycles",
        "dynamic_three_leg_v6_capital_allocations",
        "dynamic_three_leg_v6_risk_reservations",
        "dynamic_three_leg_v6_transaction_c_commits",
        "auto_trade_decisions",
    ):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert conn.execute("SELECT state FROM posterior_commit_outbox WHERE outbox_event_id='o1'").fetchone()[0] == "claimed"


def test_v6_transaction_c_rejects_old_or_prefix_only_exit_codes_before_write():
    conn = _conn()
    bad_intent = {
        "order_intent_id": "i1",
        "posterior_run_id": "p1",
        "order_action": "SELL",
        "reason_family": "market_alpha_take_profit",
        "reason_code": "market_alpha_take_profit_top_yes_wave_completed",
    }
    result = _commit(conn, intents=[bad_intent])
    assert result["ok"] is False
    assert "v6_transaction_c_exit_reason_outside_exact_closed_set" in result["block_reasons"]
    assert conn.execute("SELECT COUNT(*) FROM target_realization_v6_plans").fetchone()[0] == 0

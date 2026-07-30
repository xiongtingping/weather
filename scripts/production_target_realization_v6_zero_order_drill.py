#!/usr/bin/env python3
"""Isolated no-CLOB drill for Target Realization v6 atomic policy state."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dynamic_three_leg_v6_schema_contract import initialize_dynamic_three_leg_v6_schema  # noqa: E402
from production_dynamic_three_leg_v6_transaction_c_writer import commit_target_realization_v6_transaction_c  # noqa: E402
from sequential_bayes_schema_contract import TABLE_DDL  # noqa: E402
from target_realization_v6_runtime import build_target_realization_v6_plan  # noqa: E402


NOW = "2026-07-16T08:00:00Z"
ARTIFACT = "a" * 64
POLICY = "b" * 64


def _connection() -> sqlite3.Connection:
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
    conn.execute(TABLE_DDL["posterior_execution_intents"])
    initialize_dynamic_three_leg_v6_schema(conn)
    conn.execute(
        "INSERT INTO posterior_commit_outbox VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("o1", "p1", "f1", "corr1", "claimed", NOW, None, 1, 1, "drill", NOW, None),
    )
    return conn


def _plan() -> dict:
    return build_target_realization_v6_plan(
        posterior_run_id="p1",
        realization_state_id="r1",
        canonical_observation_event_id="event1",
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
        capital_snapshot_id="capital1",
        risk_snapshot_id="risk1",
        capital_epoch_id="epoch1",
        strategy_artifact_sha256=ARTIFACT,
        decision_as_of=NOW,
    )


def _args() -> dict:
    return {
        "outbox_event_id": "o1",
        "lease_owner": "drill",
        "claim_generation": 1,
        "plan": _plan(),
        "decisions": [{
            "decision_id": "drill-decision-1",
            "run_date": "2026-07-16",
            "slug": "zuuu",
            "event_date": "2026-07-16",
            "outcome": "portfolio",
            "side": "",
            "action": "hold",
            "decided_state": "v6_zero_order_drill",
            "will_order": False,
            "reason_codes": ["v6_zero_order_drill"],
            "reasons": [{"reason_code": "v6_zero_order_drill"}],
            "details": {"places_orders": False},
        }],
        "intents": [],
        "production_activation": {
            "schema": "target_realization_v6_production_activation_v1",
            "state": "enabled",
            "strategy_artifact_sha256": ARTIFACT,
            "exact_exit_reason_policy_sha256": POLICY,
            "cutover_artifact_sha256": "c" * 64,
            "live_decision_write_authorized": True,
            "live_submit_authorized": True,
        },
        "exact_exit_reason_policy_sha256": POLICY,
        "committed_at": NOW,
    }


def main() -> int:
    fault_results = []
    for fault in ("policy_state", "decisions", "intents", "outbox"):
        conn = _connection()
        rolled_back = False
        try:
            commit_target_realization_v6_transaction_c(conn, **_args(), fault_after=fault)
        except RuntimeError:
            rolled_back = all(
                conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
                for table in (
                    "target_realization_v6_plans",
                    "dynamic_three_leg_v6_main_yes_cycles",
                    "dynamic_three_leg_v6_capital_allocations",
                    "dynamic_three_leg_v6_risk_reservations",
                    "dynamic_three_leg_v6_transaction_c_commits",
                    "auto_trade_decisions",
                    "posterior_execution_intents",
                )
            ) and conn.execute("SELECT state FROM posterior_commit_outbox").fetchone()[0] == "claimed"
        fault_results.append({"fault_after": fault, "rolled_back": rolled_back})
        conn.close()

    conn = _connection()
    committed = commit_target_realization_v6_transaction_c(conn, **_args())
    counts = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "target_realization_v6_plans",
            "dynamic_three_leg_v6_main_yes_cycles",
            "dynamic_three_leg_v6_capital_allocations",
            "dynamic_three_leg_v6_risk_reservations",
            "dynamic_three_leg_v6_transaction_c_commits",
            "auto_trade_decisions",
            "posterior_execution_intents",
        )
    }
    outbox_state = conn.execute("SELECT state FROM posterior_commit_outbox").fetchone()[0]
    conn.close()
    ok = bool(
        committed.get("ok") is True
        and committed.get("intent_count") == 0
        and counts["posterior_execution_intents"] == 0
        and outbox_state == "completed"
        and all(row["rolled_back"] for row in fault_results)
    )
    result = {
        "schema": "production_target_realization_v6_zero_order_drill_v1",
        "ok": ok,
        "state": "passed" if ok else "failed",
        "transaction_c": committed,
        "fault_injection": fault_results,
        "row_counts": counts,
        "outbox_state": outbox_state,
        "uses_archive_db": False,
        "external_disk_dependency": False,
        "network_calls": 0,
        "clob_calls": 0,
        "places_orders": False,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())

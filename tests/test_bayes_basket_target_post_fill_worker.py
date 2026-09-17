from __future__ import annotations

import json
import sqlite3

import pytest

import bayes_basket_target.execution_transition_runtime as transition_runtime
import bayes_basket_target.production_worker as worker_module
from bayes_basket_target.execution_transition_runtime import scope_sha256
from bayes_basket_target.execution_version_vector import (
    build_execution_version_vector,
)
from bayes_basket_target.production_worker import TargetProductionWorker


NOW = "2026-07-27T06:30:00Z"
SCOPE = {
    "station_id": "RKPK",
    "event_date": "2026-07-27",
    "settlement_timezone": "Asia/Seoul",
    "bucket": "38",
    "token_id": "token-38",
    "side": "YES",
    "order_action": "BUY",
}
VECTOR = build_execution_version_vector({
    "schema": "bayes_basket_target_execution_version_vector_v2",
    "probability_sha256": "1" * 64,
    "official_ws_quote_sha256": "2" * 64,
    "inventory_sha256": "3" * 64,
    "order_state_sha256": "4" * 64,
    "station_policy_sha256": "5" * 64,
    "cash_policy_decision_sha256": "6" * 64,
    "economic_policy_schema": "delta_basket_score_policy_artifact_v2",
    "economic_policy_artifact_sha256": "7" * 64,
    "target_price_id": "target-price-38",
    "quote_observed_at": "2026-07-27T06:29:00Z",
    "quote_expires_at": "2026-07-27T06:31:00Z",
}).to_dict()
REQUEST = {
    "schema": "bayes_basket_target_post_fill_recompute_request_v1",
    "request_id": "recompute_test",
    "order_intent_id": "intent_test",
    "cycle_id": "cycle_test",
    "receipt_id": "receipt_test",
    "probability_sha256": VECTOR["probability_sha256"],
    "official_ws_quote_sha256": VECTOR["official_ws_quote_sha256"],
    "station_policy_sha256": VECTOR["station_policy_sha256"],
    "cash_policy_decision_sha256": VECTOR[
        "cash_policy_decision_sha256"
    ],
    "economic_policy_schema": VECTOR["economic_policy_schema"],
    "economic_policy_artifact_sha256": VECTOR[
        "economic_policy_artifact_sha256"
    ],
    "source_execution_version_vector": VECTOR,
    "source_execution_version_vector_sha256": VECTOR["vector_sha256"],
    "source_scope_sha256": scope_sha256(SCOPE),
    "source_transition_sha256": "f" * 64,
    "inventory_version": "invrec_test",
    "order_state_version": "ordrec_test",
    "quote_sequence": 7,
    "scope": SCOPE,
    "state": "PENDING",
}


def _worker() -> TargetProductionWorker:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_target_execution_transition_versions("
        "order_intent_id TEXT PRIMARY KEY,state TEXT,"
        "inventory_version TEXT,order_state_version TEXT,"
        "evidence_json TEXT,content_sha256 TEXT,"
        "filled_size REAL,filled_notional_usdc REAL)"
    )
    conn.execute(
        "INSERT INTO basket_target_execution_transition_versions "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            "intent_test",
            "FULL_FILLED",
            "invrec_test",
            "ordrec_test",
            json.dumps({
                "version_vector": VECTOR,
                "quote_sequence": 7,
                "scope_sha256": scope_sha256(SCOPE),
            }),
            "f" * 64,
            1.0,
            0.4,
        ),
    )
    conn.execute(
        "CREATE TABLE basket_confirmed_inventory_snapshot("
        "content_sha256 TEXT,inventory_json TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_order_state_snapshot("
        "content_sha256 TEXT,order_state_json TEXT)"
    )
    conn.execute(
        "INSERT INTO basket_confirmed_inventory_snapshot VALUES(?,?)",
        (
            "8" * 64,
            json.dumps({
                "filled_by_bucket": {"38": 1.0},
                "filled_cost_by_bucket": {"38": 0.4},
                "transition_content_sha256": ["f" * 64],
            }),
        ),
    )
    conn.execute(
        "INSERT INTO basket_order_state_snapshot VALUES(?,?)",
        (
            "9" * 64,
            json.dumps({
                "pending_by_bucket": {"38": 0.0},
                "pending_notional_by_bucket": {"38": 0.0},
                "transition_content_sha256": ["f" * 64],
            }),
        ),
    )
    worker = object.__new__(TargetProductionWorker)
    worker.conn = conn
    worker.commit_conn = conn
    return worker


@pytest.mark.parametrize(
    ("transition_state", "filled_size", "filled_notional"),
    [
        ("FULL_FILLED", 1.0, 0.4),
        ("PARTIAL_FILLED", 0.25, 0.1),
    ],
)
def test_authoritative_fill_request_recompiles_and_completes_atomically(
    monkeypatch: pytest.MonkeyPatch,
    transition_state: str,
    filled_size: float,
    filled_notional: float,
) -> None:
    worker = _worker()
    worker.commit_conn.execute(
        "UPDATE basket_target_execution_transition_versions "
        "SET state=?,filled_size=?,filled_notional_usdc=? "
        "WHERE order_intent_id='intent_test'",
        (transition_state, filled_size, filled_notional),
    )
    worker.commit_conn.execute(
        "UPDATE basket_confirmed_inventory_snapshot SET inventory_json=?",
        (json.dumps({
            "filled_by_bucket": {"38": filled_size},
            "filled_cost_by_bucket": {"38": filled_notional},
            "transition_content_sha256": ["f" * 64],
        }),),
    )
    monkeypatch.setattr(
        worker_module,
        "load_post_fill_recompute_requests",
        lambda _conn: [REQUEST],
    )
    monkeypatch.setattr(
        TargetProductionWorker,
        "_event_day_open",
        staticmethod(lambda *_args, **_kwargs: True),
    )
    # Charter 125 (c52267cc): every recompile reads the day's observed running
    # high, through the market registry this fixture's ledger does not carry.
    # The recompile's atomicity is the subject here, so the day has no high yet.
    monkeypatch.setattr(
        TargetProductionWorker,
        "_observed_running_high_c",
        lambda *_args, **_kwargs: None,
    )
    compile_calls: list[dict] = []

    def compile_cycle(_conn: sqlite3.Connection, **kwargs: object) -> dict:
        compile_calls.append(dict(kwargs))
        return {
            "status": "ready_no_live",
            "ok": True,
            "production_cycle_sha256": "a" * 64,
            "production_market_snapshot": {
                "inventory_sha256": "8" * 64,
                "order_state_sha256": "9" * 64,
            },
        }

    monkeypatch.setattr(
        worker_module,
        "compile_production_cycle_from_ssot",
        compile_cycle,
    )
    monkeypatch.setattr(
        TargetProductionWorker,
        "_commit_candidate",
        lambda *_args, **_kwargs: {
            "state": "committed",
            "writes_target_ledger": True,
            "calls_order_gateway": False,
        },
    )
    completions: list[dict] = []

    def complete(_conn: sqlite3.Connection, **kwargs: object) -> dict:
        completions.append(dict(kwargs))
        return {"state": "APPLIED", "idempotent": False}

    monkeypatch.setattr(
        transition_runtime,
        "complete_post_fill_recompute_request",
        complete,
        raising=False,
    )
    result = worker._consume_post_fill_recompute_requests(
        activation={"policy_authority_scope": "PRODUCTION_NO_LIVE_ONLY"},
        scope_config={
            "season_bin": "summer",
            "maturity_bin": "d1",
            "liquidity_bin": "liquid",
            "strategy_version": "v1",
        },
        as_of_utc=NOW,
    )
    assert result["state"] == "COMMITTED"
    assert result["request_count"] == 1
    assert result["applied_count"] == 1
    assert compile_calls[0]["station_id"] == "RKPK"
    assert completions[0]["request_id"] == "recompute_test"
    assert completions[0]["result"]["production_cycle_sha256"] == "a" * 64
    assert completions[0]["result"]["post_fill_inventory_sha256"] == "8" * 64
    assert completions[0]["result"]["post_fill_order_state_sha256"] == "9" * 64
    assert completions[0]["result"]["post_fill_transition_sha256"] == "f" * 64
    assert result["calls_order_gateway"] is False
    assert result["orders_submitted"] == 0


@pytest.mark.parametrize(
    ("state", "inventory_version", "expected_reason"),
    [
        (
            "RECONCILE_REQUIRED",
            "invrec_test",
            "transition_state_not_recomputable",
        ),
        ("FULL_FILLED", "invrec_drift", "transition_version_drift"),
    ],
)
def test_post_fill_request_state_or_version_drift_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    inventory_version: str,
    expected_reason: str,
) -> None:
    worker = _worker()
    worker.commit_conn.execute(
        "UPDATE basket_target_execution_transition_versions "
        "SET state=?,inventory_version=? WHERE order_intent_id='intent_test'",
        (state, inventory_version),
    )
    monkeypatch.setattr(
        worker_module,
        "load_post_fill_recompute_requests",
        lambda _conn: [REQUEST],
    )
    monkeypatch.setattr(
        TargetProductionWorker,
        "_event_day_open",
        staticmethod(lambda *_args, **_kwargs: True),
    )
    result = worker._consume_post_fill_recompute_requests(
        activation={"policy_authority_scope": "PRODUCTION_NO_LIVE_ONLY"},
        scope_config={
            "season_bin": "summer",
            "maturity_bin": "d1",
            "liquidity_bin": "liquid",
            "strategy_version": "v1",
        },
        as_of_utc=NOW,
    )
    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert result["applied_count"] == 0
    assert expected_reason in result["blockers"][0]["reason"]

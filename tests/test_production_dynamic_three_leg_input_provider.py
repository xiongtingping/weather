from __future__ import annotations

import hashlib
import json
import sqlite3

from dynamic_three_leg_position_lot_schema_contract import apply_dynamic_three_leg_position_lot_schema
from dynamic_three_leg_production_activation import materialize_dynamic_three_leg_activation
from market_alpha_take_profit_schema_contract import apply_market_alpha_take_profit_schema
from production_dynamic_three_leg_input_provider import (
    _actionable_bucket_labels,
    freeze_production_dynamic_three_leg_inputs,
)
from sequential_bayes_schema_contract import apply_forward_schema


NOW = "2026-07-14T08:00:00.500Z"
SLUG = "highest-temperature-in-chengdu-on-july-14-2026"


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    apply_dynamic_three_leg_position_lot_schema(conn, writer_quiescent=True)
    apply_market_alpha_take_profit_schema(conn, writer_quiescent=True)
    conn.executescript(
        """
        CREATE TABLE event_fact_history(
            id INTEGER PRIMARY KEY,slug TEXT,event_date TEXT,fact_json TEXT,
            posterior_run_id TEXT,fact_snapshot_id TEXT
        );
        CREATE TABLE event_fact_latest(slug TEXT,event_date TEXT,fact_id INTEGER);
        CREATE TABLE market_tokens(
            slug TEXT,event_date TEXT,outcome TEXT,yes_token TEXT,no_token TEXT,
            market_active INTEGER,market_closed INTEGER,market_archived INTEGER,
            market_accepting_orders INTEGER,market_enable_orderbook INTEGER
        );
        """
    )
    projection = {
        "ok": True,
        "market_bucket_probabilities": {"36°C": 0.15, "37°C": 0.60, "38°C": 0.25},
        "strong_physical_evidence": {
            "joint_feature_model_applied": True,
            "top_bucket": "37°C",
            "top_bucket_joint_strong_support": True,
            "bucket_exclusions": [],
        },
    }
    state = {
        "schema": "realization_state_l3_v2",
        "posterior_run_id": "post-current",
        "projection": projection,
    }
    state_sha = hashlib.sha256(_canonical(state).encode()).hexdigest()
    conn.execute(
        """INSERT INTO realization_states(
           realization_state_id,posterior_run_id,fact_snapshot_id,resolution_contract_id,
           target_bucket,second_target_bucket,credible_bucket_set_json,current_report_bucket,
           current_report_bucket_probability,probability_above_current_report_bucket,
           current_report_bucket_hard_zero,settlement_exclusion_certificates_json,state_json,
           state_sha256,created_at,committed_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "realization-current", "post-current", "fact-current", "resolution-current",
            "37°C", "38°C", '["37°C","38°C"]', "36°C", 0.15, 0.85, 0, "{}",
            _canonical(state), state_sha, NOW, NOW,
        ),
    )
    fact = {"posterior_ssot_fact": {
        "posterior_run_id": "post-current",
        "fact_snapshot_id": "fact-current",
        "realization_state_id": "realization-current",
    }}
    conn.execute(
        "INSERT INTO event_fact_history VALUES(1,?,?,?,?,?)",
        (SLUG, "2026-07-14", _canonical(fact), "post-current", "fact-current"),
    )
    conn.execute("INSERT INTO event_fact_latest VALUES(?,?,1)", (SLUG, "2026-07-14"))
    for bucket in ("36°C", "37°C", "38°C"):
        number = bucket[:2]
        conn.execute(
            "INSERT INTO market_tokens VALUES(?,?,?,?,?,1,0,0,1,1)",
            (SLUG, "2026-07-14", bucket, f"yes-{number}", f"no-{number}"),
        )
    conn.commit()
    return conn


def _activation() -> dict:
    return materialize_dynamic_three_leg_activation({
        "schema": "dynamic_three_leg_production_activation_v1",
        "state": "enabled",
        "strategy_artifact_sha256": "strategy-current",
        "cutover_artifact_sha256": "cutover-current",
        "risk_reservation_write_authorized": True,
        "live_decision_write_authorized": True,
        "live_submit_authorized": True,
    })


def _exchange(request) -> dict:
    books = {}
    for row in request["buckets"]:
        for token in (row["yes_token_id"], row["no_token_id"]):
            books[token] = {
                "asset_id": token,
                "timestamp": "1784016000550",
                "bids": [{"price": "0.39", "size": "100"}],
                "asks": [{"price": "0.40", "size": "100"}],
            }
    return {
        "schema": "clob_dynamic_three_leg_snapshot_v1",
        "ok": True,
        "evidence_only": True,
        "authorizes_order": False,
        "request_id": request["request_id"],
        "observed_at": "2026-07-14T08:00:00.550Z",
        "amount_decimals": 6,
        "order_books_by_token": books,
        "conditional_balance_allowance_by_token": {},
        "collateral_balance_allowance": {"balance": "100000000", "allowance": "100000000"},
    }


def test_freezes_one_exchange_backed_bundle_before_claim_without_ev_or_legacy_positions() -> None:
    requests = []
    result = freeze_production_dynamic_three_leg_inputs(
        _conn(),
        outbox={
            "slug": SLUG,
            "event_date": "2026-07-14",
            "posterior_run_id": "post-current",
            "fact_snapshot_id": "fact-current",
        },
        preliminary_as_of=NOW,
        exchange_snapshot_fetcher=lambda request: requests.append(request) or _exchange(request),
        production_activation=_activation(),
    )

    assert result["ok"] is True
    assert result["decision_as_of"] == "2026-07-14T08:00:00.550Z"
    assert result["desired_trading_plan"]["candidates"][0]["bucket"] == "37°C"
    assert result["desired_trading_plan"]["candidates"][0]["notional"] == 20.0
    assert result["capital_snapshot"]["capital_policy_id"] == "dynamic_three_leg_capital_policy_v2_top20_second4_99_excluded_no100"
    assert result["position_snapshot"]["legacy_edge_trades_consumed"] is False
    assert result["exchange_snapshot"]["ev_edge_lcb_consumed"] is False
    assert [row["bucket"] for row in requests[0]["buckets"]] == ["37°C", "38°C"]


def test_actionable_quotes_include_certified_exclusion_and_managed_position_only() -> None:
    realization = {
        "target_bucket": "37°C",
        "second_target_bucket": "38°C",
        "strong_physical_evidence": {"bucket_exclusions": [
            {
                "bucket": "35°C", "excluded": True,
                "full_support_exclusion_verified": True,
                "settlement_exclusion_certificate_state": "valid",
                "settlement_exclusion_certificate": {
                    "state": "valid", "full_support_exclusion_verified": True,
                },
            },
            {"bucket": "34°C", "excluded": True, "full_support_exclusion_verified": False},
        ]},
    }
    positions = {"positions": [
        {"bucket": "36°C", "position_size": 2.0, "management_scope": "dynamic_three_leg"},
        {"bucket": "40°C", "position_size": 3.0, "management_scope": "external_unmanaged"},
    ]}
    assert _actionable_bucket_labels(realization, positions) == ["35°C", "36°C", "37°C", "38°C"]


def test_missing_market_token_fails_before_exchange_call() -> None:
    conn = _conn()
    conn.execute("DELETE FROM market_tokens WHERE outcome='38°C'")
    calls = []
    result = freeze_production_dynamic_three_leg_inputs(
        conn,
        outbox={"slug": SLUG, "event_date": "2026-07-14", "posterior_run_id": "post-current", "fact_snapshot_id": "fact-current"},
        preliminary_as_of=NOW,
        exchange_snapshot_fetcher=lambda request: calls.append(request) or {},
        production_activation=_activation(),
    )
    assert result["ok"] is False
    assert "production_input_market_bucket_token_missing" in result["block_reasons"]
    assert calls == []

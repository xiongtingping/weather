from __future__ import annotations

import json
import sqlite3

import pytest

from dynamic_three_leg_transition_runtime import MANAGEMENT_SCOPE, build_dynamic_three_leg_transition_plan
from production_dynamic_three_leg_decision_writer import (
    ACTIVATION_SCHEMA,
    commit_production_dynamic_three_leg_decision_bundle,
)
from production_dynamic_three_leg_cycle_runner import run_production_dynamic_three_leg_cycle
from sequential_bayes_schema_contract import apply_forward_schema
from dynamic_three_leg_position_lot_schema_contract import apply_dynamic_three_leg_position_lot_schema
from dynamic_three_leg_production_activation import materialize_dynamic_three_leg_activation
from dynamic_three_leg_continuation_schema_contract import apply_continuation_schema
from resident_dynamic_three_leg_intent_submitter import (
    reconcile_dynamic_three_leg_intent,
    submit_prepared_dynamic_three_leg_intent,
    SUPERSESSION_REASON,
    supersede_stale_prepared_dynamic_three_leg_intents,
)
from resident_dynamic_three_leg_execution_loop import run_resident_dynamic_three_leg_execution_once
from production_dynamic_three_leg_continuation_worker import process_dynamic_three_leg_continuation_once


NOW = "2026-07-13T00:00:00.500Z"
SLUG = "highest-temperature-in-chengdu-on-july-13-2026"


def _conn(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    apply_forward_schema(conn)
    apply_dynamic_three_leg_position_lot_schema(conn, writer_quiescent=True)
    apply_continuation_schema(conn, writer_quiescent=True)
    conn.executescript(
        """
        CREATE TABLE event_fact_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT, event_date TEXT,
            fact_snapshot_id TEXT, posterior_run_id TEXT
        );
        CREATE TABLE event_fact_latest(slug TEXT,event_date TEXT,fact_id INTEGER);
        CREATE TABLE auto_trade_decisions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT,run_date TEXT,slug TEXT,event_date TEXT,
            outcome TEXT,side TEXT,action TEXT,decided_state TEXT,will_order INTEGER,
            reason_codes_json TEXT,reasons_json TEXT,details_json TEXT,created_at TEXT,
            correlation_id TEXT,fact_snapshot_id TEXT,posterior_run_id TEXT,
            decision_id TEXT UNIQUE,order_intent_id TEXT UNIQUE
        );
        INSERT INTO event_fact_history(slug,event_date,fact_snapshot_id,posterior_run_id)
        VALUES('highest-temperature-in-chengdu-on-july-13-2026','2026-07-13','facts-current','post-current');
        INSERT INTO event_fact_latest VALUES('highest-temperature-in-chengdu-on-july-13-2026','2026-07-13',1);
        INSERT INTO posterior_commit_outbox(
            outbox_event_id,posterior_run_id,fact_snapshot_id,correlation_id,state,created_at,
            claimed_at,attempt_count,claim_generation,lease_owner,lease_until
        ) VALUES('outbox-current','post-current','facts-current','corr-current','claimed',
                 '2026-07-13T00:00:00.100Z','2026-07-13T00:00:00.200Z',0,1,'resident-live',
                 '2026-07-13T00:00:02.000Z');
        INSERT INTO posterior_outbox_execution_contracts(
            outbox_event_id,posterior_run_id,canonical_observation_event_id,
            first_actionable_seen_at,observation_to_order_hard_deadline_at,
            inference_decision_deadline_at,transaction_b_committed_at,
            execution_eligible_at_commit,execution_na_reason,deadline_policy,created_at
        ) VALUES('outbox-current','post-current','canobs-current','2026-07-13T00:00:00.000Z',
                 '2026-07-13T00:00:03.000Z','2026-07-13T00:00:03.000Z',
                 '2026-07-13T00:00:00.100Z',1,'','canonical_first_actionable_plus_3000ms',
                 '2026-07-13T00:00:00.100Z');
        INSERT INTO dynamic_three_leg_risk_reservations(
            risk_reservation_id,trading_plan_id,posterior_run_id,capital_snapshot_id,
            risk_snapshot_id,capital_epoch_id,reserved_buy_notional,state,
            strategy_artifact_sha256,available_cash_usdc,risk_limit_usdc,
            bound_transition_plan_id,expires_at,reservation_json,reservation_sha256,
            created_at,updated_at
        ) VALUES(
            'risk-reservation-current','trplan-current','post-current','capital-current',
            'risk-current','capital-epoch-current',30.0,'active',
            'strategy-current',100.0,30.0,NULL,
            '2026-07-13T00:00:03.000Z','{}','reservation-sha-current',
            '2026-07-13T00:00:00.100Z','2026-07-13T00:00:00.100Z'
        );
        """
    )
    return conn


def _desired() -> dict:
    return {
        "schema": "dynamic_three_leg_trading_plan_v1",
        "ok": True,
        "trading_plan_id": "trplan-current",
        "posterior_run_id": "post-current",
        "realization_state_id": "rstate-current",
        "fact_snapshot_id": "facts-current",
        "strategy_artifact_sha256": "strategy-current",
        "market_snapshot_id": "market-current",
        "capital_snapshot_id": "capital-current",
        "risk_snapshot_id": "risk-current",
        "capital_epoch_id": "capital-epoch-current",
        "risk_reservation_id": "risk-reservation-current",
        "second_yes_entry_window_state_id": "second-window-open",
        "second_yes_entry_window_state": {
            "schema": "second_yes_entry_window_state_v1",
            "second_yes_entry_window_state_id": "second-window-open",
            "entry_allowed": True,
            "entry_block_reason": "",
            "entry_window_start_at": "2026-07-12T23:30:00.000Z",
            "entry_window_end_at": "2026-07-13T00:02:00.000Z",
            "predicted_peak_at": "2026-07-13T00:02:00.000Z",
        },
        "candidates": [
            {"candidate_id": "top", "strategy_leg": "top_yes_wave", "bucket": "33C", "side": "YES", "notional": 20.0},
            {"candidate_id": "second", "strategy_leg": "second_yes", "bucket": "34C", "side": "YES", "notional": 4.99},
            {"candidate_id": "no", "strategy_leg": "strongly_excluded_no", "bucket": "31C", "side": "NO", "notional": 5.0},
        ],
    }


def _desired_with_closed_second_window() -> dict:
    desired = _desired()
    second = next(
        row for row in desired["candidates"] if row["strategy_leg"] == "second_yes"
    )
    desired["candidates"] = [
        row for row in desired["candidates"] if row["strategy_leg"] != "second_yes"
    ]
    desired["retention_candidates"] = [
        {
            **second,
            "entry_allowed": False,
            "entry_block_reason": "second_yes_before_entry_window",
        }
    ]
    desired["second_yes_entry_window_state_id"] = "second-window-closed"
    desired["second_yes_entry_window_state"] = {
        "schema": "second_yes_entry_window_state_v1",
        "second_yes_entry_window_state_id": "second-window-closed",
        "entry_allowed": False,
        "entry_block_reason": "second_yes_before_entry_window",
        "predicted_peak_at": "2026-07-13T00:31:00.000Z",
        "entry_window_opens_at": "2026-07-13T00:01:00.000Z",
        "entry_window_closes_at": "2026-07-13T00:31:00.000Z",
    }
    return desired


def _positions(rows=None) -> dict:
    return {"position_snapshot_id": "positions-current", "as_of": "2026-07-13T00:00:00.000Z", "complete": True, "positions": rows or [], "unresolved_intents": []}


def _position(bucket: str, side: str, *, notional: float, size: float, cost: float) -> dict:
    leg = "strongly_excluded_no" if side == "NO" else "second_yes" if bucket == "34C" else "top_yes_wave"
    return {"management_scope": MANAGEMENT_SCOPE, "position_lot_id": f"lot-{bucket}-{side}", "position_lot_version": "3", "bucket": bucket, "side": side, "strategy_leg": leg, "token_id": f"token-{bucket}-{side.lower()}", "position_size": size, "entry_notional_usdc": notional, "average_cost": cost}


def _market() -> dict:
    return {
        "market_snapshot_id": "market-current", "as_of": "2026-07-13T00:00:00.300Z", "expires_at": "2026-07-13T00:00:01.000Z",
        "buckets": {
            "31C": {"yes_token_id": "token-31C-yes", "no_token_id": "token-31C-no", "yes_bid": 0.01, "yes_ask": 0.02, "no_bid": 0.97, "no_ask": 0.98},
            "32C": {"yes_token_id": "token-32C-yes", "no_token_id": "token-32C-no", "yes_bid": 0.20, "yes_ask": 0.21, "no_bid": 0.78, "no_ask": 0.79},
            "33C": {"yes_token_id": "token-33C-yes", "no_token_id": "token-33C-no", "yes_bid": 0.49, "yes_ask": 0.50, "no_bid": 0.49, "no_ask": 0.50},
            "34C": {"yes_token_id": "token-34C-yes", "no_token_id": "token-34C-no", "yes_bid": 0.19, "yes_ask": 0.20, "no_bid": 0.79, "no_ask": 0.80},
        },
    }


def _activation() -> dict:
    return materialize_dynamic_three_leg_activation({"schema": ACTIVATION_SCHEMA, "state": "enabled", "strategy_artifact_sha256": "strategy-current", "cutover_artifact_sha256": "cutover-current", "live_decision_write_authorized": True, "live_submit_authorized": True, "risk_reservation_write_authorized": True})


def _commit(conn: sqlite3.Connection, transition: dict, **overrides) -> dict:
    desired = overrides.pop("desired_trading_plan", _desired())
    return commit_production_dynamic_three_leg_decision_bundle(
        conn, outbox_event_id="outbox-current", lease_owner="resident-live", claim_generation=1,
        slug=SLUG, event_date="2026-07-13", desired_trading_plan=desired,
        transition_plan=transition, market_snapshot=_market(), production_activation=_activation(),
        decision_as_of=NOW, **overrides,
    )


def test_default_disabled_activation_writes_nothing(monkeypatch) -> None:
    conn = _conn()
    transition = build_dynamic_three_leg_transition_plan(desired_trading_plan=_desired(), position_snapshot=_positions(), decision_as_of=NOW)
    monkeypatch.delenv("PM_DYNAMIC_THREE_LEG_PRODUCTION_ENABLED", raising=False)
    monkeypatch.delenv("PM_DYNAMIC_THREE_LEG_LIVE_SUBMIT_ENABLED", raising=False)
    result = _commit(conn, transition)
    assert result["ok"] is False
    assert result["wrote_rows"] is False
    assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0] == 0
    assert conn.execute("SELECT state FROM posterior_commit_outbox").fetchone()[0] == "claimed"


def test_buy_bundle_is_atomic_durable_and_idempotent(monkeypatch) -> None:
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_PRODUCTION_ENABLED", "1")
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_LIVE_SUBMIT_ENABLED", "1")
    conn = _conn()
    transition = build_dynamic_three_leg_transition_plan(desired_trading_plan=_desired(), position_snapshot=_positions(), decision_as_of=NOW)
    result = _commit(conn, transition)
    assert result["ok"] is True
    assert result["places_orders"] is False
    assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions WHERE will_order=1").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM posterior_execution_intents WHERE state='prepared' AND live_submit_authorized=1").fetchone()[0] == 3
    assert conn.execute("SELECT state FROM posterior_commit_outbox").fetchone()[0] == "completed"
    retry = _commit(conn, transition)
    assert retry["idempotent"] is True
    assert conn.execute("SELECT COUNT(*) FROM posterior_execution_intents").fetchone()[0] == 3


def test_closed_second_yes_window_records_authoritative_no_order_decision(monkeypatch) -> None:
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_PRODUCTION_ENABLED", "1")
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_LIVE_SUBMIT_ENABLED", "1")
    conn = _conn()
    desired = _desired_with_closed_second_window()
    transition = build_dynamic_three_leg_transition_plan(
        desired_trading_plan=desired,
        position_snapshot=_positions(),
        decision_as_of=NOW,
    )

    result = commit_production_dynamic_three_leg_decision_bundle(
        conn,
        outbox_event_id="outbox-current",
        lease_owner="resident-live",
        claim_generation=1,
        slug=SLUG,
        event_date="2026-07-13",
        desired_trading_plan=desired,
        transition_plan=transition,
        market_snapshot=_market(),
        production_activation=_activation(),
        decision_as_of=NOW,
    )

    assert result["ok"] is True
    assert conn.execute(
        "SELECT COUNT(*) FROM posterior_execution_intents WHERE order_action='BUY'"
    ).fetchone()[0] == 2
    row = conn.execute(
        """SELECT outcome,side,action,decided_state,will_order,reason_codes_json,details_json
           FROM auto_trade_decisions
           WHERE reason_codes_json LIKE '%second_yes_before_entry_window%'"""
    ).fetchone()
    assert tuple(row[:5]) == ("34C", "YES", "hold", "hold_no_order", 0)
    details = json.loads(row[6])
    assert details["strategy_leg"] == "second_yes"
    assert details["entry_allowed"] is False
    assert details["second_yes_entry_window_state"]["second_yes_entry_window_state_id"] == "second-window-closed"
    assert details["uses_ev_edge_lcb"] is False


def test_role_swap_is_a_dynamic_reallocation_not_a_bucket_level_hold() -> None:
    rows = [
        {
            **_position("33C", "YES", notional=4.99, size=9.98, cost=0.5),
            "position_lot_id": "lot-33-second",
            "strategy_leg": "second_yes",
        },
        {
            **_position("34C", "YES", notional=20.0, size=100.0, cost=0.2),
            "position_lot_id": "lot-34-top",
            "strategy_leg": "top_yes_wave",
        },
    ]
    transition = build_dynamic_three_leg_transition_plan(
        desired_trading_plan=_desired(), position_snapshot=_positions(rows), decision_as_of=NOW
    )

    assert transition["phase"] == "SELL_OLD"
    # Roles follow the latest posterior. The former second-YES lot is now the
    # current top bucket, so it remains in place instead of being sold and
    # repurchased. The former top is only trimmed to the strict second cap.
    assert [(row["position_lot_id"], row["reason_code"]) for row in transition["sell_intents"]] == [
        ("lot-34-top", "target_realization_top_yes_invalidated")
    ]
    sell = transition["sell_intents"][0]
    assert sell["full_exit"] is False
    assert sell["target_notional_after_fill_usdc"] == 4.99


def test_multiple_lots_have_deterministic_sell_order_and_cascading_expected_balance() -> None:
    base = _position("32C", "YES", notional=5.0, size=25.0, cost=0.2)
    rows = [
        {**base, "position_lot_id": "lot-old", "opened_at": "2026-07-13T00:00:00Z"},
        {**base, "position_lot_id": "lot-new", "opened_at": "2026-07-13T00:01:00Z"},
    ]
    transition = build_dynamic_three_leg_transition_plan(
        desired_trading_plan=_desired(), position_snapshot=_positions(rows), decision_as_of=NOW
    )

    sells = transition["sell_intents"]
    assert [row["position_lot_id"] for row in sells] == ["lot-new", "lot-old"]
    assert [row["execution_sequence"] for row in sells] == [1, 2]
    assert [row["expected_token_balance"] for row in sells] == [50.0, 25.0]


def test_cycle_runner_reserves_and_commits_in_one_outer_transaction(monkeypatch) -> None:
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_PRODUCTION_ENABLED", "1")
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_LIVE_SUBMIT_ENABLED", "1")
    conn = _conn()
    # The fixture's pre-existing reservation has an intentionally synthetic
    # hash for direct writer tests; the cycle must create its own bound proof.
    conn.execute("DELETE FROM dynamic_three_leg_risk_reservations")
    conn.commit()
    transition = build_dynamic_three_leg_transition_plan(
        desired_trading_plan=_desired(), position_snapshot=_positions(), decision_as_of=NOW,
    )
    conn.execute("BEGIN IMMEDIATE")
    result = run_production_dynamic_three_leg_cycle(
        conn,
        outbox_event_id="outbox-current", lease_owner="resident-live", claim_generation=1,
        slug=SLUG, event_date="2026-07-13", desired_trading_plan=_desired(),
        transition_plan=transition, market_snapshot=_market(),
        capital_snapshot={
            "capital_snapshot_id": "capital-current", "capital_epoch_id": "capital-epoch-current",
            "available_cash_usdc": 100.0, "expires_at": "2026-07-13T00:00:03.000Z",
        },
        risk_snapshot={
            "risk_snapshot_id": "risk-current", "capital_epoch_id": "capital-epoch-current",
            "maximum_new_buy_notional_usdc": 30.0, "new_buy_authorized": True,
            "expires_at": "2026-07-13T00:00:03.000Z",
        },
        production_activation=_activation(), decision_as_of=NOW,
    )
    assert result["ok"] is True
    conn.commit()
    assert conn.execute("SELECT state,bound_transition_plan_id FROM dynamic_three_leg_risk_reservations").fetchone()[0] == "bound"
    assert conn.execute("SELECT state FROM posterior_commit_outbox").fetchone()[0] == "completed"


def test_sell_old_bundle_contains_no_buy_intent(monkeypatch) -> None:
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_PRODUCTION_ENABLED", "1")
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_LIVE_SUBMIT_ENABLED", "1")
    conn = _conn()
    transition = build_dynamic_three_leg_transition_plan(desired_trading_plan=_desired(), position_snapshot=_positions([_position("32C", "YES", notional=20.0, size=100.0, cost=0.2)]), decision_as_of=NOW)
    result = _commit(conn, transition)
    assert result["ok"] is True
    assert conn.execute("SELECT DISTINCT order_action FROM posterior_execution_intents").fetchall() == [("SELL",)]
    assert conn.execute("SELECT reason_code FROM posterior_execution_intents").fetchone()[0] == "target_realization_top_yes_invalidated"


def test_fault_after_intents_rolls_back_everything(monkeypatch) -> None:
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_PRODUCTION_ENABLED", "1")
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_LIVE_SUBMIT_ENABLED", "1")
    conn = _conn()
    transition = build_dynamic_three_leg_transition_plan(desired_trading_plan=_desired(), position_snapshot=_positions(), decision_as_of=NOW)
    with pytest.raises(RuntimeError, match="fault_after_intents"):
        _commit(conn, transition, fault_after="intents")
    assert conn.execute("SELECT COUNT(*) FROM dynamic_three_leg_transition_plans").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM posterior_execution_intents").fetchone()[0] == 0
    assert conn.execute("SELECT state FROM posterior_commit_outbox").fetchone()[0] == "claimed"
    assert conn.execute("SELECT state,bound_transition_plan_id FROM dynamic_three_leg_risk_reservations").fetchone() == ("active", None)


def test_transaction_c_requires_real_sufficient_unbound_risk_reservation(monkeypatch) -> None:
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_PRODUCTION_ENABLED", "1")
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_LIVE_SUBMIT_ENABLED", "1")
    conn = _conn()
    conn.execute("UPDATE dynamic_three_leg_risk_reservations SET reserved_buy_notional=4.99")
    transition = build_dynamic_three_leg_transition_plan(
        desired_trading_plan=_desired(), position_snapshot=_positions(), decision_as_of=NOW
    )

    result = _commit(conn, transition)

    assert result["ok"] is False
    assert "dynamic_three_leg_production_risk_reservation_insufficient" in result["block_reasons"]
    assert conn.execute("SELECT COUNT(*) FROM posterior_execution_intents").fetchone()[0] == 0


def test_hold_records_ssot_decision_without_order_intent(monkeypatch) -> None:
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_PRODUCTION_ENABLED", "1")
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_LIVE_SUBMIT_ENABLED", "1")
    conn = _conn()
    positions = [
        _position("33C", "YES", notional=20.0, size=40.0, cost=0.5),
        _position("34C", "YES", notional=5.0, size=25.0, cost=0.2),
        _position("31C", "NO", notional=5.0, size=5.1020408163, cost=0.98),
    ]
    transition = build_dynamic_three_leg_transition_plan(desired_trading_plan=_desired(), position_snapshot=_positions(positions), decision_as_of=NOW)
    assert transition["phase"] == "HOLD"
    result = _commit(conn, transition)
    assert result["ok"] is True
    assert conn.execute("SELECT will_order,decided_state FROM auto_trade_decisions").fetchone() == (0, "hold_no_order")
    assert conn.execute("SELECT COUNT(*) FROM posterior_execution_intents").fetchone()[0] == 0


def _prepare_one_intent(conn: sqlite3.Connection, monkeypatch) -> str:
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_PRODUCTION_ENABLED", "1")
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_LIVE_SUBMIT_ENABLED", "1")
    transition = build_dynamic_three_leg_transition_plan(
        desired_trading_plan=_desired(), position_snapshot=_positions(), decision_as_of=NOW
    )
    assert _commit(conn, transition)["ok"] is True
    conn.commit()
    return conn.execute("SELECT order_intent_id FROM posterior_execution_intents ORDER BY order_intent_id LIMIT 1").fetchone()[0]


def _advance_latest_posterior(conn: sqlite3.Connection) -> None:
    conn.execute(
        """INSERT INTO event_fact_history(slug,event_date,fact_snapshot_id,posterior_run_id)
           VALUES(?,?,?,?)""",
        (SLUG, "2026-07-13", "facts-next", "post-next"),
    )
    fact_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "UPDATE event_fact_latest SET fact_id=? WHERE slug=? AND event_date=?",
        (fact_id, SLUG, "2026-07-13"),
    )
    conn.commit()


def test_latest_posterior_atomically_supersedes_old_prepared_dynamic_plan(monkeypatch) -> None:
    conn = _conn()
    _prepare_one_intent(conn, monkeypatch)
    _advance_latest_posterior(conn)

    result = supersede_stale_prepared_dynamic_three_leg_intents(
        conn, superseded_at="2026-07-13T00:00:00.650Z",
    )

    assert result["state"] == "superseded"
    assert result["places_orders"] is False
    assert len(result["superseded_order_intent_ids"]) == 3
    assert conn.execute(
        "SELECT COUNT(*) FROM posterior_execution_intents WHERE state='cancelled_superseded' AND last_error=?",
        (SUPERSESSION_REASON,),
    ).fetchone()[0] == 3
    assert conn.execute(
        "SELECT COUNT(*) FROM auto_trade_decisions WHERE decided_state='cancelled_superseded' AND will_order=0"
    ).fetchone()[0] == 3
    assert conn.execute("SELECT state FROM dynamic_three_leg_transition_plans").fetchone()[0] == "superseded"
    assert conn.execute("SELECT state FROM dynamic_three_leg_risk_reservations").fetchone()[0] == "superseded"


def test_current_prepared_dynamic_plan_is_not_superseded(monkeypatch) -> None:
    conn = _conn()
    _prepare_one_intent(conn, monkeypatch)

    result = supersede_stale_prepared_dynamic_three_leg_intents(conn, superseded_at=NOW)

    assert result["state"] == "idle_current"
    assert result["wrote_rows"] is False
    assert conn.execute(
        "SELECT COUNT(*) FROM posterior_execution_intents WHERE state='prepared'"
    ).fetchone()[0] == 3
    assert conn.execute("SELECT state FROM dynamic_three_leg_transition_plans").fetchone()[0] == "prepared"
    assert conn.execute("SELECT state FROM dynamic_three_leg_risk_reservations").fetchone()[0] == "bound"


def test_stale_dynamic_plan_with_inflight_intent_remains_reconcile_first(monkeypatch) -> None:
    conn = _conn()
    intent_id = _prepare_one_intent(conn, monkeypatch)
    conn.execute(
        "UPDATE posterior_execution_intents SET state='reconcile_required',expected_order_id='expected-old' WHERE order_intent_id=?",
        (intent_id,),
    )
    conn.commit()
    _advance_latest_posterior(conn)

    result = supersede_stale_prepared_dynamic_three_leg_intents(conn, superseded_at=NOW)

    assert result["state"] == "blocked"
    assert any("requires_reconcile" in reason for reason in result["block_reasons"])
    assert conn.execute(
        "SELECT state FROM posterior_execution_intents WHERE order_intent_id=?", (intent_id,)
    ).fetchone()[0] == "reconcile_required"
    assert conn.execute(
        "SELECT COUNT(*) FROM posterior_execution_intents WHERE state='prepared'"
    ).fetchone()[0] == 2
    assert conn.execute("SELECT state FROM dynamic_three_leg_transition_plans").fetchone()[0] == "prepared"


def test_superseded_buy_plan_with_prior_fill_consumes_risk(monkeypatch) -> None:
    conn = _conn()
    intent_id = _prepare_one_intent(conn, monkeypatch)
    conn.execute(
        "UPDATE posterior_execution_intents SET state='filled' WHERE order_intent_id=?",
        (intent_id,),
    )
    conn.commit()
    _advance_latest_posterior(conn)

    result = supersede_stale_prepared_dynamic_three_leg_intents(conn, superseded_at=NOW)

    assert result["state"] == "superseded"
    assert conn.execute("SELECT state FROM dynamic_three_leg_risk_reservations").fetchone()[0] == "consumed"
    assert conn.execute(
        "SELECT COUNT(*) FROM posterior_execution_intents WHERE state='cancelled_superseded'"
    ).fetchone()[0] == 2


def _pre_submit_ok(_intent: dict) -> dict:
    return {"ok": True, "risk_reservation_active": True, "position_lot_current": True, "market_quote_current": True, "balance_sufficient": True}


def test_resident_submitter_is_default_disabled(monkeypatch) -> None:
    conn = _conn()
    intent_id = _prepare_one_intent(conn, monkeypatch)
    monkeypatch.delenv("PM_DYNAMIC_THREE_LEG_RESIDENT_SUBMITTER_ENABLED", raising=False)
    calls = []
    result = submit_prepared_dynamic_three_leg_intent(
        conn, order_intent_id=intent_id, submit_as_of="2026-07-13T00:00:00.700Z",
        pre_submit_validator=_pre_submit_ok,
        gateway_place_order=lambda payload: calls.append(payload) or {"ok": True},
    )
    assert result["called_gateway"] is False
    assert calls == []
    assert conn.execute("SELECT state FROM posterior_execution_intents WHERE order_intent_id=?", (intent_id,)).fetchone()[0] == "prepared"


def test_resident_submitter_cas_then_calls_gateway_and_records_t5(monkeypatch) -> None:
    conn = _conn()
    intent_id = _prepare_one_intent(conn, monkeypatch)
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_RESIDENT_SUBMITTER_ENABLED", "1")
    calls = []
    result = submit_prepared_dynamic_three_leg_intent(
        conn, order_intent_id=intent_id, submit_as_of="2026-07-13T00:00:00.700Z",
        pre_submit_validator=_pre_submit_ok,
        gateway_place_order=lambda payload: calls.append(payload) or {
            "ok": True, "accepted": True, "orderID": "order-1",
            "expected_order_id": "expected-1", "exchange_post_started": True,
            "exchange_post_started_at": "2026-07-13T00:00:00.800Z",
        },
    )
    assert result["ok"] is True
    assert len(calls) == 1
    assert calls[0]["observation_to_order_hard_deadline_at"] == "2026-07-13T00:00:03.000Z"
    assert calls[0]["post_only"] is False
    assert tuple(conn.execute("SELECT state,exchange_order_id,exchange_post_started_at FROM posterior_execution_intents WHERE order_intent_id=?", (intent_id,)).fetchone()) == ("gateway_accepted", "order-1", "2026-07-13T00:00:00.800Z")
    retry = submit_prepared_dynamic_three_leg_intent(
        conn, order_intent_id=intent_id, submit_as_of="2026-07-13T00:00:00.900Z",
        pre_submit_validator=_pre_submit_ok,
        gateway_place_order=lambda _payload: (_ for _ in ()).throw(AssertionError("must not resubmit")),
    )
    assert retry["idempotent"] is True


def test_resident_submitter_gateway_exception_requires_reconcile_without_resubmit(monkeypatch) -> None:
    conn = _conn()
    intent_id = _prepare_one_intent(conn, monkeypatch)
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_RESIDENT_SUBMITTER_ENABLED", "1")
    calls = {"count": 0}
    def broken(_payload):
        calls["count"] += 1
        raise TimeoutError("unknown after local gateway call")
    first = submit_prepared_dynamic_three_leg_intent(
        conn, order_intent_id=intent_id, submit_as_of="2026-07-13T00:00:00.700Z",
        pre_submit_validator=_pre_submit_ok, gateway_place_order=broken,
    )
    second = submit_prepared_dynamic_three_leg_intent(
        conn, order_intent_id=intent_id, submit_as_of="2026-07-13T00:00:00.800Z",
        pre_submit_validator=_pre_submit_ok, gateway_place_order=broken,
    )
    assert first["state"] == "reconcile_required"
    assert second["called_gateway"] is False
    assert calls["count"] == 1


def test_definitive_submit_failure_blocks_transition_cancels_siblings_and_releases_risk(monkeypatch) -> None:
    conn = _conn()
    intent_id = _prepare_one_intent(conn, monkeypatch)
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_RESIDENT_SUBMITTER_ENABLED", "1")
    result = submit_prepared_dynamic_three_leg_intent(
        conn, order_intent_id=intent_id, submit_as_of="2026-07-13T00:00:00.700Z",
        pre_submit_validator=_pre_submit_ok,
        gateway_place_order=lambda _payload: {"ok": False, "error": "exchange_rejected", "exchange_post_started": False},
    )
    assert result["state"] == "failed"
    assert result["transition_lifecycle"] == {"changed": True, "state": "blocked", "risk_reservation_state": "released"}
    assert conn.execute("SELECT state FROM dynamic_three_leg_transition_plans").fetchone()[0] == "blocked"
    assert conn.execute("SELECT state FROM dynamic_three_leg_risk_reservations").fetchone()[0] == "released"
    assert conn.execute("SELECT COUNT(*) FROM posterior_execution_intents WHERE state='cancelled_superseded'").fetchone()[0] == 2


def test_resident_submitter_expires_intent_before_gateway(monkeypatch) -> None:
    conn = _conn()
    intent_id = _prepare_one_intent(conn, monkeypatch)
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_RESIDENT_SUBMITTER_ENABLED", "1")
    calls = []
    result = submit_prepared_dynamic_three_leg_intent(
        conn, order_intent_id=intent_id, submit_as_of="2026-07-13T00:00:03.001Z",
        pre_submit_validator=_pre_submit_ok,
        gateway_place_order=lambda payload: calls.append(payload) or {"ok": True},
    )
    assert result["state"] == "expired"
    assert calls == []
    assert tuple(conn.execute("SELECT will_order,decided_state FROM auto_trade_decisions WHERE order_intent_id=?", (intent_id,)).fetchone()) == (0, "expired")


def test_second_yes_intent_cannot_submit_at_or_after_predicted_peak(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_PRODUCTION_ENABLED", "1")
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_LIVE_SUBMIT_ENABLED", "1")
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_RESIDENT_SUBMITTER_ENABLED", "1")
    desired = _desired()
    desired["second_yes_entry_window_state"] = {
        **desired["second_yes_entry_window_state"],
        "entry_window_start_at": "2026-07-12T23:30:00.000Z",
        "entry_window_end_at": "2026-07-13T00:00:00.750Z",
        "predicted_peak_at": "2026-07-13T00:00:00.750Z",
    }
    transition = build_dynamic_three_leg_transition_plan(
        desired_trading_plan=desired, position_snapshot=_positions(), decision_as_of=NOW
    )
    assert _commit(conn, transition, desired_trading_plan=desired)["ok"] is True
    conn.commit()
    intent_id, quote_expiry = conn.execute(
        "SELECT order_intent_id,quote_expires_at FROM posterior_execution_intents WHERE reason_code='second_yes'"
    ).fetchone()
    assert quote_expiry == "2026-07-13T00:00:00.750Z"
    calls = []
    result = submit_prepared_dynamic_three_leg_intent(
        conn,
        order_intent_id=intent_id,
        submit_as_of="2026-07-13T00:00:00.750Z",
        pre_submit_validator=_pre_submit_ok,
        gateway_place_order=lambda payload: calls.append(payload) or {"ok": True},
    )
    assert result["state"] == "expired"
    assert result["reason"] == "second_yes_entry_window_elapsed_before_submit"
    assert calls == []


def test_resident_submitter_refuses_superseded_latest_posterior(monkeypatch) -> None:
    conn = _conn()
    intent_id = _prepare_one_intent(conn, monkeypatch)
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_RESIDENT_SUBMITTER_ENABLED", "1")
    conn.execute("UPDATE event_fact_latest SET fact_id=999 WHERE slug=?", (SLUG,))
    conn.commit()
    calls = []

    result = submit_prepared_dynamic_three_leg_intent(
        conn, order_intent_id=intent_id, submit_as_of="2026-07-13T00:00:00.700Z",
        pre_submit_validator=_pre_submit_ok,
        gateway_place_order=lambda payload: calls.append(payload) or {"ok": True},
    )

    assert result["reason"] == "dynamic_three_leg_pre_submit_ssot_or_transition_stale"
    assert result["called_gateway"] is False
    assert calls == []


def test_resident_submitter_refuses_failed_risk_or_balance_validation(monkeypatch) -> None:
    conn = _conn()
    intent_id = _prepare_one_intent(conn, monkeypatch)
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_RESIDENT_SUBMITTER_ENABLED", "1")
    calls = []

    result = submit_prepared_dynamic_three_leg_intent(
        conn, order_intent_id=intent_id, submit_as_of="2026-07-13T00:00:00.700Z",
        pre_submit_validator=lambda _intent: {
            "ok": False, "risk_reservation_active": False,
            "position_lot_current": True, "market_quote_current": True,
            "balance_sufficient": False,
        },
        gateway_place_order=lambda payload: calls.append(payload) or {"ok": True},
    )

    assert result["reason"] == "dynamic_three_leg_pre_submit_validation_failed"
    assert result["called_gateway"] is False
    assert calls == []


def test_resident_submitter_refuses_transition_position_or_reservation_drift(monkeypatch) -> None:
    conn = _conn()
    intent_id = _prepare_one_intent(conn, monkeypatch)
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_RESIDENT_SUBMITTER_ENABLED", "1")
    conn.execute(
        "UPDATE dynamic_three_leg_transition_plans SET risk_reservation_id='risk-reservation-drifted'"
    )
    conn.commit()
    calls = []

    result = submit_prepared_dynamic_three_leg_intent(
        conn, order_intent_id=intent_id, submit_as_of="2026-07-13T00:00:00.700Z",
        pre_submit_validator=_pre_submit_ok,
        gateway_place_order=lambda payload: calls.append(payload) or {"ok": True},
    )

    assert result["reason"] == "dynamic_three_leg_pre_submit_ssot_or_transition_stale"
    assert result["called_gateway"] is False
    assert calls == []


def test_submitting_claim_is_durable_before_gateway_call(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "resident-submit.sqlite"
    conn = _conn(str(db_path))
    intent_id = _prepare_one_intent(conn, monkeypatch)
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_RESIDENT_SUBMITTER_ENABLED", "1")

    def gateway(_payload):
        observer = sqlite3.connect(db_path)
        try:
            assert observer.execute(
                "SELECT state FROM posterior_execution_intents WHERE order_intent_id=?",
                (intent_id,),
            ).fetchone()[0] == "submitting"
        finally:
            observer.close()
        return {
            "ok": True,
            "accepted": True,
            "orderID": "durable-order-1",
            "exchange_post_started": True,
            "exchange_post_started_at": "2026-07-13T00:00:00.800Z",
        }

    result = submit_prepared_dynamic_three_leg_intent(
        conn,
        order_intent_id=intent_id,
        submit_as_of="2026-07-13T00:00:00.700Z",
        pre_submit_validator=_pre_submit_ok,
        gateway_place_order=gateway,
    )
    assert result["state"] == "gateway_accepted"
    observer = sqlite3.connect(db_path)
    try:
        assert observer.execute(
            "SELECT state FROM posterior_execution_intents WHERE order_intent_id=?",
            (intent_id,),
        ).fetchone()[0] == "gateway_accepted"
    finally:
        observer.close()


def test_reconcile_ambiguous_intent_by_identity_without_resubmit(monkeypatch) -> None:
    conn = _conn()
    intent_id = _prepare_one_intent(conn, monkeypatch)
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_RESIDENT_SUBMITTER_ENABLED", "1")
    first = submit_prepared_dynamic_three_leg_intent(
        conn,
        order_intent_id=intent_id,
        submit_as_of="2026-07-13T00:00:00.700Z",
        pre_submit_validator=_pre_submit_ok,
        gateway_place_order=lambda _payload: {
            "ok": False,
            "pending": True,
            "expected_order_id": "expected-reconcile-1",
            "exchange_post_started": True,
            "exchange_post_started_at": "2026-07-13T00:00:00.800Z",
        },
    )
    assert first["state"] == "reconcile_required"
    looked_up = []
    intent_size = conn.execute(
        "SELECT size FROM posterior_execution_intents WHERE order_intent_id=?", (intent_id,)
    ).fetchone()[0]
    reconciled = reconcile_dynamic_three_leg_intent(
        conn,
        order_intent_id=intent_id,
        reconcile_as_of="2026-07-13T00:00:01.000Z",
        gateway_lookup_order=lambda lookup: looked_up.append(lookup) or {
            "ok": True,
            "status": "filled",
            "orderID": "expected-reconcile-1",
            "original_size": intent_size,
            "size_matched": intent_size,
        },
    )
    assert reconciled["state"] == "filled"
    assert reconciled["submitted_order"] is False
    assert len(looked_up) == 1
    assert conn.execute(
        "SELECT state FROM posterior_execution_intents WHERE order_intent_id=?",
        (intent_id,),
    ).fetchone()[0] == "filled"


def test_reconcile_not_found_keeps_uncertain_intent_closed_to_resubmit(monkeypatch) -> None:
    conn = _conn()
    intent_id = _prepare_one_intent(conn, monkeypatch)
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_RESIDENT_SUBMITTER_ENABLED", "1")
    conn.execute(
        "UPDATE posterior_execution_intents SET state='reconcile_required' WHERE order_intent_id=?",
        (intent_id,),
    )
    conn.commit()
    result = reconcile_dynamic_three_leg_intent(
        conn,
        order_intent_id=intent_id,
        reconcile_as_of="2026-07-13T00:00:01.000Z",
        gateway_lookup_order=lambda lookup: {
            "ok": True,
            "status": "not_found",
            "idempotency_key": lookup["idempotency_key"],
        },
    )
    assert result["state"] == "reconcile_required"
    retry = submit_prepared_dynamic_three_leg_intent(
        conn,
        order_intent_id=intent_id,
        submit_as_of="2026-07-13T00:00:01.100Z",
        pre_submit_validator=_pre_submit_ok,
        gateway_place_order=lambda _payload: (_ for _ in ()).throw(AssertionError("must not resubmit")),
    )
    assert retry["called_gateway"] is False


def test_all_buy_fills_complete_transition_and_consume_reservation(monkeypatch) -> None:
    conn = _conn()
    _prepare_one_intent(conn, monkeypatch)
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_RESIDENT_SUBMITTER_ENABLED", "1")
    rows = conn.execute("SELECT order_intent_id,decision_id,idempotency_key,token_id,size FROM posterior_execution_intents ORDER BY order_intent_id").fetchall()
    for index, (intent_id, _decision_id, _key, _token, intent_size) in enumerate(rows):
        expected = f"fill-{index}"
        conn.execute("UPDATE posterior_execution_intents SET state='reconcile_required',expected_order_id=? WHERE order_intent_id=?", (expected, intent_id))
        conn.commit()
        result = reconcile_dynamic_three_leg_intent(
            conn, order_intent_id=intent_id, reconcile_as_of=f"2026-07-13T00:00:01.00{index}Z",
            gateway_lookup_order=lambda lookup, expected=expected: {
                "ok": True, "status": "filled", "orderID": expected,
                "token_id": lookup["token_id"], "original_size": intent_size, "size_matched": intent_size,
            },
        )
        assert result["state"] == "filled"
    assert conn.execute("SELECT state FROM dynamic_three_leg_transition_plans").fetchone()[0] == "completed"
    assert conn.execute("SELECT state FROM dynamic_three_leg_risk_reservations").fetchone()[0] == "consumed"


def test_reconcile_rejects_order_identity_with_wrong_token(monkeypatch) -> None:
    conn = _conn()
    intent_id = _prepare_one_intent(conn, monkeypatch)
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_RESIDENT_SUBMITTER_ENABLED", "1")
    conn.execute("UPDATE posterior_execution_intents SET state='reconcile_required',expected_order_id='expected-wrong-token' WHERE order_intent_id=?", (intent_id,))
    conn.commit()
    result = reconcile_dynamic_three_leg_intent(
        conn, order_intent_id=intent_id, reconcile_as_of="2026-07-13T00:00:01.000Z",
        gateway_lookup_order=lambda _lookup: {"ok": True, "status": "filled", "orderID": "expected-wrong-token", "token_id": "another-token"},
    )
    assert result["state"] == "reconcile_required"
    assert result["reason"] == "gateway_reconcile_identity_unproven"


def test_reconcile_rejects_overfill_and_order_price_drift(monkeypatch) -> None:
    conn = _conn()
    intent_id = _prepare_one_intent(conn, monkeypatch)
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_RESIDENT_SUBMITTER_ENABLED", "1")
    conn.execute(
        "UPDATE posterior_execution_intents SET state='reconcile_required',expected_order_id='expected-overfill' WHERE order_intent_id=?",
        (intent_id,),
    )
    conn.commit()
    row = conn.execute(
        "SELECT size,limit_price,token_id FROM posterior_execution_intents WHERE order_intent_id=?",
        (intent_id,),
    ).fetchone()
    event_count_before = conn.execute(
        "SELECT COUNT(*) FROM dynamic_three_leg_position_lot_events"
    ).fetchone()[0]
    result = reconcile_dynamic_three_leg_intent(
        conn,
        order_intent_id=intent_id,
        reconcile_as_of="2026-07-13T00:00:01.000Z",
        gateway_lookup_order=lambda _lookup: {
            "ok": True,
            "status": "filled",
            "orderID": "expected-overfill",
            "token_id": row[2],
            "original_size": row[0],
            "size_matched": float(row[0]) + 1.0,
            "price": float(row[1]) + 0.01,
        },
    )
    assert result["state"] == "reconcile_required"
    assert result["reason"] == "gateway_reconcile_identity_unproven"
    assert conn.execute(
        "SELECT COUNT(*) FROM dynamic_three_leg_position_lot_events"
    ).fetchone()[0] == event_count_before


def test_cancelled_order_applies_only_verified_partial_fill_then_closes_transition(monkeypatch) -> None:
    conn = _conn()
    intent_id = _prepare_one_intent(conn, monkeypatch)
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_RESIDENT_SUBMITTER_ENABLED", "1")
    row = conn.execute(
        "SELECT size,limit_price,token_id FROM posterior_execution_intents WHERE order_intent_id=?",
        (intent_id,),
    ).fetchone()
    matched = float(row[0]) / 2.0
    conn.execute(
        "UPDATE posterior_execution_intents SET state='reconcile_required',expected_order_id='expected-cancelled-partial' WHERE order_intent_id=?",
        (intent_id,),
    )
    conn.commit()
    result = reconcile_dynamic_three_leg_intent(
        conn,
        order_intent_id=intent_id,
        reconcile_as_of="2026-07-13T00:00:01.000Z",
        gateway_lookup_order=lambda _lookup: {
            "ok": True,
            "status": "cancelled",
            "orderID": "expected-cancelled-partial",
            "token_id": row[2],
            "original_size": row[0],
            "size_matched": matched,
            "price": row[1],
        },
    )
    assert result["state"] == "failed"
    assert result["position_lot_update"]["applied_size"] == matched
    assert tuple(conn.execute(
        "SELECT acquired_size,remaining_size FROM dynamic_three_leg_position_lots"
    ).fetchone()) == (matched, matched)
    assert conn.execute(
        "SELECT state FROM dynamic_three_leg_transition_plans"
    ).fetchone()[0] == "blocked"
    assert conn.execute(
        "SELECT state FROM dynamic_three_leg_risk_reservations"
    ).fetchone()[0] == "consumed"
    assert conn.execute(
        "SELECT COUNT(*) FROM posterior_execution_intents WHERE state='cancelled_superseded'"
    ).fetchone()[0] == 2


def test_sell_exit_is_not_blocked_by_expired_buy_reservation(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_PRODUCTION_ENABLED", "1")
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_LIVE_SUBMIT_ENABLED", "1")
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_RESIDENT_SUBMITTER_ENABLED", "1")
    position = _position("32C", "YES", notional=20.0, size=100.0, cost=0.2)
    conn.execute(
        """INSERT INTO dynamic_three_leg_position_lots(
           position_lot_id,position_lot_version,management_scope,slug,event_date,bucket,
           outcome_side,token_id,entry_strategy_leg,entry_strategy_artifact_sha256,
           entry_order_intent_id,entry_posterior_run_id,entry_price,acquired_size,
           remaining_size,remaining_cost_usdc,frozen_take_profit_price,state,
           opened_at,closed_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            position["position_lot_id"], 3, MANAGEMENT_SCOPE, SLUG, "2026-07-13", "32C",
            "YES", position["token_id"], "top_yes_wave", "strategy-current", "entry-old",
            "post-old", 0.2, 100.0, 100.0, 20.0, 0.90, "open", NOW, None, NOW,
        ),
    )
    conn.commit()
    transition = build_dynamic_three_leg_transition_plan(
        desired_trading_plan=_desired(),
        position_snapshot=_positions([position]),
        decision_as_of=NOW,
    )
    assert _commit(conn, transition)["ok"] is True
    intent_id = conn.execute("SELECT order_intent_id FROM posterior_execution_intents").fetchone()[0]
    conn.execute("UPDATE dynamic_three_leg_risk_reservations SET expires_at='2026-07-13T00:00:00.600Z'")
    conn.commit()
    calls = []
    result = submit_prepared_dynamic_three_leg_intent(
        conn, order_intent_id=intent_id, submit_as_of="2026-07-13T00:00:00.700Z",
        pre_submit_validator=_pre_submit_ok,
        gateway_place_order=lambda payload: calls.append(payload) or {"ok": True, "accepted": True, "orderID": "sell-order-1", "exchange_post_started": True, "exchange_post_started_at": "2026-07-13T00:00:00.800Z"},
    )
    assert result["state"] == "gateway_accepted"
    assert len(calls) == 1
    reconciled = reconcile_dynamic_three_leg_intent(
        conn,
        order_intent_id=intent_id,
        reconcile_as_of="2026-07-13T00:00:00.900Z",
        gateway_lookup_order=lambda _lookup: {
            "ok": True,
            "status": "filled",
            "orderID": "sell-order-1",
            "token_id": position["token_id"],
            "original_size": 100.0,
            "size_matched": 100.0,
            "price": 0.20,
        },
    )
    assert reconciled["state"] == "filled"
    continuation = conn.execute(
        "SELECT posterior_run_id,previous_transition_plan_id,transition_generation,state FROM dynamic_three_leg_continuation_events"
    ).fetchone()
    assert tuple(continuation) == ("post-current", transition["transition_plan_id"], 2, "pending")

    continuation_desired = {
        **_desired(),
        "trading_plan_id": "trplan-continuation",
        "market_snapshot_id": "market-continuation",
        "capital_snapshot_id": "capital-continuation",
        "risk_snapshot_id": "risk-continuation",
        "capital_epoch_id": "epoch-continuation",
        "risk_reservation_id": "reservation-continuation",
    }
    continuation_positions = {
        "position_snapshot_id": "positions-after-sell",
        "as_of": "2026-07-13T00:00:00.910Z",
        "complete": True,
        "positions": [],
        "unresolved_intents": [],
    }
    continuation_transition = build_dynamic_three_leg_transition_plan(
        desired_trading_plan=continuation_desired,
        position_snapshot=continuation_positions,
        decision_as_of="2026-07-13T00:00:00.920Z",
    )
    continuation_market = {**_market(), "market_snapshot_id": "market-continuation", "as_of": "2026-07-13T00:00:00.910Z", "expires_at": "2026-07-13T00:00:01.500Z"}
    continued = process_dynamic_three_leg_continuation_once(
        conn,
        preliminary_as_of="2026-07-13T00:00:00.900Z",
        lease_owner="continuation-test",
        frozen_inputs_for=lambda _event: {
            "ok": True,
            "decision_as_of": "2026-07-13T00:00:00.920Z",
            "desired_trading_plan": continuation_desired,
            "transition_plan": continuation_transition,
            "market_snapshot": continuation_market,
            "capital_snapshot": {
                "capital_snapshot_id": "capital-continuation", "risk_snapshot_id": "risk-continuation",
                "capital_epoch_id": "epoch-continuation", "risk_reservation_id": "reservation-continuation",
                "available_cash_usdc": 100.0, "expires_at": "2026-07-13T00:00:01.500Z",
            },
            "risk_snapshot": {
                "risk_snapshot_id": "risk-continuation", "capital_epoch_id": "epoch-continuation",
                "maximum_new_buy_notional_usdc": 30.0, "new_buy_authorized": True,
                "expires_at": "2026-07-13T00:00:01.500Z",
            },
            "production_activation": _activation(),
        },
    )
    assert continued["state"] == "continuation_committed"
    assert conn.execute("SELECT state FROM dynamic_three_leg_continuation_events").fetchone()[0] == "completed"
    assert conn.execute("SELECT COUNT(*) FROM dynamic_three_leg_continuation_transition_plans").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM posterior_execution_intents WHERE state='prepared'").fetchone()[0] == 3


def test_resident_loop_is_default_off_before_opening_database(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PM_DYNAMIC_THREE_LEG_RESIDENT_SUBMITTER_ENABLED", raising=False)
    monkeypatch.delenv("PM_DYNAMIC_THREE_LEG_LIVE_SUBMIT_ENABLED", raising=False)
    missing = tmp_path / "must-not-be-created.sqlite"
    result = run_resident_dynamic_three_leg_execution_once(db_path=missing)
    assert result["state"] == "disabled"
    assert result["called_gateway"] is False
    assert not missing.exists()


def test_resident_loop_uses_real_guard_before_single_place_call(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "resident-loop.sqlite"
    conn = _conn(str(db_path))
    _prepare_one_intent(conn, monkeypatch)
    conn.close()
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_RESIDENT_SUBMITTER_ENABLED", "1")
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_LIVE_SUBMIT_ENABLED", "1")

    class Gateway:
        def __init__(self):
            self.validated = []
            self.placed = []

        def validate_execution_guard(self, intent):
            self.validated.append(intent["order_intent_id"])
            return {"ok": True, "position_lot_current": True, "market_quote_current": True, "balance_sufficient": True}

        def place_order(self, payload):
            self.placed.append(payload["order_intent_id"])
            return {"ok": True, "accepted": True, "orderID": "loop-order-1", "exchange_post_started": True, "exchange_post_started_at": "2026-07-13T00:00:00.800Z"}

        def lookup_order(self, _lookup):
            raise AssertionError("no reconcile expected")

    gateway = Gateway()
    result = run_resident_dynamic_three_leg_execution_once(
        db_path=db_path, gateway=gateway, action_as_of="2026-07-13T00:00:00.700Z"
    )
    assert result["state"] == "submitted"
    assert len(gateway.validated) == len(gateway.placed) == 1
    check = sqlite3.connect(db_path)
    try:
        assert check.execute("SELECT COUNT(*) FROM posterior_execution_intents WHERE state='gateway_accepted'").fetchone()[0] == 1
        assert check.execute("SELECT COUNT(*) FROM posterior_execution_intents WHERE state='prepared'").fetchone()[0] == 2
    finally:
        check.close()


def test_resident_loop_reconciles_ambiguity_before_any_prepared_intent(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "resident-reconcile-first.sqlite"
    conn = _conn(str(db_path))
    first = _prepare_one_intent(conn, monkeypatch)
    conn.execute("UPDATE posterior_execution_intents SET state='reconcile_required',expected_order_id='expected-1' WHERE order_intent_id=?", (first,))
    conn.commit()
    conn.close()
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_RESIDENT_SUBMITTER_ENABLED", "1")
    monkeypatch.setenv("PM_DYNAMIC_THREE_LEG_LIVE_SUBMIT_ENABLED", "1")

    class Gateway:
        def lookup_order(self, lookup):
            observer = sqlite3.connect(db_path)
            try:
                size = observer.execute("SELECT size FROM posterior_execution_intents WHERE order_intent_id=?", (first,)).fetchone()[0]
            finally:
                observer.close()
            return {"ok": True, "status": "filled", "orderID": "expected-1", "idempotency_key": lookup["idempotency_key"], "original_size": size, "size_matched": size}

        def validate_execution_guard(self, _intent):
            raise AssertionError("prepared validation must wait for reconcile")

        def place_order(self, _payload):
            raise AssertionError("prepared submit must wait for reconcile")

    result = run_resident_dynamic_three_leg_execution_once(
        db_path=db_path, gateway=Gateway(), action_as_of="2026-07-13T00:00:01.000Z"
    )
    assert result["state"] == "reconciled"
    assert result["result"]["state"] == "filled"
    assert result["called_gateway"] is True

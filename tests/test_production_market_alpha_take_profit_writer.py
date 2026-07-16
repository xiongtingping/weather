from __future__ import annotations

import sqlite3

import pytest

from market_alpha_take_profit_activation import materialize_market_alpha_take_profit_activation
from market_alpha_take_profit_schema_contract import apply_market_alpha_take_profit_schema
from dynamic_three_leg_position_lot_schema_contract import apply_dynamic_three_leg_position_lot_schema
from production_market_alpha_take_profit_writer import (
    SUPERSESSION_REASON,
    commit_market_alpha_take_profit_candidate,
    load_top_yes_wave_reentry_state,
    same_posterior_wave_reentry_suppressed,
    supersede_stale_prepared_market_alpha_intents,
)
from sequential_bayes_schema_contract import apply_forward_schema
from resident_dynamic_three_leg_intent_submitter import (
    reconcile_dynamic_three_leg_intent,
    submit_prepared_dynamic_three_leg_intent,
)


NOW = "2026-07-14T08:00:00.500Z"
SLUG = "highest-temperature-in-chengdu-on-july-14-2026"
ARTIFACT = "strategy-approved"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    apply_dynamic_three_leg_position_lot_schema(conn, writer_quiescent=True)
    apply_market_alpha_take_profit_schema(conn, writer_quiescent=True)
    conn.executescript(
        """
        CREATE TABLE event_fact_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,slug TEXT,event_date TEXT,
            fact_snapshot_id TEXT,posterior_run_id TEXT
        );
        CREATE TABLE event_fact_latest(slug TEXT,event_date TEXT,fact_id INTEGER);
        CREATE TABLE auto_trade_decisions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT,run_date TEXT,slug TEXT,event_date TEXT,
            outcome TEXT,side TEXT,action TEXT,decided_state TEXT,will_order INTEGER,
            reason_codes_json TEXT,reasons_json TEXT,details_json TEXT,created_at TEXT,
            correlation_id TEXT,fact_snapshot_id TEXT,posterior_run_id TEXT,
            decision_id TEXT UNIQUE,order_intent_id TEXT UNIQUE
        );
        INSERT INTO event_fact_history(slug,event_date,fact_snapshot_id,posterior_run_id)
        VALUES('highest-temperature-in-chengdu-on-july-14-2026','2026-07-14','fact-current','post-current');
        INSERT INTO event_fact_latest VALUES('highest-temperature-in-chengdu-on-july-14-2026','2026-07-14',1);
        """
    )
    conn.execute(
        """INSERT INTO dynamic_three_leg_position_lots(
           position_lot_id,position_lot_version,management_scope,slug,event_date,bucket,
           outcome_side,token_id,entry_strategy_leg,entry_strategy_artifact_sha256,
           entry_order_intent_id,entry_posterior_run_id,entry_price,acquired_size,
           remaining_size,remaining_cost_usdc,frozen_take_profit_price,state,
           opened_at,closed_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "lot-33-yes", 4, "dynamic_three_leg_weather", SLUG, "2026-07-14", "33C",
            "YES", "token-33-yes", "top_yes_wave", ARTIFACT, "entry-intent", "post-entry",
            0.50, 20.0, 20.0, 10.0, 0.90, "open", NOW, None, NOW,
        ),
    )
    conn.commit()
    return conn


def _fact() -> dict:
    return {
        "slug": SLUG,
        "event_date": "2026-07-14",
        "posterior_run_id": "post-current",
        "realization_state_id": "realization-current",
        "fact_snapshot_id": "fact-current",
        "target_bucket": "33C",
    }


def _position() -> dict:
    return {
        "management_scope": "dynamic_three_leg_weather",
        "strategy_leg": "top_yes_wave",
        "bucket": "33C",
        "side": "YES",
        "token_id": "token-33-yes",
        "position_lot_id": "lot-33-yes",
        "position_lot_version": "4",
        "position_size": 20.0,
        "entry_strategy_artifact_sha256": ARTIFACT,
        "frozen_take_profit_price": 0.90,
    }


def _quote() -> dict:
    return {
        "market_snapshot_id": "market-current",
        "token_id": "token-33-yes",
        "requested_size": 20.0,
        "price_for_requested_size": 0.91,
        "as_of": "2026-07-14T08:00:00.300Z",
        "expires_at": "2026-07-14T08:00:01.000Z",
    }


def _activation() -> dict:
    return materialize_market_alpha_take_profit_activation(
        {
            "schema": "market_alpha_take_profit_production_activation_v1",
            "state": "enabled",
            "strategy_artifact_sha256": ARTIFACT,
            "cutover_artifact_sha256": "cutover-approved",
            "live_decision_write_authorized": True,
            "live_submit_authorized": True,
            "reason_family": "market_alpha_take_profit",
            "reason_code": "market_alpha_take_profit_top_yes_wave_completed",
            "frozen_take_profit_price": 0.90,
        }
    )


def _commit(conn: sqlite3.Connection, **kwargs) -> dict:
    return commit_market_alpha_take_profit_candidate(
        conn,
        posterior_ssot_fact=_fact(),
        position_snapshot_id="positions-current",
        position=_position(),
        executable_sell_quote=_quote(),
        activation=_activation(),
        decision_as_of=NOW,
        **kwargs,
    )


def test_atomic_market_alpha_writer_emits_exact_independent_sell_family(monkeypatch) -> None:
    monkeypatch.setenv("PM_MARKET_ALPHA_TAKE_PROFIT_PRODUCTION_ENABLED", "1")
    monkeypatch.setenv("PM_MARKET_ALPHA_TAKE_PROFIT_LIVE_SUBMIT_ENABLED", "1")
    conn = _conn()

    result = _commit(conn)

    assert result["ok"] is True
    assert conn.execute("SELECT reason_family,reason_code FROM posterior_execution_intents").fetchone() == (
        "market_alpha_take_profit",
        "market_alpha_take_profit_top_yes_wave_completed",
    )
    assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0] == 1
    assert conn.execute("SELECT state FROM market_alpha_take_profit_cycles").fetchone()[0] == "prepared"
    assert _commit(conn)["idempotent"] is True


@pytest.mark.parametrize("fault_after", ["cycle", "decision", "intent"])
def test_market_alpha_writer_faults_roll_back_all_three_rows(monkeypatch, fault_after: str) -> None:
    monkeypatch.setenv("PM_MARKET_ALPHA_TAKE_PROFIT_PRODUCTION_ENABLED", "1")
    monkeypatch.setenv("PM_MARKET_ALPHA_TAKE_PROFIT_LIVE_SUBMIT_ENABLED", "1")
    conn = _conn()

    with pytest.raises(RuntimeError, match=f"market_alpha_fault_after_{fault_after}"):
        _commit(conn, fault_after=fault_after)

    assert conn.execute("SELECT COUNT(*) FROM market_alpha_take_profit_cycles").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM posterior_execution_intents").fetchone()[0] == 0


def test_target_realization_sell_has_precedence(monkeypatch) -> None:
    monkeypatch.setenv("PM_MARKET_ALPHA_TAKE_PROFIT_PRODUCTION_ENABLED", "1")
    monkeypatch.setenv("PM_MARKET_ALPHA_TAKE_PROFIT_LIVE_SUBMIT_ENABLED", "1")
    conn = _conn()
    conn.execute(
        """INSERT INTO posterior_execution_intents(
           order_intent_id,decision_id,transition_plan_id,trading_plan_id,posterior_run_id,
           realization_state_id,fact_snapshot_id,position_snapshot_id,position_lot_id,
           position_lot_version,strategy_artifact_sha256,market_snapshot_id,capital_snapshot_id,
           risk_snapshot_id,capital_epoch_id,risk_reservation_id,candidate_id,bucket,outcome_side,
           order_action,token_id,limit_price,size,notional,reason_family,reason_code,state,
           live_submit_authorized,observation_to_order_hard_deadline_at,quote_expires_at,
           idempotency_key,intent_json,intent_sha256,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("weather-exit", "weather-decision", "weather-transition", "weather-plan", "post-current",
         "realization-current", "fact-current", "positions-current", "lot-33-yes", "4", ARTIFACT,
         "market-old", "capital", "risk", "epoch", "reservation", None, "33C", "YES", "SELL",
         "token-33-yes", 0.5, 20.0, 10.0, "target_realization", "target_realization_top_yes_invalidated",
         "prepared", 1, "2026-07-14T08:00:01Z", "2026-07-14T08:00:01Z", "weather-idempotency", "{}", "sha", NOW, NOW),
    )
    conn.commit()

    result = _commit(conn)

    assert result["ok"] is False
    assert "market_alpha_superseded_by_target_realization_exit" in result["block_reasons"]
    assert conn.execute("SELECT COUNT(*) FROM market_alpha_take_profit_cycles").fetchone()[0] == 0


def test_completed_same_posterior_wave_suppresses_reentry_but_new_run_does_not(monkeypatch) -> None:
    monkeypatch.setenv("PM_MARKET_ALPHA_TAKE_PROFIT_PRODUCTION_ENABLED", "1")
    monkeypatch.setenv("PM_MARKET_ALPHA_TAKE_PROFIT_LIVE_SUBMIT_ENABLED", "1")
    conn = _conn()
    assert _commit(conn)["ok"] is True
    conn.execute("UPDATE market_alpha_take_profit_cycles SET state='completed'")
    conn.commit()

    assert same_posterior_wave_reentry_suppressed(conn, posterior_run_id="post-current", bucket="33C") is True
    assert same_posterior_wave_reentry_suppressed(conn, posterior_run_id="post-next", bucket="33C") is False
    state = load_top_yes_wave_reentry_state(conn, posterior_run_id="post-current")
    assert state["suppressed_buckets"] == ["33C"]


def test_default_environment_is_fail_closed() -> None:
    result = _commit(_conn())

    assert result["ok"] is False
    assert result["block_reasons"] == ["market_alpha_production_environment_disabled"]


def test_market_alpha_submit_and_reconcile_close_cycle_without_blind_resubmit(monkeypatch) -> None:
    for name in (
        "PM_MARKET_ALPHA_TAKE_PROFIT_PRODUCTION_ENABLED",
        "PM_MARKET_ALPHA_TAKE_PROFIT_LIVE_SUBMIT_ENABLED",
        "PM_DYNAMIC_THREE_LEG_RESIDENT_SUBMITTER_ENABLED",
        "PM_DYNAMIC_THREE_LEG_LIVE_SUBMIT_ENABLED",
    ):
        monkeypatch.setenv(name, "1")
    conn = _conn()
    prepared = _commit(conn)
    order_intent_id = prepared["order_intent_id"]
    idempotency_key = conn.execute(
        "SELECT idempotency_key FROM posterior_execution_intents WHERE order_intent_id=?",
        (order_intent_id,),
    ).fetchone()[0]

    submitted = submit_prepared_dynamic_three_leg_intent(
        conn,
        order_intent_id=order_intent_id,
        submit_as_of="2026-07-14T08:00:00.600Z",
        pre_submit_validator=lambda _intent: {
            "ok": True,
            "position_lot_current": True,
            "market_quote_current": True,
            "balance_sufficient": True,
        },
        gateway_place_order=lambda _payload: {"ok": True, "accepted": True, "orderID": "exchange-1"},
        market_alpha_activation=_activation(),
    )
    assert submitted["state"] == "gateway_accepted"
    assert conn.execute("SELECT state FROM market_alpha_take_profit_cycles").fetchone()[0] == "gateway_accepted"

    reconciled = reconcile_dynamic_three_leg_intent(
        conn,
        order_intent_id=order_intent_id,
        reconcile_as_of="2026-07-14T08:00:00.700Z",
        gateway_lookup_order=lambda _lookup: {
            "ok": True,
            "status": "filled",
            "orderID": "exchange-1",
            "idempotency_key": idempotency_key,
            "token_id": "token-33-yes",
            "original_size": 20.0,
            "size_matched": 20.0,
        },
    )
    assert reconciled["state"] == "filled"
    assert conn.execute("SELECT state FROM market_alpha_take_profit_cycles").fetchone()[0] == "completed"
    assert same_posterior_wave_reentry_suppressed(conn, posterior_run_id="post-current", bucket="33C") is True

    retry = submit_prepared_dynamic_three_leg_intent(
        conn,
        order_intent_id=order_intent_id,
        submit_as_of="2026-07-14T08:00:00.800Z",
        pre_submit_validator=lambda _intent: (_ for _ in ()).throw(AssertionError("must not validate")),
        gateway_place_order=lambda _payload: (_ for _ in ()).throw(AssertionError("must not submit")),
        market_alpha_activation=_activation(),
    )
    assert retry["idempotent"] is True
    assert retry["called_gateway"] is False


def _advance_latest(conn: sqlite3.Connection) -> None:
    conn.execute(
        """INSERT INTO event_fact_history(slug,event_date,fact_snapshot_id,posterior_run_id)
           VALUES(?,?,?,?)""",
        (SLUG, "2026-07-14", "fact-next", "post-next"),
    )
    fact_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "UPDATE event_fact_latest SET fact_id=? WHERE slug=? AND event_date=?",
        (fact_id, SLUG, "2026-07-14"),
    )
    conn.commit()


def test_stale_prepared_market_alpha_is_atomically_superseded_before_new_posterior(monkeypatch) -> None:
    monkeypatch.setenv("PM_MARKET_ALPHA_TAKE_PROFIT_PRODUCTION_ENABLED", "1")
    monkeypatch.setenv("PM_MARKET_ALPHA_TAKE_PROFIT_LIVE_SUBMIT_ENABLED", "1")
    conn = _conn()
    prepared = _commit(conn)
    _advance_latest(conn)

    result = supersede_stale_prepared_market_alpha_intents(
        conn, superseded_at="2026-07-14T08:01:00.000Z",
    )

    assert result["state"] == "superseded"
    assert result["places_orders"] is False
    assert result["superseded_order_intent_ids"] == [prepared["order_intent_id"]]
    assert conn.execute(
        "SELECT state,last_error FROM posterior_execution_intents"
    ).fetchone() == ("cancelled_superseded", SUPERSESSION_REASON)
    assert conn.execute(
        "SELECT decided_state,will_order FROM auto_trade_decisions"
    ).fetchone() == ("cancelled_superseded", 0)
    assert conn.execute("SELECT state FROM market_alpha_take_profit_cycles").fetchone()[0] == "blocked"


def test_current_prepared_market_alpha_is_not_superseded(monkeypatch) -> None:
    monkeypatch.setenv("PM_MARKET_ALPHA_TAKE_PROFIT_PRODUCTION_ENABLED", "1")
    monkeypatch.setenv("PM_MARKET_ALPHA_TAKE_PROFIT_LIVE_SUBMIT_ENABLED", "1")
    conn = _conn()
    _commit(conn)

    result = supersede_stale_prepared_market_alpha_intents(conn, superseded_at=NOW)

    assert result["state"] == "idle_current"
    assert result["wrote_rows"] is False
    assert conn.execute("SELECT state FROM posterior_execution_intents").fetchone()[0] == "prepared"
    assert conn.execute("SELECT state FROM market_alpha_take_profit_cycles").fetchone()[0] == "prepared"


def test_stale_inflight_market_alpha_remains_reconcile_first(monkeypatch) -> None:
    monkeypatch.setenv("PM_MARKET_ALPHA_TAKE_PROFIT_PRODUCTION_ENABLED", "1")
    monkeypatch.setenv("PM_MARKET_ALPHA_TAKE_PROFIT_LIVE_SUBMIT_ENABLED", "1")
    conn = _conn()
    _commit(conn)
    conn.execute("UPDATE posterior_execution_intents SET state='reconcile_required'")
    conn.execute("UPDATE market_alpha_take_profit_cycles SET state='reconcile_required'")
    conn.commit()
    _advance_latest(conn)

    result = supersede_stale_prepared_market_alpha_intents(conn, superseded_at=NOW)

    assert result["state"] == "idle_current"
    assert conn.execute("SELECT state FROM posterior_execution_intents").fetchone()[0] == "reconcile_required"
    assert conn.execute("SELECT state FROM market_alpha_take_profit_cycles").fetchone()[0] == "reconcile_required"

from __future__ import annotations

import json
import plistlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
import scan_weather_market as swm
from sequential_bayes_execution_contract import (
    EXPECTED_ACTIVATION_MODE,
    EXPECTED_COMPOSER_SCHEMA,
    EXPECTED_FORMULA,
)


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolate_archive_writer_queue(monkeypatch):
    monkeypatch.setenv("PM_ARCHIVE_DB_WRITE_QUEUE_ENABLED", "0")


def _setup(tmp_path, monkeypatch, *, size: float = 10.0):
    db = tmp_path / "archive.db"
    # Each test owns an isolated temp DB and must not wait on the production
    # archive writer queue used by the live launchd processes.
    monkeypatch.setenv("PM_ARCHIVE_DB_WRITE_QUEUE_ENABLED", "0")
    monkeypatch.setattr(swm, "DB_PATH", db)
    swm.init_db()
    conn = sqlite3.connect(db)
    conn.execute(
        """
        INSERT INTO edge_trades(
            slug, event_date, outcome, side, state,
            entry_at, entry_price, entry_size, position_size, avg_price, updated_at
        ) VALUES(
            'highest-temperature-in-chengdu-on-july-11-2026',
            '2026-07-11', '33°C', 'NO', 'entry',
            '2026-07-10T00:00:00+00:00', 0.80, ?, ?, 0.80,
            '2026-07-10T00:00:00+00:00'
        )
        """,
        (size, size),
    )
    conn.commit()
    conn.close()
    payload = {
        "target_realization_v3": {
            "execution_phase": "SELL_OLD",
            "plan_id": "trv3-unit",
        },
        "event_payloads": [
            {
                "payload": {
                    "slug": "highest-temperature-in-chengdu-on-july-11-2026",
                    "event_date": "2026-07-11",
                    "title": "Highest temperature in Chengdu on July 11?",
                    "rows": [
                        {
                            "outcome": "33°C",
                            "no_token": "no-33-token",
                            "best_bid_no": 0.79,
                            "best_ask_no": 0.80,
                        }
                    ],
                    "entry_expected_targets": [
                        {
                            "outcome": "33°C",
                            "side": "NO",
                            "held_target_bucket": "33°C",
                            "held_target_side": "NO",
                            "held_token_id": "no-33-token",
                            "exit_bid": 0.79,
                            "target_realization_leg_role": "old_bucket_exit",
                            "target_realization_execution_action": "SELL",
                            "target_realization_plan_action": "sell_old",
                            "target_realization_reason_code": "target_realization_no_invalidated",
                        }
                    ],
                }
            }
        ],
    }
    return db, payload


def test_existing_worker_schema_fast_path_marks_ready_without_writer_init(tmp_path, monkeypatch):
    db, _payload = _setup(tmp_path, monkeypatch)
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE auto_trade_jobs ADD COLUMN order_intent_id TEXT")
    conn.execute("ALTER TABLE auto_trade_decisions ADD COLUMN decision_id TEXT")
    conn.execute("ALTER TABLE auto_trade_decisions ADD COLUMN order_intent_id TEXT")
    conn.commit()
    conn.close()
    cache_key = swm._db_cache_key()
    swm._DB_SCHEMA_READY_KEYS.discard(cache_key)

    assert swm._mark_existing_auto_trade_worker_schema_ready() is True
    assert cache_key in swm._DB_SCHEMA_READY_KEYS
    assert db.exists()


def test_sell_first_partial_fill_keeps_remaining_position_and_blocks_buy(tmp_path, monkeypatch):
    db, payload = _setup(tmp_path, monkeypatch, size=10.0)

    def node_order(command, *_args, **_kwargs):
        if command == "place-order":
            return True, {"orderID": "sell-partial"}
        return True, {}

    monkeypatch.setattr(swm, "_run_node_order", node_order)
    monkeypatch.setattr(
        swm,
        "_run_node_get_order",
        lambda _order_id: (
            True,
            {"order": {"status": "matched", "size_matched": "4", "price": "0.78", "side": "SELL"}},
        ),
    )

    result = swm._direct_target_old_exit_cleanup_after_success(
        payload,
        run_id=None,
        run_date="2026-07-11",
        job_id=1,
        worker_started_at="2026-07-11T00:00:00+00:00",
    )

    assert result["ok"] is False
    assert result["all_filled"] is False
    assert result["partially_filled"] == 1
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT state, position_size, exit_reason_codes FROM edge_trades WHERE outcome='33°C' AND side='NO'"
    ).fetchone()
    lock = conn.execute(
        """
        SELECT state FROM target_realization_execution_locks
        WHERE outcome='33°C' AND side='NO'
          AND lock_type='dynamic_posterior_reselection_sell'
        """
    ).fetchone()
    conn.close()
    assert row[0] == "entry"
    assert row[1] == 6.0
    assert "target_realization_no_invalidated" in row[2]
    assert lock == ("active",)


def test_sell_first_full_fill_zeroes_position_only_after_confirmation(tmp_path, monkeypatch):
    db, payload = _setup(tmp_path, monkeypatch, size=10.0)

    def node_order(command, *_args, **_kwargs):
        if command == "place-order":
            return True, {"orderID": "sell-full"}
        return True, {}

    monkeypatch.setattr(swm, "_run_node_order", node_order)
    monkeypatch.setattr(
        swm,
        "_run_node_get_order",
        lambda _order_id: (
            True,
            {"order": {"status": "matched", "size_matched": "10", "price": "0.78", "side": "SELL"}},
        ),
    )

    result = swm._direct_target_old_exit_cleanup_after_success(
        payload,
        run_id=None,
        run_date="2026-07-11",
        job_id=1,
        worker_started_at="2026-07-11T00:00:00+00:00",
    )

    assert result["ok"] is True
    assert result["all_filled"] is True
    assert result["fully_exited"] == 1
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT state, position_size, exit_reason_codes FROM edge_trades WHERE outcome='33°C' AND side='NO'"
    ).fetchone()
    conn.close()
    assert row[0] == "exit"
    assert row[1] == 0.0


def test_dynamic_sell_lock_and_pending_intent_commit_before_clob_submit(tmp_path, monkeypatch):
    db, payload = _setup(tmp_path, monkeypatch, size=10.0)
    payload["target_realization_v3"]["plan_id"] = "dynamic-revision-1"
    target = payload["event_payloads"][0]["payload"]["entry_expected_targets"][0]
    target["target_realization_v3_plan_id"] = "dynamic-revision-1"
    observed = {}

    def node_order(command, *_args, **_kwargs):
        if command == "place-order":
            conn = sqlite3.connect(db)
            lock = conn.execute(
                """
                SELECT state, plan_id FROM target_realization_execution_locks
                WHERE outcome='33°C' AND side='NO'
                  AND lock_type='dynamic_posterior_reselection_sell'
                """
            ).fetchone()
            intent = conn.execute(
                """
                SELECT json_extract(details_json, '$.order_intent_id') FROM auto_trade_decisions
                WHERE decided_state='pending_submit' ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            conn.close()
            observed["lock_before_submit"] = lock
            observed["intent_before_submit"] = intent
            return True, {"orderID": "dynamic-sell-1"}
        return True, {}

    monkeypatch.setattr(swm, "_run_node_order", node_order)
    monkeypatch.setattr(
        swm,
        "_run_node_get_order",
        lambda _order_id: (True, {"order": {"status": "matched", "size_matched": "10", "price": "0.78"}}),
    )

    result = swm._direct_target_old_exit_cleanup_after_success(
        payload,
        run_id=None,
        run_date="2026-07-11",
        job_id=1,
        worker_started_at="2026-07-11T00:00:00+00:00",
    )

    assert result["all_filled"] is True
    assert observed["lock_before_submit"] == ("pending", "dynamic-revision-1")
    assert observed["intent_before_submit"] and observed["intent_before_submit"][0]
    conn = sqlite3.connect(db)
    final_lock = conn.execute(
        """
        SELECT state, source_order_id, plan_id FROM target_realization_execution_locks
        WHERE outcome='33°C' AND side='NO'
          AND lock_type='dynamic_posterior_reselection_sell'
        """
    ).fetchone()
    conn.close()
    assert final_lock == ("reconciled", "dynamic-sell-1", "dynamic-revision-1")


def test_orphaned_dynamic_sell_lock_fails_closed_without_second_sell(tmp_path, monkeypatch):
    db, payload = _setup(tmp_path, monkeypatch, size=10.0)
    conn = sqlite3.connect(db)
    conn.execute(
        """
        INSERT INTO target_realization_execution_locks(
            slug, event_date, outcome, side, lock_type, state,
            source_order_id, plan_id, locked_at, expires_at, details_json, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, '', 'interrupted-revision', ?, '', '{}', ?)
        """,
        (
            "highest-temperature-in-chengdu-on-july-11-2026",
            "2026-07-11",
            "33°C",
            "NO",
            "dynamic_posterior_reselection_sell",
            "pending",
            "2026-07-11T00:00:00+00:00",
            "2026-07-11T00:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()
    calls = {"sell": 0}

    def node_order(command, *_args, **_kwargs):
        if command == "place-order":
            calls["sell"] += 1
        return True, {"orderID": "must-not-submit"}

    monkeypatch.setattr(swm, "_run_node_order", node_order)
    result = swm._direct_target_old_exit_cleanup_after_success(
        payload,
        run_id=None,
        run_date="2026-07-11",
        job_id=2,
        worker_started_at="2026-07-11T00:00:00+00:00",
    )

    assert result["all_filled"] is False
    assert calls == {"sell": 0}
    assert result["errors"][0]["reason"] == "target_realization_sell_reconcile_locked"


def test_passive_sell_retry_reuses_open_order_and_accounts_only_incremental_fill(
    tmp_path, monkeypatch
):
    db, payload = _setup(tmp_path, monkeypatch, size=10.0)
    place_calls = []

    def node_order(command, *_args, **kwargs):
        if command == "place-order":
            place_calls.append(kwargs)
            return True, {"orderID": "sell-maker-open", "status": "live"}
        return True, {}

    states = iter(
        [
            {"order": {"status": "live", "size_matched": "0", "price": "0.80"}},
            {"order": {"status": "matched", "size_matched": "10", "price": "0.80"}},
        ]
    )
    monkeypatch.setattr(swm, "_run_node_order", node_order)
    monkeypatch.setattr(swm, "_run_node_get_order", lambda _order_id: (True, next(states)))

    first = swm._direct_target_old_exit_cleanup_after_success(
        payload,
        run_id=None,
        run_date="2026-07-11",
        job_id=1,
        worker_started_at="2026-07-11T00:00:00+00:00",
    )
    second = swm._direct_target_old_exit_cleanup_after_success(
        payload,
        run_id=None,
        run_date="2026-07-11",
        job_id=1,
        worker_started_at="2026-07-11T00:00:01+00:00",
    )

    assert first["all_filled"] is False
    assert second["all_filled"] is True
    assert len(place_calls) == 1
    assert place_calls[0]["post_only"] is True
    conn = sqlite3.connect(db)
    position = conn.execute(
        "SELECT state, position_size FROM edge_trades WHERE outcome='33°C' AND side='NO'"
    ).fetchone()
    latest = conn.execute(
        "SELECT details_json FROM auto_trade_decisions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert position == ("exit", 0.0)
    details = json.loads(latest[0])
    assert details["reused_existing_order"] is True
    assert details["matched_size_increment"] == 10.0


def test_v3_gateway_forces_post_only_gtc_and_rejects_passive_price_mismatch(
    monkeypatch
):
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")
    context = {
        "target_realization_v3_plan_id": "trv3-maker",
        "target_realization_maker_only": True,
        "target_realization_price_source": "best_bid",
        "passive_limit_price": 0.50,
        "best_bid": 0.50,
        "best_ask": 0.51,
    }
    ok, payload = swm._run_node_order(
        "place-order",
        "token-maker",
        "BUY",
        price=0.50,
        size=5.0,
        order_type="FAK",
        order_context=context,
    )
    assert ok is True
    assert "--order-type" in payload["cmd"]
    assert payload["cmd"][payload["cmd"].index("--order-type") + 1] == "GTC"
    assert payload["cmd"][payload["cmd"].index("--post-only") + 1] == "true"
    assert payload["post_only"] is True

    ok_mismatch, mismatch = swm._run_node_order(
        "place-order",
        "token-maker",
        "BUY",
        price=0.51,
        size=5.0,
        order_type="GTC",
        order_context=context,
    )
    assert ok_mismatch is False
    assert mismatch["error"] == "target_realization_v3_passive_price_mismatch"


def test_sell_first_rejects_risk_rebalance_as_executable_exit(
    tmp_path, monkeypatch
):
    db, payload = _setup(tmp_path, monkeypatch, size=10.0)
    target = payload["event_payloads"][0]["payload"]["entry_expected_targets"][0]
    target["position_size"] = 4.0
    target["target_realization_reason_code"] = (
        "target_realization_no_single_bucket_cap_rebalance"
    )

    def node_order(command, *_args, **_kwargs):
        if command == "place-order":
            return True, {"orderID": "sell-rebalance"}
        return True, {}

    monkeypatch.setattr(swm, "_run_node_order", node_order)
    monkeypatch.setattr(
        swm,
        "_run_node_get_order",
        lambda _order_id: (
            True,
            {"order": {"status": "matched", "size_matched": "4", "price": "0.78"}},
        ),
    )

    result = swm._direct_target_old_exit_cleanup_after_success(
        payload,
        run_id=None,
        run_date="2026-07-11",
        job_id=1,
        worker_started_at="2026-07-11T00:00:00+00:00",
    )

    assert result["ok"] is False
    assert result["all_filled"] is False
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT state, position_size, exit_reason_codes FROM edge_trades WHERE outcome='33°C' AND side='NO'"
    ).fetchone()
    conn.close()
    assert row[0] == "entry"
    assert row[1] == 10.0


def test_old_top_yes_profit_take_is_not_executable(tmp_path, monkeypatch):
    db, payload = _setup(tmp_path, monkeypatch, size=10.0)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE edge_trades SET side='YES' WHERE outcome='33°C'")
    conn.commit()
    conn.close()
    event_payload = payload["event_payloads"][0]["payload"]
    event_payload["rows"][0].update(
        {"yes_token": "yes-33-token", "best_bid": 0.79, "best_ask": 0.80}
    )
    target = event_payload["entry_expected_targets"][0]
    target.update(
        {
            "side": "YES",
            "held_target_side": "YES",
            "held_token_id": "yes-33-token",
            "target_realization_reason_code": "target_realization_top_yes_profit_lock",
            "target_realization_v3_plan_id": "trv3-profit-lock",
        }
    )

    monkeypatch.setattr(
        swm,
        "_run_node_order",
        lambda command, *_args, **_kwargs: (
            (True, {"orderID": "sell-profit"}) if command == "place-order" else (True, {})
        ),
    )
    monkeypatch.setattr(
        swm,
        "_run_node_get_order",
        lambda _order_id: (
            True,
            {"order": {"status": "matched", "size_matched": "10", "price": "0.78"}},
        ),
    )

    result = swm._direct_target_old_exit_cleanup_after_success(
        payload,
        run_id=None,
        run_date="2026-07-11",
        job_id=1,
        worker_started_at="2026-07-11T00:00:00+00:00",
    )

    assert result["all_filled"] is False
    conn = sqlite3.connect(db)
    lock = conn.execute(
        """
        SELECT lock_type, state, source_order_id, plan_id
        FROM target_realization_execution_locks
        WHERE outcome='33°C' AND side='YES'
        """
    ).fetchone()
    conn.close()
    assert lock is None




def test_retired_recycle_sell_path_has_no_callable() -> None:
    assert not hasattr(swm, "_target_realization_v3_submit_no_recycle_orders")


def _valid_buy_candidate() -> dict:
    return {
        "slug": "highest-temperature-in-chengdu-on-july-11-2026",
        "event_date": "2026-07-11",
        "outcome": "33°C",
        "side": "YES",
        "target_realization_v3_plan_id": "trv3-buy",
        "target_realization_execution_action": "BUY",
        "target_realization_plan_notional": 10.0,
        "target_realization_order_price_cap": 0.60,
        "ask": 0.50,
        "bid": 0.50,
        "best_bid": 0.50,
        "best_ask": 0.51,
        "price": 0.50,
        "limit_price": 0.50,
        "order_type": "GTC",
        "post_only": True,
        "target_realization_maker_only": True,
        "q_entry": 0.70,
        "sequential_bayes_execution_contract": {
            "schema": "sequential_bayes_execution_contract_v1",
            "contract_id": "seq-v3-buy",
            "trade_eligible": True,
            "blockers": [],
            "composer_schema": EXPECTED_COMPOSER_SCHEMA,
            "activation_mode": EXPECTED_ACTIVATION_MODE,
            "formula": EXPECTED_FORMULA,
            "signal_at": "2099-01-01T00:00:00+00:00",
            "signal_to_order_slo_deadline_at": "2099-01-01T00:00:03+00:00",
            "signal_to_order_hard_deadline_at": "2099-01-01T00:00:05+00:00",
            "observation_trigger_at": "2099-01-01T00:00:00+00:00",
            "observation_to_order_slo_deadline_at": "2099-01-01T00:00:03+00:00",
            "observation_to_order_hard_deadline_at": "2099-01-01T00:00:05+00:00",
        },
    }


def test_last_mile_mutual_exclusion_blocks_same_bucket_opposite_position(tmp_path, monkeypatch):
    db, _payload = _setup(tmp_path, monkeypatch, size=10.0)
    candidate = _valid_buy_candidate()

    reason = swm._target_realization_v3_entry_block_reason(candidate)

    assert reason == "target_realization_same_bucket_opposite_side_position_exists"
    conn = sqlite3.connect(db)
    conn.execute("UPDATE edge_trades SET state='exit', position_size=0")
    conn.commit()
    conn.close()
    assert swm._target_realization_v3_entry_block_reason(candidate) == ""


def test_last_mile_mutual_exclusion_covers_pending_order_and_queued_intent(
    tmp_path, monkeypatch
):
    db = tmp_path / "archive.db"
    monkeypatch.setattr(swm, "DB_PATH", db)
    swm.init_db()
    candidate = _valid_buy_candidate()
    conn = sqlite3.connect(db)
    conn.execute(
        """
        INSERT INTO auto_orders(
            order_id,slug,event_date,outcome,side,action,price,size,
            order_type,strategy,created_at,order_status
        ) VALUES('opposite-live',?,?,?,?,? ,0.8,5,'GTC','posterior_settlement',?,'live')
        """,
        (
            candidate["slug"],
            candidate["event_date"],
            candidate["outcome"],
            "NO",
            "entry",
            "2026-07-11T00:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()
    assert (
        swm._target_realization_v3_entry_block_reason(candidate)
        == "target_realization_same_bucket_opposite_side_order_exists"
    )
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM auto_orders")
    conn.execute(
        """
        INSERT INTO auto_trade_jobs(
            run_date,source,state,priority,payload_json,retry_count,max_retry,
            next_retry_at,created_at,updated_at
        ) VALUES('2026-07-11','posterior_target_direct','queued',10000,?,0,3,'',?,?)
        """,
        (
            json.dumps(
                {
                    "entry_expected_targets": [
                        {
                            "slug": candidate["slug"],
                            "event_date": candidate["event_date"],
                            "outcome": candidate["outcome"],
                            "side": "NO",
                            "target_realization_execution_action": "BUY",
                        }
                    ]
                }
            ),
            "2026-07-11T00:00:00+00:00",
            "2026-07-11T00:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()
    assert (
        swm._target_realization_v3_entry_block_reason(candidate)
        == "target_realization_same_bucket_opposite_side_intent_exists"
    )


def test_last_mile_does_not_compare_yes_price_to_posterior_fair_value(tmp_path, monkeypatch):
    db = tmp_path / "archive.db"
    monkeypatch.setattr(swm, "DB_PATH", db)
    swm.init_db()
    candidate = _valid_buy_candidate()
    candidate["bid"] = 0.59
    candidate["best_bid"] = 0.59
    candidate["price"] = 0.59
    candidate["limit_price"] = 0.59
    candidate["q_entry"] = 0.58

    assert swm._target_realization_v3_entry_block_reason(candidate) == ""


def test_last_mile_does_not_apply_retired_buy_price_cap(tmp_path, monkeypatch):
    db = tmp_path / "archive.db"
    monkeypatch.setattr(swm, "DB_PATH", db)
    swm.init_db()
    candidate = _valid_buy_candidate()
    candidate["target_realization_order_price_cap"] = 0.10
    candidate["price"] = 0.80
    candidate["limit_price"] = 0.80

    assert swm._target_realization_v3_entry_block_reason(candidate) == ""


def test_last_mile_active_lock_blocks_reentry_even_when_plan_candidate_is_stale(
    tmp_path, monkeypatch
):
    db = tmp_path / "archive.db"
    monkeypatch.setattr(swm, "DB_PATH", db)
    swm.init_db()
    conn = sqlite3.connect(db)
    conn.execute(
        """
        INSERT INTO target_realization_execution_locks(
            slug, event_date, outcome, side, lock_type, state,
            source_order_id, plan_id, locked_at, expires_at,
            details_json, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "highest-temperature-in-chengdu-on-july-11-2026",
            "2026-07-11",
            "33°C",
            "YES",
                "sell_reconcile",
            "active",
                "sell-reconcile",
            "trv3-old",
            "2026-07-11T06:00:00+00:00",
            "2026-07-11T23:59:59",
            "{}",
            "2026-07-11T06:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()

    assert (
        swm._target_realization_v3_entry_block_reason(_valid_buy_candidate())
            == "target_realization_reentry_locked"
    )


def test_live_worker_claim_filter_never_picks_legacy_job(tmp_path, monkeypatch):
    db = tmp_path / "archive.db"
    monkeypatch.setattr(swm, "DB_PATH", db)
    swm.init_db()
    conn = sqlite3.connect(db)
    rows = [
        (
            "2026-07-11",
            "legacy",
            "queued",
            20000,
            json.dumps({"entry_expected_targets": [{"outcome": "33°C", "side": "YES"}]}),
            "2026-07-11T00:00:00+00:00",
        ),
        (
            "2026-07-11",
            "posterior_target_direct",
            "queued",
            10000,
            json.dumps(
                {
                    "target_realization_v3": {
                        "plan_version": "strong_posterior_three_leg_strategy_v2",
                        "plan_id": "trv3-only",
                        "execution_phase": "BUY_NEW",
                    }
                }
            ),
            "2026-07-11T00:00:01+00:00",
        ),
    ]
    for run_date, source, state, priority, payload_json, created_at in rows:
        conn.execute(
            """
            INSERT INTO auto_trade_jobs(
                run_date, source, state, priority, payload_json,
                retry_count, max_retry, next_retry_at, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, 0, 3, '', ?, ?)
            """,
            (run_date, source, state, priority, payload_json, created_at, created_at),
        )
    conn.commit()
    legacy_id, v3_id = [row[0] for row in conn.execute("SELECT id FROM auto_trade_jobs ORDER BY id")]
    conn.close()
    monkeypatch.setenv("PM_AUTO_TRADE_JOB_CLAIM_ONLY_TARGET_REALIZATION_V3", "1")

    assert swm._auto_trade_job_claim_one(requested_job_id=legacy_id) is None
    picked = swm._auto_trade_job_claim_one()
    assert picked is not None
    assert picked["id"] == v3_id




def test_worker_rejects_unknown_v3_phase_without_orders_or_general_scan(tmp_path, monkeypatch):
    db = tmp_path / "archive.db"
    monkeypatch.setattr(swm, "DB_PATH", db)
    monkeypatch.setattr(swm, "DATA_DIR", tmp_path)
    swm.init_db()
    payload = {
        "exit_only": True,
        "target_realization_v3": {
            "plan_version": "strong_posterior_three_leg_strategy_v2",
            "plan_id": "trv3-unknown-phase",
            "execution_phase": "REMOVED_LEGACY_PHASE",
            "slug": "highest-temperature-in-chengdu-on-july-11-2026",
            "event_date": "2026-07-11",
        },
    }
    conn = sqlite3.connect(db)
    conn.execute(
        """
        INSERT INTO auto_trade_jobs(
            run_date, source, state, priority, payload_json,
            retry_count, max_retry, next_retry_at, created_at, updated_at
        ) VALUES('2026-07-11', 'posterior_target_direct', 'queued', 10000, ?,
                 0, 3, '', '2026-07-11T00:00:00+00:00', '2026-07-11T00:00:00+00:00')
        """,
        (json.dumps(payload),),
    )
    job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    seen = {"general": 0}

    def general(*_args, **_kwargs):
        seen["general"] += 1
        raise AssertionError("general trade scan must not run for an unknown v3 phase")

    monkeypatch.setattr(swm, "run_auto_trades_for_date", general)
    monkeypatch.setenv("PM_AUTO_TRADE_ENABLED", "1")
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")
    monkeypatch.setenv("PM_AUTO_TRADE_JOB_CLAIM_ONLY_TARGET_REALIZATION_V3", "1")

    result = swm.run_auto_trade_job_worker_once(requested_job_id=job_id)

    assert result["ok"] is False
    assert result["result"].get("status") == "target_realization_v3_unknown_execution_phase", result
    assert result["result"]["places_orders"] is False
    assert seen == {"general": 0}


def test_sell_fill_refresh_enqueues_buy_phase_inside_three_second_target(tmp_path, monkeypatch):
    monkeypatch.setattr(swm, "DB_PATH", tmp_path / "archive.db")
    captured = {}

    def run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["timeout"] = kwargs["timeout"]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "status": "recorded",
                    "job_id": 77,
                    "job_enqueued": True,
                    "target_realization_v3_plan_id": "trv3-refreshed",
                    "target_realization_v3_execution_phase": "BUY_NEW",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(swm.subprocess, "run", run)
    monkeypatch.setenv("PM_TARGET_REALIZATION_V3_FILL_REFRESH_TIMEOUT_SEC", "2.0")
    transition_at = "2026-07-11T06:00:00.000+00:00"
    result = swm._target_realization_v3_refresh_after_sell_fill(
        {
            "target_realization_v3": {
                "slug": "highest-temperature-in-chengdu-on-july-11-2026",
                "event_date": "2026-07-11",
            }
        },
        transition_signal_at=transition_at,
    )

    assert result["ok"] is True
    assert result["job_enqueued"] is True
    assert result["job_id"] == 77
    assert result["target_realization_v3_execution_phase"] == "BUY_NEW"
    assert result["elapsed_ms"] < 3000.0
    assert captured["timeout"] == 2.0
    assert "--transition-signal-at" in captured["cmd"]
    assert transition_at in captured["cmd"]


def test_sell_worker_immediately_chains_refreshed_buy_job(tmp_path, monkeypatch):
    db = tmp_path / "archive.db"
    monkeypatch.setattr(swm, "DB_PATH", db)
    monkeypatch.setattr(swm, "DATA_DIR", tmp_path)
    swm.init_db()
    slug = "highest-temperature-in-chengdu-on-july-11-2026"
    sell_payload = {
        "exit_only": True,
        "target_realization_v3": {
            "plan_version": "strong_posterior_three_leg_strategy_v2",
            "plan_id": "trv3-sell",
            "execution_phase": "SELL_OLD",
            "slug": slug,
            "event_date": "2026-07-11",
        },
        "posterior_target_direct": {"slug": slug, "event_date": "2026-07-11"},
        "entry_expected_targets": [
            {
                "outcome": "31°C",
                "side": "NO",
                "target_realization_v3_execution_phase": "SELL_OLD",
                "target_realization_execution_action": "SELL",
            }
        ],
    }
    buy_payload = {
        "target_realization_v3": {
            "plan_version": "strong_posterior_three_leg_strategy_v2",
            "plan_id": "trv3-buy",
            "execution_phase": "BUY_NEW",
            "slug": slug,
            "event_date": "2026-07-11",
        },
        "posterior_target_direct": {"slug": slug, "event_date": "2026-07-11"},
        "entry_expected_targets": [
            {
                "slug": slug,
                "event_date": "2026-07-11",
                "outcome": "33°C",
                "side": "YES",
                "target_realization_v3_plan_id": "trv3-buy",
                "target_realization_v3_execution_phase": "BUY_NEW",
                "target_realization_execution_action": "BUY",
            }
        ],
    }
    conn = sqlite3.connect(db)
    for priority, payload in ((20000, sell_payload), (19000, buy_payload)):
        conn.execute(
            """
            INSERT INTO auto_trade_jobs(
                run_date, source, state, priority, payload_json,
                retry_count, max_retry, next_retry_at, created_at, updated_at
            ) VALUES('2026-07-11', 'posterior_target_direct', 'queued', ?, ?,
                     0, 3, '', '2026-07-11T00:00:00+00:00', '2026-07-11T00:00:00+00:00')
            """,
            (priority, json.dumps(payload)),
        )
    sell_job_id, buy_job_id = [
        row[0] for row in conn.execute("SELECT id FROM auto_trade_jobs ORDER BY id")
    ]
    conn.commit()
    conn.close()
    seen = {"buy": 0}
    monkeypatch.setattr(
        swm,
        "_direct_target_old_exit_cleanup_after_success",
        lambda *_args, **_kwargs: {
            "ok": True,
            "status": "checked",
            "submitted": 1,
            "fully_exited": 1,
            "partially_filled": 0,
            "skipped": 0,
            "errors": [],
            "unfilled": [],
            "all_filled": True,
        },
    )
    monkeypatch.setattr(
        swm,
        "_target_realization_v3_refresh_after_sell_fill",
        lambda *_args, **_kwargs: {
            "ok": True,
            "status": "recorded",
            "job_id": buy_job_id,
            "job_enqueued": True,
            "elapsed_ms": 10.0,
        },
    )
    monkeypatch.setattr(swm, "_record_direct_target_already_active_job_decisions", lambda **_kwargs: {"ok": False})
    monkeypatch.setattr(swm, "_refresh_auto_trade_config", lambda: None)

    def run_buy(*_args, **_kwargs):
        seen["buy"] += 1
        return {"ok": True, "processed": 1, "skipped": 0}

    monkeypatch.setattr(swm, "run_auto_trades_for_date", run_buy)
    monkeypatch.setenv("PM_AUTO_TRADE_ENABLED", "1")
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")
    monkeypatch.setenv("PM_AUTO_TRADE_JOB_CLAIM_ONLY_TARGET_REALIZATION_V3", "1")
    monkeypatch.setenv("PM_TARGET_REALIZATION_V3_CHAIN_BUY_WORKER_INLINE", "1")

    result = swm.run_auto_trade_job_worker_once(requested_job_id=sell_job_id)

    assert result["ok"] is True
    assert result["target_realization_v3_chained_buy_worker"]["ok"] is True
    assert seen["buy"] == 1
    conn = sqlite3.connect(db)
    states = dict(conn.execute("SELECT id,state FROM auto_trade_jobs"))
    conn.close()
    assert states[sell_job_id] == "success"
    assert states[buy_job_id] == "success"

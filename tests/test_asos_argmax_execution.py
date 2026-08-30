from __future__ import annotations

import json
import sqlite3

import pytest

import asos_argmax_execution as execution
from weather_basket_yes_schema import apply_weather_basket_yes_schema


def _seed_registry(conn: sqlite3.Connection, *tokens: str) -> None:
    """State the exchange's minimum lot for each token the lane may buy.

    Sizing reads it since 2026-08-30, so a fixture without it is not a
    smaller fixture -- it is a market this lane is not allowed to size
    against.
    """
    for index, token in enumerate(tokens):
        conn.execute(
            """INSERT INTO reported_trajectory_market_registry(
                 slug,event_date,outcome,city_slug,station_id,settlement_timezone,
                 settlement_unit,source_lane,source_eligible,source_block_reason,
                 resolution_url,rules_sha256,yes_token,no_token,market_active,
                 market_accepting_orders,registry_json,refreshed_at
               ) VALUES(?,'2026-08-05',?,'atlanta','KATL','America/New_York',
                        'F','awc',1,NULL,'url',?,?,?,1,1,?,
                        '2026-08-05T00:00:00Z')""",
            (
                f"slug-{token}", f"bucket-{index}", "a" * 64, token, f"no-{token}",
                json.dumps({"order_min_size": 5, "order_price_min_tick_size": 0.01}),
            ),
        )
    conn.commit()


def _rotation_fixture(*, new_token: str = "222222") -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_weather_basket_yes_schema(conn)
    _seed_registry(conn, "111111", "222222", new_token)
    for entry_id, observation_id, observed, prior, high, token, outcome, created in (
        ("old-entry", "old-obs", "2026-08-05T18:00:00Z", 25, 26, "111111", "78-79°F", "2026-08-05T18:00:01Z"),
        ("new-entry", "new-obs", "2026-08-05T18:05:00Z", 26, 27, new_token, "80-81°F", "2026-08-05T18:05:01Z"),
    ):
        conn.execute(
            """INSERT INTO reported_trajectory_asos_argmax_entry_facts(
                 entry_fact_id,station_id,event_date,source_lane,observation_id,
                 observed_at_utc,received_at_utc,prior_high_c,new_high_c,raw_sha256,
                 source_receipt_sha256,window_id,argmax_window_start_utc,market_slug,
                 outcome,yes_token,settlement_scalar,settlement_unit,reason,
                 content_sha256,gateway_submitted,created_at_utc
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)""",
            (
                entry_id, "KATL", "2026-08-05", "madis_hfmetar", observation_id,
                observed, created, prior, high, "a" * 64, "b" * 64, "window",
                "2026-08-05T17:00:00Z", f"slug-{entry_id}", outcome, token,
                int(outcome[:2]), "F", "ASOS_NEW_HIGH_RECEIVED_DURING_ARGMAX_PERIOD",
                ("c" if entry_id == "old-entry" else "d") * 64, created,
            ),
        )
    conn.execute(
        """INSERT INTO reported_trajectory_asos_argmax_order_intents(
             order_intent_id,entry_fact_id,strategy_id,station_id,event_date,yes_token,
             side,order_type,limit_price,size,requested_notional,idempotency_key,
             ws_snapshot_sha256,guard_snapshot_sha256,guard_snapshot_json,state,
             gateway_request_json,gateway_response_json,exchange_order_id,size_matched,
             created_at_utc,updated_at_utc
           ) VALUES('old-buy','old-entry','asos_argmax_new_high_yes_v1','KATL','2026-08-05',
                    '111111','BUY','FOK',0.25,8,2,'old-buy-idem',?,?,'{}',
                    'EXCHANGE_ACCEPTED','{}','{}','old-buy-order',8,
                    '2026-08-05T18:00:01Z','2026-08-05T18:00:01Z')""",
        ("e" * 64, "f" * 64),
    )
    conn.execute(
        """INSERT INTO reported_trajectory_asos_argmax_exit_order_intents(
             exit_order_intent_id,buy_order_intent_id,entry_fact_id,strategy_id,
             station_id,event_date,yes_token,side,order_type,limit_price,size,
             source_buy_size_matched,idempotency_key,guard_snapshot_sha256,state,
             exchange_order_id,created_at_utc,updated_at_utc
           ) VALUES('old-exit','old-buy','old-entry','asos_argmax_yes_exit_099_v1',
                    'KATL','2026-08-05','111111','SELL','GTC',0.99,8,8,
                    'old-exit-idem',?,'EXCHANGE_ACCEPTED','old-exit-order',
                    '2026-08-05T18:00:02Z','2026-08-05T18:00:02Z')""",
        ("9" * 64,),
    )
    conn.commit()
    return conn


# An intent that really was cleared to submit.  This is the default so that a
# gateway which cannot account for it keeps its budget reserved -- the order may
# be resting on the exchange.
AUTHORISED_GUARD = '{"authorizes_order":true,"evidence_only":false}'


def _conn(guard_snapshot_json: str = AUTHORISED_GUARD) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE reported_trajectory_asos_argmax_order_intents(
             order_intent_id TEXT PRIMARY KEY,
             entry_fact_id TEXT NOT NULL,
             idempotency_key TEXT NOT NULL,
             size REAL NOT NULL,
             state TEXT NOT NULL,
             guard_snapshot_json TEXT NOT NULL,
             gateway_response_json TEXT NOT NULL,
             exchange_order_id TEXT,
             size_matched REAL,
             updated_at_utc TEXT NOT NULL
           )"""
    )
    conn.execute(
        """INSERT INTO reported_trajectory_asos_argmax_order_intents
             VALUES('intent','entry','idem',4,'RECONCILE_REQUIRED',?,'{}',NULL,
                    NULL,'2026-08-05T00:00:00Z')""",
        (guard_snapshot_json,),
    )
    return conn


def test_reconcile_accepts_only_terminal_exchange_receipt(monkeypatch) -> None:
    conn = _conn()
    response = {
        "ok": True, "orderID": "exchange-order", "status": "matched",
        "size_matched": 4.0, "reconcile_required": False,
    }
    monkeypatch.setattr(execution, "_post_json", lambda *_args, **_kwargs: response)

    result = execution._reconcile_pending_argmax_orders(conn)

    row = conn.execute(
        "SELECT state,exchange_order_id,size_matched,gateway_response_json FROM reported_trajectory_asos_argmax_order_intents"
    ).fetchone()
    assert row[:3] == ("EXCHANGE_ACCEPTED", "exchange-order", 4.0)
    assert json.loads(row[3]) == response
    assert result[0]["state"] == "EXCHANGE_ACCEPTED"


def test_reconcile_partial_market_buy_remains_reconcile_required(monkeypatch) -> None:
    conn = _conn()
    response = {
        "ok": True, "orderID": "exchange-order", "status": "partial",
        "size_matched": 2.0, "reconcile_required": False,
    }
    monkeypatch.setattr(execution, "_post_json", lambda *_args, **_kwargs: response)

    result = execution._reconcile_pending_argmax_orders(conn)

    assert conn.execute(
        "SELECT state FROM reported_trajectory_asos_argmax_order_intents"
    ).fetchone()[0] == "RECONCILE_REQUIRED"
    assert result[0]["state"] == "RECONCILE_REQUIRED"


def test_market_buy_sweep_uses_multiple_ask_levels_in_one_protected_fok(
    monkeypatch, tmp_path,
) -> None:
    monkeypatch.setattr(
        execution, "_official_ws_quote",
        lambda *_args, **_kwargs: ("a" * 64, {
            "book_observed_at": "2026-08-05T18:05:01Z",
            "asks": [
                {"price": 0.20, "size": 3.0},
                {"price": 0.25, "size": 10.0},
            ],
        }),
    )

    sweep = execution._official_ws_buy_sweep(
        "222222", budget=2.0, root=tmp_path, min_order_size=5.0)

    assert sweep["limit_price"] == 0.25
    assert sweep["notional"] == 2.0
    assert sweep["size"] == 8.0
    assert sweep["consumed_asks"] == [
        {"price": 0.20, "size": 3.0},
        {"price": 0.25, "size": 5.0},
    ]

    # The same book and the same budget, refused for the one reason the lane
    # could not see before 2026-08-30: eight shares is a legal order against a
    # five-share lot and not against a ten-share one.  The message says which
    # of the two constraints bit, because depth is the market's problem and
    # the lot is this lane's 2.0 USDC cap meeting the price.
    with pytest.raises(ValueError, match="market_buy_below_exchange_minimum_lot"):
        execution._official_ws_buy_sweep(
            "222222", budget=2.0, root=tmp_path, min_order_size=10.0)

    # And the boundary is inclusive.
    exact = execution._official_ws_buy_sweep(
        "222222", budget=2.0, root=tmp_path, min_order_size=8.0)
    assert exact["size"] == 8.0


def test_reconcile_releases_only_explicit_not_filled_receipt(monkeypatch) -> None:
    conn = _conn()
    response = {
        "ok": False, "accepted": False, "pending": False,
        "reconcile_attempted": True, "reconcile_required": False,
        "error": "exchange_order_not_filled",
    }
    monkeypatch.setattr(execution, "_post_json", lambda *_args, **_kwargs: response)

    execution._reconcile_pending_argmax_orders(conn)

    assert conn.execute(
        "SELECT state FROM reported_trajectory_asos_argmax_order_intents"
    ).fetchone()[0] == "RELEASED"


def test_reconcile_keeps_budget_reserved_when_identity_is_unknown(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(
        execution, "_post_json",
        lambda *_args, **_kwargs: {"ok": False, "found": False, "pending": False},
    )

    execution._reconcile_pending_argmax_orders(conn)

    assert conn.execute(
        "SELECT state FROM reported_trajectory_asos_argmax_order_intents"
    ).fetchone()[0] == "RECONCILE_REQUIRED"


def test_reconcile_releases_order_the_gateway_refused_before_the_exchange(
    monkeypatch,
) -> None:
    """A refusal that never reached the exchange must free its budget.

    Geoblocking answers this way: HTTP 403 before the order is even signed.
    ``RECONCILE_REQUIRED`` counts against the per-station allowance and no other
    terminal branch matches, so three stations lost their entire allowance to
    orders that provably never left the machine.
    """

    conn = _conn()
    response = {
        "ok": False, "found": True, "pending": False,
        "error_class": "clob_order_geoblocked", "http_status": 403,
        "geoblocked": True, "fatal": True, "retryable": False,
        "exchange_post_started": False, "signed_order_prepared": False,
    }
    monkeypatch.setattr(execution, "_post_json", lambda *_a, **_k: response)

    result = execution._reconcile_pending_argmax_orders(conn)

    assert conn.execute(
        "SELECT state FROM reported_trajectory_asos_argmax_order_intents"
    ).fetchone()[0] == "RELEASED"
    assert result[0]["state"] == "RELEASED"


@pytest.mark.parametrize(
    "response",
    [
        # Unknown key: the gateway may have lost a record of an order that did
        # go out, so this stays reserved.
        {"ok": False, "found": False, "pending": False},
        # The exchange call started -- an order may be resting.
        {"found": True, "fatal": True, "exchange_post_started": True},
        # Retryable, so not yet terminal.
        {
            "found": True, "fatal": True,
            "exchange_post_started": False, "retryable": True,
        },
        # Not declared fatal.
        {"found": True, "fatal": False, "exchange_post_started": False},
        # Carries an exchange identifier.
        {
            "found": True, "fatal": True,
            "exchange_post_started": False, "orderID": "exchange-order",
        },
        # Still working.
        {
            "found": True, "pending": True, "fatal": True,
            "exchange_post_started": False,
        },
        # Silence is not a refusal.
        {},
    ],
)
def test_reconcile_holds_budget_unless_gateway_asserts_no_exchange_call(
    monkeypatch, response,
) -> None:
    """Only the gateway's own terminal denial releases budget.

    Each clause is load-bearing: absent any one of them the order may still be
    live on the exchange, and releasing its budget would let a second order be
    placed against the same allowance.
    """

    conn = _conn()
    monkeypatch.setattr(execution, "_post_json", lambda *_a, **_k: response)

    execution._reconcile_pending_argmax_orders(conn)

    assert conn.execute(
        "SELECT state FROM reported_trajectory_asos_argmax_order_intents"
    ).fetchone()[0] == "RECONCILE_REQUIRED"


def test_new_high_flows_to_exact_fok_intent_and_exchange_receipt(monkeypatch, tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    apply_weather_basket_yes_schema(conn)
    conn.execute(
        """INSERT INTO reported_trajectory_asos_argmax_entry_facts(
             entry_fact_id,station_id,event_date,source_lane,observation_id,
             observed_at_utc,received_at_utc,prior_high_c,new_high_c,raw_sha256,
             source_receipt_sha256,window_id,argmax_window_start_utc,market_slug,
             outcome,yes_token,settlement_scalar,settlement_unit,reason,
             content_sha256,gateway_submitted,created_at_utc
           ) VALUES('entry','KATL','2026-08-05','madis_hfmetar','obs',
                    '2026-08-05T13:30:00Z','2026-08-05T13:30:01Z',23,24,?,?,
                    'window','2026-08-05T18:00:00Z','slug','76-77°F','123456',76,'F',
                    'ASOS_NEW_HIGH_RECEIVED_DURING_ARGMAX_PERIOD',?,0,
                    '2026-08-05T13:30:01Z')""",
        ("a" * 64, "b" * 64, "c" * 64),
    )
    conn.commit()
    _seed_registry(conn, "123456")
    monkeypatch.setattr(
        execution, "_official_ws_buy_sweep",
        lambda *_args, **_kwargs: {
            "snapshot_sha256": "d" * 64,
            "limit_price": 0.25, "size": 8.0, "notional": 2.0,
            "consumed_asks": [
                {"price": 0.20, "size": 3.0},
                {"price": 0.25, "size": 5.0},
            ],
            "book_observed_at": "2026-08-05T13:30:01Z",
        },
    )
    calls = []

    def post(path, payload, **_kwargs):
        calls.append((path, payload))
        if path == "/execution-guard-snapshot":
            if payload["side"] == "SELL":
                return {
                    "ok": True, "side": "SELL", "evidence_only": True,
                    "authorizes_order": False, "evidence_errors": [],
                    "conditional_balance_allowance": {
                        "balance": "8000000", "allowance": "8000000",
                    },
                }
            return {"ok": True, "evidence_errors": [], "order_book": {
                "asks": [
                    {"price": 0.20, "size": 3.0},
                    {"price": 0.25, "size": 5.0},
                ],
            }}
        assert path == "/place-order"
        if payload["side"] == "SELL":
            return {
                "ok": True, "orderID": "exit-order", "status": "live",
                "size_matched": 0.0,
            }
        return {
            "ok": True, "orderID": "exchange-order", "status": "filled",
            "size_matched": 8.0,
        }

    monkeypatch.setattr(execution, "_post_json", post)

    result = execution.process_pending_argmax_orders(conn, root=tmp_path)

    intent = conn.execute(
        """SELECT state,side,order_type,limit_price,size,requested_notional,
                  exchange_order_id,size_matched
             FROM reported_trajectory_asos_argmax_order_intents"""
    ).fetchone()
    assert intent == ("EXCHANGE_ACCEPTED", "BUY", "FOK", 0.25, 8.0, 2.0, "exchange-order", 8.0)
    exit_intent = conn.execute(
        """SELECT state,side,order_type,limit_price,size,source_buy_size_matched,
                  exchange_order_id
             FROM reported_trajectory_asos_argmax_exit_order_intents"""
    ).fetchone()
    assert exit_intent == (
        "EXCHANGE_ACCEPTED", "SELL", "GTC", 0.99, 8.0, 8.0, "exit-order",
    )
    assert [path for path, _payload in calls] == [
        "/execution-guard-snapshot", "/place-order",
        "/execution-guard-snapshot", "/place-order",
    ]
    sell_payload = calls[-1][1]
    assert sell_payload["side"] == "SELL"
    assert sell_payload["order_type"] == "GTC"
    assert sell_payload["price"] == 0.99
    assert sell_payload["size"] == 8.0
    assert sell_payload["post_only"] is False
    assert result["submission_attempt_count"] == 2
    assert result["buy_submission_attempt_count"] == 1
    assert result["exit_submission_attempt_count"] == 1
    assert result["gateway_call_count"] == 4


def test_exit_waits_for_exact_conditional_balance_without_guessing(monkeypatch, tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    apply_weather_basket_yes_schema(conn)
    conn.execute(
        """INSERT INTO reported_trajectory_asos_argmax_entry_facts(
             entry_fact_id,station_id,event_date,source_lane,observation_id,
             observed_at_utc,received_at_utc,prior_high_c,new_high_c,raw_sha256,
             source_receipt_sha256,window_id,argmax_window_start_utc,market_slug,
             outcome,yes_token,settlement_scalar,settlement_unit,reason,
             content_sha256,gateway_submitted,created_at_utc
           ) VALUES('entry','KATL','2026-08-05','madis_hfmetar','obs',
                    '2026-08-05T13:30:00Z','2026-08-05T13:30:01Z',23,24,?, ?,
                    'window','2026-08-05T13:00:00Z','slug','76-77°F','123456',76,'F',
                    'ASOS_NEW_HIGH_RECEIVED_DURING_ARGMAX_PERIOD',?,0,
                    '2026-08-05T13:30:01Z')""",
        ("a" * 64, "b" * 64, "c" * 64),
    )
    conn.execute(
        """INSERT INTO reported_trajectory_asos_argmax_order_intents(
             order_intent_id,entry_fact_id,strategy_id,station_id,event_date,yes_token,
             side,order_type,limit_price,size,requested_notional,idempotency_key,
             ws_snapshot_sha256,guard_snapshot_sha256,guard_snapshot_json,state,
             gateway_request_json,gateway_response_json,exchange_order_id,size_matched,
             created_at_utc,updated_at_utc
           ) VALUES('buy','entry','asos_argmax_new_high_yes_v1','KATL','2026-08-05',
                    '123456','BUY','FOK',0.25,8,2,'idem',?,?,'{}','EXCHANGE_ACCEPTED',
                    '{}','{}','exchange',8,'2026-08-05T13:30:01Z','2026-08-05T13:30:01Z')""",
        ("d" * 64, "e" * 64),
    )
    conn.commit()
    calls = []

    def post(path, payload, **_kwargs):
        calls.append((path, payload))
        assert path == "/execution-guard-snapshot"
        return {
            "ok": True, "side": "SELL", "evidence_only": True,
            "authorizes_order": False, "evidence_errors": [],
            "conditional_balance_allowance": {
                "balance": "7999998", "allowance": "7999998",
            },
        }

    monkeypatch.setattr(execution, "_post_json", post)
    result = execution._process_prepared_argmax_exits(conn)

    assert [path for path, _payload in calls] == ["/execution-guard-snapshot"]
    assert conn.execute(
        "SELECT state FROM reported_trajectory_asos_argmax_exit_order_intents"
    ).fetchone()[0] == "PREPARED"
    assert result["submission_attempt_count"] == 0


def test_cross_bucket_new_high_sells_old_position_before_buying_new_bucket(
    monkeypatch, tmp_path,
) -> None:
    conn = _rotation_fixture()
    monkeypatch.setattr(
        execution, "_official_ws_sell_sweep",
        lambda *_args, **_kwargs: {
            "snapshot_sha256": "6" * 64, "limit_price": 0.18, "size": 8.0,
            "consumed_bids": [{"price": 0.20, "size": 4.0}, {"price": 0.18, "size": 4.0}],
            "book_observed_at": "2026-08-05T18:05:01Z",
        },
    )
    monkeypatch.setattr(
        execution, "_official_ws_buy_sweep",
        lambda *_args, **_kwargs: {
            "snapshot_sha256": "7" * 64,
            "limit_price": 0.25, "size": 8.0, "notional": 2.0,
            "consumed_asks": [{"price": 0.25, "size": 8.0}],
            "book_observed_at": "2026-08-05T18:05:01Z",
        },
    )
    calls = []

    def post(path, payload, **_kwargs):
        calls.append((path, dict(payload)))
        if path == "/lookup-exchange-order":
            if len([item for item in calls if item[0] == path]) == 1:
                return {"ok": True, "status": "live", "orderID": "old-exit-order"}
            return {"ok": True, "status": "cancelled", "orderID": "old-exit-order"}
        if path == "/cancel-order":
            return {"ok": True, "cancelled": True, "orderID": "old-exit-order"}
        if path == "/execution-guard-snapshot" and payload["side"] == "SELL":
            return {
                "ok": True, "side": "SELL", "evidence_only": True,
                "authorizes_order": False, "evidence_errors": [],
                "conditional_balance_allowance": {
                    "balance": "8000000", "allowance": "8000000",
                },
            }
        if path == "/execution-guard-snapshot":
            return {"ok": True, "evidence_errors": [], "order_book": {
                "asks": [{"price": 0.25, "size": 8.0}],
            }}
        if path == "/place-order" and payload["strategy_id"] == execution.ROTATION_SELL_STRATEGY_ID:
            return {
                "ok": True, "orderID": "rotation-sell-order", "status": "filled",
                "shares_matched": 8.0, "exchange_post_started": True,
            }
        if path == "/place-order" and payload["side"] == "BUY":
            return {
                "ok": True, "orderID": "new-buy-order", "status": "filled",
                "size_matched": 8.0,
            }
        if path == "/place-order":
            return {"ok": True, "orderID": "new-exit-order", "status": "live", "size_matched": 0.0}
        raise AssertionError((path, payload))

    monkeypatch.setattr(execution, "_post_json", post)
    result = execution.process_pending_argmax_orders(conn, root=tmp_path)

    action_order = [
        (path, payload.get("side"), payload.get("strategy_id"))
        for path, payload in calls if path in {"/cancel-order", "/place-order"}
    ]
    assert action_order == [
        ("/cancel-order", None, None),
        ("/place-order", "SELL", execution.ROTATION_SELL_STRATEGY_ID),
        ("/place-order", "BUY", execution.STRATEGY_ID),
        ("/place-order", "SELL", execution.EXIT_STRATEGY_ID),
    ]
    assert conn.execute(
        "SELECT state,shares_matched FROM reported_trajectory_asos_argmax_rotation_sell_intents"
    ).fetchone() == ("FILLED", 8.0)
    assert conn.execute(
        "SELECT state FROM reported_trajectory_asos_argmax_exit_order_intents WHERE exit_order_intent_id='old-exit'"
    ).fetchone()[0] == "RELEASED"
    assert conn.execute(
        "SELECT yes_token,state FROM reported_trajectory_asos_argmax_order_intents WHERE entry_fact_id='new-entry'"
    ).fetchone() == ("222222", "EXCHANGE_ACCEPTED")
    assert result["rotation_sell_submission_attempt_count"] == 1
    assert result["buy_submission_attempt_count"] == 1


def test_same_bucket_new_high_does_not_repeat_buy(monkeypatch, tmp_path) -> None:
    conn = _rotation_fixture(new_token="111111")
    calls = []
    monkeypatch.setattr(execution, "_post_json", lambda *args, **kwargs: calls.append((args, kwargs)))

    result = execution.process_pending_argmax_orders(conn, root=tmp_path)

    assert calls == []
    assert result["results"][0]["state"] == "same_bucket_no_new_buy"
    assert conn.execute(
        "SELECT COUNT(*) FROM reported_trajectory_asos_argmax_order_intents"
    ).fetchone()[0] == 1


def test_rotation_cancel_unknown_blocks_sell_and_new_buy(monkeypatch, tmp_path) -> None:
    conn = _rotation_fixture()
    calls = []

    def post(path, payload, **_kwargs):
        calls.append(path)
        if path == "/lookup-exchange-order":
            return {"ok": True, "status": "live"}
        if path == "/cancel-order":
            return {"ok": False, "cancelled": False}
        raise AssertionError(path)

    monkeypatch.setattr(execution, "_post_json", post)
    result = execution.process_pending_argmax_orders(conn, root=tmp_path)

    assert calls == ["/lookup-exchange-order", "/cancel-order", "/reconcile-order"]
    assert result["results"][0]["state"] == "rotation_blocked_before_new_buy"
    assert conn.execute(
        "SELECT state FROM reported_trajectory_asos_argmax_exit_order_intents WHERE exit_order_intent_id='old-exit'"
    ).fetchone()[0] == "RECONCILE_REQUIRED"
    assert conn.execute(
        "SELECT COUNT(*) FROM reported_trajectory_asos_argmax_order_intents WHERE entry_fact_id='new-entry'"
    ).fetchone()[0] == 0


def test_rotation_partial_sell_is_reconcile_required_and_blocks_new_buy(
    monkeypatch, tmp_path,
) -> None:
    conn = _rotation_fixture()
    conn.execute(
        "UPDATE reported_trajectory_asos_argmax_exit_order_intents SET state='RELEASED',exchange_order_id=NULL"
    )
    conn.commit()
    monkeypatch.setattr(
        execution, "_official_ws_sell_sweep",
        lambda *_args, **_kwargs: {
            "snapshot_sha256": "6" * 64, "limit_price": 0.18, "size": 8.0,
            "consumed_bids": [{"price": 0.18, "size": 8.0}],
            "book_observed_at": "2026-08-05T18:05:01Z",
        },
    )
    calls = []

    def post(path, payload, **_kwargs):
        calls.append((path, payload.get("side")))
        if path == "/execution-guard-snapshot":
            return {
                "ok": True, "side": "SELL", "evidence_only": True,
                "authorizes_order": False, "evidence_errors": [],
                "conditional_balance_allowance": {
                    "balance": "8000000", "allowance": "8000000",
                },
            }
        if path == "/place-order":
            return {
                "ok": True, "orderID": "partial-sell", "status": "partial",
                "shares_matched": 4.0, "exchange_post_started": True,
            }
        raise AssertionError(path)

    monkeypatch.setattr(execution, "_post_json", post)
    result = execution.process_pending_argmax_orders(conn, root=tmp_path)

    assert calls == [
        ("/execution-guard-snapshot", "SELL"),
        ("/execution-guard-snapshot", "SELL"),
        ("/place-order", "SELL"),
    ]
    assert conn.execute(
        "SELECT state,shares_matched FROM reported_trajectory_asos_argmax_rotation_sell_intents"
    ).fetchone() == ("RECONCILE_REQUIRED", 4.0)
    assert result["results"][0]["state"] == "rotation_blocked_before_new_buy"
    assert conn.execute(
        "SELECT COUNT(*) FROM reported_trajectory_asos_argmax_order_intents WHERE entry_fact_id='new-entry'"
    ).fetchone()[0] == 0


def test_rotation_unknown_balance_is_persisted_for_reconciliation(monkeypatch, tmp_path) -> None:
    conn = _rotation_fixture()
    conn.execute(
        "UPDATE reported_trajectory_asos_argmax_exit_order_intents SET state='RELEASED',exchange_order_id=NULL"
    )
    conn.commit()
    monkeypatch.setattr(
        execution, "_post_json",
        lambda *_args, **_kwargs: {
            "ok": False, "side": "SELL", "evidence_only": True,
            "authorizes_order": False, "evidence_errors": ["balance_unknown"],
        },
    )

    result = execution.process_pending_argmax_orders(conn, root=tmp_path)

    assert result["results"][0]["state"] == "rotation_blocked_before_new_buy"
    assert conn.execute(
        "SELECT state FROM reported_trajectory_asos_argmax_rotation_sell_intents"
    ).fetchone()[0] == "RECONCILE_REQUIRED"
    assert conn.execute(
        "SELECT COUNT(*) FROM reported_trajectory_asos_argmax_order_intents WHERE entry_fact_id='new-entry'"
    ).fetchone()[0] == 0


def test_rotation_insufficient_ws_bid_depth_blocks_without_buying(monkeypatch, tmp_path) -> None:
    conn = _rotation_fixture()
    conn.execute(
        "UPDATE reported_trajectory_asos_argmax_exit_order_intents SET state='RELEASED',exchange_order_id=NULL"
    )
    conn.commit()
    monkeypatch.setattr(
        execution, "_post_json",
        lambda *_args, **_kwargs: {
            "ok": True, "side": "SELL", "evidence_only": True,
            "authorizes_order": False, "evidence_errors": [],
            "conditional_balance_allowance": {
                "balance": "8000000", "allowance": "8000000",
            },
        },
    )
    monkeypatch.setattr(
        execution, "_official_ws_sell_sweep",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("rotation_sell_ws_bid_depth_insufficient")
        ),
    )

    result = execution.process_pending_argmax_orders(conn, root=tmp_path)

    assert result["results"][0]["state"] == "rotation_blocked_before_new_buy"
    assert conn.execute(
        "SELECT COUNT(*) FROM reported_trajectory_asos_argmax_rotation_sell_intents"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM reported_trajectory_asos_argmax_order_intents WHERE entry_fact_id='new-entry'"
    ).fetchone()[0] == 0


def test_exchange_min_lot_is_read_per_token_and_fails_closed() -> None:
    """No rule means no order, not an order with no floor.

    The collector blocks a market that will not state ``orderMinSize``, so an
    absent value here is a registry row written before 2026-08-30.  Sizing
    against it reproduces the intents this lane spent 31 attempts on without
    ever receiving an exchange order id.
    """
    conn = sqlite3.connect(":memory:")
    apply_weather_basket_yes_schema(conn)
    _seed_registry(conn, "123456")
    assert execution._exchange_min_lot(conn, "123456") == 5.0

    with pytest.raises(ValueError, match="exchange_order_sizing_market_unknown"):
        execution._exchange_min_lot(conn, "999999")

    for label, value in (
        ("absent", {}),
        ("null", {"order_min_size": None}),
        ("zero", {"order_min_size": 0}),
        ("negative", {"order_min_size": -5}),
        ("unparseable", {"order_min_size": "five"}),
    ):
        conn.execute(
            "UPDATE reported_trajectory_market_registry SET registry_json=?"
            " WHERE yes_token='123456'",
            (json.dumps(value),),
        )
        with pytest.raises(ValueError, match="exchange_order_sizing_rule_missing"):
            assert label and execution._exchange_min_lot(conn, "123456")

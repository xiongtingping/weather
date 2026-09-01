"""The delivery link between a prepared intent and the reconciliation ledger.

Both sides of this existed from the start and nothing joined them, so
``basket_target_order_events`` was empty for the whole life of the lane.  These
tests pin the join: what gets sent, what each reply means, and what the ledger
looks like afterwards.
"""

from __future__ import annotations

import sqlite3

import pytest

from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.execution_reconciliation import record_order_event
from bayes_basket_target.order_submission import (
    BASKET_STRATEGY_ID,
    OrderSubmissionError,
    build_place_order_payload,
    classify_submission_response,
    submit_prepared_intents,
)
from bayes_basket_target.ssot import apply_production_ssot_schema
from bayes_basket_target import order_submission as _submission


@pytest.fixture(autouse=True)
def _armed(tmp_path, monkeypatch):
    """Raise the operator's arming flag for the tests that assert a send.

    The flag is an orthogonal gate added 2026-09-01 (a lowered flag holds
    every send, and is read fresh per cycle).  Without this the suite would
    pass or fail on whatever the live config happens to say, which is both
    non-hermetic and a silent rewrite of what these tests assert.  The flag's
    own behaviour is covered in test_live_intent_kill_switch.
    """

    path = tmp_path / "intent.json"
    path.write_text('{"live_enabled": true}', encoding="utf-8")
    monkeypatch.setattr(_submission, "LIVE_INTENT_PATH", path)
    return path


OBSERVED = "2026-08-11T09:30:00Z"


def _intent(**overrides) -> dict:
    return {
        "order_intent_id": "intent-a",
        "bucket": "35",
        "token_id": "token-35",
        "side": "YES",
        "order_action": "BUY",
        "limit_price": 0.12,
        "requested_size": 40.0,
        "idempotency_key": "idem-a",
        **overrides,
    }


def _conn(intents: list[dict]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    for index, intent in enumerate(intents):
        conn.execute(
            "INSERT INTO basket_target_order_intents("
            "order_intent_id,decision_id,cycle_id,bucket,token_id,side,"
            "order_action,limit_price,requested_size,requested_notional_usdc,"
            "state,live_submit_authorized,idempotency_key,intent_json,"
            "intent_sha256,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                intent["order_intent_id"], "decision-1", "cycle-1",
                intent["bucket"], intent["token_id"], intent["side"],
                intent["order_action"], intent["limit_price"],
                intent["requested_size"],
                float(intent["limit_price"]) * float(intent["requested_size"]),
                intent.get("state", "prepared"), 0, intent["idempotency_key"],
                "{}", f"{index:064d}", OBSERVED,
            ),
        )
    return conn


def _accepted(_path, _payload) -> dict:
    return {
        "ok": True, "accepted": True, "pending": False,
        "reconcile_required": False, "exchange_post_started": True,
        "exchange_order_id": "exch-1", "source": "clob_order_service",
    }


def _read_only(_path, _payload) -> dict:
    return {
        "ok": False, "accepted": False, "pending": False,
        "reconcile_required": False, "exchange_post_started": False,
        "error": "clob_order_service_read_only", "source": "clob_order_service",
    }


def test_payload_matches_the_order_service_request_contract() -> None:
    payload = build_place_order_payload(_intent())

    assert payload == {
        "strategy_id": BASKET_STRATEGY_ID,
        "order_intent_id": "intent-a",
        "idempotency_key": "idem-a",
        "token_id": "token-35",
        "outcome_side": "YES",
        "side": "BUY",
        "price": 0.12,
        "size": 40.0,
        "order_type": "GTC",
    }


@pytest.mark.parametrize("price", [0.0, 1.0, 1.5, -0.1])
def test_a_price_outside_the_open_unit_interval_is_refused(price: float) -> None:
    with pytest.raises(OrderSubmissionError, match="limit_price_invalid"):
        build_place_order_payload(_intent(limit_price=price))


def test_an_accepted_reply_is_a_post_ack() -> None:
    outcome = classify_submission_response(_accepted(None, None))

    assert outcome["event_type"] == "POST_ACK"
    assert outcome["submitted"] is True


def test_a_reply_that_reached_the_exchange_is_unknown_not_refused() -> None:
    # The request left the process and the reply will not say whether the
    # exchange took it.  Calling that a refusal is how the same order gets
    # sent twice, so it must block the bucket instead.
    outcome = classify_submission_response({
        "ok": False, "accepted": False, "pending": False,
        "reconcile_required": True, "exchange_post_started": True,
        "error": "post_timeout",
    })

    assert outcome["event_type"] == "UNKNOWN_POST"
    assert outcome["submitted"] is True


def test_a_read_only_refusal_never_reached_the_exchange() -> None:
    outcome = classify_submission_response(_read_only(None, None))

    assert outcome["submitted"] is False
    assert outcome["event_type"] == ""
    assert outcome["reason"] == "clob_order_service_read_only"


def test_an_authorized_submission_closes_the_chain_to_reconciliation() -> None:
    conn = _conn([_intent()])
    with conn:
        receipt = submit_prepared_intents(
            conn,
            cycle_id="cycle-1",
            transport=_accepted,
            live_submit_authorized=True,
            observed_at_utc=OBSERVED,
        )

    assert receipt["orders_submitted"] == 1
    assert receipt["submissions"][0]["event_type"] == "POST_ACK"
    # The two tables that were empty for the life of the lane.
    event = conn.execute(
        "SELECT order_intent_id,event_type,exchange_order_id "
        "FROM basket_target_order_events"
    ).fetchall()
    assert event == [("intent-a", "POST_ACK", "exch-1")]
    recon = conn.execute(
        "SELECT order_intent_id,state FROM basket_target_order_reconciliations"
    ).fetchall()
    assert recon == [("intent-a", "pending")]
    assert conn.execute(
        "SELECT state FROM basket_target_order_intents "
        "WHERE order_intent_id='intent-a'"
    ).fetchone()[0] == "submitted"


def test_an_unauthorized_run_walks_the_path_without_sending() -> None:
    def _forbidden(_path, _payload):
        raise AssertionError("transport must not be called without authority")

    conn = _conn([_intent()])
    with conn:
        receipt = submit_prepared_intents(
            conn,
            cycle_id="cycle-1",
            transport=_forbidden,
            live_submit_authorized=False,
            observed_at_utc=OBSERVED,
        )

    assert receipt["orders_submitted"] == 0
    assert receipt["calls_order_gateway"] is False
    row = receipt["submissions"][0]
    assert row["state"] == "not_authorized"
    # The payload it would have sent is hashed onto the receipt, so a no-live
    # run can be compared against the live one it stands in for.
    assert row["payload_sha256"] == content_sha256(
        build_place_order_payload(_intent())
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_target_order_events"
    ).fetchone()[0] == 0


def test_a_read_only_service_leaves_the_intent_prepared() -> None:
    conn = _conn([_intent()])
    with conn:
        receipt = submit_prepared_intents(
            conn,
            cycle_id="cycle-1",
            transport=_read_only,
            live_submit_authorized=True,
            observed_at_utc=OBSERVED,
        )

    assert receipt["orders_submitted"] == 0
    assert receipt["submissions"][0]["state"] == "refused"
    assert conn.execute(
        "SELECT state FROM basket_target_order_intents "
        "WHERE order_intent_id='intent-a'"
    ).fetchone()[0] == "prepared"


def test_an_unresolved_sibling_blocks_the_same_bucket_and_token() -> None:
    conn = _conn([_intent(), _intent(
        order_intent_id="intent-b", idempotency_key="idem-b"
    )])
    with conn:
        record_order_event(
            conn,
            order_intent_id="intent-a",
            event={
                "event_type": "UNKNOWN_POST",
                "observed_at": OBSERVED,
                "matched_size": 0.0,
                "matched_notional_usdc": 0.0,
                "event_payload": {},
            },
        )
        receipt = submit_prepared_intents(
            conn,
            cycle_id="cycle-1",
            transport=_accepted,
            live_submit_authorized=True,
            observed_at_utc=OBSERVED,
        )

    assert receipt["orders_submitted"] == 0
    blocked = receipt["submissions"][0]
    assert blocked["order_intent_id"] == "intent-b"
    assert blocked["state"] == "blocked"
    assert blocked["exchange_post_started"] is False


def test_the_production_plan_withholds_delivery_by_default() -> None:
    # Authority is a parameter now, but the default is withheld: wiring the
    # submitter in must not by itself turn a no-live lane into a live one.
    import inspect

    from bayes_basket_target.production import compile_production_plan

    signature = inspect.signature(compile_production_plan)

    assert signature.parameters["live_submit_authorized"].default is False
    assert signature.parameters["order_transport"].default is None


def test_authority_without_a_transport_fails_rather_than_reads_as_refused() -> None:
    # The absence of a way to reach the exchange is a caller error.  Letting it
    # fall through would record every order as refused by the market, which is
    # a different and much quieter kind of wrong.
    import pytest as _pytest

    from bayes_basket_target.contracts import ContractError
    from bayes_basket_target.production import _no_transport

    with _pytest.raises(
        ContractError, match="production_order_transport_missing"
    ):
        _no_transport("/place-order", {})

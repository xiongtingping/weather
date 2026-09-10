"""The delivery link between a prepared intent and the reconciliation ledger.

Both sides of this existed from the start and nothing joined them, so
``basket_target_order_events`` was empty for the whole life of the lane.  These
tests pin the join: what gets sent, what each reply means, and what the ledger
looks like afterwards.
"""

from __future__ import annotations

import json
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


@pytest.fixture(autouse=True)
def _admitted(monkeypatch):
    """Admit the station, for the same reason the flag above is raised.

    The station gate is the other orthogonal control on this path (2026-09-02:
    barred, held and unproven stations are refused before the send).  It reads
    the live config and the book, so leaving it live here would make this suite
    pass or fail on whichever stations happen to be barred today -- and these
    tests are about the delivery link, not about who may trade.  The gate's own
    behaviour is covered in test_live_station_exclusions.
    """

    monkeypatch.setattr(
        _submission, "live_station_admission",
        lambda station, **_kw: {
            "admitted": True, "reason": "live_station_admitted",
            "station": station, "scope": "test",
        },
    )


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
        # What the compile records about the family it chose.  Charter 94
        # makes every entry a resting rung, and the payload has to carry
        # that or the exchange is free to fill it by crossing.  Since
        # 2026-09-11 the family also fixes the order type and the expiry: an
        # entry is GTD, post-only, and dies at its own horizon.
        "execution_candidate": "MAKER",
        "post_only": True,
        "order_type": "GTD",
        "expiration": 1789088400,
        **overrides,
    }


def _intent_json(intent: dict) -> str:
    """The document production mints beside the columns, for this intent."""

    body = {
        "schema": "bayes_basket_production_candidate_intent_v1",
        "candidate_will_order": True,
        "execution_candidate": intent.get("execution_candidate", "MAKER"),
        "post_only": intent.get("post_only", True),
        "order_type": intent.get("order_type", "GTD"),
        "expiration": intent.get("expiration", 1789088400),
        "fok_price_cap": intent.get("fok_price_cap"),
    }
    return json.dumps(
        {key: value for key, value in body.items() if value is not None}
    )


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
                _intent_json(intent), f"{index:064d}", OBSERVED,
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
        "order_type": "GTD",
        "post_only": True,
        "expiration": 1789088400,
    }


def test_the_maker_family_is_sent_as_an_order_that_may_only_rest() -> None:
    """Charter 94's rung is a promise about how the order may fill.

    The compile wrote ``post_only`` on every maker intent from the day the
    lane was minted and the payload dropped it, so the gateway read its own
    default -- unset, therefore off -- and 2,336 intents went out as
    ordinary limit orders.  The rung is priced under the ask so it rested
    almost every time; between the compile and the post there is a
    three-second window in which a falling ask can reach it, and a fill
    there is a crossing buy at a price the seat was never sized on.
    """

    payload = build_place_order_payload(_intent())

    assert payload["post_only"] is True
    assert payload["order_type"] == "GTD"
    assert payload["expiration"] == 1789088400


def _taker(**overrides) -> dict:
    """An authorized immediate take: FOK, crossing, with a stated cap."""

    fields = {
        "execution_candidate": "TAKER_FOK",
        "post_only": False,
        "order_type": "FOK",
        "expiration": None,
        "fok_price_cap": 0.15,
    }
    fields.update(overrides)
    return _intent(**fields)


def test_the_taker_family_is_sent_as_an_order_that_may_cross() -> None:
    """The binding is read, not assumed: the FOK arm says the opposite."""

    payload = build_place_order_payload(_taker())

    assert payload["post_only"] is False
    assert payload["order_type"] == "FOK"
    assert payload["fok_price_cap"] == 0.15
    assert "expiration" not in payload


def test_a_family_without_the_flag_falls_back_to_what_it_says_it_is() -> None:
    payload = build_place_order_payload(_taker(post_only=None))

    assert payload["post_only"] is False


def test_an_intent_that_states_no_family_at_all_is_refused() -> None:
    """Two guesses, opposite orders: refuse rather than pick one."""

    with pytest.raises(OrderSubmissionError, match="post_only_missing"):
        build_place_order_payload(
            _intent(execution_candidate=None, post_only=None, order_type=None)
        )


def test_the_expiry_is_the_belief_s_own_end_not_a_wall_clock_horizon() -> None:
    """Charter 127(二) as ruled 2026-09-11: tie it to the model's run.

    A wall-clock hour was the first attempt and this book's own numbers
    refuse it: of the 122 rungs that filled since go-live, 30% took longer
    than an hour, and an expiry is not a decision moment -- the signature
    reads the model identity, the day's admissibility and what the chain
    HOLDS -- so an expired rung is not re-posted until the model moves
    anyway.  The served belief already carries its end: the run cycle for
    P0 and P0-M, the observation window for P1.  Expiring there makes the
    lapse and the recompute one event.
    """

    from bayes_basket_target.production import _venue_order_fields

    fields = _venue_order_fields(
        "MAKER",
        created_at_utc="2026-09-11T00:00:00Z",
        limit_price=0.12,
        belief_fresh_until_utc="2026-09-11T05:30:00Z",
    )

    assert fields["order_type"] == "GTD"
    assert fields["expiration_basis"] == "belief_fresh_until"
    assert fields["expiration"] == 1789104600

    # A belief with seconds left would mint an order the venue refuses, so
    # the floor is the matching engine's own grace and it says which it used.
    expiring = _venue_order_fields(
        "MAKER",
        created_at_utc="2026-09-11T00:00:00Z",
        limit_price=0.12,
        belief_fresh_until_utc="2026-09-11T00:00:20Z",
    )
    assert expiring["expiration_basis"] == "venue_minimum_horizon"

    # And a call with no served freshness says the horizon is a fallback
    # rather than the rule.
    assert _venue_order_fields(
        "MAKER", created_at_utc="2026-09-11T00:00:00Z", limit_price=0.12,
    )["expiration_basis"] == "fallback_horizon"


def test_a_resting_entry_without_an_expiry_is_refused() -> None:
    """GTD without an expiry is a GTC wearing the wrong name.

    Nothing downstream can catch it: the type travels in the POST and the
    expiry in the signature, so an order missing one is accepted by both
    sides and simply never expires.
    """

    with pytest.raises(OrderSubmissionError, match="expiration_invalid"):
        build_place_order_payload(_intent(expiration=None))


def test_a_take_that_drifted_past_its_authorization_is_refused() -> None:
    """The cap is what authorized the take; the limit may not exceed it."""

    with pytest.raises(
        OrderSubmissionError, match="limit_price_exceeds_fok_price_cap"
    ):
        build_place_order_payload(_taker(fok_price_cap=0.10))


def test_an_order_type_that_contradicts_the_family_is_refused() -> None:
    """A GTC on a MAKER row is what this lane sent for its whole life."""

    with pytest.raises(
        OrderSubmissionError,
        match="order_type_contradicts_execution_intent",
    ):
        build_place_order_payload(_intent(order_type="GTC"))


def test_a_resting_family_that_asks_to_cross_is_refused() -> None:
    """The flag and the family disagreeing is not a flag question."""

    with pytest.raises(
        OrderSubmissionError,
        match="post_only_contradicts_execution_intent",
    ):
        build_place_order_payload(_intent(post_only=False))


@pytest.mark.parametrize("price", [0.0, 1.0, 1.5, -0.1])
def test_a_price_outside_the_open_unit_interval_is_refused(price: float) -> None:
    with pytest.raises(OrderSubmissionError, match="limit_price_invalid"):
        build_place_order_payload(_intent(limit_price=price))


def test_an_accepted_reply_is_a_post_ack() -> None:
    outcome = classify_submission_response(_accepted(None, None))

    assert outcome["event_type"] == "POST_ACK"
    assert outcome["submitted"] is True


def test_a_fill_or_kill_the_exchange_did_not_match_is_over() -> None:
    """A killed FOK is terminal, and read as a POST_ACK it was immortal.

    The reply is identical to a resting order's: taken, with an id, nothing
    matched.  For a family that cannot rest there is no fill still to come,
    so the intent waited at ``pending`` and the sequential gate held that
    bucket shut behind it until a sweep retired the intent.
    """

    outcome = classify_submission_response(
        _accepted(None, None), order_type="FOK",
    )

    assert outcome["event_type"] == "EXPIRED"
    assert outcome["reason"] == "fok_killed"
    assert outcome["submitted"] is True


def test_the_same_reply_on_a_resting_order_is_still_a_post_ack() -> None:
    outcome = classify_submission_response(
        _accepted(None, None), order_type="GTD",
    )

    assert outcome["event_type"] == "POST_ACK"


def _matched(_path=None, _payload=None) -> dict:
    """The gateway's synchronous success reply, as it is actually shaped.

    No ``accepted`` flag: ``ok`` plus the exchange's ``orderID``, ``status``
    and matched quantities.  Recorded from the first live fills, 2026-09-05.
    """

    return {
        "ok": True, "orderID": "0xfill", "expected_order_id": "0xfill",
        "expected_order_id_matches": True, "status": "matched",
        "original_size": 40, "size_matched": "40", "shares_matched": "40",
        "exchange_response": {
            "errorMsg": "", "orderID": "0xfill", "takingAmount": "40",
            "makingAmount": "4.8", "status": "matched", "success": True,
        },
        "exchange_post_started": True, "source": "clob_order_service",
    }


def test_a_matched_reply_is_a_fill_with_the_exchanges_size_and_cost() -> None:
    # The gateway never spelled ``accepted`` on its success reply, so the
    # first five live fills were classified UNKNOWN_POST and sat in
    # reconcile_required, which the order-state producer reads as a
    # non-quiescent book.  ``ok`` plus an orderID is an acceptance, and a
    # matched one is a FILL carrying what the exchange said it did.
    outcome = classify_submission_response(_matched())

    assert outcome["event_type"] == "FILL"
    assert outcome["submitted"] is True
    assert outcome["fill"]["exchange_order_id"] == "0xfill"
    assert outcome["fill"]["shares_matched"] == 40.0
    assert outcome["fill"]["making_amount"] == 4.8


def test_an_ok_reply_not_yet_matched_is_a_post_ack() -> None:
    reply = {**_matched(), "status": "live", "size_matched": "0",
             "shares_matched": "0", "exchange_response": {"status": "live"}}

    outcome = classify_submission_response(reply)

    assert outcome["event_type"] == "POST_ACK"
    assert outcome["submitted"] is True


def test_a_filled_submission_is_recorded_as_filled_not_unknown() -> None:
    conn = _conn([_intent()])
    with conn:
        receipt = submit_prepared_intents(
            conn,
            cycle_id="cycle-1",
            transport=_matched,
            live_submit_authorized=True,
            observed_at_utc=OBSERVED,
        )

    assert receipt["orders_submitted"] == 1
    assert receipt["submissions"][0]["event_type"] == "FILL"
    event = conn.execute(
        "SELECT event_type,exchange_order_id,matched_size,matched_notional_usdc "
        "FROM basket_target_order_events"
    ).fetchall()
    assert event == [("FILL", "0xfill", 40.0, 4.8)]
    assert conn.execute(
        "SELECT state,filled_size FROM basket_target_order_reconciliations"
    ).fetchall() == [("resolved_filled", 40.0)]
    assert conn.execute(
        "SELECT state FROM basket_target_order_intents "
        "WHERE order_intent_id='intent-a'"
    ).fetchone()[0] == "filled"


def test_reconciliation_recovers_a_fill_the_submit_pass_missed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reconcile_required intent whose stored gateway reply is a match is closed as filled.

    This is the 2026-09-05 10:11Z state of the book: five orders matched on
    the exchange, five UNKNOWN_POST rows in the ledger, and a reconcile lane
    that only knew how to close "never reached".
    """

    import clob_order_service_client
    from bayes_basket_target import production_worker

    conn = _conn([_intent()])
    with conn:
        record_order_event(
            conn,
            order_intent_id="intent-a",
            event={
                "event_type": "UNKNOWN_POST", "exchange_order_id": None,
                "observed_at": OBSERVED, "matched_size": 0.0,
                "matched_notional_usdc": 0.0,
                "event_payload": {"reason": "exchange_state_unknown"},
            },
        )
    assert conn.execute(
        "SELECT state FROM basket_target_order_intents"
    ).fetchone()[0] == "reconcile_required"

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def _post(self, path, payload):
            assert path == "/reconcile-order"
            assert payload == {"idempotency_key": "idem-a"}
            return {**_matched(), "found": True, "idempotent_replay": True}

    monkeypatch.setattr(clob_order_service_client, "ClobOrderServiceClient", _Client)
    worker = production_worker.TargetProductionWorker.__new__(
        production_worker.TargetProductionWorker
    )
    worker.commit_conn = conn

    receipt = worker._reconcile_unresolved_intents(as_of_utc="2026-08-11T09:40:00Z")

    assert receipt["examined"] == 1
    assert receipt["closed"] == 1
    assert receipt["filled"] == 1
    assert conn.execute(
        "SELECT state FROM basket_target_order_intents"
    ).fetchone()[0] == "filled"
    assert conn.execute(
        "SELECT state,exchange_order_id,filled_size,filled_notional_usdc "
        "FROM basket_target_order_reconciliations"
    ).fetchall() == [("resolved_filled", "0xfill", 40.0, 4.8)]


def test_reconciliation_leaves_a_resting_order_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import clob_order_service_client
    from bayes_basket_target import production_worker

    conn = _conn([_intent()])
    with conn:
        record_order_event(
            conn,
            order_intent_id="intent-a",
            event={
                "event_type": "UNKNOWN_POST", "exchange_order_id": None,
                "observed_at": OBSERVED, "matched_size": 0.0,
                "matched_notional_usdc": 0.0, "event_payload": {},
            },
        )

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def _post(self, path, payload):
            return {**_matched(), "status": "live", "size_matched": "0",
                    "shares_matched": "0",
                    "exchange_response": {"status": "live"}}

        # Charter 110's two facts, which the lane now asks whenever the
        # point lookup has not produced a verdict (2026-09-08).  Here they
        # agree with it: the order is live on the token, so a balance
        # cannot say what it did and nothing may be concluded.
        def lookup_open_orders(self, request):
            return {"ok": True, "complete": True,
                    "open_orders": [{"id": "0xfill"}], "total_open_orders": 1}

        def lookup_position(self, request):
            return {"ok": True, "shares": 0.0, "balance_raw": "0",
                    "decimals": 6}

    monkeypatch.setattr(clob_order_service_client, "ClobOrderServiceClient", _Client)
    worker = production_worker.TargetProductionWorker.__new__(
        production_worker.TargetProductionWorker
    )
    worker.commit_conn = conn

    receipt = worker._reconcile_unresolved_intents(as_of_utc="2026-08-11T09:40:00Z")

    assert receipt["closed"] == 0
    assert receipt["still_unresolved"] == 1
    assert conn.execute(
        "SELECT state FROM basket_target_order_intents"
    ).fetchone()[0] == "reconcile_required"


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
    """An unambiguous refusal is terminal, and it is an event.

    This asserted ``event_type == ""`` until 2026-09-01: a refusal wrote
    nothing to the ledger, so the reason it was refused existed only in a
    return value nobody stored.  Two of the five live intents ever minted
    died in this branch and their cause is now unrecoverable.
    """

    outcome = classify_submission_response(_read_only(None, None))

    assert outcome["submitted"] is False
    assert outcome["event_type"] == "SUBMIT_REFUSED"
    assert outcome["reason"] == "clob_order_service_read_only"


def test_a_transport_that_never_answered_is_unknown_not_refused() -> None:
    """A timeout may have placed an order; only an answer can deny it.

    The client waits 15s while the gateway's own hop to the exchange can
    take longer, so silence is not a refusal.  On 2026-08-31 an order
    placed under exactly that silence rested on the account for eighteen
    minutes while every local table said nothing had been sent.  The
    transport now flags its own ambiguity instead of letting the caller
    infer "refused" from the absence of the success flags -- absence is
    precisely what a timeout and a refusal have in common.
    """

    outcome = classify_submission_response({
        "ok": False,
        "error": "gateway_transport:TimeoutError",
        "transport_ambiguous": True,
    })

    assert outcome["submitted"] is True
    assert outcome["event_type"] == "UNKNOWN_POST"
    assert outcome["reason"] == "gateway_transport:TimeoutError"


def test_a_gateway_5xx_is_ambiguous_and_a_4xx_is_a_refusal() -> None:
    """The gateway failing after it had the request is not a denial.

    A 5xx says the gateway broke while holding the order and may already
    have forwarded it; a 4xx says it declined to take it.  Only the second
    supports a terminal "not placed".
    """

    server_error = classify_submission_response({
        "ok": False, "http_status": 502,
        "error": "gateway_http_error", "transport_ambiguous": True,
    })
    client_error = classify_submission_response({
        "ok": False, "http_status": 400,
        "error": "gateway_http_error", "transport_ambiguous": False,
    })

    assert server_error["event_type"] == "UNKNOWN_POST"
    assert server_error["submitted"] is True
    assert client_error["event_type"] == "SUBMIT_REFUSED"
    assert client_error["submitted"] is False


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


def test_the_flag_travels_from_the_stored_row_to_the_wire() -> None:
    """End to end, because the break was in the seam and not in either end.

    ``production`` wrote ``post_only`` onto the intent's JSON; the submit
    walk selected the exchange's own columns and never that document, so the
    flag existed in the database and could not reach the payload.  A unit
    test on the builder alone would have passed throughout.
    """

    sent: list[dict] = []

    def _capture(path, payload):
        sent.append(dict(payload))
        return _accepted(path, payload)

    conn = _conn([_intent()])
    with conn:
        submit_prepared_intents(
            conn,
            cycle_id="cycle-1",
            transport=_capture,
            live_submit_authorized=True,
            observed_at_utc=OBSERVED,
        )

    assert [item["post_only"] for item in sent] == [True]


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


def test_a_read_only_service_fails_the_intent_and_frees_its_bucket() -> None:
    """A refused intent must not sit at ``prepared`` holding its bucket.

    This asserted the opposite until 2026-09-01, and the opposite is the
    defect: ``can_submit_next_order`` counts a prepared intent as pending,
    so a refused leg shut its own bucket and token until the stranded-
    intent sweep retired it -- at which point the board showed a
    cancellation whose stated cause was the sweep, not the refusal.  The
    refusal is authoritative about one thing, that no order was placed, so
    it is terminal and the bucket is free immediately.
    """

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
    submission = receipt["submissions"][0]
    assert submission["state"] == "failed"
    assert submission["event_type"] == "SUBMIT_REFUSED"
    assert submission["exchange_post_started"] is False
    assert conn.execute(
        "SELECT state FROM basket_target_order_intents "
        "WHERE order_intent_id='intent-a'"
    ).fetchone()[0] == "failed"
    # The cause is now in the ledger rather than in a discarded return value.
    kind, payload = conn.execute(
        "SELECT event_type, event_json FROM basket_target_order_events"
        " WHERE order_intent_id='intent-a'"
    ).fetchone()
    assert kind == "SUBMIT_REFUSED"
    assert "clob_order_service_read_only" in payload
    assert conn.execute(
        "SELECT state FROM basket_target_order_reconciliations"
        " WHERE order_intent_id='intent-a'"
    ).fetchone()[0] == "resolved_not_placed"


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


def test_a_live_intent_the_station_gate_stops_is_recorded_not_stranded(monkeypatch) -> None:
    """VILK, EPWA and MMMX on 2026-09-05: nine live legs refused at the gate
    (held, barred) sat ``prepared`` until the stranded sweep retired them,
    and the board showed cancellations with no cause.  A person's stop is an
    outcome: the intent ends ``failed`` with the reason on its event."""

    def _forbidden(_path, _payload):
        raise AssertionError("transport must not be called for a held station")

    monkeypatch.setattr(
        _submission, "live_station_admission",
        lambda station, **_kw: {
            "admitted": False, "reason": "live_station_held",
            "station": station, "scope": "test",
        },
    )
    conn = _conn([_intent()])
    with conn:
        conn.execute(
            "INSERT INTO basket_target_execution_cycles(cycle_id,plan_id,"
            "station_id,event_date,settlement_timezone,strategy_lane,plan_sha256,"
            "requested_reserve_usdc,state,created_at,updated_at) VALUES("
            "'cycle-1','plan-1','VILK','2026-09-06','Asia/Kolkata','basket',?,"
            "4.15,'prepared',?,?)",
            ("0" * 64, OBSERVED, OBSERVED),
        )
        receipt = submit_prepared_intents(
            conn,
            cycle_id="cycle-1",
            transport=_forbidden,
            live_submit_authorized=True,
            observed_at_utc=OBSERVED,
        )

    assert receipt["orders_submitted"] == 0
    row = receipt["submissions"][0]
    assert row["state"] == "not_authorized"
    assert row["reason"] == "live_station_held"
    assert row["event_type"] == "NEVER_REACHED_EXCHANGE"
    assert row["intent_state"] == "failed"
    assert row["reconciliation_state"] == "resolved_not_placed"
    import json as _json

    event = conn.execute(
        "SELECT event_type, event_json FROM basket_target_order_events"
    ).fetchone()
    assert event[0] == "NEVER_REACHED_EXCHANGE"
    assert _json.loads(event[1])["reason"] == "live_station_held"
    assert conn.execute(
        "SELECT state FROM basket_target_order_intents WHERE order_intent_id='intent-a'"
    ).fetchone()[0] == "failed"
    # The bucket is free for the next cycle: nothing is pending on it.
    from bayes_basket_target.execution_reconciliation import can_submit_next_order
    assert can_submit_next_order(
        conn, bucket=_intent()["bucket"], token_id=_intent()["token_id"]
    )["allowed"] is True


# --- a fill within one venue quantum is the order, and the reconciler's
# --- answer is cumulative ------------------------------------------------

def test_a_fill_one_quantum_short_of_the_request_is_the_whole_order() -> None:
    """KAUS 98-99°F, 2026-09-05 19:49Z: 5 requested, 4.99 matched at 0.47.

    The venue sizes a marketable BUY in whole cents and reports matched
    shares to two decimals, so the whole order can come back one quantum
    short of the request.  That is a full fill, not a partial one; calling
    it partial sent the intent to reconciliation, which re-applied the same
    matched order and doubled the position.
    """

    from bayes_basket_target.execution_reconciliation import (
        _state_after_event,
        fill_is_complete,
        resolve_reconciliation,
    )

    assert fill_is_complete(4.99, 5.0)
    assert fill_is_complete(4.99, 4.999999999999999)
    assert fill_is_complete(6.0, 6.000000000000001)
    assert not fill_is_complete(4.98, 5.0)
    assert not fill_is_complete(3.0, 5.0)
    assert _state_after_event("FILL", 4.99, 5.0) == (
        "resolved_filled", "filled", "full_fill",
    )
    assert _state_after_event("FILL", 3.0, 5.0) == (
        "pending", "reconcile_required", "partial_fill_requires_reconcile",
    )

    conn = _conn([_intent(requested_size=4.999999999999999, limit_price=0.47)])
    with conn:
        result = record_order_event(
            conn,
            order_intent_id="intent-a",
            event={
                "event_type": "FILL", "exchange_order_id": "0x653c",
                "observed_at": OBSERVED, "matched_size": 4.99,
                "matched_notional_usdc": 2.3453,
                "event_payload": {"exchange_status": "matched"},
            },
        )
    assert result["state"] == "filled"
    assert result["reconciliation_state"] == "resolved_filled"
    # And the authoritative path accepts the same quantity as complete.
    conn2 = _conn([_intent(requested_size=5.0, limit_price=0.47)])
    with conn2:
        resolved = resolve_reconciliation(
            conn2, order_intent_id="intent-a", authoritative_state="filled",
            filled_size=4.99, filled_notional_usdc=2.3453,
            resolved_at_utc=OBSERVED,
        )
    assert resolved["state"] == "filled"


def test_reconciliation_reads_the_gateway_reply_as_the_orders_cumulative_fill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stored reply is the exchange's whole account of the one order.

    An intent already carrying a fill from the same order gains only what
    the exchange reports beyond it; a reply that reports nothing beyond the
    recorded fill closes the intent on what it holds when that covers the
    request, and adds no second fill.
    """

    import clob_order_service_client
    from bayes_basket_target import production_worker

    # (a) A genuine partial the submit pass saw (3 of 40), then the exchange
    # reports 40 matched in total: the increment is 37, the book holds 40.
    conn = _conn([_intent()])
    with conn:
        record_order_event(
            conn,
            order_intent_id="intent-a",
            event={
                "event_type": "PARTIAL_FILL", "exchange_order_id": "0xfill",
                "observed_at": OBSERVED, "matched_size": 3.0,
                "matched_notional_usdc": 0.36,
                "event_payload": {"exchange_status": "matched"},
            },
        )
    assert conn.execute(
        "SELECT state FROM basket_target_order_intents"
    ).fetchone()[0] == "reconcile_required"

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def _post(self, path, payload):
            return {**_matched(), "found": True, "idempotent_replay": True}

    monkeypatch.setattr(clob_order_service_client, "ClobOrderServiceClient", _Client)
    worker = production_worker.TargetProductionWorker.__new__(
        production_worker.TargetProductionWorker
    )
    worker.commit_conn = conn
    receipt = worker._reconcile_unresolved_intents(as_of_utc="2026-08-11T09:40:00Z")
    assert receipt["closed"] == 1
    assert receipt["filled"] == 1
    assert conn.execute(
        "SELECT state,filled_size,filled_notional_usdc"
        " FROM basket_target_order_reconciliations"
    ).fetchall() == [("resolved_filled", 40.0, pytest.approx(4.8))]
    events = conn.execute(
        "SELECT event_type,matched_size FROM basket_target_order_events"
        " ORDER BY observed_at"
    ).fetchall()
    assert events == [("PARTIAL_FILL", 3.0), ("FILL", 37.0)]

    # (b) The KAUS shape under the pre-fix classification: the whole fill
    # already recorded, the intent parked in reconcile_required, the gateway
    # reporting the same order matched at the same size.  Nothing is added;
    # the intent closes on the fill it holds.
    conn = _conn([_intent(requested_size=5.0, limit_price=0.47)])
    with conn:
        record_order_event(
            conn,
            order_intent_id="intent-a",
            event={
                "event_type": "FILL", "exchange_order_id": "0xfill",
                "observed_at": OBSERVED, "matched_size": 4.99,
                "matched_notional_usdc": 2.3453,
                "event_payload": {"exchange_status": "matched"},
            },
        )
        # Force the parked state the old classifier produced.
        conn.execute(
            "UPDATE basket_target_order_intents SET state='reconcile_required'"
        )
        conn.execute(
            "UPDATE basket_target_order_reconciliations SET state='pending'"
        )

    class _KausClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def _post(self, path, payload):
            return {
                **_matched(), "size_matched": "4.99", "shares_matched": "4.99",
                "exchange_response": {
                    "orderID": "0xfill", "takingAmount": "4.99",
                    "makingAmount": "2.3453", "status": "matched",
                },
            }

    monkeypatch.setattr(clob_order_service_client, "ClobOrderServiceClient", _KausClient)
    worker.commit_conn = conn
    receipt = worker._reconcile_unresolved_intents(as_of_utc="2026-08-11T09:41:00Z")
    assert receipt["closed"] == 1
    assert receipt["already_recorded"] == 1
    assert receipt["filled"] == 0
    assert conn.execute(
        "SELECT state FROM basket_target_order_intents"
    ).fetchone()[0] == "filled"
    assert conn.execute(
        "SELECT state,filled_size,filled_notional_usdc"
        " FROM basket_target_order_reconciliations"
    ).fetchall() == [("resolved_filled", 4.99, 2.3453)]
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_target_order_events WHERE event_type='FILL'"
    ).fetchone()[0] == 1

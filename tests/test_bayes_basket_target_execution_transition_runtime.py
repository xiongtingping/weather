from __future__ import annotations

import json
import sqlite3

import pytest
from tests.pr12_atomic_test_support import awc_contract, install_canonical_v12

from bayes_basket_target.atomic_ledger import (
    apply_target_ledger_schema,
    build_target_atomic_bundle,
    commit_target_atomic_bundle,
    initialize_capital_pool,
)
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.execution_reconciliation import (
    ReconciliationError,
    apply_execution_receipt,
    build_no_live_execution_receipt,
    can_submit_fok,
)
from bayes_basket_target.execution_transition_runtime import (
    ExecutionTransitionError,
    _same_instant,
    apply_execution_transition_schema,
    complete_post_fill_recompute_request,
    load_post_fill_recompute_requests,
    post_fill_recompute_binding_sha256,
)
from bayes_basket_target.market_materialization import (
    build_confirmed_inventory_snapshot,
    build_official_ws_snapshot,
    build_order_state_snapshot,
    insert_market_evidence_bundle,
)
from bayes_basket_target.ssot import apply_production_ssot_schema


NOW = "2026-07-23T05:00:00Z"
EXPIRES = "2026-07-23T05:10:00Z"
SCOPE = {
    "station_id": "ZUUU",
    "event_date": "2026-07-23",
    "settlement_timezone": "America/New_York",
    "bucket": "38",
    "token_id": "token-0",
    "side": "YES",
    "order_action": "BUY",
}


def _state_rows() -> tuple[dict, dict, dict, dict]:
    inventory_raw = {
        "inventory_snapshot_id": "inventory-snapshot-0",
        "station_id": "ZUUU",
        "event_date": "2026-07-23",
        "settlement_timezone": "America/New_York",
        "inventory_version": "inventory-version-0",
        "observed_at": NOW,
        "complete": 1,
        "inventory_json": {
            "filled_by_bucket": {"38": 0.0},
            "filled_cost_by_bucket": {"38": 0.0},
        },
        "state": "COMMITTED",
        "created_at": NOW,
    }
    order_state_raw = {
        "order_state_snapshot_id": "order-state-snapshot-0",
        "station_id": "ZUUU",
        "event_date": "2026-07-23",
        "settlement_timezone": "America/New_York",
        "order_state_version": "order-state-version-0",
        "observed_at": NOW,
        "known": 1,
        "order_state_json": {
            "pending_by_bucket": {"38": 0.0},
            "pending_notional_by_bucket": {"38": 0.0},
        },
        "state": "COMMITTED",
        "created_at": NOW,
    }
    return (
        inventory_raw,
        order_state_raw,
        build_confirmed_inventory_snapshot(inventory_raw),
        build_order_state_snapshot(order_state_raw),
    )


def _db() -> tuple[sqlite3.Connection, dict[str, str], str]:
    conn = sqlite3.connect(":memory:")
    apply_target_ledger_schema(conn)
    apply_production_ssot_schema(conn)
    install_canonical_v12(conn)
    initialize_capital_pool(
        conn,
        balance_usdc=20.0,
        capital_epoch_id="execution-transition-test-epoch",
        updated_at_utc=NOW,
    )
    inventory_raw, order_state_raw, inventory, order_state = _state_rows()
    cash_input = {
        "policy_version": "cash-test-v1",
        "station_id": "ZUUU",
        "current_phase": "HEAVY",
        "event_date_local": "2026-07-23",
        "observed_at_utc": NOW,
        "station_timezone": "America/New_York",
        "checkpoint_id": "cash-checkpoint-0",
    }
    quote = build_official_ws_snapshot({
        "snapshot_id": "quote-snapshot-0",
        "market_id": "market-0",
        "station_id": "ZUUU",
        "event_date": "2026-07-23",
        "settlement_timezone": "America/New_York",
        "sequence": 10,
        "observed_at": NOW,
        "quote_version": "quote-version-0",
        "quote_observed_at": NOW,
        "quote_expires_at": EXPIRES,
        "quote_freshness_policy_sha256": "1" * 64,
        "quote_source": "OFFICIAL_CLOB_WS",
        "inventory_version": inventory["inventory_version"],
        "order_state_version": order_state["order_state_version"],
        "remaining_budget_usdc": 20.0,
        "buckets": [{
            "bucket": "38",
            "ordinal": 0,
            "token_id": "token-0",
            "ask_levels": [{"price": 0.4, "shares": 100.0}],
            "bid_levels": [{"price": 0.3, "shares": 100.0}],
        }],
        "cash_phase_input": cash_input,
        "state": "COMMITTED",
        "created_at": NOW,
    })
    quote_ingress = dict(quote)
    quote_ingress["buckets"] = quote_ingress.pop("buckets_json")
    quote_ingress["cash_phase_input"] = quote_ingress.pop("cash_phase_input_json")
    insert_market_evidence_bundle(conn, quote_ingress, inventory_raw, order_state_raw)
    vector = {
        "schema": "bayes_basket_target_execution_version_vector_v2",
        "probability_sha256": "2" * 64,
        "official_ws_quote_sha256": quote["content_sha256"],
        "inventory_sha256": inventory["content_sha256"],
        "order_state_sha256": order_state["content_sha256"],
        "station_policy_sha256": "5" * 64,
        "cash_policy_decision_sha256": "6" * 64,
        "economic_policy_schema": "delta_basket_score_policy_artifact_v2",
        "economic_policy_artifact_sha256": "7" * 64,
        "target_price_id": "price-0",
        "quote_observed_at": NOW,
        "quote_expires_at": EXPIRES,
    }
    contract, revision_sha = awc_contract(
        station_id="ZUUU", observation_id="observation-0",
        event_date="2026-07-23", settlement_timezone=SCOPE["settlement_timezone"],
        observed_at_utc=NOW, raw_object_sha256="8" * 64,
    )
    bundle = build_target_atomic_bundle(
        awc_observation_contract=contract,
        truth_observation_revision_content_sha256=revision_sha,
        bound={
            "bound_id": "bound-0",
            "station_id": "ZUUU",
            "event_date": "2026-07-23",
            "bound_generation": 0,
            "hard_lower_bound": 37,
            "bound_json": {"source": "official_metar"},
        },
        plan={
            "strategy_lane": "basket",
            "settlement_timezone": "America/New_York",
            "probability_checkpoint_id": "p1-0",
            "quote_snapshot_id": quote["snapshot_id"],
            "lineage": {
                "probability_stage": "P1",
                "settlement_timezone": "America/New_York",
                "canonical_observation_event_id": "observation-0",
                "truth_observation_revision_content_sha256": revision_sha,
                "canonical_observation_committed_at_utc": "2026-07-23T05:00:00.000Z",
                "hard_bound_id": "bound-0",
                "source_lane": "AWC",
                "report_type": "METAR",
            },
        },
        decision={
            "strategy_lane": "basket",
            "decided_state": "prepared",
            "will_order": False,
            "reason_codes": [],
            "details": {"candidate_will_order": True, "no_live": True},
        },
        intents=[
            {
                "bucket": "38",
                "token_id": "token-0",
                "side": "YES",
                "order_action": "BUY",
                "limit_price": 0.4,
                "requested_size": 1.0,
                "requested_notional_usdc": 0.4,
                "intent_json": {
                    "candidate_will_order": True,
                    "execution_candidate": "MAKER",
                    "no_live": True,
                    "target_execution_hard_deadline_at": "2026-07-23T05:00:03.000Z",
                },
            },
            {
                "bucket": "38",
                "token_id": "token-0",
                "side": "YES",
                "order_action": "BUY",
                "limit_price": 0.4,
                "requested_size": 1.0,
                "requested_notional_usdc": 0.4,
                "intent_json": {
                    "candidate_will_order": True,
                    "execution_candidate": "TAKER_FOK",
                    "no_live": True,
                    "target_execution_hard_deadline_at": "2026-07-23T05:00:03.000Z",
                },
            },
        ],
        reserved_amount_usdc=0.8,
        created_at_utc=NOW,
        execution_version_vectors=[vector, vector],
    )
    commit_target_atomic_bundle(conn, bundle=bundle)
    intent_id = str(bundle["intents"][0]["order_intent_id"])
    return conn, vector, intent_id


def _fok_intent_id(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT order_intent_id,intent_json FROM basket_target_order_intents"
    ).fetchall()
    matches = [
        str(intent_id)
        for intent_id, raw in rows
        if json.loads(str(raw)).get("execution_candidate") == "TAKER_FOK"
    ]
    assert len(matches) == 1
    return matches[0]


def _receipt(vector: dict, intent_id: str, receipt_type: str, **changes: object) -> dict:
    values = {
        "receipt_type": receipt_type,
        "order_intent_id": intent_id,
        "scope": SCOPE,
        "version_vector": vector,
        "quote_sequence": 10,
        "observed_at_utc": NOW,
        "requested_size": 1.0,
        "matched_size": 0.0,
        "matched_notional_usdc": 0.0,
    }
    values.update(changes)
    return build_no_live_execution_receipt(**values)


def test_authoritative_cancel_confirm_is_the_only_fok_gate() -> None:
    conn, vector, intent_id = _db()
    apply_execution_transition_schema(conn)
    requested = _receipt(vector, intent_id, "CANCEL_REQUESTED")
    apply_execution_receipt(conn, requested)
    assert can_submit_fok(
        conn,
        predecessor_order_intent_id=intent_id,
        current_vector=vector,
        at_utc="2026-07-23T05:00:01Z",
    )["allowed"] is False
    confirmed = _receipt(
        vector,
        intent_id,
        "CANCEL_CONFIRMED",
        observed_at_utc="2026-07-23T05:00:01Z",
    )
    result = apply_execution_receipt(conn, confirmed)
    assert result["authorized_for_next_fok"] is True
    gate = can_submit_fok(
        conn,
        predecessor_order_intent_id=intent_id,
        current_vector=vector,
        at_utc="2026-07-23T05:00:02Z",
    )
    assert gate["allowed"] is True


def test_quote_clock_binding_compares_instants_not_fractional_text_width() -> None:
    assert _same_instant(
        "2026-07-29T06:35:35.677Z",
        "2026-07-29T06:35:35.677000Z",
    )
    assert not _same_instant(
        "2026-07-29T06:35:35.677Z",
        "2026-07-29T06:35:35.678000Z",
    )


@pytest.mark.parametrize("receipt_type", [
    "POST_UNKNOWN", "FOK_PARTIAL_FILL", "LATE_FILL", "CANCEL_FILL_RACE", "FOK_FAILED",
])
def test_uncertain_receipts_are_reconcile_required(receipt_type: str) -> None:
    conn, vector, intent_id = _db()
    predecessor = None
    if receipt_type.startswith("FOK_"):
        apply_execution_receipt(
            conn,
            _receipt(vector, intent_id, "CANCEL_REQUESTED"),
        )
        apply_execution_receipt(
            conn,
            _receipt(
                vector,
                intent_id,
                "CANCEL_CONFIRMED",
                observed_at_utc="2026-07-23T05:00:01Z",
            ),
        )
        predecessor = intent_id
        intent_id = _fok_intent_id(conn)
    receipt = _receipt(
        vector,
        intent_id,
        receipt_type,
        predecessor_order_intent_id=predecessor,
        observed_at_utc=(
            "2026-07-23T05:00:02Z" if predecessor else NOW
        ),
        matched_size=0.25 if "PARTIAL" in receipt_type or receipt_type == "LATE_FILL" else 0.0,
        matched_notional_usdc=0.1 if "PARTIAL" in receipt_type or receipt_type == "LATE_FILL" else 0.0,
    )
    result = apply_execution_receipt(conn, receipt)
    assert result["state_after"] == "RECONCILE_REQUIRED"
    assert result["intent_state"] == "reconcile_required"
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_target_order_intents "
        "WHERE cycle_id=(SELECT cycle_id FROM basket_target_order_intents "
        "WHERE order_intent_id=?) AND state='prepared'",
        (intent_id,),
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT state FROM basket_target_execution_cycles"
    ).fetchone()[0] == "reconcile_required"
    assert conn.execute(
        "SELECT state FROM basket_target_capital_reservations"
    ).fetchone()[0] == "active"
    assert can_submit_fok(
        conn,
        predecessor_order_intent_id=intent_id,
        current_vector=vector,
        at_utc="2026-07-23T05:00:03Z",
    )["allowed"] is False


def test_full_fill_is_atomic_idempotent_and_emits_recompute_request() -> None:
    conn, vector, intent_id = _db()
    receipt = _receipt(
        vector,
        intent_id,
        "MAKER_FULL_FILL",
        matched_size=1.0,
        matched_notional_usdc=0.4,
    )
    result = apply_execution_receipt(conn, receipt)
    assert result["state_after"] == "FULL_FILLED"
    assert result["post_fill_recompute_required"] is True
    assert conn.execute("SELECT state FROM basket_target_order_intents WHERE order_intent_id=?", (intent_id,)).fetchone()[0] == "filled"
    transition = conn.execute(
        "SELECT filled_size,inventory_version,order_state_version,reconciliation_version FROM basket_target_execution_transition_versions WHERE order_intent_id=?",
        (intent_id,),
    ).fetchone()
    assert transition[0] == pytest.approx(1.0)
    assert all(str(value).startswith(prefix) for value, prefix in zip(transition[1:], ("invrec_", "ordrec_", "recon_")))
    assert conn.execute(
        "SELECT state FROM basket_target_order_intents "
        "WHERE order_intent_id=?",
        (_fok_intent_id(conn),),
    ).fetchone()[0] == "cancelled"
    assert conn.execute(
        "SELECT state FROM basket_target_execution_cycles"
    ).fetchone()[0] == "settled"
    assert conn.execute(
        "SELECT state FROM basket_target_capital_reservations"
    ).fetchone()[0] == "consumed"
    balance, reserved = conn.execute(
        "SELECT balance_usdc,reserved_usdc FROM "
        "basket_target_capital_pool"
    ).fetchone()
    assert balance == pytest.approx(19.6)
    assert reserved == pytest.approx(0.0)
    requests = load_post_fill_recompute_requests(conn, order_intent_id=intent_id)
    assert len(requests) == 1
    assert requests[0]["probability_sha256"] == vector["probability_sha256"]
    assert requests[0]["source_execution_version_vector"][
        "probability_sha256"
    ] == vector["probability_sha256"]
    assert requests[0]["source_execution_version_vector_sha256"] == (
        requests[0]["source_execution_version_vector"]["vector_sha256"]
    )
    source_binding_sha = post_fill_recompute_binding_sha256(requests[0])
    completion_result = {
        "schema": "test_post_fill_completion_v1",
        "source_request_binding_sha256": source_binding_sha,
        "production_cycle_sha256": "a" * 64,
    }
    completion = complete_post_fill_recompute_request(
        conn,
        request_id=requests[0]["request_id"],
        scope=requests[0]["scope"],
        inventory_version=requests[0]["inventory_version"],
        order_state_version=requests[0]["order_state_version"],
        result=completion_result,
        result_content_sha256=content_sha256(completion_result),
        completed_at_utc="2026-07-23T05:00:01Z",
    )
    assert completion["state"] == "APPLIED"
    assert completion["idempotent"] is False
    replay = complete_post_fill_recompute_request(
        conn,
        request_id=requests[0]["request_id"],
        scope=requests[0]["scope"],
        inventory_version=requests[0]["inventory_version"],
        order_state_version=requests[0]["order_state_version"],
        result=completion_result,
        result_content_sha256=content_sha256(completion_result),
        completed_at_utc="2026-07-23T05:00:02Z",
    )
    assert replay["state"] == "APPLIED"
    assert replay["idempotent"] is True
    duplicate = apply_execution_receipt(conn, receipt)
    assert duplicate["state"] == "DUPLICATE_RECEIPT"
    assert conn.execute("SELECT COUNT(*) FROM basket_target_post_fill_recompute_requests").fetchone()[0] == 1


def test_authoritative_maker_partial_fill_closes_old_bundle_and_recomputes() -> None:
    conn, vector, intent_id = _db()
    result = apply_execution_receipt(
        conn,
        _receipt(
            vector,
            intent_id,
            "MAKER_PARTIAL_FILL",
            matched_size=0.25,
            matched_notional_usdc=0.1,
        ),
    )

    assert result["state_after"] == "PARTIAL_FILLED"
    assert result["intent_state"] == "cancelled"
    assert result["filled_size"] == pytest.approx(0.25)
    assert result["filled_notional_usdc"] == pytest.approx(0.1)
    assert result["pending_size"] == 0.0
    assert result["post_fill_recompute_required"] is True
    assert conn.execute(
        "SELECT state FROM basket_target_execution_cycles"
    ).fetchone()[0] == "settled"
    assert conn.execute(
        "SELECT state FROM basket_target_capital_reservations"
    ).fetchone()[0] == "consumed"
    balance, reserved = conn.execute(
        "SELECT balance_usdc,reserved_usdc FROM basket_target_capital_pool"
    ).fetchone()
    assert balance == pytest.approx(19.9)
    assert reserved == pytest.approx(0.0)
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_target_post_fill_recompute_requests"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT state FROM basket_target_order_intents "
        "WHERE cycle_id=(SELECT cycle_id FROM basket_target_order_intents "
        "WHERE order_intent_id=?) AND order_intent_id<>?",
        (intent_id, intent_id),
    ).fetchone()[0] == "cancelled"


def test_scope_sequence_freshness_and_live_isolation_fail_closed() -> None:
    conn, vector, intent_id = _db()
    with pytest.raises(ExecutionTransitionError, match="scope_mismatch"):
        apply_execution_receipt(conn, _receipt(vector, intent_id, "POST_ACK", scope={**SCOPE, "bucket": "39"}))
    with pytest.raises(ExecutionTransitionError, match="sequence_mismatch"):
        apply_execution_receipt(conn, _receipt(vector, intent_id, "POST_ACK", quote_sequence=9))
    live = _receipt(vector, intent_id, "POST_ACK")
    live["execution_mode"] = "production_live"
    with pytest.raises(ExecutionTransitionError, match="live_mode_forbidden"):
        apply_execution_receipt(conn, live)


def test_conflicting_receipt_and_low_high_low_transition_are_fail_closed() -> None:
    conn, vector, intent_id = _db()
    first = _receipt(vector, intent_id, "POST_ACK")
    apply_execution_receipt(conn, first)
    conflicting = dict(first)
    conflicting["event_payload"] = {"different": True}
    with pytest.raises(ExecutionTransitionError):
        apply_execution_receipt(conn, conflicting)
    for index, receipt_type in enumerate(("CANCEL_REQUESTED", "CANCEL_CONFIRMED")):
        receipt = _receipt(
            vector,
            intent_id,
            receipt_type,
            observed_at_utc=f"2026-07-23T05:00:0{index + 1}Z",
        )
        apply_execution_receipt(conn, receipt)
    with pytest.raises(
        ExecutionTransitionError,
        match="state_transition_forbidden",
    ):
        apply_execution_receipt(
            conn,
            _receipt(
                vector,
                intent_id,
                "POST_ACK",
                observed_at_utc="2026-07-23T05:00:03Z",
            ),
        )
    seq = conn.execute(
        "SELECT transition_seq FROM basket_target_execution_transition_versions WHERE order_intent_id=?", (intent_id,)
    ).fetchone()[0]
    assert seq == 3
    assert conn.execute("SELECT COUNT(*) FROM basket_target_execution_receipts WHERE order_intent_id=?", (intent_id,)).fetchone()[0] == 3


def test_fok_requires_distinct_zero_fill_cancelled_maker_predecessor() -> None:
    conn, vector, maker_intent_id = _db()
    fok_intent_id = _fok_intent_id(conn)
    with pytest.raises(
        ExecutionTransitionError,
        match="fok_predecessor_order_intent_required",
    ):
        apply_execution_receipt(
            conn,
            _receipt(
                vector,
                fok_intent_id,
                "FOK_FULL_FILL",
                matched_size=1.0,
                matched_notional_usdc=0.4,
            ),
        )
    apply_execution_receipt(
        conn,
        _receipt(vector, maker_intent_id, "CANCEL_REQUESTED"),
    )
    apply_execution_receipt(
        conn,
        _receipt(
            vector,
            maker_intent_id,
            "CANCEL_CONFIRMED",
            observed_at_utc="2026-07-23T05:00:01Z",
        ),
    )
    result = apply_execution_receipt(
        conn,
        _receipt(
            vector,
            fok_intent_id,
            "FOK_FULL_FILL",
            predecessor_order_intent_id=maker_intent_id,
            observed_at_utc="2026-07-23T05:00:02Z",
            matched_size=1.0,
            matched_notional_usdc=0.4,
        ),
    )
    assert result["state_after"] == "FULL_FILLED"


def test_receipt_type_must_match_intent_execution_candidate() -> None:
    conn, vector, maker_intent_id = _db()
    with pytest.raises(
        ExecutionTransitionError,
        match="fok_receipt_intent_candidate_mismatch",
    ):
        apply_execution_receipt(
            conn,
            _receipt(
                vector,
                maker_intent_id,
                "FOK_SUBMITTED",
                predecessor_order_intent_id="other",
            ),
        )
    with pytest.raises(
        ExecutionTransitionError,
        match="maker_receipt_intent_candidate_mismatch",
    ):
        apply_execution_receipt(
            conn,
            _receipt(
                vector,
                _fok_intent_id(conn),
                "POST_ACK",
            ),
        )


def test_late_receipt_cannot_overwrite_newer_transition() -> None:
    conn, vector, intent_id = _db()
    apply_execution_receipt(
        conn,
        _receipt(
            vector,
            intent_id,
            "POST_ACK",
            observed_at_utc="2026-07-23T05:00:02Z",
        ),
    )
    with pytest.raises(
        ExecutionTransitionError,
        match="observed_at_not_strictly_increasing",
    ):
        apply_execution_receipt(
            conn,
            _receipt(
                vector,
                intent_id,
                "CANCEL_REQUESTED",
                observed_at_utc="2026-07-23T05:00:01Z",
            ),
        )
    row = conn.execute(
        "SELECT state,filled_size FROM "
        "basket_target_execution_transition_versions "
        "WHERE order_intent_id=?",
        (intent_id,),
    ).fetchone()
    assert row == ("POST_ACKED", 0.0)


def test_partial_fill_then_cancel_confirm_cannot_erase_fill() -> None:
    conn, vector, intent_id = _db()
    apply_execution_receipt(
        conn,
        _receipt(
            vector,
            intent_id,
            "MAKER_PARTIAL_FILL",
            matched_size=0.25,
            matched_notional_usdc=0.1,
        ),
    )
    with pytest.raises(
        ExecutionTransitionError,
        match="state_transition_forbidden",
    ):
        apply_execution_receipt(
            conn,
            _receipt(
                vector,
                intent_id,
                "CANCEL_CONFIRMED",
                observed_at_utc="2026-07-23T05:00:01Z",
            ),
        )
    row = conn.execute(
        "SELECT state,filled_size,filled_notional_usdc "
        "FROM basket_target_execution_transition_versions "
        "WHERE order_intent_id=?",
        (intent_id,),
    ).fetchone()
    assert row == ("PARTIAL_FILLED", 0.25, 0.1)

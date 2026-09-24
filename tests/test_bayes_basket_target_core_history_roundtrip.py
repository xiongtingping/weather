from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from bayes_basket_target import ContractError
from bayes_basket_target.production_io import load_production_market_snapshot


# Reuse the established isolated in-memory SSOT fixture.  This import only
# loads test helpers; it never opens the real target database or a service.
sys.path.insert(0, str(Path(__file__).parent))
from test_bayes_basket_target_production_io import (  # noqa: E402
    _record_sha,
    _seed,
)


STATION = "ZUUU"
EVENT_DATE = "2026-07-23"
SETTLEMENT_TZ = "America/New_York"


def _persist_blocked_no_live_decision(
    conn: sqlite3.Connection,
    *,
    core_buckets: object,
    created_at: str = "2026-07-23T05:00:02Z",
) -> None:
    """Persist the first-round plan/decision receipt in the test SSOT."""

    details = {
        "schema": "bayes_basket_production_decision_v1",
        "plan": {"core_buckets": core_buckets},
        "decision": {"core_buckets": core_buckets},
        "core_buckets": core_buckets,
        "candidate_will_order": False,
        "execution_mode": "production_no_live",
        "live_submit_authorized": False,
    }
    conn.execute(
        "INSERT INTO auto_trade_decisions("
        "decision_id,cycle_id,plan_id,station_id,event_date,"
        "settlement_timezone,strategy_lane,decided_state,will_order,"
        "target_execution_hard_deadline_at,reason_codes_json,details_json,"
        "decision_sha256,created_at"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "decision-core-history-1",
            "cycle-core-history-1",
            "plan_core_history_1",
            STATION,
            EVENT_DATE,
            SETTLEMENT_TZ,
            "basket",
            "blocked",
            0,
            None,
            json.dumps(["no_live"], sort_keys=True),
            json.dumps(details, sort_keys=True),
            "d" * 64,
            created_at,
        ),
    )
    conn.commit()


def _append_market_round_with_outer_activity(conn: sqlite3.Connection) -> None:
    """Add a later quote/inventory/order round with activity outside the core."""

    conn.row_factory = sqlite3.Row
    prior = conn.execute(
        "SELECT * FROM basket_official_ws_snapshot WHERE snapshot_id='quote-1'"
    ).fetchone()
    assert prior is not None
    quote = {key: prior[key] for key in prior.keys()}
    buckets = json.loads(quote["buckets_json"])
    for row in buckets:
        if row["bucket"] in {"37", "39"}:
            row["active_before"] = True
            row["filled_shares"] = 99.0
            row["pending_shares"] = 77.0
    quote.update(
        {
            "snapshot_id": "quote-core-history-2",
            "sequence": 2,
            "observed_at": "2026-07-23T05:00:02Z",
            "quote_observed_at": "2026-07-23T05:00:02Z",
            "quote_expires_at": "2026-07-23T05:00:10Z",
            "quote_version": "q-core-history-2",
            "inventory_version": "inv-core-history-2",
            "order_state_version": "ord-core-history-2",
            "buckets_json": buckets,
            "cash_phase_input_json": json.loads(quote["cash_phase_input_json"]),
            "created_at": "2026-07-23T05:00:02Z",
        }
    )
    quote["content_sha256"] = _record_sha(
        "basket_official_ws_snapshot_v1", quote
    )

    inventory = {
        "inventory_snapshot_id": "inv-core-history-2",
        "station_id": STATION,
        "event_date": EVENT_DATE,
        "inventory_version": "inv-core-history-2",
        "observed_at": "2026-07-23T05:00:02Z",
        "complete": 1,
        "inventory_json": {
            "filled_by_bucket": {"37": 2.0, "39": 3.0},
            "filled_cost_by_bucket": {"37": 0.4, "39": 0.6},
        },
        "state": "COMMITTED",
        "created_at": "2026-07-23T05:00:02Z",
    }
    inventory["content_sha256"] = _record_sha(
        "basket_confirmed_inventory_snapshot_v1", inventory
    )

    order_state = {
        "order_state_snapshot_id": "ord-core-history-2",
        "station_id": STATION,
        "event_date": EVENT_DATE,
        "order_state_version": "ord-core-history-2",
        "observed_at": "2026-07-23T05:00:02Z",
        "known": 1,
        "order_state_json": {
            "pending_by_bucket": {"37": 1.0, "39": 4.0},
            "pending_notional_by_bucket": {"37": 0.2, "39": 0.8},
        },
        "state": "COMMITTED",
        "created_at": "2026-07-23T05:00:02Z",
    }
    order_state["content_sha256"] = _record_sha(
        "basket_order_state_snapshot_v1", order_state
    )

    quote_columns = (
        "snapshot_id", "market_id", "station_id", "event_date",
        "settlement_timezone", "sequence", "observed_at", "quote_version",
        "quote_observed_at", "quote_expires_at",
        "quote_freshness_policy_sha256", "quote_source", "inventory_version",
        "order_state_version", "remaining_budget_usdc", "buckets_json",
        "cash_phase_input_json", "content_sha256", "state", "created_at",
    )
    conn.execute(
        "INSERT INTO basket_official_ws_snapshot("
        + ",".join(quote_columns)
        + ") VALUES("
        + ",".join("?" for _ in quote_columns)
        + ")",
        tuple(
            json.dumps(quote[key], sort_keys=True)
            if key in {"buckets_json", "cash_phase_input_json"}
            else quote[key]
            for key in quote_columns
        ),
    )
    conn.execute(
        "INSERT INTO basket_confirmed_inventory_snapshot VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            inventory["inventory_snapshot_id"], inventory["station_id"],
            inventory["event_date"], inventory["inventory_version"],
            inventory["observed_at"], inventory["complete"],
            json.dumps(inventory["inventory_json"], sort_keys=True),
            inventory["content_sha256"], inventory["state"], inventory["created_at"],
        ),
    )
    conn.execute(
        "INSERT INTO basket_order_state_snapshot VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            order_state["order_state_snapshot_id"], order_state["station_id"],
            order_state["event_date"], order_state["order_state_version"],
            order_state["observed_at"], order_state["known"],
            json.dumps(order_state["order_state_json"], sort_keys=True),
            order_state["content_sha256"], order_state["state"], order_state["created_at"],
        ),
    )
    conn.commit()
    _seed_basket_own_fills(conn)


def _seed_basket_own_fills(conn: sqlite3.Connection) -> None:
    """The fills behind the snapshot's 37 x2 / 39 x3 are the basket's own.

    Since 2026-09-24 the basket credits only fills its own orders made (the
    account snapshot carries every lane's), so the seeded inventory needs the
    basket intents and reconciliations it stands for.
    """

    conn.execute(
        "INSERT INTO basket_target_execution_cycles(cycle_id,plan_id,station_id,"
        "event_date,settlement_timezone,strategy_lane,plan_sha256,"
        "requested_reserve_usdc,state,created_at,updated_at)"
        " VALUES(?,?,?,?,?,'basket',?,1.0,'submitted',?,?)",
        ("cycle-core-fills", "plan_core_fills", STATION, EVENT_DATE, SETTLEMENT_TZ,
         "a" * 64, "2026-07-23T05:00:00Z", "2026-07-23T05:00:00Z"),
    )
    for bucket, size, notional in (("37", 2.0, 0.4), ("39", 3.0, 0.6)):
        intent = f"intent_core_{bucket}"
        conn.execute(
            "INSERT INTO basket_target_order_intents(order_intent_id,decision_id,"
            "cycle_id,bucket,token_id,side,order_action,limit_price,requested_size,"
            "requested_notional_usdc,state,live_submit_authorized,idempotency_key,"
            "intent_json,intent_sha256,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (intent, "decision-core-fills", "cycle-core-fills", bucket, f"tok-{bucket}",
             "YES", "BUY", 0.2, size, notional, "submitted", 1, f"k-{intent}",
             "{}", "b" * 64, "2026-07-23T05:00:00Z"),
        )
        conn.execute(
            "INSERT INTO basket_target_order_reconciliations(order_intent_id,"
            "exchange_order_id,state,filled_size,filled_notional_usdc,last_event_id,"
            "blocking_reason,updated_at) VALUES(?,?,'resolved_filled',?,?,NULL,NULL,?)",
            (intent, f"0x{intent}", size, notional, "2026-07-23T05:00:01Z"),
        )
    conn.commit()

def _load_next_round(conn: sqlite3.Connection) -> dict[str, object]:
    return load_production_market_snapshot(
        conn,
        station_id=STATION,
        event_date=EVENT_DATE,
        evaluation_at_utc="2026-07-23T05:00:04Z",
    )


def test_blocked_no_live_decision_roundtrip_preserves_core_only() -> None:
    with sqlite3.connect(":memory:") as conn:
        _seed(conn, cash_promoted=False)
        _persist_blocked_no_live_decision(conn, core_buckets=["38"])
        _append_market_round_with_outer_activity(conn)

        snapshot = _load_next_round(conn)

        assert snapshot["current_core_buckets"] == ["38"]
        by_bucket = {row["bucket"]: row for row in snapshot["buckets"]}
        assert by_bucket["37"]["active_before"] is True
        assert by_bucket["39"]["active_before"] is True
        assert by_bucket["37"]["filled_shares"] == 2.0
        assert by_bucket["39"]["filled_shares"] == 3.0
        assert by_bucket["37"]["pending_shares"] == 1.0
        assert by_bucket["39"]["pending_shares"] == 4.0
        assert snapshot["current_core_buckets"] not in (
            ["37", "38", "39"],
            ["37", "39"],
        )


@pytest.mark.parametrize(
    "core_buckets",
    [
        "38",
        ["38", "38"],
        ["", "38"],
        ["37", "39"],
        ["99"],
        ["39", "38"],
    ],
    ids=[
        "not-a-list",
        "duplicate",
        "empty-bucket",
        "noncontiguous",
        "unknown",
        "reverse-order",
    ],
)
def test_malformed_or_noncontiguous_persisted_core_fails_closed(
    core_buckets: object,
) -> None:
    with sqlite3.connect(":memory:") as conn:
        _seed(conn, cash_promoted=False)
        _persist_blocked_no_live_decision(conn, core_buckets=core_buckets)

        with pytest.raises(
            ContractError,
            match="production_prior_core_buckets_invalid",
        ):
            load_production_market_snapshot(
                conn,
                station_id=STATION,
                event_date=EVENT_DATE,
                evaluation_at_utc="2026-07-23T05:00:04Z",
            )

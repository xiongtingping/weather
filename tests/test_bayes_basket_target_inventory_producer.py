from __future__ import annotations

import json
import sqlite3

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.inventory_producer import (
    build_confirmed_inventory_ingress,
    build_no_live_inventory_ingress,
    has_execution_transition_versions,
)
from bayes_basket_target.contracts import content_sha256


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE reported_trajectory_market_registry(
            station_id TEXT, event_date TEXT, outcome TEXT, yes_token TEXT,
            source_eligible INTEGER, market_active INTEGER,
            market_accepting_orders INTEGER, settlement_timezone TEXT
        )"""
    )
    conn.executemany(
        "INSERT INTO reported_trajectory_market_registry VALUES(?,?,?,?,?,?,?,?)",
        [
            ("TEST", "2026-07-23", "38°C", "yes-38", 1, 1, 1, "America/New_York"),
            ("TEST", "2026-07-23", "39°C", "yes-39", 1, 1, 1, "America/New_York"),
        ],
    )
    return conn


def _transition_db() -> sqlite3.Connection:
    conn = _db()
    conn.execute(
        """CREATE TABLE basket_target_execution_transition_versions(
            order_intent_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL,
            decision_id TEXT NOT NULL, station_id TEXT NOT NULL,
            event_date TEXT NOT NULL, settlement_timezone TEXT NOT NULL,
            bucket TEXT NOT NULL, token_id TEXT NOT NULL,
            transition_seq INTEGER NOT NULL, state TEXT NOT NULL,
            confirmed_pending_size REAL NOT NULL,
            confirmed_pending_notional_usdc REAL NOT NULL,
            filled_size REAL NOT NULL, filled_notional_usdc REAL NOT NULL,
            inventory_version TEXT NOT NULL, order_state_version TEXT NOT NULL,
            reconciliation_version TEXT NOT NULL, last_receipt_id TEXT NOT NULL,
            last_receipt_sha256 TEXT NOT NULL, evidence_json TEXT NOT NULL,
            transition_json TEXT NOT NULL, content_sha256 TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    return conn


def _insert_transition(
    conn: sqlite3.Connection,
    *,
    intent: str,
    bucket: str,
    token: str,
    state: str,
    size: float,
    notional: float,
    pending: float = 0.0,
    pending_notional: float = 0.0,
    seq: int = 1,
    updated_at: str = "2026-07-23T04:00:00Z",
) -> None:
    evidence = {"scope_sha256": "1" * 64, "quote_sequence": 1}
    transition = {
        "schema": "bayes_basket_target_execution_transition_v1",
        "order_intent_id": intent,
        "cycle_id": "cycle-1",
        "decision_id": "decision-1",
        "scope_sha256": "1" * 64,
        "transition_seq": seq,
        "state": state,
        "reason": "test",
        "filled_size": size,
        "filled_notional_usdc": notional,
        "last_receipt_sha256": "2" * 64,
        "evidence": evidence,
    }
    digest = content_sha256({
        "schema": "basket_target_execution_transition_version_v1",
        "transition": transition,
        "evidence": evidence,
    })
    conn.execute(
        "INSERT INTO basket_target_execution_transition_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            intent, "cycle-1", "decision-1", "TEST", "2026-07-23", "America/New_York",
            bucket, token, seq, state, pending, pending_notional, size, notional,
            "invrec-test", "ordrec-test", "recon-test", "receipt-1", "2" * 64,
            json.dumps(evidence, sort_keys=True), json.dumps(transition, sort_keys=True),
            digest, updated_at,
        ),
    )


def _manifest(*, with_details: bool = True) -> dict:
    value = {
        "schema": "reported_trajectory_held_yes_inventory_v1",
        "source": "official_polymarket_data_api_complete_positions",
        "complete": True,
        "observed_at": "2026-07-23T04:00:00Z",
        "token_ids": ["yes-38"],
        "active_token_ids": ["yes-38"],
    }
    if with_details:
        value["position_rows"] = [{
            "asset": "yes-38",
            "outcome": "YES",
            "size": 4,
            "avg_price": 0.31,
            "current_price": 0.4,
        }]
    return value


def test_inventory_producer_maps_official_position_to_full_bucket_coverage(tmp_path) -> None:
    path = tmp_path / "positions.json"
    path.write_text(json.dumps(_manifest()), encoding="utf-8")
    with _db() as conn:
        packet = build_confirmed_inventory_ingress(
            conn, manifest_path=path, remaining_budget_usdc=20.0,
        )
    assert packet["schema"] == "bayes_basket_confirmed_inventory_ingress_v1"
    market = packet["markets"][0]
    assert market["inventory_json"]["filled_by_bucket"] == {"38°C": 4.0, "39°C": 0.0}
    assert market["inventory_json"]["filled_cost_by_bucket"]["38°C"] == pytest.approx(1.24)
    assert market["inventory_json"]["position_counts_by_bucket"]["39°C"] == 0
    assert market["complete"] == 1


def test_inventory_producer_rejects_token_only_manifest(tmp_path) -> None:
    path = tmp_path / "positions.json"
    path.write_text(json.dumps(_manifest(with_details=False)), encoding="utf-8")
    with _db() as conn:
        with pytest.raises(ContractError, match="position_details_missing"):
            build_confirmed_inventory_ingress(
                conn, manifest_path=path, remaining_budget_usdc=20.0,
            )


def test_inventory_producer_rejects_negative_budget(tmp_path) -> None:
    path = tmp_path / "positions.json"
    path.write_text(json.dumps(_manifest()), encoding="utf-8")
    with _db() as conn:
        with pytest.raises(ContractError, match="remaining_budget_usdc_invalid"):
            build_confirmed_inventory_ingress(
                conn, manifest_path=path, remaining_budget_usdc=-1.0,
            )


def test_no_live_inventory_starts_at_zero_without_position_manifest() -> None:
    with _db() as conn:
        packet = build_no_live_inventory_ingress(
            conn,
            remaining_budget_usdc=30.0,
            observed_at_utc="2026-07-23T04:00:00Z",
        )
    market = packet["markets"][0]
    assert packet["producer_schema"] == "bayes_basket_no_live_inventory_v1"
    assert market["inventory_json"]["filled_by_bucket"] == {"38°C": 0.0, "39°C": 0.0}
    assert market["inventory_json"]["production_no_live"] is True
    assert market["inventory_json"]["exchange_position_read"] is False


def test_no_live_inventory_projects_all_authoritative_full_fills_and_deducts_cost() -> None:
    with _transition_db() as conn:
        _insert_transition(conn, intent="low-1", bucket="38°C", token="yes-38", state="FULL_FILLED", size=2, notional=0.60)
        _insert_transition(conn, intent="high-1", bucket="39°C", token="yes-39", state="FULL_FILLED", size=3, notional=0.90)
        _insert_transition(conn, intent="low-2", bucket="38°C", token="yes-38", state="FULL_FILLED", size=4, notional=1.20)
        assert has_execution_transition_versions(conn)
        packet = build_no_live_inventory_ingress(
            conn, remaining_budget_usdc=10.0, observed_at_utc="2026-07-23T05:00:00Z"
        )
    market = packet["markets"][0]
    data = market["inventory_json"]
    assert data["filled_by_bucket"] == {"38°C": 6.0, "39°C": 3.0}
    assert data["filled_cost_by_bucket"]["38°C"] == pytest.approx(1.80)
    assert market["remaining_budget_usdc"] == pytest.approx(7.30)
    assert len(data["lineage"]) == 3
    assert len(data["transition_content_sha256"]) == 3
    assert packet["source_schema"] == "target_execution_transition_runtime_no_live"


def test_no_live_inventory_projects_terminal_partial_fill_actuals() -> None:
    with _transition_db() as conn:
        _insert_transition(
            conn,
            intent="partial-1",
            bucket="38°C",
            token="yes-38",
            state="PARTIAL_FILLED",
            size=0.25,
            notional=0.10,
        )
        packet = build_no_live_inventory_ingress(
            conn,
            remaining_budget_usdc=10.0,
            observed_at_utc="2026-07-23T05:00:00Z",
        )

    market = packet["markets"][0]
    assert market["inventory_json"]["filled_by_bucket"]["38°C"] == (
        pytest.approx(0.25)
    )
    assert market["inventory_json"]["filled_cost_by_bucket"]["38°C"] == (
        pytest.approx(0.10)
    )
    assert market["remaining_budget_usdc"] == pytest.approx(9.90)
    assert market["inventory_json"]["lineage"][0]["state"] == (
        "PARTIAL_FILLED"
    )


def _ledger_db() -> sqlite3.Connection:
    conn = _db()
    conn.execute(
        "CREATE TABLE basket_target_order_intents("
        "order_intent_id TEXT PRIMARY KEY, cycle_id TEXT, bucket TEXT,"
        " token_id TEXT, order_action TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_target_execution_cycles("
        "cycle_id TEXT PRIMARY KEY, station_id TEXT, event_date TEXT,"
        " settlement_timezone TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_target_order_reconciliations("
        "order_intent_id TEXT PRIMARY KEY, state TEXT, filled_size REAL,"
        " filled_notional_usdc REAL, exchange_order_id TEXT, updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO basket_target_execution_cycles VALUES"
        "('cycle-live','TEST','2026-07-23','America/New_York')"
    )
    return conn


def _insert_ledger_fill(
    conn: sqlite3.Connection,
    *,
    intent: str,
    bucket: str,
    token: str,
    size: float,
    notional: float,
    action: str = "BUY",
    state: str = "resolved_filled",
    updated_at: str = "2026-07-23T04:00:00Z",
) -> None:
    conn.execute(
        "INSERT INTO basket_target_order_intents VALUES(?,?,?,?,?)",
        (intent, "cycle-live", bucket, token, action),
    )
    conn.execute(
        "INSERT INTO basket_target_order_reconciliations VALUES(?,?,?,?,?,?)",
        (intent, state, size, notional, "0x" + intent, updated_at),
    )


def test_live_fills_settled_in_the_ledger_are_inventory() -> None:
    """A fill the exchange settled is a position, even with no transition.

    The transition runtime is the no-live simulator's fill source and never
    ran on a live order; on 2026-09-05 the first five real fills sat in the
    reconciliation ledger while every inventory snapshot read zero.
    """

    with _ledger_db() as conn:
        _insert_ledger_fill(
            conn, intent="live-1", bucket="39°C", token="yes-39", size=5, notional=2.15
        )
        assert not has_execution_transition_versions(conn)
        packet = build_no_live_inventory_ingress(
            conn, remaining_budget_usdc=10.0, observed_at_utc="2026-07-23T05:00:00Z"
        )
    market = packet["markets"][0]
    data = market["inventory_json"]
    assert data["filled_by_bucket"] == {"38°C": 0.0, "39°C": 5.0}
    assert data["filled_cost_by_bucket"]["39°C"] == pytest.approx(2.15)
    assert data["token_ids_by_bucket"] == {"38°C": "yes-38", "39°C": "yes-39"}
    assert market["remaining_budget_usdc"] == pytest.approx(7.85)
    assert data["source"] == "target_reconciliation_ledger_fills"
    assert data["lineage"][0]["source"] == "target_reconciliation_ledger"
    assert data["lineage"][0]["exchange_order_id"] == "0xlive-1"
    assert len(data["ledger_fill_content_sha256"]) == 1
    assert packet["source_schema"] == "target_reconciliation_ledger_fills"
    assert market["inventory_version"].startswith("invrec_ingress_")


def test_a_ledger_sell_fill_removes_what_a_buy_added() -> None:
    with _ledger_db() as conn:
        _insert_ledger_fill(
            conn, intent="buy-1", bucket="39°C", token="yes-39", size=5, notional=2.15
        )
        _insert_ledger_fill(
            conn, intent="sell-1", bucket="39°C", token="yes-39", size=2,
            notional=1.00, action="SELL", updated_at="2026-07-23T04:30:00Z",
        )
        packet = build_no_live_inventory_ingress(
            conn, remaining_budget_usdc=10.0, observed_at_utc="2026-07-23T05:00:00Z"
        )
    data = packet["markets"][0]["inventory_json"]
    assert data["filled_by_bucket"]["39°C"] == pytest.approx(3.0)
    assert data["filled_cost_by_bucket"]["39°C"] == pytest.approx(1.15)


def test_a_ledger_fill_after_the_quote_asof_fails_closed() -> None:
    with _ledger_db() as conn:
        _insert_ledger_fill(
            conn, intent="late", bucket="39°C", token="yes-39", size=5,
            notional=2.15, updated_at="2026-07-23T05:01:00Z",
        )
        with pytest.raises(ContractError, match="ledger_fill_after_quote_asof"):
            build_no_live_inventory_ingress(
                conn, remaining_budget_usdc=10.0, observed_at_utc="2026-07-23T05:00:00Z"
            )


def test_a_ledger_fill_off_the_registry_token_is_refused() -> None:
    with _ledger_db() as conn:
        _insert_ledger_fill(
            conn, intent="wrong", bucket="39°C", token="yes-38", size=5, notional=2.15
        )
        with pytest.raises(ContractError, match="ledger_fill_bucket_token_scope_conflict"):
            build_no_live_inventory_ingress(
                conn, remaining_budget_usdc=10.0, observed_at_utc="2026-07-23T05:00:00Z"
            )


def test_transition_reconcile_state_never_becomes_known_inventory() -> None:
    with _transition_db() as conn:
        _insert_transition(conn, intent="bad", bucket="38°C", token="yes-38", state="RECONCILE_REQUIRED", size=2, notional=0.60)
        with pytest.raises(ContractError, match="execution_transition_reconcile_required"):
            build_no_live_inventory_ingress(
                conn, remaining_budget_usdc=10.0, observed_at_utc="2026-07-23T05:00:00Z"
            )


def test_transition_after_quote_asof_fails_closed_instead_of_rewinding() -> None:
    with _transition_db() as conn:
        _insert_transition(
            conn, intent="future", bucket="38°C", token="yes-38", state="FULL_FILLED",
            size=2, notional=0.60, updated_at="2026-07-23T05:01:00Z",
        )
        with pytest.raises(ContractError, match="execution_transition_after_quote_asof"):
            build_no_live_inventory_ingress(
                conn, remaining_budget_usdc=10.0, observed_at_utc="2026-07-23T05:00:00Z"
            )

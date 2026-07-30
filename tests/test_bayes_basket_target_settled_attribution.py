from __future__ import annotations

import json
import sqlite3

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.delta_v2_settlement_label import LABEL_SCHEMA
from bayes_basket_target.delta_v2_training import FACT_COLUMNS, FACT_SCHEMA
from bayes_basket_target.execution_version_vector import build_execution_version_vector
from bayes_basket_target.settled_attribution import (
    TERMS_SCHEMA,
    build_settled_attribution,
)


AS_OF = "2026-07-30T00:00:00Z"
SHA = "a" * 64


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_policy_training_fact("
        + ",".join(
            f"{field} INTEGER" if field in {"source_sequence", "intent_count"}
            else f"{field} TEXT"
            for field in FACT_COLUMNS
        )
        + ")"
    )
    conn.execute(
        "CREATE TABLE basket_policy_training_label("
        "label_id TEXT,fact_id TEXT,truth_id TEXT,truth_revision_no INTEGER,"
        "truth_sha256 TEXT,official_tmax_c REAL,winning_bucket TEXT,"
        "settlement_closed_at_utc TEXT,labeled_at_utc TEXT,outcome_json TEXT,"
        "authority_scope TEXT,supersedes_label_id TEXT,content_sha256 TEXT)"
    )
    return conn


def _policy() -> dict[str, object]:
    return {
        "schema": "delta_basket_score_policy_version_vector_v2",
        "strategy_version": "basket-v2",
        "season_bin": "summer",
        "maturity_bin": "d0",
        "liquidity_bin": "normal",
        "station_policy": {"policy_sha256": "1" * 64},
        "cash_phase_policy_version": "cash-v1",
        "cash_phase_policy_sha256": "2" * 64,
        "delta_v2_artifact_id": "delta-policy-v2",
        "delta_v2_artifact_sha256": "3" * 64,
    }


def _feature(*, complete_vector: bool = True) -> dict[str, object]:
    vector = build_execution_version_vector(
        {
            "schema": "bayes_basket_target_execution_version_vector_v2",
            "probability_sha256": "4" * 64,
            "official_ws_quote_sha256": "5" * 64,
            "inventory_sha256": "6" * 64,
            "order_state_sha256": "7" * 64,
            "station_policy_sha256": "1" * 64,
            "cash_policy_decision_sha256": "8" * 64,
            "economic_policy_schema": "delta_basket_score_policy_artifact_v2",
            "economic_policy_artifact_sha256": "3" * 64,
            "target_price_id": "target-price-38",
            "quote_observed_at": "2026-07-28T00:00:00Z",
            "quote_expires_at": "2026-07-28T00:01:00Z",
        }
    ).to_dict()
    return {
        "schema": "delta_basket_score_forward_feature_vector_v2",
        "execution_version_vectors": [vector] if complete_vector else [],
        "no_live_boundary": {
            "execution_mode": "production_no_live",
            "live_submit_authorized": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
    }


def _fact(conn: sqlite3.Connection, *, complete_vector: bool = True) -> tuple[str, dict[str, object], dict[str, object]]:
    policy = _policy()
    feature = _feature(complete_vector=complete_vector)
    values: dict[str, object] = {
        "station_id": "RKPK",
        "event_date": "2026-07-28",
        "settlement_timezone": "Asia/Karachi",
        "decision_at_utc": "2026-07-28T00:00:00Z",
        "decision_scope_key": "scope-1",
        "captured_at_utc": "2026-07-28T00:00:01Z",
        "source_sequence": 3,
        "quote_observed_at": "2026-07-28T00:00:00Z",
        "quote_expires_at": "2026-07-28T00:01:00Z",
        "quote_freshness_policy_sha256": "9" * 64,
        "stage": "P1",
        "probability_envelope_id": "env-1",
        "posterior_version": "p1-v1",
        "posterior_sha256": "a" * 64,
        "ws_snapshot_id": "ws-1",
        "official_exclusion_mask_sha256": "b" * 64,
        "quote_version": "quote-v1",
        "quote_sha256": "c" * 64,
        "inventory_snapshot_id": "inventory-1",
        "inventory_version": "inventory-v1",
        "inventory_sha256": "d" * 64,
        "order_state_snapshot_id": "order-1",
        "order_state_version": "order-v1",
        "order_state_sha256": "e" * 64,
        "cycle_id": "cycle-1",
        "decision_id": "decision-1",
        "decision_sha256": "f" * 64,
        "plan_sha256": "0" * 64,
        "intent_count": 1,
        "intent_set_sha256": "1" * 64,
        "policy_version_vector_json": _json(policy),
        "feature_json": _json(feature),
        "partition_key": "RKPK|summer|P1|d0|normal",
        "authority_scope": "PRODUCTION_NO_LIVE_FORWARD",
        "created_at_utc": "2026-07-28T00:00:01Z",
    }
    fact_sha = content_sha256({"schema": FACT_SCHEMA, **values})
    fact_id = f"d2fact_{fact_sha[:40]}"
    stored = {"fact_id": fact_id, **values, "content_sha256": fact_sha}
    conn.execute(
        "INSERT INTO basket_policy_training_fact(" + ",".join(FACT_COLUMNS) + ") VALUES("
        + ",".join("?" for _ in FACT_COLUMNS) + ")",
        tuple(stored[field] for field in FACT_COLUMNS),
    )
    return fact_id, stored, policy


def _label(
    conn: sqlite3.Connection,
    *,
    fact: dict[str, object],
    policy: dict[str, object],
    include_terms: bool = True,
    bad_closure: bool = False,
) -> str:
    policy_sha = content_sha256(policy)
    outcome: dict[str, object] = {
        "schema": "delta_basket_score_no_live_outcome_v2",
        "label_semantics": "hypothetical_requested_intents_not_fills",
        "winning_bucket": "38°C",
        "requested_cost_usdc": 0.6,
        "winning_requested_shares": 2.0,
        "hypothetical_requested_pnl_usdc": 1.4,
        "orders_submitted": 0,
        "calls_order_gateway": False,
    }
    if include_terms:
        outcome["settled_attribution"] = {
            "schema": TERMS_SCHEMA,
            "fact_id": fact["fact_id"],
            "fact_content_sha256": fact["content_sha256"],
            "truth_sha256": SHA,
            "winning_bucket": "38°C",
            "policy_version_vector_sha256": policy_sha,
            "coverage_usdc": 2.0,
            "overlap_usdc": -0.2,
            "edge_usdc": 0.1,
            "execution_cost_usdc": 0.4,
            "failure_cost_usdc": 0.1 if not bad_closure else 0.0,
            "counterfactual_pnl_usdc": 1.4,
        }
    identity: dict[str, object] = {
        "fact_id": fact["fact_id"],
        "truth_id": "truth-1",
        "truth_revision_no": 0,
        "truth_sha256": SHA,
        "official_tmax_c": 38.1,
        "winning_bucket": "38°C",
        "settlement_closed_at_utc": "2026-07-29T00:00:00Z",
        "labeled_at_utc": "2026-07-29T00:01:00Z",
        "outcome_json": _json(outcome),
        "authority_scope": "NO_LIVE_REPLAY_CANDIDATE",
        "supersedes_label_id": None,
    }
    digest = content_sha256({"schema": LABEL_SCHEMA, **identity})
    label_id = f"d2label_{digest[:40]}"
    conn.execute(
        "INSERT INTO basket_policy_training_label VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (label_id, *(identity[key] for key in identity), digest),
    )
    return label_id


def test_counterfactual_terms_close_and_reader_is_read_only() -> None:
    conn = _db()
    _fact_id, fact, policy = _fact(conn)
    _label(conn, fact=fact, policy=policy)
    changes_before = conn.total_changes

    receipt = build_settled_attribution(conn, as_of_utc=AS_OF)

    assert conn.total_changes == changes_before
    assert receipt["state"] == "COMPLETE_COUNTERFACTUAL_NO_LIVE"
    assert receipt["realized_pnl_status"] == "NA_PRODUCTION_NO_LIVE"
    row = receipt["rows"][0]
    assert row["status"] == "COUNTERFACTUAL_CLOSED"
    assert row["counterfactual_pnl_usdc"] == pytest.approx(1.4)
    assert row["realized_pnl_usdc"] is None
    terms = row["attribution_terms"]
    assert terms["coverage_usdc"] + terms["overlap_usdc"] + terms["edge_usdc"] - terms["execution_cost_usdc"] - terms["failure_cost_usdc"] == pytest.approx(1.4)
    aggregate = next(iter(receipt["by_policy_version_vector"].values()))
    assert aggregate["attributed_count"] == 1
    assert aggregate["counterfactual_closure_usdc"] == pytest.approx(0.0)
    assert receipt["calls_order_gateway"] is False
    assert receipt["orders_submitted"] == 0


def test_missing_label_is_explicit_na_not_zero() -> None:
    conn = _db()
    _fact(conn)

    receipt = build_settled_attribution(conn, as_of_utc=AS_OF)

    assert receipt["state"] == "INCOMPLETE_NA"
    assert receipt["rows"][0]["status"] == "NA_UNSETTLED_OR_LABEL_MISSING"
    assert receipt["rows"][0]["counterfactual_pnl_usdc"] is None
    assert receipt["rows"][0]["realized_pnl_usdc"] is None


def test_station_and_event_window_scope_is_bounded_in_authority_query() -> None:
    conn = _db()
    _fact(conn)

    included = build_settled_attribution(
        conn,
        as_of_utc=AS_OF,
        station_ids=["RKPK"],
        window_start_date="2026-07-28",
        window_end_date="2026-07-28",
    )
    excluded = build_settled_attribution(
        conn,
        as_of_utc=AS_OF,
        station_ids=["OTHER"],
        window_start_date="2026-07-28",
        window_end_date="2026-07-28",
    )

    assert included["fact_count"] == 1
    assert included["station_ids"] == ["RKPK"]
    assert excluded["fact_count"] == 0
    assert excluded["state"] == "NO_SETTLED_FACTS_NA"


def test_old_standard_label_without_terms_is_explicit_na() -> None:
    conn = _db()
    _fact_id, fact, policy = _fact(conn)
    _label(conn, fact=fact, policy=policy, include_terms=False)

    receipt = build_settled_attribution(conn, as_of_utc=AS_OF)

    assert receipt["state"] == "INCOMPLETE_NA"
    row = receipt["rows"][0]
    assert row["status"] == "NA_ATTRIBUTION_TERMS_MISSING"
    assert row["counterfactual_pnl_usdc"] == pytest.approx(1.4)
    assert row["attribution_terms"] is None


def test_fact_hash_drift_fails_closed() -> None:
    conn = _db()
    fact_id, _fact_row, _policy_row = _fact(conn)
    conn.execute(
        "UPDATE basket_policy_training_fact SET content_sha256=? WHERE fact_id=?",
        ("0" * 64, fact_id),
    )

    receipt = build_settled_attribution(conn, as_of_utc=AS_OF)

    assert receipt["state"] == "BLOCKED_FAIL_CLOSED"
    assert "fact_content_sha256_drift" in receipt["rows"][0]["blockers"]


def test_label_hash_drift_fails_closed() -> None:
    conn = _db()
    _fact_id, fact, policy = _fact(conn)
    label_id = _label(conn, fact=fact, policy=policy)
    conn.execute(
        "UPDATE basket_policy_training_label SET content_sha256=? WHERE label_id=?",
        ("0" * 64, label_id),
    )

    receipt = build_settled_attribution(conn, as_of_utc=AS_OF)

    assert receipt["state"] == "BLOCKED_FAIL_CLOSED"
    assert "label_content_sha256_drift" in receipt["rows"][0]["blockers"]


def test_incomplete_execution_version_vector_fails_closed() -> None:
    conn = _db()
    _fact(conn, complete_vector=False)

    receipt = build_settled_attribution(conn, as_of_utc=AS_OF)

    assert receipt["state"] == "BLOCKED_FAIL_CLOSED"
    assert "execution_version_vector_count_invalid" in receipt["rows"][0]["blockers"]


def test_decomposition_that_does_not_close_fails_closed() -> None:
    conn = _db()
    _fact_id, fact, policy = _fact(conn)
    _label(conn, fact=fact, policy=policy, bad_closure=True)

    receipt = build_settled_attribution(conn, as_of_utc=AS_OF)

    assert receipt["state"] == "BLOCKED_FAIL_CLOSED"
    assert "settled_attribution_closure_failed" in receipt["rows"][0]["blockers"]


def test_max_facts_is_resource_bounded() -> None:
    conn = _db()
    with pytest.raises(ContractError, match="settled_attribution_max_facts_invalid"):
        build_settled_attribution(conn, as_of_utc=AS_OF, max_facts=257)


def test_keyset_pagination_keeps_each_attribution_page_bounded() -> None:
    conn = _db()
    first_id, first, policy = _fact(conn)
    _label(conn, fact=first, policy=policy)
    # A new scope is sufficient to make a distinct, append-only fact.
    second_id = first_id + "x"
    second = dict(first)
    second["fact_id"] = second_id
    second["decision_scope_key"] = "scope-2"
    identity = {
        key: second[key]
        for key in FACT_COLUMNS
        if key not in {"fact_id", "content_sha256"}
    }
    second["content_sha256"] = content_sha256({"schema": FACT_SCHEMA, **identity})
    second["fact_id"] = f"d2fact_{second['content_sha256'][:40]}"
    conn.execute(
        "INSERT INTO basket_policy_training_fact(" + ",".join(FACT_COLUMNS) + ") VALUES("
        + ",".join("?" for _ in FACT_COLUMNS) + ")",
        tuple(second[field] for field in FACT_COLUMNS),
    )
    _label(conn, fact=second, policy=policy)

    page_one = build_settled_attribution(conn, as_of_utc=AS_OF, max_facts=1)
    page_two = build_settled_attribution(
        conn,
        as_of_utc=AS_OF,
        max_facts=1,
        after_fact_rowid=page_one["source_cursor_after"],
    )

    assert page_one["has_more"] is True
    assert page_two["has_more"] is False
    assert page_one["rows"][0]["fact_id"] != page_two["rows"][0]["fact_id"]

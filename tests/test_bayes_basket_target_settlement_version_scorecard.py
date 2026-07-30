from __future__ import annotations

import json
import sqlite3

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.delta_v2_settlement_label import LABEL_SCHEMA
from bayes_basket_target.delta_v2_training import FACT_COLUMNS, FACT_SCHEMA
from bayes_basket_target.execution_version_vector import build_execution_version_vector
from bayes_basket_target.settled_attribution import TERMS_SCHEMA
from bayes_basket_target.settlement_version_scorecard import (
    build_settlement_version_evidence_packet,
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
            f"{field} INTEGER"
            if field in {"source_sequence", "intent_count"}
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
    conn.execute(
        "CREATE TABLE basket_target_market_registry("
        "station_id TEXT,event_date TEXT,settlement_timezone TEXT,"
        "source_eligible INTEGER)"
    )
    conn.execute(
        "INSERT INTO basket_target_market_registry VALUES(?,?,?,1)",
        ("RKPK", "2026-07-28", "Asia/Seoul"),
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


def _feature(*, probability: bool = True) -> dict[str, object]:
    vector = build_execution_version_vector({
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
    }).to_dict()
    result: dict[str, object] = {
        "schema": "delta_basket_score_forward_feature_vector_v2",
        "execution_version_vectors": [vector],
        "final_support": ["38°C", "39°C"],
        "no_live_boundary": {
            "execution_mode": "production_no_live",
            "live_submit_authorized": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
    }
    if probability:
        result["probability_by_bucket"] = {
            "37°C": 0.1,
            "38°C": 0.7,
            "39°C": 0.2,
        }
    return result


def _fact(
    conn: sqlite3.Connection,
    *,
    scope: str = "scope-1",
    probability: bool = True,
) -> tuple[dict[str, object], dict[str, object]]:
    policy = _policy()
    values: dict[str, object] = {
        "station_id": "RKPK",
        "event_date": "2026-07-28",
        "settlement_timezone": "Asia/Seoul",
        "decision_at_utc": "2026-07-28T00:00:00Z",
        "decision_scope_key": scope,
        "captured_at_utc": "2026-07-28T00:00:01Z",
        "source_sequence": 3,
        "quote_observed_at": "2026-07-28T00:00:00Z",
        "quote_expires_at": "2026-07-28T00:01:00Z",
        "quote_freshness_policy_sha256": "9" * 64,
        "stage": "P1",
        "probability_envelope_id": "env-" + scope,
        "posterior_version": "p1-v1",
        "posterior_sha256": "a" * 64,
        "official_exclusion_mask_sha256": "b" * 64,
        "ws_snapshot_id": "ws-" + scope,
        "quote_version": "quote-v1",
        "quote_sha256": "c" * 64,
        "inventory_snapshot_id": "inventory-" + scope,
        "inventory_version": "inventory-v1",
        "inventory_sha256": "d" * 64,
        "order_state_snapshot_id": "order-" + scope,
        "order_state_version": "order-v1",
        "order_state_sha256": "e" * 64,
        "cycle_id": "cycle-" + scope,
        "decision_id": "decision-" + scope,
        "decision_sha256": "f" * 64,
        "plan_sha256": "0" * 64,
        "intent_count": 1,
        "intent_set_sha256": "1" * 64,
        "policy_version_vector_json": _json(policy),
        "feature_json": _json(_feature(probability=probability)),
        "partition_key": "RKPK|summer|P1|d0|normal",
        "authority_scope": "PRODUCTION_NO_LIVE_FORWARD",
        "created_at_utc": "2026-07-28T00:00:01Z",
    }
    digest = content_sha256({"schema": FACT_SCHEMA, **values})
    stored = {
        "fact_id": f"d2fact_{digest[:40]}",
        **values,
        "content_sha256": digest,
    }
    conn.execute(
        "INSERT INTO basket_policy_training_fact("
        + ",".join(FACT_COLUMNS)
        + ") VALUES("
        + ",".join("?" for _ in FACT_COLUMNS)
        + ")",
        tuple(stored[field] for field in FACT_COLUMNS),
    )
    return stored, policy


def _label(
    conn: sqlite3.Connection,
    *,
    fact: dict[str, object],
    policy: dict[str, object],
    include_terms: bool,
    labeled_at: str = "2026-07-29T00:01:00Z",
) -> None:
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
            "policy_version_vector_sha256": content_sha256(policy),
            "coverage_usdc": 2.0,
            "overlap_usdc": -0.2,
            "edge_usdc": 0.1,
            "execution_cost_usdc": 0.4,
            "failure_cost_usdc": 0.1,
            "counterfactual_pnl_usdc": 1.4,
        }
    identity: dict[str, object] = {
        "fact_id": fact["fact_id"],
        "truth_id": "truth-" + str(fact["fact_id"])[-8:],
        "truth_revision_no": 0,
        "truth_sha256": SHA,
        "official_tmax_c": 38.1,
        "winning_bucket": "38°C",
        "settlement_closed_at_utc": "2026-07-29T00:00:00Z",
        "labeled_at_utc": labeled_at,
        "outcome_json": _json(outcome),
        "authority_scope": "NO_LIVE_REPLAY_CANDIDATE",
        "supersedes_label_id": None,
    }
    digest = content_sha256({"schema": LABEL_SCHEMA, **identity})
    conn.execute(
        "INSERT INTO basket_policy_training_label VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"d2label_{digest[:40]}",
            *(identity[key] for key in identity),
            digest,
        ),
    )


def _build(conn: sqlite3.Connection, *, max_facts: int = 512) -> dict:
    return build_settlement_version_evidence_packet(
        conn,
        as_of_utc=AS_OF,
        station_ids=["RKPK"],
        window_start_date="2026-07-28",
        window_end_date="2026-07-28",
        max_facts=max_facts,
    )


def test_complete_counterfactual_scorecard_still_marks_actual_fill_na() -> None:
    conn = _db()
    fact, policy = _fact(conn)
    _label(conn, fact=fact, policy=policy, include_terms=True)
    changes = conn.total_changes

    packet = _build(conn)

    assert conn.total_changes == changes
    assert packet["state"] == "BLOCKED_EVIDENCE"
    assert packet["production_no_live_blocked_by_forward_oos"] is False
    assert packet["promotion_receipt"]["formal_promotion_eligible"] is False
    assert packet["promotion_receipt"]["activation_written"] is False
    scorecard = packet["scorecards"][0]
    assert scorecard["fixed_denominators"] == {
        "fact_count": 1,
        "labeled_count": 1,
        "calibration_count": 1,
        "attribution_count": 1,
        "actual_fill_count": 0,
    }
    assert scorecard["calibration"]["mean_brier"] == pytest.approx(0.14)
    assert scorecard["coverage"]["support_hit_rate"] == 1.0
    assert scorecard["pnl_attribution"]["realized_pnl_usdc"] is None
    assert packet["rows"][0]["realized_pnl_status"] == (
        "NA_ACTUAL_FILL_EVIDENCE_MISSING"
    )


def test_legacy_label_without_terms_is_na_not_zero() -> None:
    conn = _db()
    fact, policy = _fact(conn)
    _label(conn, fact=fact, policy=policy, include_terms=False)

    packet = _build(conn)

    assert packet["state"] == "BLOCKED_EVIDENCE"
    assert "NA_ATTRIBUTION_TERMS_MISSING" in packet["evidence_blockers"]
    assert packet["rows"][0]["counterfactual_pnl_usdc"] == pytest.approx(1.4)
    assert packet["rows"][0]["realized_pnl_usdc"] is None


def test_future_label_is_unsettled_evidence_not_integrity_failure() -> None:
    conn = _db()
    fact, policy = _fact(conn)
    _label(
        conn,
        fact=fact,
        policy=policy,
        include_terms=True,
        labeled_at="2026-07-31T00:01:00Z",
    )

    packet = _build(conn)

    assert packet["state"] == "BLOCKED_EVIDENCE"
    assert packet["integrity_blockers"] == []
    assert packet["rows"][0]["settlement_status"] == (
        "NA_UNSETTLED_OR_LABEL_MISSING"
    )


def test_registry_timezone_drift_fails_closed() -> None:
    conn = _db()
    fact, policy = _fact(conn)
    _label(conn, fact=fact, policy=policy, include_terms=True)
    conn.execute(
        "UPDATE basket_target_market_registry "
        "SET settlement_timezone='America/New_York'"
    )

    packet = _build(conn)

    assert packet["state"] == "BLOCKED_FAIL_CLOSED"
    assert any(
        value.startswith("settlement_timezone_registry_mismatch:")
        for value in packet["integrity_blockers"]
    )


def test_fact_hash_drift_fails_closed() -> None:
    conn = _db()
    fact, policy = _fact(conn)
    _label(conn, fact=fact, policy=policy, include_terms=True)
    conn.execute(
        "UPDATE basket_policy_training_fact SET content_sha256=?",
        ("0" * 64,),
    )

    packet = _build(conn)

    assert packet["state"] == "BLOCKED_FAIL_CLOSED"
    assert any(
        "fact_content_sha256_drift" in value
        for value in packet["integrity_blockers"]
    )


def test_fact_cap_is_hard_and_never_promotes_truncated_window() -> None:
    conn = _db()
    first, policy = _fact(conn, scope="scope-1")
    _label(conn, fact=first, policy=policy, include_terms=True)
    second, policy = _fact(conn, scope="scope-2")
    _label(conn, fact=second, policy=policy, include_terms=True)

    packet = _build(conn, max_facts=1)

    assert packet["state"] == "BLOCKED_FAIL_CLOSED"
    assert "settlement_version_fact_row_cap_reached" in (
        packet["integrity_blockers"]
    )
    assert packet["promotion_receipt"]["formal_promotion_eligible"] is False


def test_missing_probability_is_explicit_evidence_gap() -> None:
    conn = _db()
    fact, policy = _fact(conn, probability=False)
    _label(conn, fact=fact, policy=policy, include_terms=True)

    packet = _build(conn)

    assert packet["state"] == "BLOCKED_EVIDENCE"
    assert "NA_PROBABILITY_VECTOR_MISSING" in packet["evidence_blockers"]
    assert packet["scorecards"][0]["calibration"]["mean_brier"] is None


def test_invalid_scope_and_reversed_window_are_rejected() -> None:
    conn = _db()
    with pytest.raises(ContractError, match="station_scope_invalid"):
        build_settlement_version_evidence_packet(
            conn,
            as_of_utc=AS_OF,
            station_ids=[],
            window_start_date="2026-07-28",
            window_end_date="2026-07-28",
        )
    with pytest.raises(ContractError, match="window_reversed"):
        build_settlement_version_evidence_packet(
            conn,
            as_of_utc=AS_OF,
            station_ids=["RKPK"],
            window_start_date="2026-07-29",
            window_end_date="2026-07-28",
        )

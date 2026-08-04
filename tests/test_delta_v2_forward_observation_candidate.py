from __future__ import annotations

import json
import sqlite3

from bayes_basket_target.contracts import ProbabilityEnvelope, content_sha256
from bayes_basket_target.delta_v2_forward_observation_candidate import (
    assert_forward_observation_candidate,
    build_from_connection,
)
from bayes_basket_target.delta_v2_settlement_label import LABEL_SCHEMA
from bayes_basket_target.delta_v2_training import FACT_COLUMNS, FACT_SCHEMA


AS_OF = "2026-08-01T12:30:00Z"


def _schema(conn: sqlite3.Connection) -> None:
    fact_columns = ",".join(f"{column} TEXT" for column in FACT_COLUMNS)
    conn.execute(f"CREATE TABLE basket_policy_training_fact ({fact_columns})")
    conn.execute(
        "CREATE TABLE basket_probability_envelope ("
        "envelope_id TEXT,stage TEXT,checkpoint_id TEXT,station_id TEXT,event_date TEXT,"
        "settlement_timezone TEXT,decision_at_utc TEXT,fresh_until_utc TEXT,"
        "bucket_ordinals_json TEXT,probabilities_json TEXT,posterior_version TEXT,"
        "posterior_sha256 TEXT,bridge_sha256 TEXT,lineage_json TEXT,content_sha256 TEXT,"
        "state TEXT,created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_official_ws_snapshot ("
        "snapshot_id TEXT,market_id TEXT,station_id TEXT,event_date TEXT,settlement_timezone TEXT,"
        "sequence INTEGER,observed_at TEXT,quote_version TEXT,quote_observed_at TEXT,"
        "quote_expires_at TEXT,quote_freshness_policy_sha256 TEXT,quote_source TEXT,"
        "inventory_version TEXT,order_state_version TEXT,remaining_budget_usdc REAL,"
        "buckets_json TEXT,cash_phase_input_json TEXT,content_sha256 TEXT,state TEXT,created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_policy_training_label ("
        "label_id TEXT,fact_id TEXT,truth_id TEXT,truth_revision_no INTEGER,"
        "truth_sha256 TEXT,official_tmax_c REAL,winning_bucket TEXT,"
        "settlement_closed_at_utc TEXT,labeled_at_utc TEXT,outcome_json TEXT,"
        "authority_scope TEXT,supersedes_label_id TEXT,content_sha256 TEXT)"
    )


def _fact_row(*, quote_sha256: str) -> dict[str, str]:
    feature = {
        "schema": "delta_basket_score_forward_feature_vector_v2",
        "cash_phase_decision": {
            "next_phase": "TAIL",
            "policy_version": "rkpk-candidate-v1",
        },
        "plan": {
            "delta_v2_action_selection": {
                "selected_action": {
                    "authorized": True,
                    "action": {"bucket": "39°C", "limit_price": 0.2},
                },
            },
        },
        "no_execution_policy": {"policy": "NO_EXECUTION"},
    }
    row = {
        field: "" for field in FACT_COLUMNS if field not in {"fact_id", "content_sha256"}
    }
    row.update({
        "station_id": "RKPK",
        "event_date": "2026-07-29",
        "settlement_timezone": "Asia/Seoul",
        "decision_at_utc": "2026-07-29T05:30:00Z",
        "decision_scope_key": "scope-1",
        "captured_at_utc": "2026-07-29T05:30:00Z",
        "source_sequence": "1",
        "quote_observed_at": "2026-07-29T05:29:30Z",
        "quote_expires_at": "2026-07-29T05:31:00Z",
        "quote_freshness_policy_sha256": "1" * 64,
        "stage": "P1",
        "probability_envelope_id": "p1-1",
        "posterior_version": "p1-v1",
        "posterior_sha256": "2" * 64,
        "official_exclusion_mask_sha256": "3" * 64,
        "ws_snapshot_id": "ws-1",
        "quote_version": "q1",
        "quote_sha256": quote_sha256,
        "inventory_snapshot_id": "inventory-1",
        "inventory_version": "inventory-v1",
        "inventory_sha256": "4" * 64,
        "order_state_snapshot_id": "order-1",
        "order_state_version": "order-v1",
        "order_state_sha256": "5" * 64,
        "cycle_id": "cycle-1",
        "decision_id": "decision-1",
        "decision_sha256": "6" * 64,
        "plan_sha256": "7" * 64,
        "intent_count": "0",
        "intent_set_sha256": "8" * 64,
        "policy_version_vector_json": "{}",
        "feature_json": json.dumps(feature, sort_keys=True),
        "partition_key": "RKPK|summer|P1|d2|liquid",
        "authority_scope": "PRODUCTION_NO_LIVE_FORWARD",
        "created_at_utc": "2026-07-29T05:30:00Z",
    })
    fact_sha = content_sha256({"schema": FACT_SCHEMA, **row})
    return {"fact_id": f"d2fact_{fact_sha[:40]}", **row, "content_sha256": fact_sha}


def _insert_valid_rows(conn: sqlite3.Connection) -> str:
    envelope = ProbabilityEnvelope.from_mapping({
        "stage": "P1",
        "checkpoint_id": "checkpoint-1",
        "station_id": "RKPK",
        "event_date": "2026-07-29",
        "settlement_timezone": "Asia/Seoul",
        "decision_at_utc": "2026-07-29T05:00:00Z",
        "fresh_until_utc": "2026-07-29T06:00:00Z",
        "bucket_ordinals": {"39°C": 0},
        "probabilities": {"39°C": 1.0},
        "posterior_version": "p1-v1",
        "posterior_sha256": "2" * 64,
        "bridge_sha256": "9" * 64,
        "lineage": {"truth": "awc"},
    })
    conn.execute(
        "INSERT INTO basket_probability_envelope VALUES(" + ",".join("?" for _ in range(17)) + ")",
        ("p1-1", envelope.stage, envelope.checkpoint_id, envelope.station_id,
         envelope.event_date, envelope.settlement_timezone, envelope.decision_at_utc,
         envelope.fresh_until_utc, json.dumps(envelope.bucket_ordinals, sort_keys=True),
         json.dumps(envelope.probabilities, sort_keys=True), envelope.posterior_version,
         envelope.posterior_sha256, envelope.bridge_sha256,
         json.dumps(envelope.lineage, sort_keys=True), envelope.content_sha256,
         "COMMITTED", "2026-07-29T05:00:00Z"),
    )
    snapshot_identity = {
        "snapshot_id": "ws-1",
        "market_id": "market-1",
        "station_id": "RKPK",
        "event_date": "2026-07-29",
        "settlement_timezone": "Asia/Seoul",
        "sequence": 1,
        "observed_at": "2026-07-29T05:29:30Z",
        "quote_version": "q1",
        "quote_observed_at": "2026-07-29T05:29:30Z",
        "quote_expires_at": "2026-07-29T05:31:00Z",
        "quote_freshness_policy_sha256": "1" * 64,
        "quote_source": "OFFICIAL_CLOB_WS",
        "inventory_version": "inventory-v1",
        "order_state_version": "order-v1",
        "remaining_budget_usdc": 100.0,
        "buckets_json": [{"bucket": "39°C"}],
        "cash_phase_input_json": {},
        "state": "COMMITTED",
        "created_at": "2026-07-29T05:29:30Z",
    }
    quote_sha256 = content_sha256({"schema": "basket_official_ws_snapshot_v1", **snapshot_identity})
    conn.execute(
        "INSERT INTO basket_official_ws_snapshot VALUES(" + ",".join("?" for _ in range(20)) + ")",
        tuple(
            json.dumps(value, sort_keys=True) if key in {"buckets_json", "cash_phase_input_json"} else value
            for key, value in snapshot_identity.items()
            for _ in (0,)
        )[:-2] + (quote_sha256, snapshot_identity["state"], snapshot_identity["created_at"]),
    )
    fact = _fact_row(quote_sha256=quote_sha256)
    conn.execute(
        "INSERT INTO basket_policy_training_fact(" + ",".join(FACT_COLUMNS) + ") VALUES(" + ",".join("?" for _ in FACT_COLUMNS) + ")",
        tuple(fact[field] for field in FACT_COLUMNS),
    )
    outcome = json.dumps({
        "label_semantics": "hypothetical_requested_intents_not_fills",
        "requested_cost_usdc": 1.5,
        "hypothetical_requested_pnl_usdc": -0.25,
    }, sort_keys=True)
    label_identity = {
        "fact_id": fact["fact_id"],
        "truth_id": "truth-1",
        "truth_revision_no": 1,
        "truth_sha256": "b" * 64,
        "official_tmax_c": 39.0,
        "winning_bucket": "39°C",
        "settlement_closed_at_utc": "2026-07-29T15:00:00Z",
        "labeled_at_utc": "2026-07-29T15:05:00Z",
        "outcome_json": outcome,
        "authority_scope": "AWC_FORMAL_TRUTH",
        "supersedes_label_id": None,
    }
    label_sha = content_sha256({"schema": LABEL_SCHEMA, **label_identity})
    conn.execute(
        "INSERT INTO basket_policy_training_label VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"d2label_{label_sha[:40]}", *label_identity.values(), label_sha),
    )
    return fact["fact_id"]


def test_projection_uses_exact_fact_bound_probability_quote_and_label() -> None:
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    _insert_valid_rows(conn)

    artifact = build_from_connection(conn, as_of_utc=AS_OF)

    assert_forward_observation_candidate(artifact)
    assert artifact["state"] == "READY_NO_LIVE"
    assert artifact["source_blockers"] == []
    assert artifact["counts"]["observation_count"] == 1
    assert artifact["counts"]["candidate_price_count"] == 1
    assert artifact["counts"]["counterfactual_settlement_attribution_count"] == 1
    observation = artifact["observations"][0]
    assert observation["candidate_price"] == 0.2
    assert observation["candidate_price_basis"] == "exact_plan_post_only_maker_limit"
    assert observation["probabilities"] == {"39°C": 1.0}
    assert observation["execution_cost_usdc"] == 1.5
    assert observation["settlement_pnl_usdc"] == -0.25
    assert observation["fill_status"] == "NO_EXECUTION"
    assert observation["source_lineage"]["official_ws_quote_sha256"] == conn.execute(
        "SELECT content_sha256 FROM basket_official_ws_snapshot"
    ).fetchone()[0]


def test_projection_rejects_ws_snapshot_hash_drift() -> None:
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    _insert_valid_rows(conn)
    conn.execute("UPDATE basket_official_ws_snapshot SET content_sha256=?", ("c" * 64,))

    artifact = build_from_connection(conn, as_of_utc=AS_OF)

    assert artifact["state"] == "BLOCKED_EVIDENCE"
    assert artifact["counts"]["observation_count"] == 0
    assert artifact["source_blockers"] == [
        "delta_v2_forward_observation_ws_snapshot_drift"
    ]


def test_projection_recomputes_ws_snapshot_hash() -> None:
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    _insert_valid_rows(conn)
    conn.execute("UPDATE basket_official_ws_snapshot SET buckets_json=?", ('[{"bucket":"40°C"}]',))

    artifact = build_from_connection(conn, as_of_utc=AS_OF)

    assert artifact["counts"]["observation_count"] == 0
    assert artifact["source_blockers"] == [
        "delta_v2_forward_observation_ws_snapshot_hash_invalid"
    ]


def test_projection_recomputes_probability_envelope_hash() -> None:
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    _insert_valid_rows(conn)
    conn.execute(
        "UPDATE basket_probability_envelope SET posterior_version=?",
        ("tampered-p1-v1",),
    )

    artifact = build_from_connection(conn, as_of_utc=AS_OF)

    assert artifact["counts"]["observation_count"] == 0
    assert artifact["source_blockers"] == [
        "delta_v2_forward_observation_probability_envelope_hash_invalid"
    ]

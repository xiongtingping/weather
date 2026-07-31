from __future__ import annotations

import json
import sqlite3

import pytest

from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.execution_version_vector import (
    build_execution_version_vector,
    build_no_execution_version_policy,
)
from bayes_basket_target.forward_scorecard import (
    ForwardScorecardError,
    MAX_FEATURE_JSON_BYTES,
    build_forward_scorecard,
)
from bayes_basket_target.delta_v2_training import FACT_SCHEMA
from bayes_basket_target.delta_v2_settlement_label import LABEL_SCHEMA


AS_OF = "2026-07-27T00:00:00Z"
SHA = "a" * 64


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE basket_policy_training_fact(
          fact_id TEXT PRIMARY KEY,
          station_id TEXT NOT NULL,
          event_date TEXT NOT NULL,
          settlement_timezone TEXT NOT NULL,
          decision_at_utc TEXT NOT NULL,
          decision_scope_key TEXT NOT NULL,
          captured_at_utc TEXT NOT NULL,
          source_sequence INTEGER NOT NULL,
          quote_observed_at TEXT NOT NULL,
          quote_expires_at TEXT NOT NULL,
          quote_freshness_policy_sha256 TEXT NOT NULL,
          stage TEXT NOT NULL,
          probability_envelope_id TEXT NOT NULL,
          posterior_version TEXT NOT NULL,
          posterior_sha256 TEXT NOT NULL,
          official_exclusion_mask_sha256 TEXT NOT NULL,
          ws_snapshot_id TEXT NOT NULL,
          quote_version TEXT NOT NULL,
          quote_sha256 TEXT NOT NULL,
          inventory_snapshot_id TEXT NOT NULL,
          inventory_version TEXT NOT NULL,
          inventory_sha256 TEXT NOT NULL,
          order_state_snapshot_id TEXT NOT NULL,
          order_state_version TEXT NOT NULL,
          order_state_sha256 TEXT NOT NULL,
          cycle_id TEXT NOT NULL,
          decision_id TEXT NOT NULL,
          decision_sha256 TEXT NOT NULL,
          plan_sha256 TEXT NOT NULL,
          intent_count INTEGER NOT NULL,
          intent_set_sha256 TEXT NOT NULL,
          policy_version_vector_json TEXT NOT NULL,
          feature_json TEXT NOT NULL,
          partition_key TEXT NOT NULL,
          authority_scope TEXT NOT NULL,
          created_at_utc TEXT NOT NULL,
          content_sha256 TEXT NOT NULL
        );
        CREATE TABLE basket_policy_training_label(
          label_id TEXT PRIMARY KEY,
          fact_id TEXT NOT NULL,
          truth_id TEXT NOT NULL,
          truth_revision_no INTEGER NOT NULL,
          truth_sha256 TEXT NOT NULL,
          official_tmax_c REAL NOT NULL,
          winning_bucket TEXT NOT NULL,
          settlement_closed_at_utc TEXT NOT NULL,
          labeled_at_utc TEXT NOT NULL,
          outcome_json TEXT NOT NULL,
          authority_scope TEXT NOT NULL,
          supersedes_label_id TEXT,
          content_sha256 TEXT NOT NULL
        );
        CREATE TABLE basket_official_ws_snapshot(
          snapshot_id TEXT PRIMARY KEY,
          market_id TEXT NOT NULL,
          station_id TEXT NOT NULL,
          event_date TEXT NOT NULL,
          settlement_timezone TEXT NOT NULL,
          sequence INTEGER NOT NULL,
          observed_at TEXT NOT NULL,
          quote_version TEXT NOT NULL,
          quote_observed_at TEXT NOT NULL,
          quote_expires_at TEXT NOT NULL,
          quote_freshness_policy_sha256 TEXT NOT NULL,
          quote_source TEXT NOT NULL,
          inventory_version TEXT NOT NULL,
          order_state_version TEXT NOT NULL,
          remaining_budget_usdc REAL NOT NULL,
          buckets_json TEXT NOT NULL,
          cash_phase_input_json TEXT NOT NULL,
          content_sha256 TEXT NOT NULL,
          state TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """
    )
    return conn


def _feature(
    *,
    phase: str = "STANDARD",
    cash_scorecard: dict | None = None,
    station_scorecard: dict | None = None,
    plan: dict | None = None,
) -> str:
    vector = build_execution_version_vector(
        {
            "schema": "bayes_basket_target_execution_version_vector_v2",
            "probability_sha256": SHA,
            "official_ws_quote_sha256": "b" * 64,
            "inventory_sha256": "c" * 64,
            "order_state_sha256": "d" * 64,
            "station_policy_sha256": "e" * 64,
            "cash_policy_decision_sha256": "f" * 64,
            "economic_policy_schema": "delta_basket_score_policy_artifact_v2",
            "economic_policy_artifact_sha256": "0" * 64,
            "target_price_id": "target-price-1",
            "quote_observed_at": "2026-07-26T23:59:00Z",
            "quote_expires_at": "2026-07-27T00:00:05Z",
        }
    )
    payload = {
        "schema": "delta_basket_score_forward_feature_vector_v2",
        "cash_phase_decision": {"phase": phase},
        "execution_version_vectors": [vector.to_dict()],
        "no_live_boundary": {
            "execution_mode": "production_no_live",
            "live_submit_authorized": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
    }
    if cash_scorecard is not None:
        payload["cash_phase_scorecard"] = cash_scorecard
    if station_scorecard is not None:
        payload["station_policy_scorecard"] = station_scorecard
    if plan is not None:
        payload["plan"] = plan
    return json.dumps(payload, sort_keys=True)


def _fact(
    conn: sqlite3.Connection,
    seed: str,
    *,
    feature: str | None = None,
    station: str = "RKPK",
    intent_count: int = 1,
    ws_snapshot_id: str | None = None,
    quote_sha256: str | None = None,
    event_date: str = "2026-07-26",
) -> str:
    feature_json = _feature() if feature is None else feature
    identity = {
        "station_id": station,
        "event_date": event_date,
        "settlement_timezone": "Asia/Shanghai",
        "decision_at_utc": "2026-07-26T12:00:00Z",
        "decision_scope_key": f"scope-{seed}",
        "captured_at_utc": "2026-07-26T12:00:00Z",
        "source_sequence": 0,
        "quote_observed_at": "2026-07-26T11:59:00Z",
        "quote_expires_at": "2026-07-26T12:00:05Z",
        "quote_freshness_policy_sha256": "1" * 64,
        "stage": "P1",
        "probability_envelope_id": f"envelope-{seed}",
        "posterior_version": "posterior-v1",
        "posterior_sha256": "2" * 64,
        "official_exclusion_mask_sha256": "3" * 64,
        "ws_snapshot_id": ws_snapshot_id or f"ws-{seed}",
        "quote_version": "quote-v1",
        "quote_sha256": quote_sha256 or "4" * 64,
        "inventory_snapshot_id": f"inventory-{seed}",
        "inventory_version": "inventory-v1",
        "inventory_sha256": "5" * 64,
        "order_state_snapshot_id": f"order-state-{seed}",
        "order_state_version": "order-state-v1",
        "order_state_sha256": "6" * 64,
        "cycle_id": f"cycle-{seed}",
        "decision_id": f"decision-{seed}",
        "decision_sha256": "7" * 64,
        "plan_sha256": "8" * 64,
        "intent_count": intent_count,
        "intent_set_sha256": "9" * 64,
        "policy_version_vector_json": "{}",
        "feature_json": feature_json,
        "partition_key": f"{station}|summer|P1|d0|normal",
        "authority_scope": "PRODUCTION_NO_LIVE_FORWARD",
        "created_at_utc": "2026-07-26T12:00:01Z",
    }
    fact_sha = content_sha256({"schema": FACT_SCHEMA, **identity})
    fact_id = f"d2fact_{fact_sha[:40]}"
    conn.execute(
        "INSERT INTO basket_policy_training_fact("
        + ",".join(("fact_id", *identity.keys(), "content_sha256"))
        + ") VALUES(" + ",".join("?" for _ in range(len(identity) + 2)) + ")",
        (fact_id, *identity.values(), fact_sha),
    )
    return fact_id


def _label(
    conn: sqlite3.Connection,
    fact_id: str,
    semantics: str,
    *,
    revision: int = 0,
    labeled_at: str = "2026-07-26T23:00:00Z",
    settlement_closed_at: str = "2026-07-26T23:00:00Z",
    outcome_json: str | None = None,
) -> str:
    outcome_json = outcome_json or json.dumps(
        {"label_semantics": semantics}, sort_keys=True
    )
    identity = {
        "fact_id": fact_id,
        "truth_id": f"truth-{fact_id}",
        "truth_revision_no": revision,
        "truth_sha256": "a" * 64,
        "official_tmax_c": 38.0,
        "winning_bucket": "38°C",
        "settlement_closed_at_utc": settlement_closed_at,
        "labeled_at_utc": labeled_at,
        "outcome_json": outcome_json,
        "authority_scope": "NO_LIVE_REPLAY_CANDIDATE",
        "supersedes_label_id": None,
    }
    label_sha = content_sha256({"schema": LABEL_SCHEMA, **identity})
    label_id = f"d2label_{label_sha[:40]}"
    conn.execute(
        "INSERT INTO basket_policy_training_label("
        + ",".join(("label_id", *identity.keys(), "content_sha256"))
        + ") VALUES(" + ",".join("?" for _ in range(len(identity) + 2)) + ")",
        (label_id, *identity.values(), label_sha),
    )
    return label_id


def _insert_ws(
    conn: sqlite3.Connection,
    *,
    snapshot_id: str,
    sequence: int,
    quote_observed_at: str,
    best_ask: float,
    queue_known: bool = True,
    queue_ahead_shares: float = 5.0,
) -> str:
    buckets = [
        {
            "bucket": "38°C",
            "token_id": "token-38",
            "best_ask": best_ask,
            "ask_levels": [{"price": best_ask, "shares": 100.0}],
            "bid_available": True,
            "bid_price": 0.1,
            "bid_levels": [{"price": 0.1, "shares": queue_ahead_shares}],
            "maker_queue_known": queue_known,
            "maker_queue_ahead_shares": queue_ahead_shares,
            "quote_source": "OFFICIAL_CLOB_WS",
        }
    ]
    cash = {"schema": "cash_phase_input_test_v1"}
    identity = {
        "snapshot_id": snapshot_id,
        "market_id": "market-rkpk",
        "station_id": "RKPK",
        "event_date": "2026-07-26",
        "settlement_timezone": "Asia/Shanghai",
        "sequence": sequence,
        "observed_at": quote_observed_at,
        "quote_version": f"quote-{sequence}",
        "quote_observed_at": quote_observed_at,
        "quote_expires_at": (
            "2026-07-26T12:00:05Z"
            if sequence == 1
            else "2026-07-26T12:01:06Z"
        ),
        "quote_freshness_policy_sha256": "1" * 64,
        "quote_source": "OFFICIAL_CLOB_WS",
        "inventory_version": "inventory-v1",
        "order_state_version": "order-state-v1",
        "remaining_budget_usdc": 12.0,
        "buckets_json": buckets,
        "cash_phase_input_json": cash,
        "state": "COMMITTED",
        "created_at": quote_observed_at,
    }
    digest = content_sha256({
        "schema": "basket_official_ws_snapshot_v1",
        **identity,
    })
    stored = {
        **identity,
        "buckets_json": json.dumps(buckets, sort_keys=True),
        "cash_phase_input_json": json.dumps(cash, sort_keys=True),
        "content_sha256": digest,
    }
    columns = tuple(stored)
    conn.execute(
        "INSERT INTO basket_official_ws_snapshot("
        + ",".join(columns)
        + ") VALUES("
        + ",".join("?" for _ in columns)
        + ")",
        tuple(stored[column] for column in columns),
    )
    return digest


def _maker_plan() -> dict:
    return {
        "schema": "target_basket_plan_v11",
        "targets": [
            {
                "bucket": "38°C",
                "execution_candidate": "MAKER",
                "maker_candidate_id": "maker_candidate_test_38",
                "candidate_price": 0.1,
                "effective_price": 0.11,
                "clip_shares": 10.0,
                "maker_timeout_seconds": 60.0,
                "target_price_id": "target-price-1",
                "maker_grid_artifact_sha256": "a" * 64,
                "maker_grid_artifact_curve_sha256": "b" * 64,
                "maker_grid_point_sha256": "c" * 64,
                "maker_grid_curve_sha256": "d" * 64,
            }
        ],
    }


def _formal_cash_scorecard() -> dict:
    scorecard = {
        "schema": "bayes_basket_target_cash_phase_forward_scorecard_v1",
        "production_no_live": True,
        "live_order_authorized": False,
        "writes_production_db": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "policy_version": "cash-v1",
        "scope": {
            "window_start_utc": "2026-07-01T00:00:00Z",
            "window_end_utc": "2026-07-27T00:00:00Z",
            "station_count": 1,
            "event_count": 1,
            "decision_count": 1,
        },
        "phase_observations": {
            phase: {"observed": True, "count": 1}
            for phase in ("HEAVY", "STANDARD", "TAIL")
        },
        "coverage": {
            key: {
                "complete": True,
                "sample_count": 1,
                "content_sha256": "a" * 64,
            }
            for key in ("price", "candidate_fill", "pnl_attribution")
        },
        "fault_scorecard": {"case_count": 1, "all_boundary_cases_pass": True},
        "pnl_attribution": {"settled_event_count": 1, "attributed_event_count": 1},
        "checks": {
            "target_only": True,
            "strict_asof": True,
            "price_coverage_complete": True,
            "candidate_fill_scorecard_complete": True,
            "pnl_attribution_complete": True,
            "fault_scorecard_complete": True,
            "phase_monotonic": True,
            "cash_phase_does_not_change_weather_plan": True,
        },
    }
    scorecard["content_sha256"] = content_sha256(scorecard)
    return scorecard


def _formal_station_scorecard(*, station_ids: list[str] | None = None) -> dict:
    families = {}
    for family in ("target", "reserve", "cadence"):
        families[family] = {
            slice_name: {"complete": True, "sample_count": 1}
            for slice_name in (
                "same_station_time_oos",
                "leave_one_station_out",
                "cross_season",
                "windows_3_7_15_30",
                "per_station_baseline_and_interval",
                "forward_shadow_and_pnl",
            )
        }
    scorecard = {
        "schema": "bayes_basket_target_station_policy_forward_scorecard_v1",
        "production_no_live": True,
        "live_order_authorized": False,
        "writes_production_db": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "candidate_bundle_sha256": "a" * 64,
        "scope": {
            "station_ids": station_ids or ["RKPK"],
            "station_count": len(station_ids or ["RKPK"]),
            "season_count": 4,
            "window_count": 4,
            "window_start_utc": "2026-01-01T00:00:00Z",
            "window_end_utc": "2026-07-27T00:00:00Z",
        },
        "families": families,
        "checks": {
            "target_only": True,
            "strict_asof_et": True,
            "four_seasons": True,
            "four_windows": True,
            "candidate_scope_matches": True,
            "all_station_family_scores": True,
            "random_baseline_intervals": True,
            "forward_shadow_and_pnl_attribution": True,
            "target_final_audit_fallback_absent": True,
        },
    }
    scorecard["content_sha256"] = content_sha256(scorecard)
    return scorecard


def test_empty_is_explicitly_blocked_and_has_no_fabricated_pass() -> None:
    conn = _db()
    receipt = build_forward_scorecard(conn, AS_OF)

    assert receipt["counts"]["fact_rows_evaluated"] == 0
    assert receipt["ready_for_cash_scorecard"] is False
    assert receipt["ready_for_station_scorecard"] is False
    assert "no_training_facts" in receipt["blockers"]
    assert receipt["orders_submitted"] == 0
    assert receipt["calls_order_gateway"] is False
    assert len(receipt["content_sha256"]) == 64


def test_one_valid_fact_reports_exact_vector_but_remains_unready_without_label() -> None:
    conn = _db()
    _fact(conn, "fact-1")
    conn.commit()

    receipt = build_forward_scorecard(conn, AS_OF)

    assert receipt["counts"]["fact_rows_evaluated"] == 1
    assert receipt["counts"]["labeled_facts"] == 0
    assert receipt["counts"]["unlabeled_facts"] == 1
    assert receipt["exact_vector_coverage"]["facts_with_exact_vector"] == 1
    assert receipt["exact_vector_coverage"]["coverage_ratio"] == 1.0
    assert receipt["ready_for_cash_scorecard"] is False
    assert "unlabeled_training_facts" in receipt["blockers"]


def test_malformed_feature_fails_closed_without_throwing_or_passing() -> None:
    conn = _db()
    _fact(conn, "fact-bad", feature="{not-json")

    receipt = build_forward_scorecard(conn, AS_OF)

    assert receipt["counts"]["malformed_feature_facts"] == 1
    assert receipt["exact_vector_coverage"]["facts_with_exact_vector"] == 0
    assert any(item.startswith("malformed_feature_json:") for item in receipt["blockers"])
    assert receipt["ready_for_cash_scorecard"] is False
    assert receipt["ready_for_station_scorecard"] is False


def test_oversized_feature_is_not_copied_into_the_projection() -> None:
    conn = _db()
    oversized = "{" + ("x" * MAX_FEATURE_JSON_BYTES) + "}"
    _fact(conn, "fact-large", feature=oversized)

    receipt = build_forward_scorecard(conn, AS_OF)

    assert receipt["counts"]["malformed_feature_facts"] == 1
    assert "malformed_feature_json:json_oversized" in receipt["blockers"]
    assert receipt["ready_for_cash_scorecard"] is False


def test_max_rows_is_a_page_size_and_does_not_hide_facts() -> None:
    conn = _db()
    for index in range(3):
        _fact(conn, f"fact-{index}")
    conn.commit()

    receipt = build_forward_scorecard(conn, AS_OF, max_rows=2)

    assert receipt["counts"]["fact_rows_evaluated"] == 3
    assert receipt["counts"]["fact_rows_read"] == 3
    assert "fact_row_cap_reached" not in receipt["blockers"]


def test_event_date_scope_excludes_other_days_before_bounded_projection() -> None:
    conn = _db()
    excluded = _fact(
        conn,
        "old-day",
        event_date="2026-07-25",
    )
    selected = _fact(
        conn,
        "closed-day",
        event_date="2026-07-26",
    )
    _label(
        conn,
        selected,
        "hypothetical_requested_intents_not_fills",
    )
    conn.execute(
        "UPDATE basket_policy_training_fact SET content_sha256=? "
        "WHERE fact_id=?",
        ("0" * 64, excluded),
    )
    conn.commit()

    receipt = build_forward_scorecard(
        conn,
        AS_OF,
        event_date_allowlist=["2026-07-26"],
        max_rows=512,
    )

    assert receipt["event_date_allowlist"] == ["2026-07-26"]
    assert receipt["counts"]["fact_rows_evaluated"] == 1
    assert receipt["counts"]["labeled_facts"] == 1
    assert "fact_content_sha256_mismatch" not in receipt["blockers"]


def test_retired_non_v2_facts_remain_auditable_without_poisoning_v2_scorecard() -> None:
    conn = _db()
    current_fact_id = _fact(conn, "current-v2")
    _label(
        conn,
        current_fact_id,
        "hypothetical_requested_intents_not_fills",
    )
    columns = [
        str(row[1])
        for row in conn.execute(
            "PRAGMA table_info(basket_policy_training_fact)"
        ).fetchall()
    ]
    current = conn.execute(
        "SELECT " + ",".join(columns)
        + " FROM basket_policy_training_fact WHERE fact_id=?",
        (current_fact_id,),
    ).fetchone()
    retired = dict(zip(columns, current))
    retired["fact_id"] = "sdfact_" + "a" * 40
    retired["decision_scope_key"] = "retired-sdfact-scope"
    retired["content_sha256"] = "a" * 64
    conn.execute(
        "INSERT INTO basket_policy_training_fact("
        + ",".join(columns)
        + ") VALUES("
        + ",".join("?" for _ in columns)
        + ")",
        tuple(retired[column] for column in columns),
    )

    receipt = build_forward_scorecard(conn, AS_OF)

    assert receipt["counts"]["fact_rows_read"] == 2
    assert receipt["counts"]["fact_rows_evaluated"] == 1
    assert receipt["counts"]["retired_non_v2_fact_rows_excluded"] == 1
    assert receipt["fact_sha_coverage"]["coverage_ratio"] == 1.0
    assert "fact_content_sha256_mismatch" not in receipt["blockers"]


def test_fact_and_label_reads_are_keyset_paginated_without_truncation() -> None:
    conn = _db()
    fact_ids = [_fact(conn, f"fact-cap-{index}") for index in range(3)]
    for fact_id in fact_ids:
        for revision in range(2):
            _label(
                conn,
                fact_id,
                "hypothetical_requested_intents_not_fills",
                revision=revision,
            )
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    receipt = build_forward_scorecard(conn, AS_OF, max_rows=2)

    assert receipt["counts"]["fact_rows_read"] == 3
    assert receipt["counts"]["label_rows_read"] == 6
    assert "fact_row_cap_reached" not in receipt["blockers"]
    assert "label_row_cap_reached" not in receipt["blockers"]
    assert sum("LIMIT 2" in sql for sql in statements) >= 2


def test_complete_pagination_still_has_a_hard_total_row_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _db()
    for index in range(3):
        _fact(conn, f"fact-total-limit-{index}")
    conn.commit()
    monkeypatch.setattr(
        "bayes_basket_target.forward_scorecard.MAX_TOTAL_SCORECARD_ROWS",
        2,
    )

    with pytest.raises(
        ForwardScorecardError,
        match="forward_scorecard_fact_rows_limit_exceeded",
    ):
        build_forward_scorecard(conn, AS_OF, max_rows=2)


def test_receipt_is_deterministic_under_insertion_order() -> None:
    first = _db()
    second = _db()
    rows = [("fact-b", "B"), ("fact-a", "A")]
    for fact_id, station in rows:
        _fact(first, fact_id, station=station)
    for fact_id, station in reversed(rows):
        _fact(second, fact_id, station=station)
    first.commit()
    second.commit()

    left = build_forward_scorecard(first, AS_OF)
    right = build_forward_scorecard(second, AS_OF)

    assert left == right


def test_requested_hypothetical_intents_are_not_counted_as_fills() -> None:
    conn = _db()
    fact_id = _fact(conn, "fact-requested")
    _label(
        conn,
        fact_id,
        "hypothetical_requested_intents_not_fills",
    )
    conn.commit()

    receipt = build_forward_scorecard(conn, AS_OF)
    semantics = receipt["requested_semantics"]

    assert semantics["requested_hypothetical_intents_not_fills"] == 1
    assert semantics["actual_fill_evidence"] == 0
    assert semantics["by_label_semantics"] == {
        "hypothetical_requested_intents_not_fills": 1
    }
    assert "requested_hypothetical_intents_are_not_fills" not in receipt["blockers"]
    assert receipt["no_live_fill_isolation"]["isolated"] is True
    assert receipt["ready_for_cash_scorecard"] is False


def test_cash_adapter_uses_committed_next_phase_and_fails_closed_on_missing_economics(
) -> None:
    conn = _db()
    feature = json.loads(_feature(phase="STANDARD"))
    feature["cash_phase_decision"].update({
        "next_phase": "TAIL",
        "policy_version": "cash-policy-v1",
    })
    fact_id = _fact(
        conn,
        "cash-adapter-next-phase",
        feature=json.dumps(feature, sort_keys=True),
    )
    _label(conn, fact_id, "hypothetical_requested_intents_not_fills")

    receipt = build_forward_scorecard(conn, AS_OF)
    candidate = receipt["cash_observation_candidate"]

    assert receipt["counts"]["by_phase"]["TAIL"]["facts"] == 1
    assert "STANDARD" not in receipt["counts"]["by_phase"]
    assert candidate["state"] == "BLOCKED_EVIDENCE"
    assert candidate["phase_counts"] == {"TAIL": 1}
    assert candidate["unique_event_count"] == 1
    assert candidate["field_coverage"] == {
        "winning_bucket_probability": 0,
        "all_in_yes_ask": 0,
    }
    assert "cash_adapter_winning_bucket_probability_missing" in (
        candidate["blockers"]
    )
    assert "cash_adapter_all_in_yes_ask_missing" in candidate["blockers"]
    assert candidate["observations"][0]["fill_status"] == "NO_EXECUTION"


def test_station_policy_candidate_reports_transfer_evidence_gaps() -> None:
    conn = _db()
    fact_id = _fact(conn, "station-candidate")
    _label(conn, fact_id, "hypothetical_requested_intents_not_fills")

    candidate = build_forward_scorecard(
        conn,
        AS_OF,
    )["station_policy_candidate"]

    assert candidate["state"] == "BLOCKED_EVIDENCE"
    assert candidate["station_ids"] == ["RKPK"]
    assert candidate["season_bins"] == ["summer"]
    assert candidate["lead_buckets"] == ["d0"]
    assert candidate["run_cycles"] == []
    assert "station_candidate_four_seasons_incomplete" in candidate["blockers"]
    assert "station_candidate_multi_station_incomplete" in candidate["blockers"]
    assert (
        "station_candidate_family_candidate_baseline_scores_missing"
        in candidate["blockers"]
    )


def test_latest_valid_label_revision_is_selected_strictly_as_of() -> None:
    conn = _db()
    fact_id = _fact(conn, "fact-revisions")
    _label(conn, fact_id, "hypothetical_requested_intents_not_fills", revision=0)
    _label(
        conn,
        fact_id,
        "hypothetical_requested_intents_not_fills",
        revision=1,
        outcome_json="{malformed",
    )
    _label(
        conn,
        fact_id,
        "actual_fills",
        revision=2,
        labeled_at="2026-07-27T00:00:01Z",
        settlement_closed_at="2026-07-27T00:00:01Z",
    )
    conn.commit()

    receipt = build_forward_scorecard(conn, AS_OF)

    assert receipt["counts"]["labeled_facts"] == 1
    assert receipt["counts"]["valid_label_revisions"] == 1
    assert receipt["requested_semantics"]["requested_hypothetical_intents_not_fills"] == 1
    assert receipt["requested_semantics"]["actual_fill_evidence"] == 0
    assert any(
        item.startswith("invalid_label_revision_skipped:outcome_json_json_malformed")
        for item in receipt["blockers"]
    )


def test_no_execution_zero_intent_is_valid_version_evidence() -> None:
    conn = _db()
    policy = build_no_execution_version_policy(
        {"policy": "NO_EXECUTION", "reason_codes": ["quote_missing"]}
    ).to_dict()
    feature = json.dumps(
        {
            "schema": "delta_basket_score_forward_feature_vector_v2",
            "cash_phase_decision": {"phase": "STANDARD"},
            "execution_version_vectors": [],
            "no_execution_policy": policy,
            "no_live_boundary": {
                "execution_mode": "production_no_live",
                "live_submit_authorized": False,
                "calls_order_gateway": False,
                "orders_submitted": 0,
            },
        },
        sort_keys=True,
    )
    _fact(conn, "fact-no-execution", feature=feature, intent_count=0)

    receipt = build_forward_scorecard(conn, AS_OF)

    assert receipt["exact_vector_coverage"]["facts_with_no_execution_policy"] == 1
    assert receipt["exact_vector_coverage"]["facts_with_version_evidence"] == 1
    assert receipt["exact_vector_coverage"]["version_evidence_coverage_ratio"] == 1.0
    assert "exact_vector_coverage_incomplete" not in receipt["blockers"]


def test_fact_and_label_content_sha_mismatch_fail_closed() -> None:
    conn = _db()
    fact_id = _fact(conn, "fact-sha")
    _label(conn, fact_id, "hypothetical_requested_intents_not_fills")
    conn.execute(
        "UPDATE basket_policy_training_fact SET content_sha256=? WHERE fact_id=?",
        ("0" * 64, fact_id),
    )
    conn.execute(
        "UPDATE basket_policy_training_label SET content_sha256=? WHERE fact_id=?",
        ("0" * 64, fact_id),
    )

    receipt = build_forward_scorecard(conn, AS_OF)

    assert receipt["fact_sha_coverage"]["facts_with_valid_content_sha256"] == 0
    assert "fact_content_sha256_mismatch" in receipt["blockers"]
    assert receipt["counts"]["labeled_facts"] == 0
    assert any(
        item.endswith(":content_sha256_mismatch")
        for item in receipt["blockers"]
        if item.startswith("invalid_label_revision_skipped:")
    )


def test_actual_fill_label_isolated_from_no_live_scorecard() -> None:
    conn = _db()
    fact_id = _fact(conn, "fact-fill")
    _label(conn, fact_id, "actual_fills")

    receipt = build_forward_scorecard(conn, AS_OF)

    assert receipt["requested_semantics"]["actual_fill_evidence"] == 1
    assert "actual_fill_evidence_conflicts_with_no_live_boundary" in receipt["blockers"]
    assert receipt["ready_for_cash_scorecard"] is False


def test_formal_no_live_candidate_fill_scorecard_can_reach_cash_ready() -> None:
    conn = _db()
    fact_id = _fact(
        conn,
        "fact-cash-formal",
        feature=_feature(cash_scorecard=_formal_cash_scorecard()),
    )
    _label(conn, fact_id, "hypothetical_requested_intents_not_fills")

    receipt = build_forward_scorecard(conn, AS_OF)

    assert receipt["cash_phase_evidence"]["formal_validator"] == (
        "validate_cash_phase_forward_scorecard"
    )
    assert receipt["cash_phase_evidence"]["candidate_count"] == 1
    assert receipt["cash_phase_evidence"]["content_addressed_by_fact_sha256"] is True
    assert receipt["ready_for_cash_scorecard"] is True
    assert receipt["ready_for_station_scorecard"] is False
    assert receipt["requested_semantics"]["actual_fill_evidence"] == 0


def test_formal_station_scorecard_can_reach_station_ready() -> None:
    conn = _db()
    fact_id = _fact(
        conn,
        "fact-station-formal",
        feature=_feature(station_scorecard=_formal_station_scorecard()),
    )
    _label(conn, fact_id, "hypothetical_requested_intents_not_fills")

    receipt = build_forward_scorecard(conn, AS_OF)

    assert receipt["station_policy_evidence"]["candidate_count"] == 1
    assert receipt["station_policy_evidence"]["content_addressed_by_fact_sha256"] is True
    assert receipt["ready_for_station_scorecard"] is True
    assert receipt["ready_for_cash_scorecard"] is False


def test_formal_scorecard_missing_coverage_cannot_be_faked_by_nonempty_fields() -> None:
    conn = _db()
    cash = _formal_cash_scorecard()
    del cash["coverage"]["candidate_fill"]
    cash["content_sha256"] = content_sha256(
        {key: value for key, value in cash.items() if key != "content_sha256"}
    )
    fact_id = _fact(
        conn,
        "fact-cash-tampered",
        feature=_feature(cash_scorecard=cash),
    )
    _label(conn, fact_id, "hypothetical_requested_intents_not_fills")

    receipt = build_forward_scorecard(conn, AS_OF)

    assert receipt["ready_for_cash_scorecard"] is False
    assert "cash_formal_scorecard_invalid:ContractError" in receipt["blockers"]


def test_formal_station_scope_tampering_is_blocked() -> None:
    conn = _db()
    station = _formal_station_scorecard(station_ids=["OTHER"])
    fact_id = _fact(
        conn,
        "fact-station-tampered",
        feature=_feature(station_scorecard=station),
    )
    _label(conn, fact_id, "hypothetical_requested_intents_not_fills")

    receipt = build_forward_scorecard(conn, AS_OF)

    assert receipt["ready_for_station_scorecard"] is False
    assert "station_formal_scorecard_invalid:ContractError" in receipt["blockers"]


def test_maker_forward_projects_official_ws_cross_and_labeled_pnl() -> None:
    conn = _db()
    initial_sha = _insert_ws(
        conn,
        snapshot_id="ws-maker-initial",
        sequence=1,
        quote_observed_at="2026-07-26T11:59:59Z",
        best_ask=0.2,
    )
    _insert_ws(
        conn,
        snapshot_id="ws-maker-terminal",
        sequence=2,
        quote_observed_at="2026-07-26T12:01:01Z",
        best_ask=0.09,
        queue_ahead_shares=0.0,
    )
    fact_id = _fact(
        conn,
        "maker-cross",
        feature=_feature(plan=_maker_plan()),
        ws_snapshot_id="ws-maker-initial",
        quote_sha256=initial_sha,
    )
    _label(
        conn,
        fact_id,
        "hypothetical_requested_intents_not_fills",
    )

    receipt = build_forward_scorecard(conn, AS_OF)
    maker = receipt["maker_forward_evidence"]

    assert maker["candidate_count"] == 1
    assert maker["resolved_lifecycle_count"] == 1
    assert maker["status_counts"] == {
        "COUNTERFACTUAL_CROSS_OBSERVED": 1
    }
    assert maker["counterfactual_pnl"]["labeled_count"] == 1
    assert maker["counterfactual_pnl"]["sum_usdc"] == 8.9
    assert maker["ready_for_no_live_forward_scorecard"] is True
    assert receipt["ready_for_maker_no_live_scorecard"] is True
    assert receipt["ready_for_maker_formal_promotion"] is False
    assert maker["evidence_class"] == "NO_LIVE_COUNTERFACTUAL_PROXY"
    assert maker["formal_activation_eligible"] is False
    assert maker["promotion_blockers"] == [
        "realized_maker_lifecycle_required_for_formal_promotion"
    ]


def test_maker_forward_timeout_is_nonfill_and_never_fabricates_fill() -> None:
    conn = _db()
    initial_sha = _insert_ws(
        conn,
        snapshot_id="ws-maker-initial",
        sequence=1,
        quote_observed_at="2026-07-26T11:59:59Z",
        best_ask=0.2,
    )
    _insert_ws(
        conn,
        snapshot_id="ws-maker-terminal",
        sequence=2,
        quote_observed_at="2026-07-26T12:01:01Z",
        best_ask=0.2,
    )
    fact_id = _fact(
        conn,
        "maker-nonfill",
        feature=_feature(plan=_maker_plan()),
        ws_snapshot_id="ws-maker-initial",
        quote_sha256=initial_sha,
    )
    _label(
        conn,
        fact_id,
        "hypothetical_requested_intents_not_fills",
    )

    maker = build_forward_scorecard(conn, AS_OF)["maker_forward_evidence"]

    assert maker["status_counts"] == {
        "COUNTERFACTUAL_NONFILL_TIMEOUT": 1
    }
    assert maker["counterfactual_pnl"]["labeled_count"] == 0
    assert maker["ready_for_no_live_forward_scorecard"] is True
    assert maker["ready_for_formal_maker_grid_promotion"] is False


def test_maker_forward_unknown_queue_fails_closed() -> None:
    conn = _db()
    initial_sha = _insert_ws(
        conn,
        snapshot_id="ws-maker-initial",
        sequence=1,
        quote_observed_at="2026-07-26T11:59:59Z",
        best_ask=0.2,
        queue_known=False,
    )
    _insert_ws(
        conn,
        snapshot_id="ws-maker-terminal",
        sequence=2,
        quote_observed_at="2026-07-26T12:01:01Z",
        best_ask=0.09,
        queue_known=False,
    )
    _fact(
        conn,
        "maker-unknown-queue",
        feature=_feature(plan=_maker_plan()),
        ws_snapshot_id="ws-maker-initial",
        quote_sha256=initial_sha,
    )

    maker = build_forward_scorecard(conn, AS_OF)["maker_forward_evidence"]

    assert maker["status_counts"] == {"INVALID": 1}
    assert "maker_candidate_queue_unknown" in maker["blockers"]
    assert maker["ready_for_no_live_forward_scorecard"] is False


def test_maker_forward_grid_lineage_is_mandatory() -> None:
    conn = _db()
    plan = _maker_plan()
    plan["targets"][0]["maker_grid_point_sha256"] = None
    _fact(
        conn,
        "maker-grid-missing",
        feature=_feature(plan=plan),
    )

    maker = build_forward_scorecard(conn, AS_OF)["maker_forward_evidence"]

    assert maker["candidate_count"] == 0
    assert "maker_candidate_grid_lineage_incomplete" in maker["blockers"]
    assert maker["ready_for_no_live_forward_scorecard"] is False

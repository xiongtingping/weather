from __future__ import annotations

import sqlite3

from target_realization_v3_runtime import build_target_realization_v3_decision_plan


def _timed_plan_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE event_market_ticks(
            id INTEGER PRIMARY KEY,slug TEXT,event_date TEXT,outcome TEXT,side TEXT,
            best_bid REAL,best_ask REAL,bid_depth_topN REAL,ask_depth_topN REAL,created_at TEXT
        );
        CREATE TABLE market_tokens(
            slug TEXT,event_date TEXT,outcome TEXT,yes_token TEXT,no_token TEXT
        );
        """
    )
    for bucket in ("30°C", "31°C", "32°C"):
        for side, bid, ask in (("YES", 0.20, 0.22), ("NO", 0.77, 0.79)):
            conn.execute(
                """INSERT INTO event_market_ticks(
                       slug,event_date,outcome,side,best_bid,best_ask,
                       bid_depth_topN,ask_depth_topN,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                ("s", "2026-07-12", bucket, side, bid, ask, 100, 100, "2026-07-12T07:00:00Z"),
            )
        conn.execute(
            "INSERT INTO market_tokens VALUES(?,?,?,?,?)",
            ("s", "2026-07-12", bucket, f"yes-{bucket}", f"no-{bucket}"),
        )
    return conn


def _timed_payload() -> dict:
    return {
        "posterior_ssot_fact": {
            "fact_snapshot_id": "fact-timed",
            "posterior_run_id": "post-timed",
            "realization_state_id": "realization-timed",
            "settlement_window_open": True,
            "bucket_probabilities": [
                {"bucket": "30°C", "probability": 0.1},
                {"bucket": "31°C", "probability": 0.7},
                {"bucket": "32°C", "probability": 0.2},
            ],
            "peak_timing_state": {
                "schema": "peak_timing_state_l3_v1",
                "ok": True,
                "state": "available",
                "peak_timing_state_id": "peak-timed",
                "entry_peak_at": "2026-07-12T08:00:00Z",
            },
            "strong_physical_evidence": {
                "joint_feature_model_applied": True,
                "top_bucket": "31°C",
                "top_bucket_joint_strong_support": True,
                "bucket_exclusions": [],
            },
        }
    }


def test_second_yes_only_adds_inside_authoritative_final_30_minute_window() -> None:
    conn = _timed_plan_conn()
    payload = _timed_payload()

    before = build_target_realization_v3_decision_plan(
        conn,
        snapshot_payload=payload,
        slug="s",
        event_date="2026-07-12",
        computed_at="2026-07-12T07:29:00Z",
    )
    inside = build_target_realization_v3_decision_plan(
        conn,
        snapshot_payload=payload,
        slug="s",
        event_date="2026-07-12",
        computed_at="2026-07-12T07:45:00Z",
    )
    at_peak = build_target_realization_v3_decision_plan(
        conn,
        snapshot_payload=payload,
        slug="s",
        event_date="2026-07-12",
        computed_at="2026-07-12T08:00:00Z",
    )

    before_second = next(row for row in before["legs"] if row["role"] == "second_yes")
    inside_second = next(row for row in inside["legs"] if row["role"] == "second_yes")
    at_peak_second = next(row for row in at_peak["legs"] if row["role"] == "second_yes")
    assert before_second["action"] == "hold"
    assert before_second["block_reason"] == "before_predicted_peak_final_30_minutes"
    assert inside_second["action"] == "add"
    assert inside_second["target_notional"] == 4.99
    assert at_peak_second["action"] == "hold"
    assert at_peak_second["block_reason"] == "predicted_peak_time_reached_or_passed"


def test_v3_entrypoint_emits_only_new_top_second_yes_and_least_no_roles() -> None:
    conn = sqlite3.connect(":memory:")
    payload = {
        "run_date": "2026-07-12",
        "posterior_ssot_fact": {
            "fact_snapshot_id": "fact-1",
            "posterior_run_id": "post-1",
            "settlement_window_open": True,
            "bucket_probabilities": [
                {"bucket": "30°C", "probability": 0.1},
                {"bucket": "31°C", "probability": 0.8},
                {"bucket": "32°C", "probability": 0.1},
            ],
            "strong_physical_evidence": {
                "joint_feature_model_applied": True,
                "top_bucket": "31°C",
                "top_bucket_joint_strong_support": True,
                "safe_no_amsc_observation": True,
                "safe_no_monotonic_daily_high_floor": True,
                "safe_no_observation_source": "zuuu_metar_raw_amsc",
                "safe_no_observed_max_c": 31.0,
                "bucket_exclusions": [],
            },
        },
    }

    plan = build_target_realization_v3_decision_plan(
        conn, snapshot_payload=payload, slug="highest-temperature-in-chengdu", event_date="2026-07-12"
    )

    assert plan["ok"] is True
    assert plan["version"] == "strong_posterior_three_leg_strategy_v2"
    assert [leg["role"] for leg in plan["legs"]] == ["top_yes_wave", "second_yes", "strongly_excluded_no"]
    assert plan["boundary"]["trajectory_no_absent"] is True
    assert plan["boundary"]["remote_no_absent"] is True
    assert plan["execution_price_policy"]["buy_price_cap"] is None


def test_dynamic_reselection_only_closes_posterior_settlement_position_that_left_three_legs() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE auto_orders(order_id TEXT, strategy TEXT);
        CREATE TABLE edge_trades(
            slug TEXT, event_date TEXT, outcome TEXT, side TEXT, position_size REAL,
            entry_size REAL, state TEXT, last_order_id TEXT
        );
        INSERT INTO auto_orders VALUES('managed', 'posterior_settlement');
        INSERT INTO auto_orders VALUES('unrelated', 'mirror_follow');
        INSERT INTO edge_trades VALUES('s', '2026-07-12', '30°C', 'YES', 2, 2, 'entry', 'managed');
        INSERT INTO edge_trades VALUES('s', '2026-07-12', '29°C', 'YES', 2, 2, 'entry', 'unrelated');
        """
    )
    payload = {
        "posterior_ssot_fact": {
            "fact_snapshot_id": "fact-dynamic",
            "posterior_run_id": "post-dynamic",
            "bucket_probabilities": [
                {"bucket": "31°C", "probability": 0.7},
                {"bucket": "32°C", "probability": 0.2},
                {"bucket": "33°C", "probability": 0.1},
            ],
            "strong_physical_evidence": {
                "joint_feature_model_applied": True,
                "top_bucket": "31°C",
                "top_bucket_joint_strong_support": True,
                "bucket_exclusions": [],
            },
        }
    }
    plan = build_target_realization_v3_decision_plan(
        conn, snapshot_payload=payload, slug="s", event_date="2026-07-12"
    )
    assert plan["sell_first_required"] is True
    assert plan["sell_old_legs"] == [
        {
            "bucket": "30°C",
            "side": "YES",
            "position_size": 2.0,
            "role": "dynamic_posterior_reselection_exit",
            "reason_code": "target_realization_dynamic_posterior_reselection_sell",
            "reason": "dynamic_posterior_reselection_no_longer_current_three_leg_member",
        }
    ]
    assert plan["transition_state"] == "SELL_OLD"


def test_plan_identity_is_a_posterior_revision_not_a_frozen_bucket_selection() -> None:
    conn = sqlite3.connect(":memory:")
    base_evidence = {
        "joint_feature_model_applied": True,
        "top_bucket_joint_strong_support": True,
        "bucket_exclusions": [],
    }
    first = build_target_realization_v3_decision_plan(
        conn,
        snapshot_payload={
            "posterior_ssot_fact": {
                "fact_snapshot_id": "fact-1",
                "posterior_run_id": "post-1",
                "bucket_probabilities": [
                    {"bucket": "31°C", "probability": 0.6},
                    {"bucket": "32°C", "probability": 0.3},
                    {"bucket": "33°C", "probability": 0.1},
                ],
                "strong_physical_evidence": {**base_evidence, "top_bucket": "31°C"},
            }
        },
        slug="s",
        event_date="2026-07-12",
    )
    second = build_target_realization_v3_decision_plan(
        conn,
        snapshot_payload={
            "posterior_ssot_fact": {
                "fact_snapshot_id": "fact-2",
                "posterior_run_id": "post-2",
                "bucket_probabilities": [
                    {"bucket": "31°C", "probability": 0.3},
                    {"bucket": "32°C", "probability": 0.6},
                    {"bucket": "33°C", "probability": 0.1},
                ],
                "strong_physical_evidence": {**base_evidence, "top_bucket": "32°C"},
            }
        },
        slug="s",
        event_date="2026-07-12",
    )
    assert first["target_bucket"] == "31°C"
    assert first["second_target_bucket"] == "32°C"
    assert second["target_bucket"] == "32°C"
    assert second["second_target_bucket"] == "31°C"
    assert first["plan_id"] != second["plan_id"]
    assert first["selection_is_frozen"] is False
    assert second["selection_policy"] == "dynamic_reselect_on_every_posterior_ssot_fact"


def test_continuing_dynamic_leg_reconciles_to_remaining_cap_without_rebuying_full_cap() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE auto_orders(order_id TEXT, strategy TEXT);
        CREATE TABLE edge_trades(
            slug TEXT, event_date TEXT, outcome TEXT, side TEXT, position_size REAL,
            entry_size REAL, avg_price REAL, entry_price REAL, state TEXT, last_order_id TEXT
        );
        INSERT INTO auto_orders VALUES('managed', 'posterior_settlement');
        INSERT INTO edge_trades VALUES('s', '2026-07-12', '31°C', 'YES', 40, 40, 0.5, 0.5, 'entry', 'managed');
        """
    )
    plan = build_target_realization_v3_decision_plan(
        conn,
        snapshot_payload={
            "posterior_ssot_fact": {
                "fact_snapshot_id": "fact-3",
                "posterior_run_id": "post-3",
                "bucket_probabilities": [
                    {"bucket": "31°C", "probability": 0.7},
                    {"bucket": "32°C", "probability": 0.2},
                    {"bucket": "33°C", "probability": 0.1},
                ],
                "strong_physical_evidence": {
                    "joint_feature_model_applied": True,
                    "top_bucket": "31°C",
                    "top_bucket_joint_strong_support": True,
                    "bucket_exclusions": [],
                },
            }
        },
        slug="s",
        event_date="2026-07-12",
    )
    top = plan["legs"][0]
    assert top["bucket"] == "31°C"
    assert top["current_notional_usdc"] == 20.0
    assert top["delta_notional"] == 0.0
    assert top["action"] == "hold"
    assert top["block_reason"] == "target_allocation_already_filled"

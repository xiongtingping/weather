from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import scan_weather_market as swm
from scripts import small_capital_validation_review
from scripts.weather_strategy_batch import (
    _default_pilot_cities_from_reports,
    _entry_expected_targets_from_selected_rows,
    _row_exclusion_reason,
    _row_skipped_diagnostic,
    build_strategy_batch,
    compact_batch_payload,
)


def _init_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE auto_trade_decisions (
              id INTEGER PRIMARY KEY,
              run_id INTEGER,
              run_date TEXT,
              slug TEXT,
              event_date TEXT,
              title TEXT,
              outcome TEXT,
              side TEXT,
              action TEXT,
              will_order INTEGER,
              reason_codes_json TEXT,
              reasons_json TEXT,
              details_json TEXT,
              created_at TEXT
            );
            """
        )
        rows = [
            (
                1,
                "highest-temperature-in-atlanta-on-may-31-2026",
                "104°F or higher",
                "NO",
                {
                    "q_entry": 0.84,
                    "edge_tradeability_profit_value": 0.13,
                    "q_bayes_z_stage": "d0_morning",
                    "source_fit_tier": "settlement_source",
                    "station_id": "KATL",
                    "source_station_id": "KATL",
                    "source_station_match": True,
                    "q_bayes_physical_likelihood_enabled": True,
                    "q_bayes_physical_likelihood_evidence": 12.0,
                    "q_bayes_physical_likelihood_source": "weather_physical_likelihood",
                    "q_bayes_physical_likelihood_prob": 0.71,
                    "weather_likelihood_features_source": "scan_model_members",
                    "weather_likelihood_aux_source": "open_meteo_forecast_aux,metar_aviationweather",
                    "weather_likelihood_feature_count": 40,
                    "q_bayes_bimodal_risk": True,
                    "q_bayes_bimodal_size_scale": 0.4,
                },
            ),
            (
                2,
                "highest-temperature-in-paris-on-may-31-2026",
                "26°C",
                "YES",
                {
                    "q_entry": 0.55,
                    "edge_tradeability_profit_value": 0.02,
                    "q_bayes_z_stage": "d0_morning",
                    "source_fit_tier": "settlement_source",
                    "station_id": "LFPG",
                    "source_station_id": "LFPG",
                    "source_station_match": True,
                },
            ),
        ]
        for row_id, slug, outcome, side, details in rows:
            conn.execute(
                """
                INSERT INTO auto_trade_decisions(
                  id, run_id, run_date, slug, event_date, title, outcome, side,
                  action, will_order, reason_codes_json, reasons_json, details_json, created_at
                )
                VALUES (?, 1, '2026-05-31', ?, '2026-05-31', 'Highest temperature?',
                        ?, ?, 'entry', 0, '[]', '[]', ?, ?)
                """,
                (
                    row_id,
                    slug,
                    outcome,
                    side,
                    json.dumps(details),
                    f"2026-05-31T0{row_id}:00:00+00:00",
                ),
            )
        conn.commit()
    finally:
        conn.close()


def test_strategy_batch_prefers_tail_no_reverse_edge(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _init_db(db_path)

    batch = build_strategy_batch(db_path, ["2026-05-31"], max_events=1)

    assert batch["selected_events"] == 1
    assert batch["include_event_keys"] == [
        {
            "slug": "highest-temperature-in-atlanta-on-may-31-2026",
            "event_date": "2026-05-31",
        }
    ]
    assert batch["selected_rows"][0]["strategy_recommendation"] == "priority_tail_no_reverse_edge"
    assert batch["selected_rows"][0]["reverse_edge_execution_side"] == "NO"
    assert batch["selected_rows"][0]["reverse_edge_execution_action"] == "BUY_NO"


def test_compact_batch_payload_drops_large_plan_summary(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _init_db(db_path)
    batch = build_strategy_batch(db_path, ["2026-05-31"], max_events=1)

    compact = compact_batch_payload(batch)

    assert "plan_summary" not in compact
    assert compact["selected_rows"][0]["slug"] == "highest-temperature-in-atlanta-on-may-31-2026"
    assert compact["selected_rows"][0]["reverse_edge_execution_action"] == "BUY_NO"
    assert compact["resource_focus_policy"] == "warn"
    assert compact["min_entry_edge"] == 0.10
    assert compact["selected_resource_focus"]["selected_count"] == 1
    assert compact["selected_settlement_quality"]["selected_settlement_count"] == 0
    assert compact["selected_settlement_quality"]["target_met"] is None
    assert compact["selected_physical_edge_quality"]["selected_d0_count"] == 0
    assert compact["selected_physical_edge_quality"]["physical_evidence_count"] == 0
    assert compact["selected_physical_edge_quality"]["bimodal_risk_count"] == 0
    assert compact["selected_physical_edge_quality"]["target_met"] is None
    assert compact["skipped_recommendations"]["shadow_only"] == 1
    assert compact["skipped_diagnostics"]["edge_lcb_too_thin_after_fees"] == 1
    assert compact["skipped_recommendation_examples"][0]["skipped_diagnostic"] == (
        "edge_lcb_too_thin_after_fees"
    )
    assert compact["skipped_diagnostic_examples"]["edge_lcb_too_thin_after_fees"][0][
        "strategy_recommendation"
    ] == "shadow_only"
    assert compact["selected_rows"][0]["source_fit_tier"] == "settlement_source"
    assert compact["selected_rows"][0]["station_id"] == "KATL"
    assert compact["selected_rows"][0]["source_station_match"] is True
    assert compact["selected_rows"][0]["q_bayes_physical_likelihood_enabled"] is True
    assert compact["selected_rows"][0]["weather_likelihood_feature_count"] == 40
    assert compact["selected_rows"][0]["q_bayes_bimodal_risk"] is True
    assert "q_bayes_model_evidence" not in compact["selected_rows"][0]


def test_strategy_batch_diagnoses_physical_bayes_without_tradeability(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO auto_trade_decisions(
              id, run_id, run_date, slug, event_date, title, outcome, side,
              action, will_order, reason_codes_json, reasons_json, details_json, created_at
            )
            VALUES (3, 1, '2026-05-31',
                    'highest-temperature-in-istanbul-on-may-31-2026',
                    '2026-05-31', 'Highest temperature?', '31°C', 'YES',
                    'entry', 0, '[]', '[]', ?, '2026-05-31T03:00:00+00:00')
            """,
            (
                json.dumps(
                    {
                        "q_entry": 0.62,
                        "q_bayes_lcb": 0.60,
                        "q_bayes_posterior": 0.66,
                        "ask": 0.48,
                        "q_bayes_z_stage": "d0_morning",
                        "q_bayes_physical_likelihood_enabled": True,
                        "q_bayes_physical_likelihood_evidence": 12.0,
                        "q_bayes_physical_likelihood_source": "weather_physical_likelihood",
                        "weather_likelihood_feature_count": 40,
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    batch = build_strategy_batch(db_path, ["2026-05-31"], max_events=1)

    assert batch["skipped_recommendations"]["blocked_or_missing_edge"] == 1
    assert batch["skipped_diagnostics"]["physical_bayes_positive_raw_edge_tradeability_missing"] == 1
    example = [
        item
        for item in batch["skipped_recommendation_examples"]
        if item["slug"] == "highest-temperature-in-istanbul-on-may-31-2026"
    ][0]
    assert example["skipped_diagnostic"] == "physical_bayes_positive_raw_edge_tradeability_missing"
    assert abs(example["skipped_diagnostic_raw_lcb_edge"] - 0.12) < 1e-9
    diagnostic_example = batch["skipped_diagnostic_examples"][
        "physical_bayes_positive_raw_edge_tradeability_missing"
    ][0]
    assert diagnostic_example["slug"] == "highest-temperature-in-istanbul-on-may-31-2026"
    assert diagnostic_example["q_bayes_lcb"] == 0.60
    assert diagnostic_example["ask"] == 0.48


def test_strategy_batch_enqueues_active_yes_target_adjustment_without_edge_candidate(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE event_runs (
              id INTEGER PRIMARY KEY,
              run_date TEXT,
              event_date TEXT,
              slug TEXT,
              title TEXT,
              payload_json TEXT,
              created_at TEXT
            );
            CREATE TABLE edge_trades (
              id INTEGER PRIMARY KEY,
              slug TEXT,
              event_date TEXT,
              outcome TEXT,
              side TEXT,
              state TEXT,
              position_size REAL
            );
            """
        )
        slug = "highest-temperature-in-chengdu-on-june-17-2026"
        payload = {
            "rows": [
                {
                    "outcome": "24°C or below",
                    "q_temperature_bucket_posterior": 0.2474,
                    "q_bayes_physical_likelihood_hard_zero": True,
                    "q_bayes_physical_likelihood_hard_reasons": [
                        "observed_today_max_above_bucket"
                    ],
                    "q_bayes_physical_likelihood_observed_prob": 0.0,
                    "weather_likelihood_refreshed_at": "2026-06-17T07:01:04+00:00",
                    "weather_likelihood_feature_count": 231,
                },
                {
                    "outcome": "25°C",
                    "q_temperature_bucket_posterior": 0.2169,
                    "q_bayes_physical_likelihood_observed_prob": 0.985,
                    "weather_likelihood_refreshed_at": "2026-06-17T07:01:04+00:00",
                    "weather_likelihood_feature_count": 231,
                },
                {
                    "outcome": "26°C",
                    "q_temperature_bucket_posterior": 0.1458,
                    "q_bayes_physical_likelihood_observed_prob": 0.015,
                    "weather_likelihood_refreshed_at": "2026-06-17T07:01:04+00:00",
                    "weather_likelihood_feature_count": 231,
                },
            ]
        }
        conn.execute(
            """
            INSERT INTO event_runs(run_date, event_date, slug, title, payload_json, created_at)
            VALUES ('2026-06-17', '2026-06-17', ?, 'Highest temperature?', ?, '2026-06-17T07:15:37+00:00')
            """,
            (slug, json.dumps(payload)),
        )
        conn.execute(
            """
            INSERT INTO edge_trades(slug, event_date, outcome, side, state, position_size)
            VALUES (?, '2026-06-17', '24°C or below', 'YES', 'entry', 149.0)
            """,
            (slug,),
        )
        conn.commit()
    finally:
        conn.close()

    batch = build_strategy_batch(
        db_path,
        ["2026-06-17"],
        max_events=1,
        pilot_cities=["chengdu"],
    )

    assert batch["target_adjustment_candidate_events"] == 1
    assert batch["target_adjustment_candidate_events_added"] == 1
    assert batch["include_event_keys"] == [
        {"slug": "highest-temperature-in-chengdu-on-june-17-2026", "event_date": "2026-06-17"}
    ]
    selected = batch["selected_rows"][0]
    assert selected["strategy_recommendation"] == "target_realization_switch"
    assert selected["target_adjustment_action"] == "SWITCH_BUY"
    assert selected["target_bucket"] == "25°C"
    assert selected["ssot_target_bucket"] == "25°C"
    assert selected["posterior_settlement_target_bucket"] == "25°C"
    assert selected["new_target_bucket"] == "25°C"
    assert selected["current_top_bucket"] == "25°C"
    assert selected["posterior_settlement_current_top_bucket"] == "25°C"
    assert selected["switch_reference_bucket"] == "24°C or below"
    assert selected["held_target_bucket"] == "24°C or below"
    assert selected["old_target_bucket"] == "24°C or below"
    assert selected["leg_bucket"] == "24°C or below"
    assert selected["held_target_side"] == "YES"
    assert selected["old_bucket_reverse_side"] == "NO"
    assert selected["switch_buy_allowed"] is True
    expected_targets = _entry_expected_targets_from_selected_rows(
        batch["selected_rows"],
        include_event_key=True,
    )
    expected_target_keys = {
        (target["outcome"], target["side"])
        for target in expected_targets
    }
    assert ("25°C", "YES") in expected_target_keys
    assert ("24°C or below", "NO") in expected_target_keys
    new_target = next(
        target
        for target in expected_targets
        if target["outcome"] == "25°C" and target["side"] == "YES"
    )
    assert new_target["target_realization_execution_action"] == "BUY"
    assert new_target["sell_old_buy_new_semantics"] == "buy_new_bucket_side"
    assert new_target["target_bucket"] == "25°C"
    assert new_target["ssot_target_bucket"] == "25°C"
    assert new_target["current_top_bucket"] == "25°C"
    assert new_target["new_target_bucket"] == "25°C"
    assert new_target["switch_reference_bucket"] == "24°C or below"
    assert new_target["old_target_bucket"] == "24°C or below"
    assert new_target["leg_bucket"] == "24°C or below"
    reverse_target = next(
        target
        for target in expected_targets
        if target["outcome"] == "24°C or below" and target["side"] == "NO"
    )
    assert reverse_target["target_realization_leg_role"] == "old_bucket_reverse"
    assert reverse_target["target_realization_execution_action"] == "BUY"
    assert reverse_target["target_bucket"] == "25°C"
    assert reverse_target["current_top_bucket"] == "25°C"
    assert reverse_target["new_target_bucket"] == "25°C"
    assert reverse_target["switch_reference_bucket"] == "24°C or below"
    assert reverse_target["paired_exit_side"] == "YES"
    assert reverse_target["paired_exit_execution_action"] == "SELL"
    assert "target_realization_switch_old_bucket_reverse" in reverse_target["entry_reasons"]


def test_strategy_batch_enqueues_active_no_target_adjustment_reverse_yes(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE event_runs (
              id INTEGER PRIMARY KEY,
              run_date TEXT,
              event_date TEXT,
              slug TEXT,
              title TEXT,
              payload_json TEXT,
              created_at TEXT
            );
            CREATE TABLE edge_trades (
              id INTEGER PRIMARY KEY,
              slug TEXT,
              event_date TEXT,
              outcome TEXT,
              side TEXT,
              state TEXT,
              position_size REAL
            );
            """
        )
        slug = "highest-temperature-in-chengdu-on-june-17-2026"
        payload = {
            "rows": [
                {
                    "outcome": "28°C",
                    "q_temperature_bucket_posterior": 0.18,
                    "weather_likelihood_refreshed_at": "2026-06-17T07:01:04+00:00",
                    "weather_likelihood_feature_count": 231,
                },
                {
                    "outcome": "29°C",
                    "q_temperature_bucket_posterior": 0.49,
                    "q_bayes_physical_likelihood_observed_prob": 0.90,
                    "observed_today_max_c": 28.4,
                    "temperature_momentum_effective_c_per_hour": 0.55,
                    "weather_likelihood_refreshed_at": "2026-06-17T07:01:04+00:00",
                    "weather_likelihood_feature_count": 231,
                },
                {
                    "outcome": "30°C",
                    "q_temperature_bucket_posterior": 0.33,
                    "q_bayes_physical_likelihood_observed_prob": 0.10,
                    "weather_likelihood_refreshed_at": "2026-06-17T07:01:04+00:00",
                    "weather_likelihood_feature_count": 231,
                },
            ]
        }
        conn.execute(
            """
            INSERT INTO event_runs(run_date, event_date, slug, title, payload_json, created_at)
            VALUES ('2026-06-17', '2026-06-17', ?, 'Highest temperature?', ?, '2026-06-17T07:15:37+00:00')
            """,
            (slug, json.dumps(payload)),
        )
        conn.execute(
            """
            INSERT INTO edge_trades(slug, event_date, outcome, side, state, position_size)
            VALUES (?, '2026-06-17', '28°C', 'NO', 'entry', 20.0)
            """,
            (slug,),
        )
        conn.commit()
    finally:
        conn.close()

    batch = build_strategy_batch(
        db_path,
        ["2026-06-17"],
        max_events=1,
        pilot_cities=["chengdu"],
    )

    selected = batch["selected_rows"][0]
    assert selected["strategy_recommendation"] == "target_realization_switch"
    assert selected["target_bucket"] == "29°C"
    assert selected["ssot_target_bucket"] == "29°C"
    assert selected["posterior_settlement_target_bucket"] == "29°C"
    assert selected["new_target_bucket"] == "29°C"
    assert selected["current_top_bucket"] == "29°C"
    assert selected["posterior_settlement_current_top_bucket"] == "29°C"
    assert selected["switch_reference_bucket"] == "28°C"
    assert selected["held_target_bucket"] == "28°C"
    assert selected["old_target_bucket"] == "28°C"
    assert selected["leg_bucket"] == "28°C"
    assert selected["held_target_side"] == "NO"
    assert selected["current_position_side"] == "NO"
    assert selected["old_bucket_reverse_side"] == "YES"
    expected_targets = _entry_expected_targets_from_selected_rows(
        batch["selected_rows"],
        include_event_key=True,
    )
    expected_target_keys = {
        (target["outcome"], target["side"])
        for target in expected_targets
    }
    assert ("29°C", "YES") in expected_target_keys
    assert ("28°C", "YES") in expected_target_keys
    reverse_target = next(
        target
        for target in expected_targets
        if target["outcome"] == "28°C" and target["side"] == "YES"
    )
    assert reverse_target["target_realization_leg_role"] == "old_bucket_reverse"
    assert reverse_target["target_realization_execution_action"] == "BUY"
    assert reverse_target["target_bucket"] == "29°C"
    assert reverse_target["ssot_target_bucket"] == "29°C"
    assert reverse_target["current_top_bucket"] == "29°C"
    assert reverse_target["new_target_bucket"] == "29°C"
    assert reverse_target["switch_reference_bucket"] == "28°C"
    assert reverse_target["old_target_bucket"] == "28°C"
    assert reverse_target["leg_bucket"] == "28°C"
    assert reverse_target["paired_exit_side"] == "NO"
    assert reverse_target["paired_exit_execution_action"] == "SELL"
    assert reverse_target["held_target_side"] == "NO"


def test_strategy_batch_enqueues_blocked_ssot_main_candidate_for_diagnostic(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE event_fact_history (
              id INTEGER PRIMARY KEY,
              run_date TEXT,
              slug TEXT,
              event_date TEXT,
              fact_as_of TEXT,
              fact_json TEXT
            );
            CREATE TABLE event_fact_latest (
              slug TEXT,
              event_date TEXT,
              fact_id INTEGER,
              updated_at TEXT
            );
            """
        )
        slug = "highest-temperature-in-chengdu-on-june-23-2026"
        fact = {
            "title": "Highest temperature in Chengdu on June 23?",
            "target_bucket": "29°C",
            "auto_trade_ssot": {
                "main_candidate": {
                    "outcome": "28°C",
                    "side": "NO",
                    "entry_ok": False,
                    "entry_reasons": [
                        "posterior_settlement阻断(remote_bucket_realization_prob_above_max)"
                    ],
                    "weather_strategy_entry_block_reason": (
                        "remote_bucket_realization_prob_above_max"
                    ),
                    "target_bucket": "29°C",
                    "target_posterior": 0.42,
                    "remote_bucket_realization_prob": 0.285,
                    "edge_tradeability_profit_value": 0.17,
                    "q_entry": 0.715,
                    "ask": 0.67,
                    "spread": 0.01,
                    "token_id": "NO_TOKEN",
                    "station_id": "ZUUU",
                    "source_fit_tier": "settlement_source",
                }
            },
            "distribution": [
                {"outcome": "28°C", "q_bayes_posterior": 0.285},
                {"outcome": "29°C", "q_bayes_posterior": 0.42},
            ],
            "target_realization_capital_plan": {
                "ok": True,
                "version": "target_realization_capital_plan_v2",
                "target_bucket": "29°C",
                "target_posterior": 0.42,
                "second_target_bucket": "30°C",
                "second_target_posterior": 0.21,
                "summary": "按 SSOT 目标桶维持。",
                "buy_new_leg": {
                    "bucket": "29°C",
                    "side": "YES",
                    "execution_action": "BUY",
                },
                "sell_old_buy_new_legs": [
                    {
                        "bucket": "29°C",
                        "side": "YES",
                        "execution_action": "BUY",
                    }
                ],
            },
            "target_switch_context": {
                "target_bucket": "29°C",
                "candidate_bucket": "30°C",
                "trajectory_momentum_impact": {
                    "label": "动量压制上切",
                    "trade_push": "支持维持 29°C，压制上切 30°C",
                },
            },
            "temperature_path_distribution": {
                "target_bucket": "29°C",
                "trade_follow": {
                    "allowed": False,
                    "reason": "momentum_state_cooling",
                },
            },
        }
        conn.execute(
            """
            INSERT INTO event_fact_history(id, run_date, slug, event_date, fact_as_of, fact_json)
            VALUES (10, '2026-06-23', ?, '2026-06-23', '2026-06-23T04:54:00+00:00', ?)
            """,
            (slug, json.dumps(fact)),
        )
        conn.execute(
            """
            INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at)
            VALUES (?, '2026-06-23', 10, '2026-06-23T04:54:00+00:00')
            """,
            (slug,),
        )
        conn.commit()
    finally:
        conn.close()

    batch = build_strategy_batch(
        db_path,
        ["2026-06-23"],
        max_events=1,
        pilot_cities=["chengdu"],
    )

    assert batch["plan_rows_seen"] == 0
    assert batch["target_adjustment_candidate_events"] == 0
    assert batch["ssot_main_candidate_events"] == 1
    assert batch["ssot_main_candidate_events_added"] == 1
    assert batch["ssot_diagnostic_candidate_events"] == 1
    assert batch["ssot_diagnostic_candidate_events_added"] == 1
    assert batch["include_event_keys"] == [
        {"slug": slug, "event_date": "2026-06-23"}
    ]
    selected = batch["selected_rows"][0]
    assert selected["strategy_recommendation"] == "target_realization_order_intent"
    assert selected["auto_trade_ssot_diagnostic"] is True
    assert selected["outcome"] == "28°C"
    assert selected["side"] == "NO"
    assert selected["entry_ok"] is False
    assert selected["weather_strategy_entry_block_reason"] == (
        "remote_bucket_realization_prob_above_max"
    )
    assert selected["target_realization_capital_plan"]["target_bucket"] == "29°C"
    assert selected["target_realization_capital_plan_version"] == (
        "target_realization_capital_plan_v2"
    )
    assert selected["buy_new_leg"]["bucket"] == "29°C"
    assert selected["trajectory_momentum_impact"]["label"] == "动量压制上切"
    assert selected["temperature_path_trade_follow_allowed"] is False
    assert selected["temperature_path_trade_follow_reason"] == "momentum_state_cooling"

    targets = _entry_expected_targets_from_selected_rows(
        batch["selected_rows"],
        include_event_key=True,
    )
    assert targets[0]["entry_ok"] is False
    assert targets[0]["entry_reasons"] == [
        "posterior_settlement阻断(remote_bucket_realization_prob_above_max)"
    ]
    assert targets[0]["token_id"] == "NO_TOKEN"
    assert targets[0]["weather_strategy_entry_block_reason"] == (
        "remote_bucket_realization_prob_above_max"
    )
    assert targets[0]["target_bucket"] == "29°C"
    assert targets[0]["remote_bucket_realization_prob"] == 0.285
    assert targets[0]["target_realization_capital_plan"]["target_bucket"] == "29°C"
    assert targets[0]["buy_new_leg"]["side"] == "YES"
    assert targets[0]["target_switch_context"]["candidate_bucket"] == "30°C"
    assert targets[0]["trajectory_momentum_impact"]["trade_push"] == (
        "支持维持 29°C，压制上切 30°C"
    )


def test_strategy_batch_prefers_executable_ssot_capital_plan_leg(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE event_fact_history (
              id INTEGER PRIMARY KEY,
              run_date TEXT,
              slug TEXT,
              event_date TEXT,
              fact_as_of TEXT,
              fact_json TEXT
            );
            CREATE TABLE event_fact_latest (
              slug TEXT,
              event_date TEXT,
              fact_id INTEGER,
              updated_at TEXT
            );
            """
        )
        slug = "highest-temperature-in-chengdu-on-july-1-2026"
        fact = {
            "title": "Highest temperature in Chengdu on July 1?",
            "target_bucket": "31°C",
            "auto_trade_ssot": {
                "main_candidate": {
                    "outcome": "31°C",
                    "side": "YES",
                    "entry_ok": False,
                    "entry_reasons": ["blocked_path_reliability_low"],
                    "weather_strategy_entry_block_reason": "blocked_path_reliability_low",
                    "target_bucket": "31°C",
                    "target_posterior": 0.6291010079885099,
                    "edge_tradeability_profit_value": 0.0599,
                    "q_entry": 0.6291010079885099,
                    "ask": 0.23,
                    "station_id": "ZUUU",
                    "source_fit_tier": "settlement_source",
                }
            },
            "target_realization_capital_plan": {
                "ok": True,
                "version": "target_realization_capital_plan_v2",
                "target_bucket": "31°C",
                "target_posterior": 0.6291010079885099,
                "second_target_bucket": "32°C",
                "second_target_posterior": 0.20887225515410227,
                "temperature_path_trade_follow_allowed": False,
                "temperature_path_trade_follow_reason": "blocked_path_reliability_low",
                "legs": [
                    {
                        "role": "target_yes",
                        "bucket": "31°C",
                        "side": "YES",
                        "posterior": 0.6291010079885099,
                        "prob_for_size": 0.6291010079885099,
                        "price": 0.23,
                        "target_notional": 0.0,
                        "action": "hold",
                        "temperature_path_trade_follow_allowed": False,
                        "temperature_path_trade_follow_reason": "blocked_path_reliability_low",
                    },
                    {
                        "role": "remote_no",
                        "bucket": "34°C",
                        "side": "NO",
                        "posterior": 0.000003315561843732211,
                        "prob_for_size": 0.9999966844381563,
                        "target_realization_reliability": 0.9857,
                        "price": 0.96,
                        "target_notional": 170.32839526379937,
                        "target_size": 177.42541173312435,
                        "action": "add",
                    },
                ],
            },
            "temperature_path_distribution": {
                "target_bucket": "31°C",
                "trade_follow": {
                    "allowed": False,
                    "reason": "blocked_path_reliability_low",
                },
            },
        }
        conn.execute(
            """
            INSERT INTO event_fact_history(id, run_date, slug, event_date, fact_as_of, fact_json)
            VALUES (11, '2026-07-01', ?, '2026-07-01', '2026-06-30T16:04:00+00:00', ?)
            """,
            (slug, json.dumps(fact)),
        )
        conn.execute(
            """
            INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at)
            VALUES (?, '2026-07-01', 11, '2026-06-30T16:04:00+00:00')
            """,
            (slug,),
        )
        conn.commit()
    finally:
        conn.close()

    batch = build_strategy_batch(
        db_path,
        ["2026-07-01"],
        max_events=1,
        pilot_cities=["chengdu"],
    )

    assert batch["ssot_main_candidate_events"] == 2
    assert batch["ssot_main_candidate_events_added"] == 1
    assert batch["ssot_diagnostic_candidate_events"] == 1
    assert batch["ssot_diagnostic_candidate_events_added"] == 0
    selected = batch["selected_rows"][0]
    assert selected["outcome"] == "34°C"
    assert selected["side"] == "NO"
    assert selected["entry_ok"] is True
    assert selected["target_realization_batch_reason"] == "target_realization_capital_plan_leg"
    assert selected["target_realization_leg_role"] == "remote_no"
    assert selected["target_realization_position_effect"] == "open_or_add_remote_no_side"
    assert selected["target_bucket"] == "31°C"
    assert selected["remote_no_bucket"] == "34°C"
    assert selected["target_realization_plan_notional"] == 170.32839526379937
    assert selected["kelly_sizing_source"] == "ui_snapshot_ssot_capital_plan"

    targets = _entry_expected_targets_from_selected_rows(
        batch["selected_rows"],
        include_event_key=True,
    )
    assert targets[0]["outcome"] == "34°C"
    assert targets[0]["side"] == "NO"
    assert targets[0]["strategy_family"] == "posterior_settlement"
    assert targets[0]["target_realization_plan_notional"] == 170.32839526379937
    assert targets[0]["target_realization_leg_role"] == "remote_no"
    assert targets[0]["target_realization_position_effect"] == "open_or_add_remote_no_side"
    assert targets[0]["remote_bucket_realization_prob"] == 0.000003315561843732211


def test_strategy_batch_enqueues_allowed_ssot_main_candidate(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE event_fact_history (
              id INTEGER PRIMARY KEY,
              run_date TEXT,
              slug TEXT,
              event_date TEXT,
              fact_as_of TEXT,
              fact_json TEXT
            );
            CREATE TABLE event_fact_latest (
              slug TEXT,
              event_date TEXT,
              fact_id INTEGER,
              updated_at TEXT
            );
            """
        )
        slug = "highest-temperature-in-chengdu-on-june-23-2026"
        fact = {
            "title": "Highest temperature in Chengdu on June 23?",
            "target_bucket": "29°C",
            "auto_trade_ssot": {
                "main_candidate": {
                    "outcome": "28°C",
                    "side": "NO",
                    "entry_ok": True,
                    "entry_reasons": [],
                    "weather_strategy_entry_block_reason": "",
                    "target_bucket": "29°C",
                    "target_posterior": 0.59,
                    "remote_bucket_realization_prob": 0.0,
                    "edge_tradeability_profit_value": 0.17,
                    "edge_tradeability_pass": True,
                    "posterior_settlement_size_scale": 0.2,
                    "q_entry": 1.0,
                    "q_quote": 1.0,
                    "ask": 0.67,
                    "spread": 0.01,
                    "token_id": "NO_TOKEN",
                    "station_id": "ZUUU",
                    "source_fit_tier": "settlement_source",
                }
            },
        }
        conn.execute(
            """
            INSERT INTO event_fact_history(id, run_date, slug, event_date, fact_as_of, fact_json)
            VALUES (10, '2026-06-23', ?, '2026-06-23', '2026-06-23T05:28:00+00:00', ?)
            """,
            (slug, json.dumps(fact)),
        )
        conn.execute(
            """
            INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at)
            VALUES (?, '2026-06-23', 10, '2026-06-23T05:28:00+00:00')
            """,
            (slug,),
        )
        conn.commit()
    finally:
        conn.close()

    batch = build_strategy_batch(
        db_path,
        ["2026-06-23"],
        max_events=1,
        pilot_cities=["chengdu"],
    )

    assert batch["plan_rows_seen"] == 0
    assert batch["ssot_main_candidate_events"] == 1
    assert batch["ssot_main_candidate_events_added"] == 1
    assert batch["ssot_diagnostic_candidate_events"] == 0
    assert batch["ssot_diagnostic_candidate_events_added"] == 0
    assert batch["include_event_keys"] == [
        {"slug": slug, "event_date": "2026-06-23"}
    ]
    selected = batch["selected_rows"][0]
    assert selected["strategy_recommendation"] == "target_realization_order_intent"
    assert selected["entry_ok"] is True
    assert selected["weather_strategy_entry_allowed"] is True
    assert selected["weather_strategy_edge_tier"] == "normal_trade"
    assert selected["entry_usable_edge_size_scale"] == 0.2
    assert selected["target_realization_batch_reason"] == "auto_trade_ssot_main_candidate"

    targets = _entry_expected_targets_from_selected_rows(
        batch["selected_rows"],
        include_event_key=True,
    )
    assert targets[0]["entry_ok"] is True
    assert targets[0]["token_id"] == "NO_TOKEN"
    assert targets[0]["target_bucket"] == "29°C"
    assert targets[0]["remote_bucket_realization_prob"] == 0.0


def test_reverse_edge_order_token_resolves_to_no_side() -> None:
    row = {"yes_token": "YES_TOKEN", "no_token": "NO_TOKEN"}
    candidate = {"token_id": "STALE_YES_TOKEN"}

    assert swm._resolve_entry_order_token_id(row, candidate, "NO") == "NO_TOKEN"
    assert swm._resolve_entry_order_token_id(row, candidate, "YES") == "YES_TOKEN"


def test_strategy_batch_expected_targets_preserve_reverse_edge_side(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _init_db(db_path)
    batch = build_strategy_batch(db_path, ["2026-05-31"], max_events=1)

    targets = _entry_expected_targets_from_selected_rows(batch["selected_rows"])

    assert targets == [
        {
            "outcome": "104°F or higher",
            "side": "NO",
            "strategy": "main",
            "entry_ok": True,
            "entry_reasons": ["priority_tail_no_reverse_edge"],
            "edge_score": None,
            "edge_min": 0.13,
            "exec_edge_entry": 0.13,
            "edge_realizable": 0.13,
            "edge_tradeability_profit_value": 0.13,
            "edge_tradeability_pass": False,
            "ask": None,
            "q_min_bound": 0.84,
            "spread": None,
            "q_entry": 0.84,
            "q_quote": None,
            "q_calibrated": None,
            "q_raw": None,
            "entry_usable_edge": None,
            "entry_usable_edge_size_scale": None,
            "entry_usable_edge_size_scale_before_season": None,
            "weather_strategy_season": "spring",
            "weather_strategy_season_size_scale": 1.0,
            "weather_strategy_stage": "d0_morning",
            "weather_strategy_mode": "settlement_dominant",
            "weather_strategy_edge_weight": 0.3,
            "weather_strategy_settlement_weight": 0.7,
            "weather_strategy_settlement_eligible": True,
            "source_fit_tier": "settlement_source",
            "source_fit_na_reason": "",
            "station_id": "KATL",
            "source_station_id": "KATL",
            "source_station_match": True,
            "q_bayes_physical_likelihood_enabled": True,
            "q_bayes_physical_likelihood_evidence": 12.0,
            "q_bayes_physical_likelihood_source": "weather_physical_likelihood",
            "q_bayes_physical_likelihood_prob": 0.71,
            "weather_likelihood_features_source": "scan_model_members",
            "weather_likelihood_aux_source": "open_meteo_forecast_aux,metar_aviationweather",
            "weather_likelihood_feature_count": 40,
            "q_bayes_bimodal_risk": True,
            "q_bayes_bimodal_size_scale": 0.4,
            "weather_strategy_reverse_edge": True,
            "weather_strategy_reverse_edge_action": "buy_no_equivalent_to_sell_yes",
            "reverse_edge_execution_side": "NO",
            "reverse_edge_execution_action": "BUY_NO",
            "target_realization_leg_role": "new_bucket_entry",
            "target_realization_execution_action": "BUY",
            "target_realization_position_effect": "open_or_add_new_bucket_side",
            "sell_old_buy_new_semantics": "buy_new_bucket_side",
        }
    ]


def test_strategy_batch_excludes_nonpositive_entry_usable_edge(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO auto_trade_decisions(
              id, run_id, run_date, slug, event_date, title, outcome, side,
              action, will_order, reason_codes_json, reasons_json, details_json, created_at
            )
            VALUES (3, 1, '2026-05-31',
                    'highest-temperature-in-shanghai-on-may-31-2026',
                    '2026-05-31', 'Highest temperature?', '25°C', 'YES',
                    'entry', 0, '[]', '[]', ?, '2026-05-31T03:00:00+00:00')
            """,
            (
                json.dumps(
                    {
                        "q_entry": 0.68,
                        "edge_tradeability_profit_value": 0.26,
                        "entry_usable_edge": -0.01,
                        "entry_usable_edge_size_scale": 0.0,
                        "q_bayes_z_stage": "d0_morning",
                        "source_fit_tier": "settlement_source",
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    batch = build_strategy_batch(db_path, ["2026-05-31"], max_events=2)

    selected_slugs = {row["slug"] for row in batch["selected_rows"]}
    assert "highest-temperature-in-shanghai-on-may-31-2026" not in selected_slugs
    assert batch["selected_events"] == 1
    assert batch["excluded_entry_reasons"]["entry_usable_edge_nonpositive"] == 1
    assert batch["excluded_entry_reasons_by_city"]["shanghai"][
        "entry_usable_edge_nonpositive"
    ] == 1
    assert batch["excluded_entry_examples"][0]["slug"] == (
        "highest-temperature-in-shanghai-on-may-31-2026"
    )


def test_strategy_batch_excludes_edge_below_probe_floor(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO auto_trade_decisions(
              id, run_id, run_date, slug, event_date, title, outcome, side,
              action, will_order, reason_codes_json, reasons_json, details_json, created_at
            )
            VALUES (3, 1, '2026-05-31',
                    'highest-temperature-in-buenos-aires-on-may-31-2026',
                    '2026-05-31', 'Highest temperature?', '18°C', 'YES',
                    'entry', 0, '[]', '[]', ?, '2026-05-31T03:00:00+00:00')
            """,
            (
                json.dumps(
                    {
                        "q_entry": 0.62,
                        "edge_tradeability_profit_value": 0.07,
                        "entry_usable_edge": 0.07,
                        "entry_usable_edge_size_scale": 1.0,
                        "q_bayes_z_stage": "d1",
                        "source_fit_tier": "settlement_source",
                        "station_id": "KMIA",
                        "source_station_id": "KMIA",
                        "source_station_match": True,
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    batch = build_strategy_batch(db_path, ["2026-05-31"], max_events=3)

    selected_slugs = {row["slug"] for row in batch["selected_rows"]}
    assert "highest-temperature-in-buenos-aires-on-may-31-2026" not in selected_slugs
    assert batch["min_entry_edge"] == 0.10
    assert batch["skipped_diagnostics"]["edge_lcb_too_thin_after_fees"] >= 1


def test_strategy_batch_blocks_probe_band_below_strict_live_floor_by_default(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO auto_trade_decisions(
              id, run_id, run_date, slug, event_date, title, outcome, side,
              action, will_order, reason_codes_json, reasons_json, details_json, created_at
            )
            VALUES (3, 1, '2026-05-31',
                    'highest-temperature-in-buenos-aires-on-may-31-2026',
                    '2026-05-31', 'Highest temperature?', '18°C', 'YES',
                    'entry', 0, '[]', '[]', ?, '2026-05-31T03:00:00+00:00')
            """,
            (
                json.dumps(
                    {
                        "q_entry": 0.62,
                        "edge_tradeability_profit_value": 0.09,
                        "entry_usable_edge": 0.09,
                        "entry_usable_edge_size_scale": 1.0,
                        "q_bayes_z_stage": "d1",
                        "source_fit_tier": "settlement_source",
                        "station_id": "SABE",
                        "source_station_id": "SABE",
                        "source_station_match": True,
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    batch = build_strategy_batch(db_path, ["2026-05-31"], max_events=3)

    selected_slugs = {row["slug"] for row in batch["selected_rows"]}
    assert "highest-temperature-in-buenos-aires-on-may-31-2026" not in selected_slugs
    assert batch["min_entry_edge"] == 0.10
    assert batch["excluded_entry_reasons"]["entry_edge_below_strict_min"] >= 1


def test_strategy_batch_excludes_d0_entry_without_physical_evidence(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO auto_trade_decisions(
              id, run_id, run_date, slug, event_date, title, outcome, side,
              action, will_order, reason_codes_json, reasons_json, details_json, created_at
            )
            VALUES (3, 1, '2026-05-31',
                    'highest-temperature-in-miami-on-may-31-2026',
                    '2026-05-31', 'Highest temperature?', '85°F or below', 'YES',
                    'entry', 0, '[]', '[]', ?, '2026-05-31T03:00:00+00:00')
            """,
            (
                json.dumps(
                    {
                        "q_entry": 0.90,
                        "edge_tradeability_profit_value": 0.20,
                        "entry_usable_edge": 0.18,
                        "entry_usable_edge_size_scale": 1.0,
                        "q_bayes_z_stage": "d0_morning",
                        "source_fit_tier": "settlement_source",
                        "station_id": "KMIA",
                        "source_station_id": "KMIA",
                        "source_station_match": True,
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    batch = build_strategy_batch(db_path, ["2026-05-31"], max_events=3)

    selected_slugs = {row["slug"] for row in batch["selected_rows"]}
    assert "highest-temperature-in-miami-on-may-31-2026" not in selected_slugs
    assert batch["require_d0_physical_evidence"] is True
    assert batch["excluded_entry_reasons"]["d0_physical_evidence_missing"] == 1


def test_strategy_batch_excludes_settlement_hold_review_from_entry_enqueue(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO auto_trade_decisions(
              id, run_id, run_date, slug, event_date, title, outcome, side,
              action, will_order, reason_codes_json, reasons_json, details_json, created_at
            )
            VALUES (3, 1, '2026-05-31',
                    'highest-temperature-in-miami-on-may-31-2026',
                    '2026-05-31', 'Highest temperature?', '85°F or below', 'YES',
                    'entry', 0, '[]', '[]', ?, '2026-05-31T03:00:00+00:00')
            """,
            (
                json.dumps(
                    {
                        "q_entry": 0.92,
                        "edge_tradeability_profit_value": 0.30,
                        "entry_usable_edge": 0.25,
                        "entry_usable_edge_size_scale": 1.0,
                        "q_bayes_z_stage": "d0_locked",
                        "source_fit_tier": "settlement_source",
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    batch = build_strategy_batch(db_path, ["2026-05-31"], max_events=3)

    selected = {(row["slug"], row["strategy_recommendation"]) for row in batch["selected_rows"]}
    assert (
        "highest-temperature-in-miami-on-may-31-2026",
        "settlement_hold_review",
    ) not in selected
    assert batch["excluded_entry_reasons"]["not_entry_candidate_recommendation"] == 1


def test_strategy_batch_excludes_major_settlement_without_station_match(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO auto_trade_decisions(
              id, run_id, run_date, slug, event_date, title, outcome, side,
              action, will_order, reason_codes_json, reasons_json, details_json, created_at
            )
            VALUES (3, 1, '2026-05-31',
                    'highest-temperature-in-miami-on-may-31-2026',
                    '2026-05-31', 'Highest temperature?', '85°F or below', 'YES',
                    'entry', 0, '[]', '[]', ?, '2026-05-31T03:00:00+00:00')
            """,
            (
                json.dumps(
                    {
                        "q_entry": 0.90,
                        "edge_tradeability_profit_value": 0.20,
                        "entry_usable_edge": 0.18,
                        "entry_usable_edge_size_scale": 1.0,
                        "q_bayes_z_stage": "d1",
                        "source_fit_tier": "settlement_source",
                        "station_id": "KMIA",
                        "source_station_id": "KOPF",
                        "source_station_match": False,
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    batch = build_strategy_batch(db_path, ["2026-05-31"], max_events=3)

    assert "settlement_quality_station_not_matched" in batch["excluded_entry_reasons"]
    selected_slugs = {row["slug"] for row in batch["selected_rows"]}
    assert "highest-temperature-in-miami-on-may-31-2026" not in selected_slugs


def test_strategy_batch_caps_major_settlement_share(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _init_db(db_path)
    rows = [
        (
            3,
            "highest-temperature-in-buenos-aires-on-may-31-2026",
            "18°C",
            "YES",
            {
                "q_entry": 0.74,
                "edge_tradeability_profit_value": 0.18,
                "entry_usable_edge": 0.08,
                "entry_usable_edge_size_scale": 1.0,
                "q_bayes_z_stage": "d1",
                "source_fit_tier": "settlement_source",
            },
        ),
        (
            4,
            "highest-temperature-in-cape-town-on-may-31-2026",
            "21°C",
            "YES",
            {
                "q_entry": 0.70,
                "edge_tradeability_profit_value": 0.16,
                "entry_usable_edge": 0.07,
                "entry_usable_edge_size_scale": 1.0,
                "q_bayes_z_stage": "d1",
                "source_fit_tier": "settlement_source",
            },
        ),
        (
            5,
            "highest-temperature-in-london-on-may-31-2026",
            "24°C",
            "YES",
            {
                "q_entry": 0.90,
                "edge_tradeability_profit_value": 0.30,
                "entry_usable_edge": 0.20,
                "entry_usable_edge_size_scale": 1.0,
                "q_bayes_z_stage": "d1",
                "source_fit_tier": "settlement_source",
            },
        ),
        (
            6,
            "highest-temperature-in-new-york-on-may-31-2026",
            "80°F",
            "YES",
            {
                "q_entry": 0.88,
                "edge_tradeability_profit_value": 0.28,
                "entry_usable_edge": 0.18,
                "entry_usable_edge_size_scale": 1.0,
                "q_bayes_z_stage": "d1",
                "source_fit_tier": "settlement_source",
            },
        ),
        (
            7,
            "highest-temperature-in-miami-on-may-31-2026",
            "88°F",
            "YES",
            {
                "q_entry": 0.86,
                "edge_tradeability_profit_value": 0.26,
                "entry_usable_edge": 0.16,
                "entry_usable_edge_size_scale": 1.0,
                "q_bayes_z_stage": "d1",
                "source_fit_tier": "settlement_source",
            },
        ),
    ]
    conn = sqlite3.connect(str(db_path))
    try:
        for row_id, slug, outcome, side, details in rows:
            conn.execute(
                """
                INSERT INTO auto_trade_decisions(
                  id, run_id, run_date, slug, event_date, title, outcome, side,
                  action, will_order, reason_codes_json, reasons_json, details_json, created_at
                )
                VALUES (?, 1, '2026-05-31', ?, '2026-05-31', 'Highest temperature?',
                        ?, ?, 'entry', 0, '[]', '[]', ?, ?)
                """,
                (
                    row_id,
                    slug,
                    outcome,
                    side,
                    json.dumps(details),
                    f"2026-05-31T0{row_id}:00:00+00:00",
                ),
            )
        conn.commit()
    finally:
        conn.close()

    batch = build_strategy_batch(db_path, ["2026-05-31"], max_events=3)

    selected_lanes = [row["focus_lane"] for row in batch["selected_rows"]]
    assert batch["selected_events"] == 3
    assert selected_lanes.count("major_settlement") <= 1
    assert sum(1 for lane in selected_lanes if lane != "major_settlement") >= 2
    focus = batch["selected_resource_focus"]
    assert focus["target_structural_edge_min_share"] == 0.70
    assert focus["target_major_settlement_max_share"] == 0.30
    assert focus["major_settlement_count"] == selected_lanes.count("major_settlement")
    assert focus["structural_edge_count"] == sum(
        1 for lane in selected_lanes if lane in {"tail_no_reverse_edge", "secondary_edge", "d0_physical_or_settlement"}
    )
    assert focus["major_settlement_share"] <= 1 / 3


def test_strategy_batch_warns_when_selected_resource_focus_misses_target(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO auto_trade_decisions(
              id, run_id, run_date, slug, event_date, title, outcome, side,
              action, will_order, reason_codes_json, reasons_json, details_json, created_at
            )
            VALUES (3, 1, '2026-05-31',
                    'highest-temperature-in-miami-on-may-31-2026',
                    '2026-05-31', 'Highest temperature?', '85°F or below', 'YES',
                    'entry', 0, '[]', '[]', ?, '2026-05-31T03:00:00+00:00')
            """,
            (
                json.dumps(
                    {
                        "q_entry": 0.90,
                        "edge_tradeability_profit_value": 0.20,
                        "entry_usable_edge": 0.18,
                        "entry_usable_edge_size_scale": 1.0,
                        "q_bayes_z_stage": "d1",
                        "source_fit_tier": "settlement_source",
                        "station_id": "KMIA",
                        "source_station_id": "KMIA",
                        "source_station_match": True,
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    batch = build_strategy_batch(db_path, ["2026-05-31"], max_events=2)

    assert batch["ok"] is True
    assert "selected_resource_focus_target_not_met" in batch["warnings"]
    assert batch["blockers"] == []
    assert batch["selected_resource_focus"]["target_met"] is False


def test_strategy_batch_enforces_selected_resource_focus_policy(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO auto_trade_decisions(
              id, run_id, run_date, slug, event_date, title, outcome, side,
              action, will_order, reason_codes_json, reasons_json, details_json, created_at
            )
            VALUES (3, 1, '2026-05-31',
                    'highest-temperature-in-miami-on-may-31-2026',
                    '2026-05-31', 'Highest temperature?', '85°F or below', 'YES',
                    'entry', 0, '[]', '[]', ?, '2026-05-31T03:00:00+00:00')
            """,
            (
                json.dumps(
                    {
                        "q_entry": 0.90,
                        "edge_tradeability_profit_value": 0.20,
                        "entry_usable_edge": 0.18,
                        "entry_usable_edge_size_scale": 1.0,
                        "q_bayes_z_stage": "d1",
                        "source_fit_tier": "settlement_source",
                        "station_id": "KMIA",
                        "source_station_id": "KMIA",
                        "source_station_match": True,
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    batch = build_strategy_batch(
        db_path,
        ["2026-05-31"],
        max_events=2,
        resource_focus_policy="enforce",
    )

    assert batch["ok"] is False
    assert batch["warnings"] == []
    assert "selected_resource_focus_target_not_met" in batch["blockers"]
    assert batch["resource_focus_policy"] == "enforce"


def test_strategy_batch_filters_to_pilot_cities(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _init_db(db_path)

    batch = build_strategy_batch(
        db_path,
        ["2026-05-31"],
        max_events=3,
        pilot_cities=["atlanta"],
    )

    assert batch["pilot_cities"] == ["atlanta"]
    assert batch["selected_events"] == 1
    assert batch["selected_rows"][0]["city_key"] == "atlanta"
    assert batch["include_event_keys"] == [
        {
            "slug": "highest-temperature-in-atlanta-on-may-31-2026",
            "event_date": "2026-05-31",
        }
    ]


def test_strategy_batch_observation_target_focus_prioritizes_gap_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _init_db(db_path)
    target_path = tmp_path / "paper_observation_target_package_latest.json"
    target_path.write_text(
        json.dumps(
            {
                "city_targets": [{"city_key": "miami", "needed_resolved_rows": 2}],
                "strategy_targets": [
                    {
                        "target_strategy": "d0_physical_settlement_hybrid",
                        "needed_resolved_rows": 0,
                        "needed_additional_wins_for_hit_rate": 1,
                        "recommended_observation_action": (
                            "prioritize_d0_physical_settlement_candidates"
                        ),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO auto_trade_decisions(
              id, run_id, run_date, slug, event_date, title, outcome, side,
              action, will_order, reason_codes_json, reasons_json, details_json, created_at
            )
            VALUES (3, 1, '2026-05-31',
                    'highest-temperature-in-miami-on-may-31-2026',
                    '2026-05-31', 'Highest temperature?', '85°F or below', 'YES',
                    'entry', 0, '[]', '[]', ?, '2026-05-31T03:00:00+00:00')
            """,
            (
                json.dumps(
                    {
                        "q_entry": 0.90,
                        "edge_tradeability_profit_value": 0.20,
                        "entry_usable_edge": 0.18,
                        "entry_usable_edge_size_scale": 1.0,
                        "q_bayes_z_stage": "d0_morning",
                        "source_fit_tier": "settlement_source",
                        "station_id": "KMIA",
                        "source_station_id": "KMIA",
                        "source_station_match": True,
                        "q_bayes_physical_likelihood_enabled": True,
                        "q_bayes_physical_likelihood_evidence": 9.0,
                        "weather_likelihood_feature_count": 12,
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    batch = build_strategy_batch(
        db_path,
        ["2026-05-31"],
        max_events=1,
        observation_target_focus=True,
        observation_target_package_path=target_path,
    )

    assert batch["selected_rows"][0]["slug"] == "highest-temperature-in-miami-on-may-31-2026"
    assert batch["selected_rows"][0]["observation_target_match"] is True
    assert batch["selected_rows"][0]["observation_target_strategy"] == (
        "d0_physical_settlement_hybrid"
    )
    assert batch["observation_target_focus"]["package_ok"] is True
    assert batch["observation_target_focus"]["candidate_target_rows"] >= 1


def test_strategy_batch_observation_target_focus_includes_expansion_cities(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "archive.db"
    _init_db(db_path)
    target_path = tmp_path / "paper_observation_target_package_latest.json"
    target_path.write_text(
        json.dumps(
            {
                "city_targets": [{"city": "chongqing", "needed_resolved_rows": 5}],
                "observation_expansion_city_targets": [
                    {"city": "buenos aires", "needed_resolved_rows": 9}
                ],
            }
        ),
        encoding="utf-8",
    )
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO auto_trade_decisions(
              id, run_id, run_date, slug, event_date, title, outcome, side,
              action, will_order, reason_codes_json, reasons_json, details_json, created_at
            )
            VALUES (3, 1, '2026-05-31',
                    'highest-temperature-in-buenos-aires-on-may-31-2026',
                    '2026-05-31', 'Highest temperature?', '32°C or higher', 'NO',
                    'entry', 0, '[]', '[]', ?, '2026-05-31T03:00:00+00:00')
            """,
            (
                json.dumps(
                    {
                        "q_entry": 0.88,
                        "edge_tradeability_profit_value": 0.16,
                        "entry_usable_edge": 0.14,
                        "entry_usable_edge_size_scale": 1.0,
                        "q_bayes_z_stage": "d0_morning",
                        "source_fit_tier": "settlement_source",
                        "station_id": "SABE",
                        "source_station_id": "SABE",
                        "source_station_match": True,
                        "q_bayes_physical_likelihood_enabled": True,
                        "q_bayes_physical_likelihood_evidence": 10.0,
                        "weather_likelihood_feature_count": 16,
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    batch = build_strategy_batch(
        db_path,
        ["2026-05-31"],
        max_events=1,
        pilot_cities=["chongqing"],
        observation_target_focus=True,
        observation_target_package_path=target_path,
    )

    assert batch["pilot_cities"] == ["chongqing"]
    assert "buenos-aires" in batch["observation_scope_cities"]
    assert batch["selected_rows"][0]["slug"] == (
        "highest-temperature-in-buenos-aires-on-may-31-2026"
    )
    assert batch["selected_rows"][0]["observation_expansion_city_match"] is True
    assert batch["observation_target_focus"]["candidate_expansion_rows"] == 1


def test_strategy_batch_marks_recommended_pilot_observation_city(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "archive.db"
    _init_db(db_path)
    target_path = tmp_path / "paper_observation_target_package_latest.json"
    target_path.write_text(
        json.dumps(
            {
                "city_targets": [{"city": "chongqing", "needed_resolved_rows": 5}],
                "recommended_pilot_city_targets": [
                    {
                        "city": "buenos aires",
                        "recommended_pilot_action": (
                            "add_second_pilot_city_for_paper_observation"
                        ),
                        "observation_mode": "paper_only_not_small_capital",
                        "needed_resolved_rows": 9,
                        "blockers": [
                            "strategy_resolved_rows_below_min:reverse_edge_buy_no_sell_yes"
                        ],
                    }
                ],
                "observation_expansion_city_targets": [
                    {"city": "madrid", "needed_resolved_rows": 10}
                ],
            }
        ),
        encoding="utf-8",
    )
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO auto_trade_decisions(
              id, run_id, run_date, slug, event_date, title, outcome, side,
              action, will_order, reason_codes_json, reasons_json, details_json, created_at
            )
            VALUES (4, 1, '2026-05-31',
                    'highest-temperature-in-buenos-aires-on-may-31-2026',
                    '2026-05-31', 'Highest temperature?', '32°C or higher', 'NO',
                    'entry', 0, '[]', '[]', ?, '2026-05-31T03:00:00+00:00')
            """,
            (
                json.dumps(
                    {
                        "q_entry": 0.88,
                        "edge_tradeability_profit_value": 0.16,
                        "entry_usable_edge": 0.14,
                        "entry_usable_edge_size_scale": 1.0,
                        "q_bayes_z_stage": "d0_morning",
                        "source_fit_tier": "settlement_source",
                        "station_id": "SABE",
                        "source_station_id": "SABE",
                        "source_station_match": True,
                        "q_bayes_physical_likelihood_enabled": True,
                        "q_bayes_physical_likelihood_evidence": 10.0,
                        "weather_likelihood_feature_count": 16,
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    batch = build_strategy_batch(
        db_path,
        ["2026-05-31"],
        max_events=1,
        pilot_cities=["chongqing"],
        observation_target_focus=True,
        observation_target_package_path=target_path,
    )

    assert batch["pilot_cities"] == ["chongqing"]
    assert "buenos-aires" in batch["observation_scope_cities"]
    assert batch["observation_target_focus"]["recommended_pilot_city_keys"] == [
        "buenos-aires"
    ]
    assert batch["observation_target_focus"][
        "candidate_recommended_pilot_rows_by_city"
    ] == {"buenos-aires": 1}
    assert batch["observation_target_focus"][
        "candidate_recommended_pilot_rows_by_strategy"
    ] == {"reverse_edge_buy_no_sell_yes": 1}
    assert batch["observation_target_focus"]["candidate_recommended_pilot_strategy_rows"] == 1
    assert batch["selected_rows"][0]["observation_recommended_pilot_city_match"] is True
    assert batch["selected_rows"][0]["observation_recommended_pilot_target_strategy"] == (
        "reverse_edge_buy_no_sell_yes"
    )
    assert batch["selected_rows"][0]["observation_target_recommended_action"] == (
        "prioritize_recommended_pilot_reverse_no_candidates"
    )
    assert batch["selected_rows"][0]["observation_expansion_city_match"] is False


def test_strategy_batch_reports_recommended_pilot_pregate_diagnostics(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "archive.db"
    _init_db(db_path)
    target_path = tmp_path / "paper_observation_target_package_latest.json"
    target_path.write_text(
        json.dumps(
            {
                "recommended_pilot_city_targets": [
                    {
                        "city": "buenos aires",
                        "recommended_pilot_action": (
                            "add_second_pilot_city_for_paper_observation"
                        ),
                        "observation_mode": "paper_only_not_small_capital",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO auto_trade_decisions(
              id, run_id, run_date, slug, event_date, title, outcome, side,
              action, will_order, reason_codes_json, reasons_json, details_json, created_at
            )
            VALUES (4, 1, '2026-05-31',
                    'highest-temperature-in-buenos-aires-on-may-31-2026',
                    '2026-05-31', 'Highest temperature?', '18°C', 'YES',
                    'entry', 0, '[]', '[]', ?, '2026-05-31T03:00:00+00:00')
            """,
            (
                json.dumps(
                    {
                        "q_entry": 0.62,
                        "edge_tradeability_profit_value": 0.07,
                        "entry_usable_edge": 0.07,
                        "entry_usable_edge_size_scale": 1.0,
                        "q_bayes_z_stage": "d1",
                        "source_fit_tier": "settlement_source",
                        "station_id": "SABE",
                        "source_station_id": "SABE",
                        "source_station_match": True,
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    batch = build_strategy_batch(
        db_path,
        ["2026-05-31"],
        max_events=1,
        pilot_cities=["chongqing"],
        observation_target_focus=True,
        observation_target_package_path=target_path,
    )

    focus = batch["observation_target_focus"]
    assert focus["recommended_pilot_city_keys"] == ["buenos-aires"]
    assert focus["candidate_recommended_pilot_rows_by_city"] == {}
    diagnostics = focus["recommended_pilot_city_diagnostics"]["buenos-aires"]
    assert diagnostics["scoped_plan_rows"] >= 1
    assert diagnostics["candidate_rows"] == 0
    assert diagnostics["skipped_diagnostics"]["edge_lcb_too_thin_after_fees"] >= 1
    assert diagnostics["primary_reason"] == "edge_lcb_too_thin_after_fees"
    assert diagnostics["primary_action"] == (
        "paper_observe_only_until_lcb_edge_reaches_entry_floor"
    )
    assert diagnostics["recommended_actions"][0]["action"] == (
        "paper_observe_only_until_lcb_edge_reaches_entry_floor"
    )


def test_compact_batch_payload_surfaces_observation_target_focus(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _init_db(db_path)
    target_path = tmp_path / "paper_observation_target_package_latest.json"
    target_path.write_text(
        json.dumps(
            {
                "strategy_targets": [
                    {
                        "target_strategy": "reverse_edge_buy_no_sell_yes",
                        "needed_resolved_rows": 2,
                        "needed_additional_wins_for_hit_rate": 0,
                        "recommended_observation_action": (
                            "prioritize_reverse_no_tail_candidates"
                        ),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    batch = build_strategy_batch(
        db_path,
        ["2026-05-31"],
        max_events=1,
        observation_target_focus=True,
        observation_target_package_path=target_path,
    )
    compact = compact_batch_payload(batch)

    assert compact["observation_target_focus"]["package_ok"] is True
    assert compact["selected_rows"][0]["observation_target_match"] is True
    assert compact["selected_rows"][0]["observation_target_strategy"] == (
        "reverse_edge_buy_no_sell_yes"
    )
    assert compact["selected_rows"][0]["observation_target_needed_resolved_rows"] == 2


def test_strategy_batch_expected_targets_use_autumn_adjusted_size_scale(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO auto_trade_decisions(
              id, run_id, run_date, slug, event_date, title, outcome, side,
              action, will_order, reason_codes_json, reasons_json, details_json, created_at
            )
            VALUES (3, 1, '2026-09-30',
                    'highest-temperature-in-atlanta-on-september-30-2026',
                    '2026-09-30', 'Highest temperature?', '84°F', 'YES',
                    'entry', 0, '[]', '[]', ?, '2026-09-29T03:00:00+00:00')
            """,
            (
                json.dumps(
                    {
                        "q_entry": 0.72,
                        "edge_tradeability_profit_value": 0.12,
                        "entry_usable_edge": 0.08,
                        "entry_usable_edge_size_scale": 1.0,
                        "q_bayes_z_stage": "d1",
                        "source_fit_tier": "settlement_source",
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    batch = build_strategy_batch(db_path, ["2026-09-30"], max_events=1)
    targets = _entry_expected_targets_from_selected_rows(batch["selected_rows"])

    assert batch["selected_rows"][0]["weather_strategy_season"] == "autumn"
    assert batch["selected_rows"][0]["entry_usable_edge_size_scale_adjusted"] == 0.40
    assert targets[0]["entry_usable_edge_size_scale"] == 0.40
    assert targets[0]["weather_strategy_season_size_scale"] == 0.40


def test_settled_or_past_rows_report_no_new_entry_before_size_zero() -> None:
    row = {
        "strategy_recommendation": "strict_edge_candidate_review",
        "weather_strategy_stage": "settled_or_past",
        "entry_usable_edge": -0.01,
        "entry_usable_edge_size_scale": 0.0,
        "entry_usable_edge_na_reason": "strategy_size_ratio_zero",
        "edge_tradeability_profit_value": 0.20,
    }

    assert _row_exclusion_reason(row) == "market_settled_or_past_no_new_entry"
    assert _row_skipped_diagnostic(row) == "market_settled_or_past_no_new_entry"


def test_non_entry_strategy_diagnostic_takes_precedence_over_size_zero() -> None:
    settlement_watch = {
        "strategy_recommendation": "settlement_dominant_watch",
        "weather_strategy_stage": "d0_morning",
        "entry_usable_edge_size_scale": 0.0,
        "entry_usable_edge_na_reason": "strategy_size_ratio_zero",
    }
    shadow_only = {
        "strategy_recommendation": "shadow_only",
        "weather_strategy_stage": "d0_morning",
        "weather_strategy_edge_tier_reason": "edge_lcb_too_thin_after_fees",
        "entry_usable_edge": -0.01,
        "entry_usable_edge_size_scale": 0.0,
        "entry_usable_edge_na_reason": "strategy_size_ratio_zero",
    }

    assert _row_skipped_diagnostic(settlement_watch) == "settlement_watch_not_entry"
    assert _row_skipped_diagnostic(shadow_only) == "edge_lcb_too_thin_after_fees"


def test_strategy_batch_default_pilot_cities_reuse_small_capital_defaults(monkeypatch) -> None:
    monkeypatch.setattr(
        small_capital_validation_review,
        "_default_pilot_cities",
        lambda limit: (["chongqing", "ankara"][:limit], "objective_audit_latest"),
    )

    assert _default_pilot_cities_from_reports(2) == ["chongqing", "ankara"]

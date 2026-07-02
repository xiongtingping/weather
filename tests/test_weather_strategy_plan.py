from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from scripts import weather_strategy_plan
from scripts.weather_strategy_plan import build_strategy_plan
from scripts.weather_strategy_plan import _normalize_event_dates


def test_strategy_plan_defaults_to_latest_per_key_across_runs(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(str(db_path))
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
                1,
                "highest-temperature-in-test-on-june-1",
                "33C",
                "YES",
                {"entry_usable_edge_na_reason": "event_worker_failed"},
                "2026-05-30T16:00:00+00:00",
            ),
            (
                2,
                None,
                "highest-temperature-in-test-on-june-1",
                "33C",
                "YES",
                {
                    "q_bayes_enabled": True,
                    "q_bayes_posterior": 0.62,
                    "q_bayes_prior_source": "market_history_low_sample",
                    "entry_usable_edge": 0.03,
                },
                "2026-05-31T04:00:00+00:00",
            ),
        ]
        for row in rows:
            conn.execute(
                """
                INSERT INTO auto_trade_decisions(
                  id, run_id, run_date, slug, event_date, title, outcome, side,
                  action, will_order, reason_codes_json, reasons_json, details_json, created_at
                )
                VALUES (?, ?, '2026-05-31', ?, '2026-06-01', 'Highest temperature in Test?', ?, ?,
                        'entry', 0, '[]', '[]', ?, ?)
                """,
                (row[0], row[1], row[2], row[3], row[4], json.dumps(row[5]), row[6]),
            )
        conn.commit()
    finally:
        conn.close()

    report = build_strategy_plan(db_path, ["2026-06-01"], rows_limit=10)

    plan = report["plans"][0]
    assert plan["selection"] == "latest_per_market_outcome_side_strategy"
    assert plan["rows"][0]["q_bayes_enabled"] is True
    assert plan["rows"][0]["q_bayes_prior_source"] == "market_history_low_sample"


def test_stage_from_details_uses_et_date_for_past_boundary(monkeypatch) -> None:
    class FixedDatetime(weather_strategy_plan.datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            if tz is None:
                return cls(2026, 6, 1, 0, 30)
            return cls(2026, 6, 1, 0, 30, tzinfo=weather_strategy_plan.timezone.utc).astimezone(tz)

    monkeypatch.setattr(weather_strategy_plan, "datetime", FixedDatetime)

    assert weather_strategy_plan._stage_from_details({}, "2026-05-31") == "d0_morning"
    assert weather_strategy_plan._stage_from_details({}, "2026-05-30") == "settled_or_past"
    assert (
        weather_strategy_plan._stage_from_details(
            {"q_bayes_z_stage": "settled_or_past"},
            "2026-05-31",
        )
        == "settled_or_past"
    )


def test_normalize_event_dates_dedupes_comma_inputs() -> None:
    assert _normalize_event_dates(["2026-05-31,2026-06-01", "2026-05-31"]) == [
        "2026-05-31",
        "2026-06-01",
    ]


def test_strategy_plan_hydrates_missing_station_from_fact_layer(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(str(db_path))
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
            CREATE TABLE event_fact_latest (
              slug TEXT NOT NULL,
              event_date TEXT NOT NULL,
              fact_id INTEGER NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(slug,event_date)
            );
            CREATE TABLE event_fact_history (
              id INTEGER PRIMARY KEY,
              run_date TEXT NOT NULL,
              slug TEXT NOT NULL,
              event_date TEXT NOT NULL,
              fact_as_of TEXT NOT NULL,
              model_snapshot_id INTEGER,
              market_snapshot_id INTEGER,
              composer_version TEXT NOT NULL,
              market_updated_at TEXT,
              model_updated_at TEXT,
              market_stale_sec INTEGER,
              model_cache_age_sec INTEGER,
              best_side TEXT,
              best_outcome TEXT,
              best_edge_score REAL,
              best_signal_edge_entry REAL,
              fact_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            """
        )
        details = {
            "q_entry": 0.62,
            "q_bayes_lcb": 0.60,
            "q_bayes_posterior": 0.66,
            "ask": 0.48,
            "q_bayes_z_stage": "d0_morning",
            "q_bayes_physical_likelihood_enabled": True,
            "weather_likelihood_feature_count": 40,
        }
        slug = "highest-temperature-in-seattle-on-may-31-2026"
        conn.execute(
            """
            INSERT INTO auto_trade_decisions(
              id, run_id, run_date, slug, event_date, title, outcome, side,
              action, will_order, reason_codes_json, reasons_json, details_json, created_at
            )
            VALUES (1, 1, '2026-05-31', ?, '2026-05-31',
                    'Highest temperature in Seattle?', '64-65°F', 'YES',
                    'entry', 0, '[]', '[]', ?, '2026-05-31T05:00:00+00:00')
            """,
            (slug, json.dumps(details)),
        )
        fact = {
            "station_id": "KSEA",
            "resolution_source": "https://www.wunderground.com/history/daily/us/wa/seatac/KSEA",
            "market_var": "temperature_2m_max",
            "market_unit": "F",
            "location_basis": "station",
        }
        conn.execute(
            """
            INSERT INTO event_fact_history(
              id, run_date, slug, event_date, fact_as_of, composer_version,
              fact_json, created_at
            )
            VALUES (1, '2026-05-31', ?, '2026-05-31', '2026-05-31T05:00:00Z',
                    'test', ?, '2026-05-31T05:00:00Z')
            """,
            (slug, json.dumps(fact)),
        )
        conn.execute(
            """
            INSERT INTO event_fact_latest(slug,event_date,fact_id,updated_at)
            VALUES (?, '2026-05-31', 1, '2026-05-31T05:00:00Z')
            """,
            (slug,),
        )
        conn.commit()
    finally:
        conn.close()

    report = build_strategy_plan(db_path, ["2026-05-31"], rows_limit=10)

    row = report["plans"][0]["rows"][0]
    assert row["station_id"] == "KSEA"
    assert row["source_station_id"] == "KSEA"
    assert row["source_station_match"] is True
    assert row["source_fit_tier"] == "settlement_source"
    assert row["weather_strategy_settlement_eligible"] is True
    assert row["resolution_source_profile_source"] == "event_fact_latest"


def test_strategy_plan_derives_mixed_settlement_and_reverse_edge_fields(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(str(db_path))
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
        details = {
            "q_entry": 0.84,
            "q_bayes_z_stage": "d0_morning",
            "edge_tradeability_profit_value": 0.13,
            "edge_tradeability_pass": True,
            "source_fit_tier": "settlement_source",
            "resolution_source": "https://www.wunderground.com/history/daily/us/ga/atlanta/KATL",
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
        }
        conn.execute(
            """
            INSERT INTO auto_trade_decisions(
              id, run_id, run_date, slug, event_date, title, outcome, side,
              action, will_order, reason_codes_json, reasons_json, details_json, created_at
            )
            VALUES (1, 9, '2026-05-31', 'highest-temperature-in-atlanta-on-may-31-2026',
                    '2026-05-31', 'Highest temperature in Atlanta?', '104°F or higher', 'NO',
                    'entry', 0, '[]', '[]', ?, '2026-05-31T05:00:00+00:00')
            """,
            (json.dumps(details),),
        )
        conn.commit()
    finally:
        conn.close()

    report = build_strategy_plan(db_path, ["2026-05-31"], rows_limit=10)

    row = report["plans"][0]["rows"][0]
    assert row["weather_strategy_stage"] == "d0_morning"
    assert row["weather_strategy_mode"] == "settlement_dominant"
    assert row["weather_strategy_edge_tier"] == "normal_trade"
    assert row["weather_strategy_reverse_edge"] is True
    assert row["weather_strategy_reverse_edge_action"] == "buy_no_equivalent_to_sell_yes"
    assert row["reverse_edge_execution_side"] == "NO"
    assert row["reverse_edge_execution_action"] == "BUY_NO"
    assert row["weather_strategy_tail_no"] is True
    assert row["weather_strategy_settlement_eligible"] is True
    assert row["resolution_source"].endswith("/KATL")
    assert row["station_id"] == "KATL"
    assert row["source_station_id"] == "KATL"
    assert row["source_station_match"] is True
    assert row["q_bayes_physical_likelihood_enabled"] is True
    assert row["q_bayes_physical_likelihood_evidence"] == 12.0
    assert row["q_bayes_physical_likelihood_source"] == "weather_physical_likelihood"
    assert row["weather_likelihood_feature_count"] == 40
    assert row["q_bayes_bimodal_risk"] is True
    assert row["q_bayes_bimodal_size_scale"] == 0.4
    assert row["market_tier"] == "secondary"
    assert row["focus_lane"] == "tail_no_reverse_edge"
    assert row["strategy_recommendation"] == "priority_tail_no_reverse_edge"
    assert report["plans"][0]["summary"]["reverse_edge"] == 1
    assert report["plans"][0]["summary"]["by_edge_tier"]["normal_trade"] == 1
    focus = report["plans"][0]["summary"]["resource_focus"]
    assert focus["target_structural_edge_min_share"] == 0.70
    assert focus["target_major_settlement_max_share"] == 0.30
    assert focus["structural_edge_count"] == 1
    assert focus["major_settlement_count"] == 0
    assert focus["target_met"] is True


def test_strategy_plan_resource_focus_flags_major_settlement_overweight(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(str(db_path))
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
                "highest-temperature-in-london-on-may-31-2026",
                {
                    "q_entry": 0.90,
                    "q_bayes_z_stage": "d1",
                    "edge_tradeability_profit_value": 0.20,
                    "entry_usable_edge": 0.15,
                    "source_fit_tier": "settlement_source",
                },
            ),
            (
                2,
                "highest-temperature-in-paris-on-may-31-2026",
                {
                    "q_entry": 0.88,
                    "q_bayes_z_stage": "d1",
                    "edge_tradeability_profit_value": 0.18,
                    "entry_usable_edge": 0.12,
                    "source_fit_tier": "settlement_source",
                },
            ),
            (
                3,
                "highest-temperature-in-atlanta-on-may-31-2026",
                {
                    "q_entry": 0.72,
                    "q_bayes_z_stage": "d1",
                    "edge_tradeability_profit_value": 0.12,
                    "entry_usable_edge": 0.08,
                    "source_fit_tier": "settlement_source",
                },
            ),
        ]
        for row_id, slug, details in rows:
            conn.execute(
                """
                INSERT INTO auto_trade_decisions(
                  id, run_id, run_date, slug, event_date, title, outcome, side,
                  action, will_order, reason_codes_json, reasons_json, details_json, created_at
                )
                VALUES (?, 1, '2026-05-31', ?, '2026-05-31', 'Highest temperature?',
                        '25°C', 'YES', 'entry', 0, '[]', '[]', ?, ?)
                """,
                (row_id, slug, json.dumps(details), f"2026-05-31T0{row_id}:00:00+00:00"),
            )
        conn.commit()
    finally:
        conn.close()

    report = build_strategy_plan(db_path, ["2026-05-31"], rows_limit=10)

    focus = report["plans"][0]["summary"]["resource_focus"]
    assert focus["actionable_count"] == 3
    assert focus["structural_edge_count"] == 1
    assert focus["major_settlement_count"] == 2
    assert focus["structural_edge_share"] == 1 / 3
    assert focus["major_settlement_share"] == 2 / 3
    assert focus["target_met"] is False


def test_strategy_plan_applies_autumn_size_scale_to_review_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(str(db_path))
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
        details = {
            "q_entry": 0.74,
            "q_bayes_z_stage": "d1",
            "edge_tradeability_profit_value": 0.12,
            "entry_usable_edge": 0.08,
            "entry_usable_edge_size_scale": 1.0,
            "source_fit_tier": "settlement_source",
        }
        conn.execute(
            """
            INSERT INTO auto_trade_decisions(
              id, run_id, run_date, slug, event_date, title, outcome, side,
              action, will_order, reason_codes_json, reasons_json, details_json, created_at
            )
            VALUES (1, 9, '2026-09-30', 'highest-temperature-in-atlanta-on-september-30-2026',
                    '2026-09-30', 'Highest temperature in Atlanta?', '84°F', 'YES',
                    'entry', 0, '[]', '[]', ?, '2026-09-29T05:00:00+00:00')
            """,
            (json.dumps(details),),
        )
        conn.commit()
    finally:
        conn.close()

    report = build_strategy_plan(db_path, ["2026-09-30"], rows_limit=10)

    row = report["plans"][0]["rows"][0]
    assert row["weather_strategy_season"] == "autumn"
    assert row["weather_strategy_season_size_scale"] == 0.40
    assert row["entry_usable_edge_size_scale_before_season"] == 1.0
    assert row["entry_usable_edge_size_scale_adjusted"] == 0.40


def test_strategy_plan_reconciles_stale_weather_tier_with_current_edge_profit(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(str(db_path))
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
        details = {
            "weather_strategy_edge_tier": "shadow_only",
            "weather_strategy_edge_tier_reason": "edge_lcb_too_thin_after_fees",
            "weather_strategy_entry_allowed": False,
            "edge_tradeability_profit_value": 0.13,
            "edge_tradeability_pass": True,
            "q_bayes_z_stage": "d0_morning",
        }
        conn.execute(
            """
            INSERT INTO auto_trade_decisions(
              id, run_id, run_date, slug, event_date, title, outcome, side,
              action, will_order, reason_codes_json, reasons_json, details_json, created_at
            )
            VALUES (1, 9, '2026-05-31', 'highest-temperature-in-miami-on-may-31-2026',
                    '2026-05-31', 'Highest temperature in Miami?', '86-87°F', 'YES',
                    'entry', 0, '[]', '[]', ?, '2026-05-31T05:00:00+00:00')
            """,
            (json.dumps(details),),
        )
        conn.commit()
    finally:
        conn.close()

    report = build_strategy_plan(db_path, ["2026-05-31"], rows_limit=10)

    row = report["plans"][0]["rows"][0]
    assert row["weather_strategy_edge_tier"] == "normal_trade"
    assert row["weather_strategy_edge_tier_reason"] == "edge_lcb_meets_strict_min"


def test_strategy_plan_keeps_distinct_strategy_families(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(str(db_path))
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
                {
                    "q_entry": 0.84,
                    "edge_tradeability_profit_value": 0.14,
                    "entry_usable_edge": 0.08,
                    "entry_usable_edge_size_scale": 1.0,
                    "entry_strategy_family": "path_probability_follow",
                    "q_bayes_z_stage": "d0_morning",
                    "source_fit_tier": "settlement_source",
                },
                "2026-05-31T05:00:00+00:00",
            ),
            (
                2,
                {
                    "q_entry": 0.84,
                    "edge_tradeability_profit_value": 0.14,
                    "entry_usable_edge": 0.0,
                    "entry_usable_edge_size_scale": 0.0,
                    "entry_strategy_family": "exploration_canary",
                    "q_bayes_z_stage": "d0_morning",
                    "source_fit_tier": "settlement_source",
                },
                "2026-05-31T06:00:00+00:00",
            ),
        ]
        for row_id, details, created_at in rows:
            conn.execute(
                """
                INSERT INTO auto_trade_decisions(
                  id, run_id, run_date, slug, event_date, title, outcome, side,
                  action, will_order, reason_codes_json, reasons_json, details_json, created_at
                )
                VALUES (?, 1, '2026-05-31', 'highest-temperature-in-atlanta-on-may-31-2026',
                        '2026-05-31', 'Highest temperature?', '104°F or higher', 'NO',
                        'entry', 0, '[]', '[]', ?, ?)
                """,
                (row_id, json.dumps(details), created_at),
            )
        conn.commit()
    finally:
        conn.close()

    report = build_strategy_plan(db_path, ["2026-05-31"], rows_limit=10)

    families = {row["strategy_family"] for row in report["plans"][0]["rows"]}
    assert {"path_probability_follow", "exploration_canary"} <= families

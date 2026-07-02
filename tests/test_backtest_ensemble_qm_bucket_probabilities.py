import argparse
import json
import sqlite3
from pathlib import Path

from scripts.backtest_ensemble_qm_bucket_probabilities import (
    _hours_bucket,
    _hours_to_peak_window,
    _regime_stratification_report,
    parse_args,
    run_backtest,
)


def _create_archive(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            """
            CREATE TABLE event_daily_temps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT,
                event_date TEXT,
                slug TEXT,
                title TEXT,
                city TEXT,
                station_id TEXT,
                resolution_source TEXT,
                market_var TEXT,
                market_unit TEXT,
                station_lat REAL,
                station_lon REAL,
                city_lat REAL,
                city_lon REAL,
                station_most_likely_label TEXT,
                station_most_likely_q REAL,
                city_most_likely_label TEXT,
                city_most_likely_q REAL,
                settlement_actual REAL,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE model_member_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model TEXT,
                loc_key TEXT,
                event_date TEXT,
                market_var TEXT,
                market_unit TEXT,
                members_json TEXT,
                created_at TEXT,
                official_updated_at TEXT,
                official_updated_source TEXT,
                members_signature TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE event_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT,
                event_date TEXT,
                slug TEXT,
                event_url TEXT,
                title TEXT,
                city TEXT,
                lat REAL,
                lon REAL,
                tz TEXT,
                location_basis TEXT,
                resolution_source TEXT,
                station_id TEXT,
                rounding_rule TEXT,
                market_var TEXT,
                market_unit TEXT,
                market_open_at TEXT,
                settlement_actual REAL,
                city_most_likely_label TEXT,
                city_most_likely_q REAL,
                payload_json TEXT,
                suggestions_json TEXT,
                created_at TEXT,
                event_peak_time_utc TEXT,
                event_end_utc TEXT,
                market_closed INTEGER
            )
            """
        )
        wu = "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU"
        actual_rows = [
            ("2026-01-01", 30.0, "2026-01-01T18:00:00+00:00"),
            ("2026-01-02", 30.0, "2026-01-02T00:30:00+00:00"),
            ("2026-01-03", 30.0, "2026-01-04T00:30:00+00:00"),
            ("2026-01-04", 30.0, "2026-01-04T18:00:00+00:00"),
        ]
        conn.executemany(
            """
            INSERT INTO event_daily_temps(
                event_date, city, station_id, resolution_source, market_var, market_unit,
                settlement_actual, created_at
            ) VALUES (?, 'Chengdu', 'ZUUU', ?, 'temperature_2m_max', 'C', ?, ?)
            """,
            [(day, wu, actual, created_at) for day, actual, created_at in actual_rows],
        )
        member_rows = [
            ("2026-01-01", "[28.0, 28.2]", "2026-01-01T12:00:00+00:00"),
            ("2026-01-02", "[28.0, 28.2]", "2026-01-02T01:00:00+00:00"),
            ("2026-01-02", "[99.0]", "2026-01-02T18:00:00+00:00"),
            ("2026-01-03", "[28.0, 28.2]", "2026-01-02T03:00:00+00:00"),
            ("2026-01-03", "[28.1, 28.3]", "2026-01-03T03:00:00+00:00"),
            ("2026-01-03", "[30.0]", "2026-01-03T18:00:00+00:00"),
            ("2026-01-05", "[28.0, 28.2]", "2026-01-04T19:00:00+00:00"),
        ]
        conn.executemany(
            """
            INSERT INTO model_member_cache(
                model, loc_key, event_date, market_var, market_unit, members_json, created_at
            ) VALUES ('gfs_seamless', '30.5583,103.9460', ?, 'temperature_2m_max', 'C', ?, ?)
            """,
            member_rows,
        )
        run_rows = [
            ("2026-01-03", "2026-01-02T04:00:00+00:00", "2026-01-03T08:00:00+00:00", "2026-01-03T12:00:00+00:00"),
            ("2026-01-03", "2026-01-03T04:00:00+00:00", "2026-01-03T08:00:00+00:00", "2026-01-03T12:00:00+00:00"),
            ("2026-01-05", "2026-01-04T20:00:00+00:00", "2026-01-05T08:00:00+00:00", "2026-01-05T12:00:00+00:00"),
        ]
        conn.executemany(
            """
            INSERT INTO event_runs(
                event_date, slug, city, station_id, market_var, market_unit, tz, lon,
                created_at, event_peak_time_utc, event_end_utc, market_closed, payload_json
            ) VALUES (?, 'highest-temperature-in-chengdu-on-january-3-2026', 'Chengdu', 'ZUUU',
                      'temperature_2m_max', 'C', 'Asia/Shanghai', 103.9, ?, ?,
                      ?, 0, '{"rows":[]}')
            """,
            run_rows,
        )
        conn.commit()
    finally:
        conn.close()


def _args(db_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        city="Chengdu",
        db=str(db_path),
        source="wunderground",
        market_var="temperature_2m_max",
        market_unit="C",
        rounding_rule="whole_degrees",
        min_events=2,
        min_market_days=1,
        require_min_market_days=False,
        min_segment_samples=1,
        max_events=365,
        min_sample_days=2,
        history_max_days=365,
        min_member_coverage=0.95,
        max_core_degradation=0.0,
        min_improvement=-1.0,
        min_boundary_improvement=-1.0,
        max_mae_degradation=1.0,
        max_logloss_degradation=1.0,
        calibration_mode="raw_seasonal_bias",
        calibration_dte_scope="d_minus_1,d0",
        disable_seasonal_correction=False,
        min_seasonal_sample_days=1,
        seasonal_shrinkage_days=10.0,
        seasonal_max_abs_adjustment=3.0,
        taf_weight_grid="0,12,24",
        taf_sigma_c=0.8,
        taf_min_samples=1,
        taf_baseline_evidence=36.0,
        include_climo_regime=False,
        compare_without_climo=False,
        climo_regime_evidence=2.0,
        climo_regime_base_evidence=36.0,
        climo_regime_min_samples=3,
        thin_minutes=0.0,
        peak_hour=16.0,
        walk_forward=True,
        strict_created_at=True,
        stratify_d0_d1=True,
        stratify_peak_window=True,
        allow_closed_snapshots=False,
        allow_snapshot_settlement_actual=False,
        allow_post_event_end=False,
        labels=["28°C", "29°C", "30°C", "31°C"],
    )


def test_created_at_walk_forward_uses_only_visible_members_and_history(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _create_archive(db_path)

    report = run_backtest(_args(db_path))

    assert report["evaluated_snapshots"] == 2
    assert report["diagnostics"]["leakage_guards"]["target_members_created_before_sample_created_at"] is True
    first, second = report["events"]
    assert first["dte_bucket"] == "d_minus_1"
    assert second["dte_bucket"] == "d0"
    assert first["hours_to_peak_bucket"] == "gt_12h"
    assert second["hours_to_peak_bucket"] == "2_to_6h"
    assert first["member_cache_created_at_max"] == "2026-01-02T03:00:00+00:00"
    assert second["member_cache_created_at_max"] == "2026-01-03T03:00:00+00:00"
    assert first["history_event_date_max"] == "2026-01-02"
    assert first["history_member_created_at_max"] == "2026-01-02T01:00:00+00:00"
    assert first["history_actual_created_at_max"] == "2026-01-02T00:30:00+00:00"
    assert report["stratified_by_dte"]["d_minus_1"]["n"] == 1
    assert report["stratified_by_dte"]["d0"]["n"] == 1
    assert report["stratified_by_hours_to_peak"]["gt_12h"]["n"] == 1
    assert report["stratified_by_hours_to_peak"]["2_to_6h"]["n"] == 1
    assert report["method"]["overall_gate"]["min_brier_improvement"] == -1.0
    assert report["method"]["required_dte_segment_gate"]["min_brier_improvement"] == 0.0
    pending = report["pending_future_snapshots"]
    assert pending["eligible_after_thinning"] == 1
    assert pending["by_event_date"] == {"2026-01-05": 1}
    assert pending["sample_rows"][0]["event_date"] == "2026-01-05"
    assert report["promotion_gate"]["qm_or_local_shadow_allowed"] is True


def test_taf_weight_calibration_uses_visible_snapshot_payload_only(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _create_archive(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            UPDATE event_runs
            SET payload_json = ?
            WHERE event_date = '2026-01-03'
              AND created_at = '2026-01-03T04:00:00+00:00'
            """,
            (
                json.dumps(
                    {
                        "taf_forecast_peak": {
                            "value_c": 30.0,
                            "source": "zuuu_taf_awc_tx_peak",
                        },
                        "weather_likelihood_features": {
                            "taf_forecast_peak_c": 30.0,
                        },
                        "rows": [],
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    args = _args(db_path)
    args.taf_min_samples = 1

    report = run_backtest(args)

    taf = report["taf_weight_calibration"]
    assert taf["status"] == "ok"
    assert taf["visible_sample_count"] == 1
    assert taf["created_at_guard"] == "uses_only_taf_fields_present_in_event_runs_payload_at_sample_created_at"
    assert taf["recommended_taf_evidence"] in {0.0, 12.0, 24.0}
    assert report["diagnostics"]["leakage_guards"]["taf_forecast_from_snapshot_payload_created_at_only"] is True


def test_chengdu_peak_window_uses_city_timezone_when_snapshot_tz_is_utc() -> None:
    row = {
        "event_date": "2026-06-11",
        "created_at": "2026-06-11T04:19:32+00:00",
        "tz": "UTC",
        "event_peak_time_utc": None,
    }

    hours = _hours_to_peak_window(row, peak_hour=16.0, default_tz="Asia/Shanghai")

    assert hours is not None
    assert round(hours, 2) == 3.67
    assert _hours_bucket(hours) == "2_to_6h"


def test_default_calibration_scope_is_d0_only_until_d1_walk_forward_passes() -> None:
    args = parse_args(["--city", "chengdu"])

    assert args.calibration_dte_scope == "d0"


def test_required_dte_segments_use_non_degradation_gate(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _create_archive(db_path)
    args = _args(db_path)
    args.min_improvement = 1.0
    args.min_boundary_improvement = 1.0

    report = run_backtest(args)

    assert "overall_metrics_below_gate" in report["blockers"]
    assert "d_minus_1_metrics_below_gate" not in report["blockers"]
    assert "d0_metrics_below_gate" not in report["blockers"]


def test_climo_regime_compare_cli_and_report_are_created_at_safe(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _create_archive(db_path)
    args = parse_args(
        [
            "--city",
            "chengdu",
            "--db",
            str(db_path),
            "--created-at-safe",
            "--include-climo-regime",
            "--compare-without-climo",
            "--stratify-by-lead-time",
            "--stratify-by-peak-window",
            "--min-events",
            "2",
            "--min-market-days",
            "1",
            "--min-segment-samples",
            "1",
            "--min-sample-days",
            "2",
            "--min-seasonal-sample-days",
            "1",
            "--calibration-dte-scope",
            "d_minus_1,d0",
            "--climo-regime-min-samples",
            "1",
            "--climo-regime-weight-grid",
            "0,1,2",
            "--labels",
            "28°C",
            "29°C",
            "30°C",
            "31°C",
        ]
    )

    report = run_backtest(args)

    assert report["diagnostics"]["leakage_guards"]["climo_regime_uses_only_visible_history_before_sample_created_at"] is True
    assert report["method"]["climo_regime"]["enabled"] is True
    assert report["climo_regime_compare"]["compare_without_climo"] is True
    assert "stratified_by_lead_time" in report["climo_regime_compare"]
    assert "stratified_by_peak_window" in report["climo_regime_compare"]
    assert report["climo_regime_weight_calibration"]["created_at_guard"] == (
        "uses_only_climo_features_and_history_visible_before_sample_created_at"
    )
    assert report["climo_regime_weight_calibration"]["grid"]


def test_climo_regime_audit_flags_and_regime_display_only_policy(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _create_archive(db_path)
    args = parse_args(
        [
            "--city",
            "chengdu",
            "--db",
            str(db_path),
            "--audit-climo-pp-impact",
            "--stratify-by-regime",
            "--min-events",
            "2",
            "--min-market-days",
            "1",
            "--min-segment-samples",
            "1",
            "--min-sample-days",
            "2",
            "--min-seasonal-sample-days",
            "1",
            "--climo-regime-min-samples",
            "1",
            "--labels",
            "28°C",
            "29°C",
            "30°C",
            "31°C",
        ]
    )

    report = run_backtest(args)

    assert report["climo_pp_impact_audit"]["climo_pp_cap_pass"] is True
    assert report["climo_regime_compare"]["temperature_trajectory_priority"] is True
    assert "climo_pp_max_delta" in report["verify_markers"][0]
    assert any(item == "temperature_trajectory_priority=true" for item in report["verify_markers"])


def test_regime_stratification_sample_below_10_is_display_only() -> None:
    report = _regime_stratification_report(
        [
            {
                "event_date": "2026-06-13",
                "actual_label": "30°C",
                "climo_regime_primary_regime": "weak_ventilation_static",
                "raw_brier": 0.2,
                "qm_brier": 0.19,
                "raw_mae": 0.2,
                "qm_mae": 0.19,
                "raw_boundary_rps": 0.2,
                "qm_boundary_rps": 0.19,
                "raw_logloss": 1.0,
                "qm_logloss": 0.9,
                "raw_top_label": "29°C",
                "qm_top_label": "30°C",
            }
            for _ in range(3)
        ]
    )

    segment = report["segments"]["weak_ventilation_static"]
    assert segment["regime_sample_count"] == 3
    assert segment["regime_auto_weight_allowed"] is False

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "temperature_posterior_backtest.py"
MATERIALIZE_SCRIPT = ROOT / "scripts" / "materialize_ctp_latest_facts.py"
AUDIT_SCRIPT = ROOT / "scripts" / "check_ctp_direct_replacement.py"


def load_backtest_module():
    spec = importlib.util.spec_from_file_location("temperature_posterior_backtest", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_fact_db(db_path: Path, payload: dict) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE event_fact_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL,
            event_date TEXT NOT NULL,
            fact_as_of TEXT NOT NULL,
            fact_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE event_fact_latest (
            slug TEXT NOT NULL,
            event_date TEXT NOT NULL,
            fact_id INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE event_fact_summary_cache (
            fact_id INTEGER PRIMARY KEY,
            slug TEXT NOT NULL,
            event_date TEXT NOT NULL,
            run_date TEXT NOT NULL,
            fact_as_of TEXT NOT NULL,
            summary_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    cur = conn.execute(
        """
        INSERT INTO event_fact_history(slug, event_date, fact_as_of, fact_json)
        VALUES (?, ?, ?, ?)
        """,
        (
            payload["slug"],
            payload["event_date"],
            "2026-06-29T08:00:00+00:00",
            json.dumps(payload),
        ),
    )
    conn.execute(
        "INSERT INTO event_fact_latest(slug, event_date, fact_id) VALUES (?, ?, ?)",
        (payload["slug"], payload["event_date"], cur.lastrowid),
    )
    conn.commit()
    conn.close()


def write_materialize_db(db_path: Path, payload: dict) -> int:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE event_fact_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            slug TEXT NOT NULL,
            event_date TEXT NOT NULL,
            fact_as_of TEXT NOT NULL,
            composer_version TEXT,
            fact_json TEXT NOT NULL,
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE event_fact_latest (
            slug TEXT NOT NULL,
            event_date TEXT NOT NULL,
            fact_id INTEGER NOT NULL,
            updated_at TEXT
        )
        """
    )
    cur = conn.execute(
        """
        INSERT INTO event_fact_history(
            run_date, slug, event_date, fact_as_of, composer_version, fact_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload["event_date"],
            payload["slug"],
            payload["event_date"],
            "2026-06-29T08:00:00+00:00",
            "test",
            json.dumps(payload),
            "2026-06-29T08:00:00+00:00",
        ),
    )
    fact_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at) VALUES (?, ?, ?, ?)",
        (payload["slug"], payload["event_date"], fact_id, "2026-06-29T08:00:00+00:00"),
    )
    conn.commit()
    conn.close()
    return fact_id


def sample_payload() -> dict:
    return {
        "slug": "highest-temperature-in-chengdu-on-june-29-2026",
        "event_date": "2026-06-29",
        "rounding_rule": "whole_degrees",
        "settlement_actual_c": 31.0,
        "distribution": [
            {"outcome": "30°C", "q_bayes_posterior": 0.1},
            {"outcome": "31°C", "q_bayes_posterior": 0.8},
            {"outcome": "32°C", "q_bayes_posterior": 0.1},
        ],
        "posterior_temperature_distribution": {
            "version": "continuous_temperature_posterior_v1",
            "target_variable": "settlement_daily_max_c",
            "status": "ok",
            "bucket_probabilities": [
                {"outcome": "30°C", "probability": 0.1},
                {"outcome": "31°C", "probability": 0.8},
                {"outcome": "32°C", "probability": 0.1},
            ],
            "cdf_values": [
                {"temp_c": 29.5, "cdf": 0.0},
                {"temp_c": 30.5, "cdf": 0.1},
                {"temp_c": 31.5, "cdf": 0.9},
                {"temp_c": 32.5, "cdf": 1.0},
            ],
            "grid": [],
            "quantiles_c": {"p50": 31.0},
            "quantile_grid": [{"q": 0.5, "temp_c": 31.0}],
            "source_components": {},
            "calibration": {},
            "observation_constraints": {"d0_violation_count": 0},
        },
    }


def sample_payload_without_ctp() -> dict:
    payload = sample_payload()
    payload.pop("posterior_temperature_distribution", None)
    return payload


def sample_payload_with_stale_regime_v1_ctp() -> dict:
    payload = sample_payload()
    payload["rounding_rule"] = "rounded_degrees"
    payload["weather_likelihood_features"] = {
        "market_var": "temperature_2m_max",
        "forecast_peak_temp_c": 32.0,
        "observed_temp_c": 26.0,
        "observed_today_max_c": 26.0,
        "observation_hour_local": 8.0,
        "expected_peak_hour_local": 16.0,
        "peak_window_start_hour_local": 14.0,
        "peak_window_end_hour_local": 17.0,
        "temperature_momentum_1h_c_per_hour": 1.1,
        "temperature_momentum_2h_c_per_hour": 0.8,
        "temperature_momentum_3h_c_per_hour": 0.6,
        "temperature_momentum_realization_state": "sustained_warming",
    }
    payload["weather_likelihood_features"]["peak_estimator"] = {
        "enabled": True,
        "source": "energy_balance_nowcast",
        "observed_temperature_trajectory_regime_v1": {
            "enabled": True,
            "available": True,
            "source": "observed_temperature_trajectory_regime_v1",
            "status": "enabled",
            "path_mu_c": 31.2,
            "path_sigma_c": 0.9,
        },
    }
    payload["posterior_temperature_distribution"]["source_components"] = {"components": [], "groups": []}
    payload["posterior_temperature_distribution"]["likelihood_updates"] = {
        "applied": False,
        "method": "none",
        "updates": [],
    }
    payload["target_bucket_selection"] = {
        "target_bucket": "31°C",
        "target_probability": 0.10,
        "target_probability_pct": 10.0,
        "target_posterior": 0.10,
        "source": "stale_fast_trade_fact_write",
    }
    payload["posterior_target_bucket_selection"] = dict(payload["target_bucket_selection"])
    payload["target_realization_capital_plan"] = {
        "ok": True,
        "version": "target_realization_capital_plan_v2",
        "target_bucket": "31°C",
        "target_posterior": 0.10,
        "second_target_bucket": "30°C",
        "second_target_posterior": 0.20,
    }
    return payload


def sample_payload_with_locked_observed_stale_trajectory_component() -> dict:
    payload = sample_payload()
    payload["rounding_rule"] = "rounded_degrees"
    payload["weather_likelihood_features"] = {
        "market_var": "temperature_2m_max",
        "forecast_peak_temp_c": 31.2,
        "observed_temp_c": 32.2,
        "observed_today_max_c": 34.0,
        "observed_today_max_source": "zuuu_metar_raw_awc_today_max_c",
        "observation_hour_local": 18.25,
        "peak_window_start_hour_local": 14.0,
        "peak_window_end_hour_local": 17.0,
        "temperature_momentum_1h_c_per_hour": -0.6,
        "temperature_momentum_2h_c_per_hour": -0.8,
        "temperature_momentum_realization_state": "cooling",
        "peak_estimator": {
            "enabled": True,
            "source": "locked_trusted_observed",
            "mu_c": 34.0,
            "sigma_c": 0.3,
            "observed_temp_c": 32.2,
            "observed_today_max_c": 34.0,
            "observed_today_max_source": "zuuu_metar_raw_awc_today_max_c",
            "hour_local": 18.25,
            "peak_window_start_hour_local": 14.0,
            "peak_window_end_hour_local": 17.0,
            "lock_threshold_hour_local": 17.0,
        },
    }
    stale_component = {
        "source_id": "temperature_trajectory",
        "group": "temperature_trajectory",
        "role": "observed_realization",
        "mean_c": 34.4,
        "sigma_c": 0.5,
        "precision": 0.5,
        "source": "observed_temperature_trajectory_regime_v1",
        "raw": {
            "source": "observed_temperature_trajectory_regime_v1",
            "path_mu_c": 34.4,
            "path_sigma_c": 0.5,
            "precision": 0.5,
        },
    }
    payload["posterior_temperature_distribution"]["source_components"] = {
        "components": [stale_component],
        "groups": [
            {
                "group": "temperature_trajectory",
                "mean_c": 34.4,
                "effective_precision": 0.5,
                "members": [stale_component],
            }
        ],
    }
    payload["q_bayes_temperature_trajectory_status"] = "enabled"
    payload["q_bayes_temperature_trajectory_enabled"] = True
    payload["q_bayes_temperature_trajectory_evidence"] = 0.5
    return payload


def test_backtest_passes_with_comparable_ctp_sample(tmp_path: Path) -> None:
    module = load_backtest_module()
    db_path = tmp_path / "archive.db"
    write_fact_db(db_path, sample_payload())

    result = module.run_backtest(db_path, min_settled=1)

    assert result["status"] == "ok"
    assert result["ctp_samples"] == 1
    assert result["comparable_samples"] == 1
    assert result["metrics"]["d0_violation_total"] == 0
    assert result["metrics"]["ctp_brier"] == result["metrics"]["baseline_brier"]


def test_backtest_fails_closed_when_ctp_samples_are_insufficient(tmp_path: Path) -> None:
    module = load_backtest_module()
    db_path = tmp_path / "archive.db"
    write_fact_db(db_path, sample_payload())

    result = module.run_backtest(db_path, min_settled=2)

    assert result["status"] == "fail"
    assert "insufficient_ctp_settled_samples" in result["gate_failures"]


def test_materialize_ctp_latest_facts_dry_run_does_not_repoint_latest(tmp_path: Path) -> None:
    module = load_module(MATERIALIZE_SCRIPT, "materialize_ctp_latest_facts")
    audit = load_module(AUDIT_SCRIPT, "check_ctp_direct_replacement")
    db_path = tmp_path / "archive.db"
    original_fact_id = write_materialize_db(db_path, sample_payload_without_ctp())

    report = module.run(db_path, apply=False, slug_like="%temperature%", limit=None)

    assert report["ok"] is True
    assert report["would_append_event_fact_history_rows"] == 1
    assert report["applied_event_fact_history_rows"] == 0
    conn = sqlite3.connect(db_path)
    latest_fact_id = conn.execute("SELECT fact_id FROM event_fact_latest").fetchone()[0]
    history_count = conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0]
    conn.close()
    assert latest_fact_id == original_fact_id
    assert history_count == 1
    assert audit.audit_latest_fact_ctp_coverage(db_path)


def test_materialize_ctp_latest_facts_apply_repoints_latest_with_ctp(tmp_path: Path) -> None:
    module = load_module(MATERIALIZE_SCRIPT, "materialize_ctp_latest_facts")
    audit = load_module(AUDIT_SCRIPT, "check_ctp_direct_replacement")
    db_path = tmp_path / "archive.db"
    original_fact_id = write_materialize_db(db_path, sample_payload_without_ctp())

    report = module.run(db_path, apply=True, slug_like="%temperature%", limit=None)

    assert report["ok"] is True
    assert report["would_append_event_fact_history_rows"] == 1
    assert report["applied_event_fact_history_rows"] == 1
    assert report["would_upsert_event_fact_summary_cache_rows"] == 1
    assert report["applied_event_fact_summary_cache_rows"] == 1
    assert report["items"][0]["new_fact_id"] != original_fact_id
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    latest = conn.execute(
        """
        SELECT l.fact_id, h.fact_json, c.summary_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        LEFT JOIN event_fact_summary_cache c ON c.fact_id = l.fact_id
        """
    ).fetchone()
    history_count = conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0]
    conn.close()
    payload = json.loads(latest["fact_json"])
    summary = json.loads(latest["summary_json"])
    posterior = payload["posterior_temperature_distribution"]
    assert latest["fact_id"] != original_fact_id
    assert history_count == 2
    assert {"grid", "cdf_values", "quantiles_c", "quantile_grid", "bucket_probabilities"}.issubset(posterior)
    assert payload["target_bucket_probability_field"] == "posterior_temperature_distribution.bucket_probabilities"
    assert payload["distribution"][0]["posterior_field"] == "posterior_temperature_distribution.bucket_probabilities"
    assert summary["summary_cache_source"] == "event_fact_summary_cache"
    assert summary["summary_cache_version"] == module.SUMMARY_CACHE_VERSION
    assert summary["target_bucket_probability_field"] == "posterior_temperature_distribution.bucket_probabilities"
    assert audit.audit_latest_fact_ctp_coverage(db_path) == []


def test_materialize_ctp_latest_facts_replays_stale_regime_v1_component(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = load_module(MATERIALIZE_SCRIPT, "materialize_ctp_latest_facts")
    db_path = tmp_path / "archive.db"
    original_fact_id = write_materialize_db(db_path, sample_payload_with_stale_regime_v1_ctp())
    monkeypatch.setenv("PM_BAYES_PROBABILITY_ENABLED", "1")
    monkeypatch.setenv("PM_BAYES_LIKELIHOOD_ENABLED", "1")
    monkeypatch.setenv("PM_BAYES_LIKELIHOOD_EVIDENCE", "0")
    monkeypatch.setenv("PM_BAYES_CLIMO_REGIME_ENABLED", "0")
    monkeypatch.setenv("PM_BAYES_WU_FORECAST_ENABLED", "0")
    monkeypatch.setenv("PM_BAYES_TAF_PEAK_ENABLED", "0")
    monkeypatch.setenv("PM_BAYES_TRAJECTORY_REGIME_V1_SOURCE_COMPONENT_ENABLED", "1")

    report = module.run(db_path, apply=True, slug_like="%temperature%", limit=None)

    assert report["ok"] is True
    assert report["already_complete"] == 0
    assert report["applied_event_fact_history_rows"] == 1
    assert report["items"][0]["new_fact_id"] != original_fact_id
    assert report["items"][0]["forced_replay_reason"] == (
        "available_regime_v1_missing_temperature_trajectory_component"
    )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    latest = conn.execute(
        """
        SELECT l.fact_id, h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        """
    ).fetchone()
    conn.close()
    payload = json.loads(latest["fact_json"])
    groups = payload["posterior_temperature_distribution"]["source_components"]["groups"]
    assert any(group.get("group") == "temperature_trajectory" for group in groups)
    assert payload["ctp_replay_reason"] == "available_regime_v1_missing_temperature_trajectory_component"
    assert payload["q_bayes_temperature_trajectory_enabled"] is True
    assert payload["q_bayes_temperature_trajectory_evidence"] > 0
    assert payload["target_bucket_probability_pct"] == payload["posterior_highest_probability_pct"]
    assert payload["target_bucket_selection"]["target_probability_pct"] == payload["posterior_highest_probability_pct"]
    assert payload["posterior_target_bucket_selection"] == payload["target_bucket_selection"]
    assert payload["target_realization_capital_plan"]["target_posterior"] == payload["target_bucket_selection"][
        "target_probability"
    ]


def test_materialize_ctp_latest_facts_replays_regime_v1_precision_change(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = load_module(MATERIALIZE_SCRIPT, "materialize_ctp_latest_facts")
    db_path = tmp_path / "archive.db"
    payload = sample_payload_with_stale_regime_v1_ctp()
    regime = payload["weather_likelihood_features"]["peak_estimator"]["observed_temperature_trajectory_regime_v1"]
    regime["precision"] = 1.5
    regime["effective_evidence"] = 1.5
    regime["evidence"] = 1.5
    regime["evidence_base"] = 1.0
    regime["evidence_scale"] = 1.5
    payload["posterior_temperature_distribution"]["source_components"] = {
        "components": [
            {
                "source_id": "temperature_trajectory",
                "group": "temperature_trajectory",
                "role": "observed_realization",
                "mean_c": 31.2,
                "sigma_c": 0.9,
                "precision": 0.1,
                "source": "observed_temperature_trajectory_regime_v1",
            }
        ],
        "groups": [
            {
                "group": "temperature_trajectory",
                "mean_c": 31.2,
                "effective_precision": 0.1,
                "members": [
                    {
                        "source_id": "temperature_trajectory",
                        "group": "temperature_trajectory",
                        "precision": 0.1,
                        "raw": {
                            "source": "observed_temperature_trajectory_regime_v1",
                            "path_mu_c": 31.2,
                            "path_sigma_c": 0.9,
                            "precision": 0.1,
                        },
                    }
                ],
            }
        ],
    }
    original_fact_id = write_materialize_db(db_path, payload)
    monkeypatch.setenv("PM_BAYES_PROBABILITY_ENABLED", "1")
    monkeypatch.setenv("PM_BAYES_LIKELIHOOD_ENABLED", "1")
    monkeypatch.setenv("PM_BAYES_LIKELIHOOD_EVIDENCE", "0")
    monkeypatch.setenv("PM_BAYES_CLIMO_REGIME_ENABLED", "0")
    monkeypatch.setenv("PM_BAYES_WU_FORECAST_ENABLED", "0")
    monkeypatch.setenv("PM_BAYES_TAF_PEAK_ENABLED", "0")
    monkeypatch.setenv("PM_BAYES_TRAJECTORY_REGIME_V1_SOURCE_COMPONENT_ENABLED", "1")

    report = module.run(db_path, apply=True, slug_like="%temperature%", limit=None)

    assert report["ok"] is True
    assert report["already_complete"] == 0
    assert report["applied_event_fact_history_rows"] == 1
    assert report["items"][0]["new_fact_id"] != original_fact_id
    assert report["items"][0]["forced_replay_reason"] == (
        "available_regime_v1_temperature_trajectory_precision_changed"
    )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    latest = conn.execute(
        """
        SELECT l.fact_id, h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        """
    ).fetchone()
    conn.close()
    materialized = json.loads(latest["fact_json"])
    assert materialized["ctp_replay_reason"] == "available_regime_v1_temperature_trajectory_precision_changed"
    assert materialized["q_bayes_temperature_trajectory_evidence"] > 0.1
    assert materialized["q_bayes_temperature_trajectory_evidence_scale"] > 0
    features = materialized["weather_likelihood_features"]
    assert features["temperature_momentum_posterior_gate_allowed"] is True
    assert "1h" in features["temperature_momentum_posterior_confirmed_windows"]
    assert features["temperature_momentum_posterior_gate_reason"] == "regime_v1_confirmed_momentum_windows"


def test_materialize_ctp_latest_facts_marks_locked_observed_trajectory_inactive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = load_module(MATERIALIZE_SCRIPT, "materialize_ctp_latest_facts")
    db_path = tmp_path / "archive.db"
    original_fact_id = write_materialize_db(
        db_path,
        sample_payload_with_locked_observed_stale_trajectory_component(),
    )
    monkeypatch.setenv("PM_BAYES_PROBABILITY_ENABLED", "1")
    monkeypatch.setenv("PM_BAYES_LIKELIHOOD_ENABLED", "1")
    monkeypatch.setenv("PM_BAYES_LIKELIHOOD_EVIDENCE", "0")
    monkeypatch.setenv("PM_BAYES_CLIMO_REGIME_ENABLED", "0")
    monkeypatch.setenv("PM_BAYES_WU_FORECAST_ENABLED", "0")
    monkeypatch.setenv("PM_BAYES_TAF_PEAK_ENABLED", "0")
    monkeypatch.setenv("PM_BAYES_TRAJECTORY_REGIME_V1_SOURCE_COMPONENT_ENABLED", "1")

    report = module.run(db_path, apply=True, slug_like="%temperature%", limit=None)

    assert report["ok"] is True
    assert report["already_complete"] == 0
    assert report["applied_event_fact_history_rows"] == 1
    assert report["items"][0]["new_fact_id"] != original_fact_id
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    latest = conn.execute(
        """
        SELECT h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        """
    ).fetchone()
    conn.close()
    payload = json.loads(latest["fact_json"])
    groups = payload["posterior_temperature_distribution"]["source_components"]["groups"]
    assert not any(group.get("group") == "temperature_trajectory" for group in groups)
    assert payload["ctp_replay_reason"] == "stale_temperature_trajectory_component_without_available_regime_v1"
    assert payload["q_bayes_temperature_trajectory_enabled"] is False
    assert payload["q_bayes_temperature_trajectory_evidence"] == 0.0
    assert payload["q_bayes_temperature_trajectory_status"] == "locked_trusted_observed_after_peak_window"
    assert payload["q_bayes_temperature_trajectory_na_reason"] == "hard_observed_peak_locked_after_peak_window"
    assert payload["q_bayes_temperature_trajectory_peak_estimator_source"] == "locked_trusted_observed"


def test_materialize_ctp_latest_facts_repairs_missing_summary_cache(tmp_path: Path) -> None:
    module = load_module(MATERIALIZE_SCRIPT, "materialize_ctp_latest_facts")
    db_path = tmp_path / "archive.db"
    original_fact_id = write_materialize_db(db_path, sample_payload())

    report = module.run(db_path, apply=True, slug_like="%temperature%", limit=None)

    assert report["ok"] is True
    assert report["already_complete"] == 1
    assert report["applied_event_fact_history_rows"] == 0
    assert report["would_upsert_event_fact_summary_cache_rows"] == 1
    assert report["applied_event_fact_summary_cache_rows"] == 1
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT summary_json FROM event_fact_summary_cache WHERE fact_id = ?",
        (original_fact_id,),
    ).fetchone()
    conn.close()
    assert row is not None
    summary = json.loads(row[0])
    assert summary["summary_cache_source"] == "event_fact_summary_cache"
    assert summary["summary_cache_version"] == module.SUMMARY_CACHE_VERSION
    assert summary["posterior_temperature_distribution"]["status"] == "ok"

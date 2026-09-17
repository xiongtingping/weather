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
            updated_at TEXT,
            PRIMARY KEY(slug, event_date)
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
        # The prior layer the fact writer's apply contract requires of every
        # payload it persists.  materialize_ctp_latest_facts hands these
        # straight to that writer, so without it the apply was refused on
        # prior_layer_contract_failed and nothing was appended -- before any
        # of the CTP behaviour these tests are about.  Production facts carry
        # a prior; this fixture predates the contract.
        # And the composer identity, without which the writer's own
        # materializer mints a blocked SSOT block
        # (joint_weak_physical_strong_model_unavailable) and the apply
        # contract refuses the payload on a dozen posterior_ssot_fact_*
        # issues.
        "production_model_version": "joint_weak_physical_strong_composer_v1",
        "production_model_ready": True,
        "prior_distribution": {"30°C": 0.2, "31°C": 0.6, "32°C": 0.2},
        "prior_na_reason": "",
        "prior_meta": {
            "schema": "temperature_prior_meta_v1",
            "state": "available",
            "source": "unit_test_backtest_prior",
            "boundary": (
                "historical_climate_date_site_terrain_weather_type_only"
                "_no_forecast_no_metar_no_market"
            ),
            "sample_count": 30,
            "date_window_days": 15,
            "shrinkage_target": "broader_settled_history",
            "prior_na_reason": "",
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
        # Named, because an unattributed observed maximum stops counting as a
        # hard observation the moment a payload declares a source lineage --
        # and the materializer's write declares one.  Without the source the
        # rewritten fact ranks below its own parent and event_fact_latest is
        # never repointed onto it, so nothing this materializer produces is
        # ever read.  A production fact names its source.
        "observed_today_max_source": "zuuu_metar_raw_awc",
        "zuuu_metar_raw_awc_today_max_c": 26.0,
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
        # Inside the ladder.  This fixture's distribution is 30/31/32 and its
        # peak estimator reads 31.2, but the observed maximum said 34.0 -- a
        # day that had already passed every bucket the market lists.  The
        # writer hard-zeroes every rung below the observed floor, so the whole
        # distribution went to zero and the SSOT block came back missing on
        # posterior_distribution_has_no_hard_observation_eligible_target.  The
        # locked-after-peak-window story this test is named for works on any
        # value the ladder can hold.
        "observed_temp_c": 30.0,
        "observed_today_max_c": 31.0,
        "observed_today_max_source": "zuuu_metar_raw_awc_today_max_c",
        # The peak estimator locks on an EXPLICIT final-daily-max verdict, not
        # on the clock: being past the peak window is no longer enough on its
        # own, so a fixture that only said "observation_hour_local is late"
        # kept the trajectory enabled and never reached the locked state this
        # test is named for.
        "temperature_observation_final_daily_max": True,
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
            "observed_temp_c": 30.0,
            "observed_today_max_c": 31.0,
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


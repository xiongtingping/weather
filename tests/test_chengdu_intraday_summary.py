import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from chengdu_intraday_summary import (
    _apply_d0_observed_feasibility_guard,
    _decision_text,
    _distribution_rows,
    _dynamic_target_bucket_selection,
    _apply_bayes_target_priority,
    _forecast_reference_bayes,
    _hard_final_bucket_lock_reason,
    _hard_source_direct_bucket,
    _high_temp_amplification_guard,
    _merge_ssot_payload_target_selection,
    _apply_observation_led_likelihood_to_rows,
    _observation_led_bayes,
    _physical_weather_modifier,
    _refresh_cadence,
    _settlement_lock_bayes,
    _source_observation_comparison,
    _sync_target_bucket_outlook_with_temperature_path_distribution,
    _target_bucket_outlook,
    _target_bucket_observation_state,
    _target_rows,
    _TEMPERATURE_PATH_CALIBRATION_CACHE,
    _temperature_momentum_supports_warming,
    _temperature_stages,
    _temperature_path_distribution,
    _warming_pace_assessment,
    build_chengdu_intraday_summary,
    build_log_line,
)


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE event_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            title TEXT,
            payload_json TEXT,
            created_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX idx_event_runs_created_at ON event_runs(created_at DESC)")
    return conn


class ChengduIntradaySummaryTest(unittest.TestCase):
    def test_hard_source_direct_bucket_uses_wu_intraday_as_lower_bound_not_final_lock(self):
        rows = [{"outcome": "27°C or below"}, {"outcome": "28°C"}, {"outcome": "29°C"}]
        features = {
            "zuuu_metar_raw_awc_today_max_c": 25.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-27T00:00:00+00:00",
            "wu_twc_observed_intraday_current_temp_c": 27.78,
            "wu_twc_observed_intraday_today_max_c": 28.0,
            "wu_twc_observed_intraday_current_at": "2026-06-27T03:46:33+00:00",
            "wu_twc_observed_intraday_today_max_at": "2026-06-27T03:46:33+00:00",
            "wu_twc_observed_intraday_fetched_at": "2026-06-27T03:48:44+00:00",
            "wu_twc_observed_intraday_stale": False,
        }

        bucket = _hard_source_direct_bucket(rows, features, "whole_degrees", "C")

        self.assertEqual(bucket["source_id"], "wu_twc_observed_intraday")
        self.assertEqual(bucket["bucket"], "28°C")
        self.assertEqual(bucket["observed_max_c"], 28.0)
        self.assertEqual(bucket["semantics"], "intraday_observed_lower_bound_constraint_for_ssot_posterior")

    def test_hard_source_direct_bucket_prefers_final_settlement(self):
        rows = [{"outcome": "32°C"}, {"outcome": "33°C"}, {"outcome": "34°C"}]
        features = {
            "settlement_actual": 33.0,
            "settlement_actual_source": "wu_twc_final_settlement",
            "settlement_signal_created_at": "2026-06-22T10:00:00+00:00",
            "zuuu_metar_raw_awc_today_max_c": 34.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-22T09:00:00+00:00",
        }

        bucket = _hard_source_direct_bucket(rows, features, "whole_degrees", "C")

        self.assertEqual(bucket["source_id"], "wu_twc_final_settlement")
        self.assertEqual(bucket["bucket"], "33°C")
        self.assertEqual(bucket["semantics"], "final_bucket_lock_for_ssot_posterior")

    def test_observation_led_candidate_does_not_override_posterior_without_explicit_switch(self):
        rows = [
            {"outcome": "27°C or below", "q_raw": 0.60, "q_temperature_bucket_posterior": 0.42},
            {"outcome": "28°C", "q_raw": 0.10, "q_temperature_bucket_posterior": 0.12},
            {"outcome": "29°C", "q_raw": 0.18, "q_temperature_bucket_posterior": 0.24},
            {"outcome": "30°C", "q_raw": 0.12, "q_temperature_bucket_posterior": 0.22},
        ]
        features = {
            "observed_temp_c": 27.78,
            "observed_today_max_c": 27.78,
            "observed_today_max_at": "2026-06-27T03:00:00+00:00",
            "peak_estimator_mu_c": 29.1,
            "peak_estimator_sigma_c": 0.35,
            "temperature_momentum_effective_c_per_hour": 0.60,
            "temperature_momentum_posterior_gate_allowed": True,
            "temperature_momentum_posterior_gate_reason": "unit_test",
        }
        forecast = _forecast_reference_bayes(rows, features, market_unit="C")
        observation = _observation_led_bayes(
            rows,
            features,
            {"temperature_momentum": {"effective_rate_c_per_hour": 0.60}},
            rounding_rule="whole_degrees",
            market_unit="C",
            event_date="2026-06-27",
            reference_bucket="27°C or below",
        )
        settlement = _settlement_lock_bayes(rows, features, rounding_rule="whole_degrees", market_unit="C")
        selected = _apply_bayes_target_priority(
            {"target_bucket": "27°C or below", "posterior_highest_bucket_label": "27°C or below"},
            forecast_reference_bayes=forecast,
            observation_led_bayes=observation,
            settlement_lock_bayes=settlement,
        )

        self.assertEqual(forecast["target_bucket"], "27°C or below")
        self.assertEqual(observation["observed_floor_bucket"], "28°C")
        self.assertEqual(observation["target_bucket"], "29°C")
        self.assertEqual(observation["switch_explanation"], "27°C or below -> 29°C")
        self.assertEqual(selected["selection_priority"], "dynamic_posterior_highest_bucket")
        self.assertEqual(selected["target_bucket"], "27°C or below")
        self.assertEqual(selected["observation_led_target_bucket"], "29°C")
        self.assertEqual(selected["target_switch_context"]["from_bucket"], "27°C or below")
        self.assertEqual(selected["target_switch_context"]["to_bucket"], "27°C or below")
        self.assertEqual(selected["target_switch_context"]["candidate_bucket"], "29°C")
        self.assertFalse(selected["target_switch_context"]["observation_led_candidate_applied"])

    def test_observation_led_likelihood_reweights_main_posterior_before_target_selection(self):
        rows = [
            {"outcome": "27°C or below", "q_raw": 0.60, "q_temperature_bucket_posterior": 0.42},
            {"outcome": "28°C", "q_raw": 0.10, "q_temperature_bucket_posterior": 0.12},
            {"outcome": "29°C", "q_raw": 0.18, "q_temperature_bucket_posterior": 0.24},
            {"outcome": "30°C", "q_raw": 0.12, "q_temperature_bucket_posterior": 0.22},
        ]
        features = {
            "observed_temp_c": 27.78,
            "observed_today_max_c": 27.78,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "peak_estimator_mu_c": 29.1,
            "peak_estimator_sigma_c": 0.35,
            "temperature_momentum_effective_c_per_hour": 0.60,
            "temperature_momentum_posterior_gate_allowed": True,
        }
        observation = _observation_led_bayes(
            rows,
            features,
            {"temperature_momentum": {"effective_rate_c_per_hour": 0.60}},
            rounding_rule="whole_degrees",
            market_unit="C",
            event_date="2026-06-27",
            reference_bucket="27°C or below",
        )

        reweighted, meta = _apply_observation_led_likelihood_to_rows(rows, observation, features)
        top = _dynamic_target_bucket_selection(
            reweighted,
            _target_rows(reweighted, single_only=False),
            _target_rows(reweighted, single_only=True),
            features,
            "whole_degrees",
            "C",
            {"temperature_momentum": {"effective_rate_c_per_hour": 0.60}},
            event_date="2026-06-27",
        )

        by_bucket = {row["outcome"]: row for row in reweighted}
        self.assertTrue(meta["applied"])
        self.assertEqual(meta["target_bucket_before"], "27°C or below")
        self.assertEqual(meta["target_bucket_after"], "29°C")
        self.assertEqual(top["target_bucket"], "29°C")
        self.assertGreater(by_bucket["29°C"]["q_temperature_bucket_posterior"], by_bucket["27°C or below"]["q_temperature_bucket_posterior"])
        self.assertEqual(by_bucket["29°C"]["q_temperature_bucket_posterior"], by_bucket["29°C"]["q_bayes_posterior"])
        self.assertEqual(by_bucket["27°C or below"]["q_temperature_bucket_posterior_pre_observation_led_likelihood"], 0.42)

    def test_hard_awc_one_hour_momentum_is_watch_only_without_context_windows(self):
        features = {
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "observed_temp_source": "zuuu_metar_raw_awc",
            "temperature_rise_rate_source": "zuuu_metar_raw_awc:1h",
            "temperature_momentum_realization_state": "emerging_warming",
            "temperature_momentum_1h_c_per_hour": 2.0,
            "temperature_momentum_effective_c_per_hour": 2.0,
            "temperature_rise_rate_c_per_hour": 2.0,
        }

        supports, gate = _temperature_momentum_supports_warming(features)

        self.assertFalse(supports)
        self.assertFalse(gate["posterior_gate_allowed"])
        self.assertEqual(gate["posterior_gate_reason"], "multi_window_confirmation_missing")

    def test_hard_awc_one_hour_momentum_uses_context_when_trend_missing(self):
        features = {
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "observed_temp_source": "zuuu_metar_raw_awc",
            "temperature_rise_rate_source": "zuuu_metar_raw_awc:1h",
            "temperature_momentum_realization_state": "emerging_warming",
            "temperature_momentum_3h_c_per_hour": 1.2,
            "temperature_momentum_2h_c_per_hour": 1.5,
            "temperature_momentum_1h_c_per_hour": 2.0,
            "temperature_momentum_effective_c_per_hour": 2.0,
            "temperature_rise_rate_c_per_hour": 2.0,
        }
        trend = {
            "temperature_momentum": {
                "state": "missing",
                "effective_rate_c_per_hour": 2.0,
            }
        }

        supports, gate = _temperature_momentum_supports_warming(features, trend)

        self.assertTrue(supports)
        self.assertTrue(gate["posterior_gate_allowed"])
        self.assertEqual(gate["state"], "emerging_warming")
        self.assertEqual(gate["posterior_gate_reason"], "one_hour_core_confirmed")

    def test_observation_led_anchors_peak_mu_after_peak_window_with_hard_awc(self):
        rows = [
            {"outcome": "32°C", "q_temperature_bucket_posterior": 0.0},
            {"outcome": "33°C", "q_temperature_bucket_posterior": 0.30},
            {"outcome": "34°C", "q_temperature_bucket_posterior": 0.63},
        ]
        features = {
            "observed_temp_c": 33.0,
            "observed_temp_source": "zuuu_metar_raw_awc",
            "observed_today_max_c": 33.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "zuuu_metar_raw_awc_today_max_c": 33.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-29T07:00:00+00:00",
            "weather_likelihood_refreshed_at": "2026-06-29T07:59:52+00:00",
            "taf_forecast_peak_at": "2026-06-29T15:00:00+08:00",
            "peak_estimator_mu_c": 33.82,
            "peak_estimator_sigma_c": 0.57,
            "peak_estimator_source": "energy_balance_nowcast",
            "temperature_momentum_realization_state": "emerging_warming",
            "temperature_momentum_effective_c_per_hour": 2.0,
            "temperature_rise_rate_c_per_hour": 2.0,
            "temperature_rise_rate_source": "zuuu_metar_raw_awc:1h",
        }

        observation = _observation_led_bayes(
            rows,
            features,
            {"temperature_momentum": {"state": "missing", "effective_rate_c_per_hour": 2.0}},
            rounding_rule="whole_degrees",
            market_unit="C",
            event_date="2026-06-29",
            reference_bucket="34°C",
        )

        by_bucket = {row["outcome"]: row for row in observation["all_rows"]}
        self.assertEqual(observation["target_bucket"], "33°C")
        self.assertEqual(observation["peak_estimator_mu_c"], 33.0)
        self.assertTrue(observation["post_peak_hard_observation_anchor_applied"])
        self.assertGreater(by_bucket["34°C"]["observation_led_probability"], 0.0)

    def test_explicit_temperature_path_switch_can_promote_observation_led_target(self):
        rows = [
            {"outcome": "27°C or below", "q_raw": 0.60, "q_temperature_bucket_posterior": 0.42},
            {"outcome": "28°C", "q_raw": 0.10, "q_temperature_bucket_posterior": 0.12},
            {"outcome": "29°C", "q_raw": 0.18, "q_temperature_bucket_posterior": 0.24},
            {"outcome": "30°C", "q_raw": 0.12, "q_temperature_bucket_posterior": 0.22},
        ]
        features = {
            "observed_temp_c": 27.78,
            "observed_today_max_c": 27.78,
            "peak_estimator_mu_c": 29.1,
            "peak_estimator_sigma_c": 0.35,
            "temperature_momentum_effective_c_per_hour": 0.60,
            "temperature_momentum_posterior_gate_allowed": True,
        }
        forecast = _forecast_reference_bayes(rows, features, market_unit="C")
        observation = _observation_led_bayes(
            rows,
            features,
            {"temperature_momentum": {"effective_rate_c_per_hour": 0.60}},
            rounding_rule="whole_degrees",
            market_unit="C",
            event_date="2026-06-27",
            reference_bucket="27°C or below",
        )
        settlement = _settlement_lock_bayes(rows, features, rounding_rule="whole_degrees", market_unit="C")

        selected = _apply_bayes_target_priority(
            {
                "target_bucket": "29°C",
                "target_probability_pct": 24.0,
                "target_probability_field": "q_temperature_bucket_posterior",
                "posterior_highest_bucket_label": "27°C or below",
                "dynamic_target_source": "temperature_trajectory_switch_pressure",
                "dynamic_target_state": "temperature_trajectory_switch",
                "dynamic_target_label": "升温轨迹切桶",
                "dynamic_target_reason": "unit test explicit switch",
                "target_row": dict(rows[2]),
            },
            forecast_reference_bayes=forecast,
            observation_led_bayes=observation,
            settlement_lock_bayes=settlement,
        )

        self.assertEqual(selected["selection_priority"], "temperature_trajectory_switch_pressure")
        self.assertEqual(selected["dynamic_target_source"], "temperature_trajectory_switch_pressure")
        self.assertEqual(selected["target_bucket"], "29°C")
        self.assertEqual(selected["target_probability_field"], "q_temperature_bucket_posterior")
        self.assertFalse(selected["target_switch_context"]["observation_led_candidate_applied"])
        self.assertEqual(
            selected["target_switch_context"]["observation_led_candidate_not_applied_reason"],
            "diagnostic_only_until_explicit_temperature_path_switch",
        )

    def test_observation_led_diagnostic_does_not_overwrite_posterior_probability_when_bucket_unchanged(self):
        rows = [
            {"outcome": "28°C", "q_raw": 0.20, "q_temperature_bucket_posterior": 0.21},
            {"outcome": "29°C", "q_raw": 0.50, "q_temperature_bucket_posterior": 0.23},
            {"outcome": "30°C", "q_raw": 0.30, "q_temperature_bucket_posterior": 0.22},
        ]
        features = {
            "observed_today_max_c": 28.0,
            "peak_estimator_mu_c": 29.0,
            "peak_estimator_sigma_c": 0.20,
            "temperature_momentum_effective_c_per_hour": 0.0,
            "temperature_momentum_posterior_gate_allowed": True,
        }
        forecast = _forecast_reference_bayes(rows, features, market_unit="C")
        observation = _observation_led_bayes(
            rows,
            features,
            {"temperature_momentum": {"effective_rate_c_per_hour": 0.0}},
            rounding_rule="whole_degrees",
            market_unit="C",
            event_date="2026-06-27",
            reference_bucket="29°C",
        )
        settlement = _settlement_lock_bayes(rows, features, rounding_rule="whole_degrees", market_unit="C")

        selected = _apply_bayes_target_priority(
            {
                "target_bucket": "29°C",
                "target_probability_pct": 23.0,
                "target_probability_field": "q_temperature_bucket_posterior",
                "posterior_highest_bucket_label": "29°C",
            },
            forecast_reference_bayes=forecast,
            observation_led_bayes=observation,
            settlement_lock_bayes=settlement,
        )

        self.assertEqual(selected["selection_priority"], "dynamic_posterior_highest_bucket")
        self.assertEqual(selected["target_bucket"], "29°C")
        self.assertEqual(selected["target_probability_pct"], 23.0)
        self.assertEqual(selected["target_probability_field"], "q_temperature_bucket_posterior")
        self.assertEqual(selected["observation_led_target_bucket"], "29°C")
        self.assertFalse(selected["target_switch_context"]["observation_led_candidate_applied"])

    def test_ssot_payload_target_selection_merge_fill_does_not_overwrite_runtime_target(self):
        computed = {
            "target_bucket": "33°C or higher",
            "target_probability_pct": 22.5,
            "target_probability_field": "q_bayes_posterior",
            "dynamic_target_source": "dynamic_posterior_highest_bucket",
            "target_row": {"outcome": "33°C or higher", "q_bayes_posterior": 0.225},
        }
        stale_payload = {
            "target_bucket": "30°C",
            "target_probability_pct": 17.6,
            "target_probability_field": "observation_led_bayes_probability",
            "dynamic_target_source": "observation_led_bayes",
            "target_bucket_selection_source": "event_fact_latest.target_bucket_selection",
        }

        merged = _merge_ssot_payload_target_selection(
            stale_payload,
            computed,
            [{"outcome": "33°C or higher", "q_bayes_posterior": 0.225}],
            source="event_fact_latest.target_bucket_selection",
        )

        self.assertEqual(merged["target_bucket"], "33°C or higher")
        self.assertEqual(merged["target_probability_pct"], 22.5)
        self.assertEqual(merged["target_probability_field"], "q_bayes_posterior")
        self.assertEqual(merged["dynamic_target_source"], "dynamic_posterior_highest_bucket")
        self.assertEqual(merged["target_bucket_selection_source"], "event_fact_latest.target_bucket_selection")

    def test_awc_metar_cloud_and_showers_adjust_observation_led_peak_path(self):
        rows = [
            {"outcome": "28°C", "q_temperature_bucket_posterior": 0.20},
            {"outcome": "29°C", "q_temperature_bucket_posterior": 0.50},
            {"outcome": "30°C", "q_temperature_bucket_posterior": 0.30},
        ]
        features = {
            "observed_temp_c": 28.0,
            "observed_today_max_c": 28.0,
            "peak_estimator_mu_c": 29.4,
            "peak_estimator_sigma_c": 0.35,
            "zuuu_metar_raw_awc_latest_raw": "METAR ZUUU 270700Z 04003MPS 9999 -SHRA BKN020 28/23 Q1005 NOSIG",
        }

        modifier = _physical_weather_modifier(features)
        observation = _observation_led_bayes(
            rows,
            features,
            {"temperature_momentum": {"effective_rate_c_per_hour": 0.2}},
            rounding_rule="whole_degrees",
            market_unit="C",
            reference_bucket="29°C",
        )

        self.assertEqual(modifier["state"], "cooling")
        self.assertLess(modifier["peak_mu_adjustment_c"], -0.5)
        self.assertTrue(modifier["components"])
        self.assertTrue(all("path_probability_scale" in item for item in modifier["components"]))
        self.assertTrue(any(item["path_probability_scale"] < 1.0 for item in modifier["components"]))
        self.assertEqual(observation["physical_weather_state"], "cooling")
        self.assertEqual(observation["base_peak_estimator_mu_c"], 29.4)
        self.assertLess(observation["peak_estimator_mu_c"], 29.0)
        self.assertEqual(observation["target_bucket"], "28°C")

    def test_taf_shra_does_not_become_observed_metar_precipitation(self):
        features = {
            "zuuu_metar_raw_awc_latest_raw": "METAR ZUUU 290200Z VRB02MPS 9999 FEW026 27/20 Q1010 NOSIG",
            "zuuu_metar_raw_awc_today_max_raw": "METAR ZUUU 290200Z VRB02MPS 9999 FEW026 27/20 Q1010 NOSIG",
            "taf_forecast_peak_raw": "TAF ZUUU 281501Z 2818/2918 22003MPS 6000 SCT050 TX29/2907Z TN22/2822Z TEMPO 2818/2821 -SHRA",
        }

        modifier = _physical_weather_modifier(features)
        component_keys = {item["key"] for item in modifier["components"]}

        self.assertNotIn("observed_precipitation", component_keys)
        self.assertIn("observed_no_precipitation", component_keys)
        self.assertFalse(modifier["metar_weather_profile"]["precip_observed"])
        self.assertTrue(modifier["metar_weather_profile"]["no_precip_confirmed"])
        self.assertEqual(modifier["metar_weather_profile"]["precip_tokens"], [])

    def test_latest_metar_no_precip_overrides_older_today_max_showers(self):
        features = {
            "zuuu_metar_raw_awc_latest_raw": "METAR ZUUU 290200Z VRB02MPS 9999 FEW026 27/20 Q1010 NOSIG",
            "zuuu_metar_raw_awc_today_max_raw": "METAR ZUUU 290000Z 04003MPS 9999 -SHRA BKN020 28/23 Q1005 NOSIG",
        }

        modifier = _physical_weather_modifier(features)
        component_keys = {item["key"] for item in modifier["components"]}

        self.assertEqual(modifier["metar_weather_profile"]["present_weather_source_fields"], ["zuuu_metar_raw_awc_latest_raw"])
        self.assertNotIn("observed_precipitation", component_keys)
        self.assertTrue(modifier["metar_weather_profile"]["no_precip_confirmed"])

    def test_metar_no_precip_clears_proxy_precip_rate_in_modifier(self):
        features = {
            "zuuu_metar_raw_awc_latest_raw": "METAR ZUUU 020300Z VRB02MPS 9999 FEW026 29/20 Q1010 NOSIG",
            "observed_precip_rate_mm_per_h": 5.8,
            "precipitation_rate_mm_per_h": 5.8,
            "is_raining": True,
            "radar_qpe_proxy_source": "open_meteo_current_hourly_precip",
        }

        modifier = _physical_weather_modifier(features)

        self.assertTrue(modifier["metar_weather_profile"]["no_precip_confirmed"])
        self.assertEqual(modifier["precipitation_profile"]["evidence_state"], "strong_no_precip")
        self.assertEqual(modifier["precipitation_profile"]["rate_mm_per_h"], 0.0)
        self.assertNotEqual(modifier["state"], "cooling")

    def test_expired_precip_nowcast_does_not_cool_physical_modifier(self):
        features = {
            "weather_likelihood_refreshed_at": "2026-07-02T09:06:00+00:00",
            "precip_nowcast_1_3h_available": True,
            "precip_nowcast_1_3h_checked_at": "2026-07-02T04:40:00+00:00",
            "precip_nowcast_1_3h_next_precip_at": "2026-07-02T07:30:00+00:00",
            "precip_nowcast_1_3h_max_rate_mm_per_h": 5.8,
            "precip_nowcast_1_3h_max_probability_pct": 85.0,
        }

        modifier = _physical_weather_modifier(features)
        nowcast = modifier["precip_nowcast_1_3h"]
        nowcast_components = [item for item in modifier["components"] if item["key"] == "precip_nowcast_1_3h"]

        self.assertEqual(nowcast["state"], "neutral")
        self.assertEqual(nowcast["risk_state"], "expired")
        self.assertEqual(nowcast["timing_na_reason"], "next_precip_at_before_reference_time")
        self.assertFalse(any(item["state"] == "cooling" for item in nowcast_components))
        self.assertNotEqual(modifier["state"], "cooling")

    def test_cavok_and_shra_same_metar_is_conflict_not_strong_cooling(self):
        features = {
            "zuuu_metar_raw_awc_latest_raw": "METAR ZUUU 290200Z VRB02MPS CAVOK -SHRA 27/20 Q1010 NOSIG",
        }

        modifier = _physical_weather_modifier(features)
        component_keys = {item["key"] for item in modifier["components"]}

        self.assertTrue(modifier["metar_weather_profile"]["present_weather_conflict"])
        self.assertFalse(modifier["metar_weather_profile"]["precip_observed"])
        self.assertFalse(modifier["metar_weather_profile"]["no_precip_confirmed"])
        self.assertNotIn("observed_precipitation", component_keys)
        self.assertNotEqual(modifier["state"], "cooling")
        self.assertEqual(modifier["precipitation_profile"]["evidence_state"], "conflict")

    def test_conflicting_metar_booleans_are_conflict_not_strong_cooling(self):
        features = {
            "metar_rain_observed": True,
            "awc_metar_no_precip_observed": True,
            "observed_precip_rate_mm_per_h": 0.1,
            "precipitation_source": "zuuu_metar_raw_awc_present_weather",
        }

        modifier = _physical_weather_modifier(features)
        component_keys = {item["key"] for item in modifier["components"]}

        self.assertNotIn("observed_precipitation", component_keys)
        self.assertEqual(modifier["precipitation_profile"]["evidence_state"], "conflict")

    def test_awc_wind_alone_is_context_not_physical_delta(self):
        features = {
            "awc_wind_dir_deg": 80.0,
            "awc_wind_speed_mps": 5.0,
            "awc_wind_variable_from_deg": 40.0,
            "awc_wind_variable_to_deg": 110.0,
        }

        modifier = _physical_weather_modifier(features)
        component_keys = {item["key"] for item in modifier["components"]}

        self.assertEqual(modifier["wind_profile"]["state"], "neutral_context")
        self.assertNotIn("wind_airmass_context", component_keys)
        self.assertFalse(modifier["wind_profile"]["override_allowed"])

    def test_awc_stable_wind_dry_clear_momentum_adds_only_aux_warming(self):
        features = {
            "zuuu_metar_raw_awc_latest_raw": "METAR ZUUU 020500Z 22003MPS CAVOK 31/18 Q1008 NOSIG",
            "awc_previous_wind_variable": True,
            "awc_previous_wind_speed_mps": 1.5,
            "awc_wind_dir_deg": 220.0,
            "awc_wind_speed_mps": 3.0,
            "cloud_cover_pct": 25.0,
            "cloud_cover_low_pct": 5.0,
            "dewpoint_c": 18.0,
            "temperature_momentum_effective_c_per_hour": 0.55,
        }

        modifier = _physical_weather_modifier(features)
        wind_components = [item for item in modifier["components"] if item["key"] == "wind_airmass_context"]

        self.assertEqual(modifier["wind_profile"]["state"], "warming_assist")
        self.assertTrue(wind_components)
        self.assertEqual(wind_components[0]["state"], "warming")
        self.assertLessEqual(wind_components[0]["peak_mu_delta_c"], 0.08)
        self.assertFalse(wind_components[0]["metrics"]["override_allowed"])
        self.assertIn("不覆盖温度轨迹", wind_components[0]["reason"])

    def test_awc_wind_with_convective_rain_and_momentum_down_adds_aux_cooling(self):
        features = {
            "zuuu_metar_raw_awc_latest_raw": "METAR ZUUU 021200Z 08005G08MPS 040V110 9999 -SHRA FEW033CB SCT033 27/24 Q1009 NOSIG",
            "awc_previous_wind_dir_deg": 180.0,
            "awc_previous_wind_speed_mps": 2.0,
            "awc_wind_dir_deg": 80.0,
            "awc_wind_speed_mps": 5.0,
            "awc_wind_gust_mps": 8.0,
            "awc_wind_variable_from_deg": 40.0,
            "awc_wind_variable_to_deg": 110.0,
            "dewpoint_c": 24.0,
            "temperature_momentum_effective_c_per_hour": -0.35,
        }

        modifier = _physical_weather_modifier(features)
        wind_components = [item for item in modifier["components"] if item["key"] == "wind_airmass_context"]

        self.assertEqual(modifier["wind_profile"]["state"], "cooling_assist")
        self.assertTrue(wind_components)
        self.assertEqual(wind_components[0]["state"], "cooling")
        self.assertGreaterEqual(wind_components[0]["peak_mu_delta_c"], -0.12)
        self.assertFalse(wind_components[0]["metrics"]["override_allowed"])
        self.assertTrue(wind_components[0]["metrics"]["convective_boundary"])

    def test_dewpoint_moisture_constraint_enters_aux_likelihood(self):
        features = {
            "zuuu_metar_raw_awc_latest_raw": "METAR ZUUU 021200Z VRB02MPS 9999 SCT020 30/25 Q1006 NOSIG",
            "observed_temp_c": 30.0,
            "dewpoint_c": 25.0,
            "cloud_cover_low_pct": 45.0,
            "temperature_momentum_effective_c_per_hour": -0.10,
        }

        modifier = _physical_weather_modifier(features)
        dew_components = [item for item in modifier["components"] if item["key"] == "dewpoint_moisture_constraint"]

        self.assertEqual(modifier["dewpoint_profile"]["state"], "moisture_constraint")
        self.assertTrue(dew_components)
        self.assertEqual(dew_components[0]["state"], "cooling")
        self.assertFalse(dew_components[0]["metrics"]["override_allowed"])
        self.assertLess(dew_components[0]["peak_mu_delta_c"], 0.0)

    def test_low_dewpoint_dry_clear_context_enters_aux_warming_likelihood(self):
        features = {
            "zuuu_metar_raw_awc_latest_raw": "METAR ZUUU 020500Z 22003MPS CAVOK 31/17 Q1013 NOSIG",
            "observed_temp_c": 31.0,
            "dewpoint_c": 17.0,
            "awc_previous_dewpoint_c": 19.0,
            "cloud_cover_pct": 20.0,
            "cloud_cover_low_pct": 5.0,
            "awc_metar_no_precip_observed": True,
            "temperature_momentum_effective_c_per_hour": 0.55,
        }

        modifier = _physical_weather_modifier(features)
        dew_components = [item for item in modifier["components"] if item["key"] == "dewpoint_dry_mixing_support"]

        self.assertEqual(modifier["dewpoint_profile"]["state"], "dry_mixing_support")
        self.assertEqual(modifier["dewpoint_profile"]["dewpoint_trend_c"], -2.0)
        self.assertTrue(dew_components)
        self.assertEqual(dew_components[0]["state"], "warming")
        self.assertGreater(dew_components[0]["peak_mu_delta_c"], 0.0)

    def test_pressure_alone_is_context_not_physical_delta(self):
        features = {
            "zuuu_metar_raw_awc_latest_raw": "METAR ZUUU 020500Z 22003MPS CAVOK 31/18 Q1014 NOSIG",
            "pressure_hpa": 1014.0,
        }

        modifier = _physical_weather_modifier(features)
        component_keys = {item["key"] for item in modifier["components"]}

        self.assertEqual(modifier["pressure_profile"]["state"], "neutral_context")
        self.assertNotIn("pressure_subsidence_context", component_keys)
        self.assertFalse(modifier["pressure_profile"]["override_allowed"])

    def test_pressure_with_clear_momentum_adds_aux_subsidence_warming(self):
        features = {
            "zuuu_metar_raw_awc_latest_raw": "METAR ZUUU 020500Z 22003MPS CAVOK 31/18 Q1014 NOSIG",
            "pressure_hpa": 1014.0,
            "awc_pressure_trend_hpa": 0.2,
            "cloud_cover_pct": 20.0,
            "cloud_cover_low_pct": 5.0,
            "awc_metar_no_precip_observed": True,
            "temperature_momentum_effective_c_per_hour": 0.55,
        }

        modifier = _physical_weather_modifier(features)
        pressure_components = [item for item in modifier["components"] if item["key"] == "pressure_subsidence_context"]

        self.assertEqual(modifier["pressure_profile"]["state"], "subsidence_warming_support")
        self.assertTrue(pressure_components)
        self.assertEqual(pressure_components[0]["state"], "warming")
        self.assertFalse(pressure_components[0]["metrics"]["override_allowed"])

    def test_pressure_falling_with_moist_convective_context_adds_aux_cooling(self):
        features = {
            "zuuu_metar_raw_awc_latest_raw": "METAR ZUUU 021200Z 08005MPS 9999 -SHRA FEW033CB 29/24 Q1005 NOSIG",
            "observed_temp_c": 29.0,
            "dewpoint_c": 24.0,
            "pressure_hpa": 1005.0,
            "awc_pressure_trend_hpa": -1.5,
            "temperature_momentum_effective_c_per_hour": -0.35,
        }

        modifier = _physical_weather_modifier(features)
        pressure_components = [item for item in modifier["components"] if item["key"] == "pressure_unsettled_context"]

        self.assertEqual(modifier["pressure_profile"]["state"], "unsettled_cooling_risk")
        self.assertTrue(pressure_components)
        self.assertEqual(pressure_components[0]["state"], "cooling")
        self.assertLess(pressure_components[0]["peak_mu_delta_c"], 0.0)

    def test_cloud_rain_modifier_scales_temperature_path_for_unreached_higher_buckets(self):
        rows = [
            {"outcome": "28°C", "q_temperature_bucket_posterior": 0.20},
            {"outcome": "29°C", "q_temperature_bucket_posterior": 0.50},
            {"outcome": "30°C", "q_temperature_bucket_posterior": 0.30},
        ]
        features = {
            "observed_temp_c": 28.0,
            "observed_today_max_c": 28.0,
            "peak_estimator_mu_c": 29.4,
            "peak_estimator_sigma_c": 0.35,
            "zuuu_metar_raw_awc_latest_raw": "METAR ZUUU 270700Z 04003MPS 9999 -SHRA BKN020 28/23 Q1005 NOSIG",
        }

        path = _temperature_path_distribution(
            rows,
            {"available": True, "target_bucket": "29°C"},
            {"available": True, "state": "available"},
            {"sample_coverage": 1.0, "confidence": 1.0, "state": "high"},
            {"observation_age_minutes": 5},
            features,
            {"observed_max_c": 28.0},
        )
        bucket_by_label = {item["bucket"]: item for item in path["bucket_distribution"]}

        self.assertEqual(path["physical_weather_state"], "cooling")
        self.assertIn("physical_weather_cooling_likelihood", path["risk_codes"])
        self.assertEqual(path["base_peak_estimator_mu_c"], 29.4)
        self.assertLess(path["peak_estimator_mu_c"], 29.0)
        self.assertFalse(bucket_by_label["28°C"]["path_probability_weather_scale_applied"])
        self.assertTrue(bucket_by_label["29°C"]["path_probability_weather_scale_applied"])
        self.assertLess(bucket_by_label["29°C"]["path_probability"], bucket_by_label["29°C"]["path_probability_raw"])

    def test_settlement_lock_priority_overrides_observation_led_target(self):
        rows = [
            {"outcome": "28°C", "q_temperature_bucket_posterior": 0.10},
            {"outcome": "29°C", "q_temperature_bucket_posterior": 0.20},
            {"outcome": "30°C", "q_temperature_bucket_posterior": 0.70},
        ]
        features = {
            "settlement_actual": 28.0,
            "settlement_actual_source": "wu_twc_final_settlement",
            "observed_today_max_c": 30.0,
            "peak_estimator_mu_c": 30.0,
            "peak_estimator_sigma_c": 0.3,
        }
        forecast = _forecast_reference_bayes(rows, features, market_unit="C")
        observation = _observation_led_bayes(
            rows,
            features,
            {},
            rounding_rule="whole_degrees",
            market_unit="C",
            reference_bucket="30°C",
        )
        settlement = _settlement_lock_bayes(rows, features, rounding_rule="whole_degrees", market_unit="C")
        selected = _apply_bayes_target_priority(
            {"target_bucket": "30°C", "posterior_highest_bucket_label": "30°C"},
            forecast_reference_bayes=forecast,
            observation_led_bayes=observation,
            settlement_lock_bayes=settlement,
        )

        self.assertTrue(settlement["locked"])
        self.assertEqual(selected["selection_priority"], "settlement_lock_bayes")
        self.assertEqual(selected["target_bucket"], "28°C")
        self.assertEqual(selected["target_probability_pct"], 100.0)

    def test_deformed_wu_intraday_settlement_actual_does_not_final_lock(self):
        features = {
            "settlement_actual": 29.0,
            "settlement_actual_source": "event_runs.settlement_actual",
            "rounding_rule": "whole_degrees",
            "source_observation_comparison": {
                "awc": {
                    "today_max_c": 28.0,
                    "raw_observations": {
                        "today_max_metar": {"temperature_raw_value": 28.0},
                    },
                },
                "wu_twc": {
                    "today_max_c": 29.0,
                    "raw_observations": {
                        "history_metric": {"raw_value": 28.0},
                        "current_metric": {"max_since7am_raw_value": 29.0},
                    },
                },
            },
            "weather_source_delay_deformation_alert": {
                "alert": True,
                "codes": ["wu_nowcast_temperature_deformation"],
            },
        }

        self.assertEqual(_hard_final_bucket_lock_reason(features), "")

    def test_warming_pace_assessment_classifies_lagging_normal_fast(self):
        base_features = {
            "observed_temp_c": 25.0,
            "peak_estimator_mu_c": 28.0,
            "observation_hour_local": 13.0,
            "expected_peak_hour_local": 16.0,
        }
        trend = {"temperature_momentum": {"effective_rate_c_per_hour": 0.4}}
        lagging = _warming_pace_assessment(base_features, trend, event_date="2026-06-21")
        self.assertTrue(lagging["available"])
        self.assertEqual(lagging["state"], "lagging")
        self.assertEqual(lagging["label"], "升温落后")
        self.assertAlmostEqual(lagging["required_rate_c_per_hour"], 1.0)

        trend["temperature_momentum"]["effective_rate_c_per_hour"] = 1.0
        normal = _warming_pace_assessment(base_features, trend, event_date="2026-06-21")
        self.assertEqual(normal["state"], "on_track")
        self.assertEqual(normal["label"], "升温正常")

        trend["temperature_momentum"]["effective_rate_c_per_hour"] = 1.6
        fast = _warming_pace_assessment(base_features, trend, event_date="2026-06-21")
        self.assertEqual(fast["state"], "fast")
        self.assertEqual(fast["label"], "升温快")

    def test_warming_pace_assessment_uses_forecast_peak_at_before_seasonal_peak_prior(self):
        features = {
            "observed_temp_c": 25.0,
            "peak_estimator_mu_c": 29.0,
            "observation_hour_local": 8.0,
            "weather_com_forecast_peak_at": "2026-06-23T13:00:00+08:00",
            "expected_peak_hour_local": 16.0,
        }
        trend = {"temperature_momentum": {"effective_rate_c_per_hour": 0.4}}

        pace = _warming_pace_assessment(features, trend, event_date="2026-06-23")

        self.assertTrue(pace["available"])
        self.assertEqual(pace["peak_hour_local"], 13.0)
        self.assertEqual(pace["peak_hour_source"], "weather_com_forecast_peak_at")
        self.assertAlmostEqual(pace["required_rate_c_per_hour"], 0.8)
        self.assertEqual(pace["state"], "lagging")

    def test_current_observation_does_not_fall_back_to_today_max_floor(self):
        features = {
            "observed_temp_c": 24.4,
            "observed_temp_source": "zuuu_metar_raw_awc",
            "observed_today_max_c": 28.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "peak_estimator_mu_c": 29.0,
            "observation_hour_local": 12.0,
            "expected_peak_hour_local": 14.0,
            "temperature_rise_rate_c_per_hour": 0.0,
            "temperature_rise_rate_window": "1h",
            "temperature_rise_rate_source": "test:1h",
        }
        trend = {"temperature_momentum": {"effective_rate_c_per_hour": 0.5}}

        guard = _high_temp_amplification_guard(features)
        pace = _warming_pace_assessment(features, trend, event_date="2026-06-24")

        self.assertEqual(guard["current_temp_c"], 24.4)
        self.assertEqual(guard["observed_current_temp_c"], 24.4)
        self.assertEqual(guard["observed_today_max_c"], 28.0)
        self.assertNotEqual(guard["current_temp_source"], "observed_today_max_floor")
        self.assertEqual(pace["observed_temp_c"], 24.4)
        self.assertNotEqual(pace["observed_temp_source"], "observed_today_max_floor")
        self.assertAlmostEqual(pace["remaining_to_peak_c"], 4.6)

    def test_future_event_ignores_date_mismatched_awc_latest_as_current(self):
        features = {
            "zuuu_metar_raw_awc_latest_temp_c": 22.0,
            "zuuu_metar_raw_awc_date_mismatch": True,
            "metar_observation_date_mismatch": True,
            "metar_observation_date_mismatch_reason": "latest_metar_local_date_not_target_event_date",
            "metar_target_event_date": "2026-06-26",
            "peak_estimator_mu_c": 29.0,
            "observation_hour_local": 7.0,
            "expected_peak_hour_local": 14.0,
            "temperature_rise_rate_c_per_hour": 0.0,
            "temperature_rise_rate_window": "1h",
            "temperature_rise_rate_source": "test:1h",
        }

        guard = _high_temp_amplification_guard(features)
        pace = _warming_pace_assessment(features, {"temperature_momentum": {"effective_rate_c_per_hour": 0.2}}, event_date="2026-06-26")

        self.assertIsNone(guard["current_temp_c"])
        self.assertEqual(guard["current_temp_source"], "missing")
        self.assertIsNone(guard["observed_current_temp_c"])
        self.assertFalse(guard["active"])
        self.assertFalse(pace["available"])
        self.assertIn("observed_temperature_missing", pace["missing_fields"])

    def test_refresh_cadence_uses_forecast_peak_at_before_expected_peak_hour_fallback(self):
        payload = {"weather_likelihood_refreshed_at": "2026-06-23T03:50:00+00:00"}
        features = {
            "observation_hour_local": 12.0,
            "weather_com_forecast_peak_at": "2026-06-23T13:00:00+08:00",
            "expected_peak_hour_local": 16.0,
            "zuuu_metar_raw_awc_latest_at": "2026-06-23T03:50:00+00:00",
            "metar_target_event_date": "2026-06-23",
        }

        cadence = _refresh_cadence(
            payload,
            features,
            event_date="2026-06-23",
            now_utc=datetime(2026, 6, 23, 4, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(cadence["peak_hour_local"], 13.0)
        self.assertEqual(cadence["peak_hour_source"], "weather_com_forecast_peak_at")
        self.assertEqual(cadence["hours_to_peak_window"], 1.0)

    def test_temperature_stage_uses_observed_max_floor_when_current_source_lags(self):
        recent = [
            {
                "_created_at": "2026-06-22T05:07:18+00:00",
                "_event_run_id": 1,
                "weather_likelihood_refreshed_at": "2026-06-22T05:07:00+00:00",
                "weather_likelihood_features": {
                    "observed_temp_c": 29.44,
                    "observed_today_max_c": 29.44,
                    "temperature_rise_rate_c_per_hour": 0.44,
                    "forecast_peak_temp": 31.0,
                },
            },
            {
                "_created_at": "2026-06-22T08:19:50+00:00",
                "_event_run_id": 2,
                "weather_likelihood_refreshed_at": "2026-06-22T08:19:50+00:00",
                "weather_likelihood_features": {
                    "observed_temp_c": 31.11,
                    "observed_today_max_c": 33.0,
                    "temperature_rise_rate_c_per_hour": -0.82,
                    "temperature_momentum_effective_c_per_hour": -6.83,
                    "temperature_momentum_realization_state": "cooling",
                    "temperature_momentum_5m_c_per_hour": -23.37,
                    "temperature_momentum_3m_c_per_hour": -23.37,
                    "forecast_peak_temp": 31.11,
                },
            },
        ]

        trend = _temperature_stages(recent)
        latest = trend["stages"][-1]
        momentum = trend["temperature_momentum"]

        self.assertEqual(latest["observed_current_temp_c"], 31.11)
        self.assertEqual(latest["observed_temp_c"], 31.11)
        self.assertEqual(latest["observed_path_temp_c"], 33.0)
        self.assertEqual(latest["observed_path_temp_source"], "observed_today_max_floor")
        self.assertNotEqual(momentum["state"], "cooling")
        self.assertNotIn("5m", momentum["rates_c_per_hour"])
        self.assertGreaterEqual(momentum["effective_rate_c_per_hour"], 0.0)

    def test_temperature_momentum_after_peak_does_not_mark_reached_bucket_blocked(self):
        trend = _temperature_stages(
            [
                {
                    "_created_at": "2026-06-22T08:34:39+00:00",
                    "weather_likelihood_refreshed_at": "2026-06-22T08:34:39+00:00",
                    "weather_likelihood_features": {
                        "observed_temp_c": 33.0,
                        "observed_today_max_c": 33.0,
                        "forecast_peak_temp": 31.11,
                        "temperature_momentum_24h_c_per_hour": 0.62,
                        "temperature_momentum_12h_c_per_hour": 0.92,
                        "temperature_momentum_6h_c_per_hour": 1.17,
                        "temperature_momentum_3h_c_per_hour": 0.67,
                        "temperature_momentum_2h_c_per_hour": 1.0,
                        "temperature_momentum_1h_c_per_hour": 1.0,
                        "temperature_momentum_45m_c_per_hour": 1.0,
                        "temperature_momentum_30m_c_per_hour": 1.0,
                        "temperature_momentum_15m_c_per_hour": -3.78,
                        "temperature_momentum_10m_c_per_hour": -3.78,
                    },
                }
            ]
        )

        momentum = trend["temperature_momentum"]
        self.assertEqual(momentum["state"], "post_peak_cooling")
        self.assertIn("不否定已达到", momentum["conclusion"])

    def test_temperature_path_distribution_reports_posterior_path_reliability_and_trade_gate(self):
        rows = [
            {
                "outcome": "30°C or below",
                "q_temperature_bucket_posterior": 0.05,
                "q_bayes_posterior": 0.05,
                "best_bid": 0.02,
                "best_ask": 0.04,
            },
            {
                "outcome": "31°C",
                "q_temperature_bucket_posterior": 0.58,
                "q_bayes_posterior": 0.58,
                "best_bid": 0.56,
                "best_ask": 0.60,
            },
            {
                "outcome": "32°C",
                "q_temperature_bucket_posterior": 0.31,
                "q_bayes_posterior": 0.31,
                "best_bid": 0.28,
                "best_ask": 0.34,
            },
        ]
        outlook = {
            "available": True,
            "target_bucket": "31°C",
            "second_target_bucket": "32°C",
        }
        trajectory = {
            "available": True,
            "state": "available",
        }
        reliability = {
            "sample_coverage": 0.80,
            "confidence": 0.74,
        }
        refresh = {
            "source_stale": False,
            "observation_age_minutes": 12.0,
            "latest_observation_time_local": "2026-06-21 14:10:00",
        }
        features = {
            "observed_temp_c": 30.6,
            "observed_today_max_c": 30.6,
            "peak_estimator_mu_c": 31.55,
            "peak_estimator_sigma_c": 0.62,
        }

        path = _temperature_path_distribution(
            rows,
            outlook,
            trajectory,
            reliability,
            refresh,
            features,
            {"observed_max_c": 30.6},
        )

        self.assertTrue(path["available"])
        self.assertEqual(path["version"], "temperature_path_distribution_v1")
        self.assertEqual(path["target_bucket"], "31°C")
        self.assertEqual(path["second_target_bucket"], "32°C")
        self.assertAlmostEqual(path["target"]["posterior"], 0.58)
        self.assertAlmostEqual(path["second_target"]["posterior"], 0.31)
        self.assertAlmostEqual(path["target_bucket_posterior"], path["target"]["posterior"])
        self.assertEqual(path["target_bucket_posterior_pct"], path["target"]["posterior_pct"])
        self.assertAlmostEqual(path["second_target_bucket_posterior"], path["second_target"]["posterior"])
        self.assertEqual(path["second_target_bucket_posterior_pct"], path["second_target"]["posterior_pct"])
        self.assertGreater(path["target"]["path_probability"], 0.70)
        self.assertGreater(path["second_target"]["path_probability"], 0.20)
        bucket_distribution = {
            item["bucket"]: item
            for item in path["bucket_distribution"]
        }
        self.assertEqual(path["bucket_distribution_count"], 3)
        self.assertAlmostEqual(bucket_distribution["30°C or below"]["posterior"], 0.05)
        self.assertEqual(bucket_distribution["30°C or below"]["path_probability"], 0.0)
        self.assertEqual(
            bucket_distribution["30°C or below"]["path_probability_source"],
            "hard_observed_max_exceeded_boundary",
        )
        self.assertAlmostEqual(bucket_distribution["31°C"]["path_probability"], path["target"]["path_probability"])
        self.assertAlmostEqual(bucket_distribution["32°C"]["path_probability"], path["second_target"]["path_probability"])
        self.assertAlmostEqual(path["target_bucket_path_probability"], path["target"]["path_probability"])
        self.assertEqual(path["target_bucket_path_probability_pct"], path["target"]["path_probability_pct"])
        self.assertAlmostEqual(path["second_target_bucket_path_probability"], path["second_target"]["path_probability"])
        self.assertEqual(path["second_target_bucket_path_probability_pct"], path["second_target"]["path_probability_pct"])
        self.assertIn(path["reliability"]["state"], {"medium", "high"})
        self.assertEqual(path["freshness"]["source_stale"], False)
        self.assertIn("目标 31°C 落桶后验", path["risk_explanation"])
        self.assertIn("路径执行概率", path["risk_explanation"])
        self.assertIn("第二目标 32°C 落桶后验", path["risk_explanation"])
        self.assertNotIn("最终后验", path["risk_explanation"])
        self.assertIn("升温窗口：仍在；硬上限：否。", path["risk_explanation"])
        self.assertIn("allowed", path["trade_follow"])
        self.assertTrue(path["path_state"]["still_in_warming_window"])
        self.assertFalse(path["path_state"]["hard_upper_bound_active"])
        self.assertEqual(path["path_state"]["hard_upper_bound_reason"], "no_hard_upper_bound_from_sources")
        self.assertEqual(path["semantics"]["posterior"], "final_settlement_bucket_probability")
        self.assertEqual(
            path["semantics"]["path_probability"],
            "probability_remaining_temperature_path_reaches_or_holds_bucket_boundary",
        )
        self.assertEqual(path["input_coverage"]["version"], "temperature_path_input_coverage_v1")
        self.assertTrue(path["input_coverage"]["realtime_hard_source"]["available"])
        self.assertTrue(path["input_coverage"]["model_forecast"]["available"])
        self.assertTrue(path["input_coverage"]["market"]["available"])
        self.assertTrue(path["input_coverage"]["path_probability"]["available"])
        self.assertEqual(path["hard_source_state"]["observed_max_c"], 30.6)

    def test_temperature_path_distribution_reports_closed_warming_window_and_hard_upper_bound(self):
        path = _temperature_path_distribution(
            [
                {"outcome": "31°C", "q_temperature_bucket_posterior": 0.58},
                {"outcome": "32°C", "q_temperature_bucket_posterior": 0.31},
            ],
            {
                "available": True,
                "target_bucket": "31°C",
                "second_target_bucket": "32°C",
            },
            {"available": True, "state": "cooling"},
            {"sample_coverage": 0.80, "confidence": 0.74},
            {"source_stale": False, "observation_age_minutes": 12.0},
            {
                "observed_today_max_c": 30.6,
                "peak_estimator_mu_c": 31.2,
                "peak_estimator_sigma_c": 0.5,
                "temperature_path_hard_upper_bound_active": True,
                "temperature_path_hard_upper_bound_c": 32.0,
                "temperature_path_hard_upper_bound_reason": "cloud_cover_and_post_peak_cooling",
            },
            {"observed_max_c": 30.6},
        )

        self.assertFalse(path["path_state"]["still_in_warming_window"])
        self.assertEqual(path["path_state"]["warming_window_reason"], "cooling")
        self.assertTrue(path["path_state"]["hard_upper_bound_active"])
        self.assertEqual(path["path_state"]["hard_upper_bound_c"], 32.0)
        self.assertEqual(
            path["path_state"]["hard_upper_bound_reason"],
            "cloud_cover_and_post_peak_cooling",
        )
        self.assertIn("升温窗口：不在；硬上限：是。", path["risk_explanation"])

    def test_temperature_path_distribution_uses_soft_forecast_when_peak_estimator_missing(self):
        rows = [
            {"outcome": "25°C or below", "q_temperature_bucket_posterior": 0.26},
            {"outcome": "26°C", "q_temperature_bucket_posterior": 0.20},
        ]
        old_env = os.environ.get("PM_TEMPERATURE_PATH_CALIBRATION_PATH")
        os.environ["PM_TEMPERATURE_PATH_CALIBRATION_PATH"] = "/tmp/weather-missing-temperature-path-calibration.json"
        _TEMPERATURE_PATH_CALIBRATION_CACHE.clear()
        try:
            path = _temperature_path_distribution(
                rows,
                {
                    "available": True,
                    "target_bucket": "25°C or below",
                    "second_target_bucket": "26°C",
                },
                {"available": False, "state": "partial", "na_reason": "observed_temperature_missing"},
                {"sample_coverage": 0.0, "confidence": 0.30},
                {"source_stale": False},
                {"forecast_peak_temp_c": 25.0},
                {},
            )
        finally:
            if old_env is None:
                os.environ.pop("PM_TEMPERATURE_PATH_CALIBRATION_PATH", None)
            else:
                os.environ["PM_TEMPERATURE_PATH_CALIBRATION_PATH"] = old_env
            _TEMPERATURE_PATH_CALIBRATION_CACHE.clear()

        self.assertTrue(path["available"])
        self.assertEqual(path["peak_estimator_mu_source"], "forecast_peak_temp_c")
        self.assertEqual(path["target"]["path_probability_source"], "soft_forecast_error_band")
        self.assertIsNotNone(path["target"]["path_probability"])
        self.assertIsNotNone(path["second_target"]["path_probability"])
        self.assertEqual(path["input_coverage"]["realtime_hard_source"]["na_reason"], "observed_temperature_missing")
        self.assertEqual(path["input_coverage"]["market"]["na_reason"], "market_probability_missing")
        self.assertEqual(path["input_coverage"]["historical_error_calibration"]["na_reason"], "no_matching_historical_error_calibration")

    def test_temperature_path_distribution_uses_weather_com_peak_when_peak_estimator_missing(self):
        rows = [
            {"outcome": "29°C", "q_temperature_bucket_posterior": 0.27},
            {"outcome": "30°C", "q_temperature_bucket_posterior": 0.13},
        ]
        old_env = os.environ.get("PM_TEMPERATURE_PATH_CALIBRATION_PATH")
        os.environ["PM_TEMPERATURE_PATH_CALIBRATION_PATH"] = "/tmp/weather-missing-temperature-path-calibration.json"
        _TEMPERATURE_PATH_CALIBRATION_CACHE.clear()
        try:
            path = _temperature_path_distribution(
                rows,
                {
                    "available": True,
                    "target_bucket": "29°C",
                    "second_target_bucket": "30°C",
                },
                {"available": True, "state": "available"},
                {"sample_coverage": 0.2, "confidence": 0.4},
                {"source_stale": False, "observation_age_minutes": 15.0},
                {
                    "weather_com_forecast_peak_c": 29.0,
                    "twc_forecast_peak_c": 29.0,
                    "observed_today_max_c": 25.0,
                    "observation_hour_local": 8.0,
                },
                {},
            )
        finally:
            if old_env is None:
                os.environ.pop("PM_TEMPERATURE_PATH_CALIBRATION_PATH", None)
            else:
                os.environ["PM_TEMPERATURE_PATH_CALIBRATION_PATH"] = old_env
            _TEMPERATURE_PATH_CALIBRATION_CACHE.clear()

        self.assertTrue(path["available"])
        self.assertEqual(path["peak_estimator_mu_source"], "weather_com_forecast_peak_c")
        self.assertEqual(path["target"]["path_probability_source"], "soft_forecast_error_band")
        self.assertIsNotNone(path["target"]["path_probability"])
        self.assertNotEqual(path["target"]["path_probability_na_reason"], "peak_estimator_mu_missing")
        self.assertEqual(path["input_coverage"]["model_forecast"]["peak_mu_source"], "weather_com_forecast_peak_c")

    def test_temperature_path_distribution_uses_historical_error_calibration(self):
        with tempfile.TemporaryDirectory() as tmp:
            cal_path = Path(tmp) / "temperature_path_calibration.json"
            cal_path.write_text(
                json.dumps(
                    {
                        "version": "temperature_path_error_calibration_v1",
                        "generated_at": "2026-06-20T00:00:00+00:00",
                        "min_sample_n": 10,
                        "default_sigma_c": 1.4,
                        "buckets": [
                            {
                                "station_id": "ZUUU",
                                "peak_mu_source": "forecast_peak_temp_c",
                                "local_hour_min": 10.0,
                                "local_hour_max": 18.0,
                                "sigma_c": 0.42,
                                "sample_n": 88,
                                "mae_c": 0.31,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            old_env = os.environ.get("PM_TEMPERATURE_PATH_CALIBRATION_PATH")
            os.environ["PM_TEMPERATURE_PATH_CALIBRATION_PATH"] = str(cal_path)
            _TEMPERATURE_PATH_CALIBRATION_CACHE.clear()
            try:
                path = _temperature_path_distribution(
                    [
                        {"outcome": "31°C", "q_temperature_bucket_posterior": 0.58},
                        {"outcome": "32°C", "q_temperature_bucket_posterior": 0.31},
                    ],
                    {
                        "available": True,
                        "target_bucket": "31°C",
                        "second_target_bucket": "32°C",
                    },
                    {"available": True, "state": "available"},
                    {"sample_coverage": 0.80, "confidence": 0.74},
                    {"source_stale": False, "observation_age_minutes": 8.0},
                    {
                        "station_id": "ZUUU",
                        "forecast_peak_temp_c": 31.55,
                        "observation_hour_local": 14.0,
                    },
                    {},
                )
            finally:
                if old_env is None:
                    os.environ.pop("PM_TEMPERATURE_PATH_CALIBRATION_PATH", None)
                else:
                    os.environ["PM_TEMPERATURE_PATH_CALIBRATION_PATH"] = old_env
                _TEMPERATURE_PATH_CALIBRATION_CACHE.clear()

        self.assertEqual(path["error_band"]["source"], "historical_error_calibration")
        self.assertEqual(path["error_band"]["sample_n"], 88)
        self.assertTrue(path["input_coverage"]["historical_error_calibration"]["available"])
        self.assertEqual(path["input_coverage"]["historical_error_calibration"]["na_reason"], "")
        self.assertEqual(path["peak_estimator_sigma_c"], 0.42)
        self.assertEqual(path["target"]["path_probability_source"], "soft_forecast_error_band")
        self.assertGreater(path["target"]["path_probability"], 0.80)

    def test_temperature_path_distribution_uses_soft_forecast_source_alias_calibration(self):
        with tempfile.TemporaryDirectory() as tmp:
            cal_path = Path(tmp) / "temperature_path_calibration.json"
            cal_path.write_text(
                json.dumps(
                    {
                        "version": "temperature_path_error_calibration_v1",
                        "generated_at": "2026-06-20T00:00:00+00:00",
                        "min_sample_n": 10,
                        "default_sigma_c": 1.4,
                        "by_peak_mu_source": {
                            "forecast_peak_temp_c": {
                                "sigma_c": 1.52,
                                "sample_n": 70,
                                "deployable_candidate": True,
                            }
                        },
                        "coverage_report": {
                            "sample_n": 550,
                            "near_close_sample_n": 340,
                            "near_close_share": 0.6182,
                            "near_close_warning": True,
                            "early_sample_n": 102,
                            "early_share": 0.1855,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            old_env = os.environ.get("PM_TEMPERATURE_PATH_CALIBRATION_PATH")
            os.environ["PM_TEMPERATURE_PATH_CALIBRATION_PATH"] = str(cal_path)
            _TEMPERATURE_PATH_CALIBRATION_CACHE.clear()
            try:
                path = _temperature_path_distribution(
                    [
                        {"outcome": "25°C or below", "q_temperature_bucket_posterior": 0.42},
                        {"outcome": "26°C", "q_temperature_bucket_posterior": 0.28},
                    ],
                    {"available": True, "target_bucket": "25°C or below", "second_target_bucket": "26°C"},
                    {"available": True, "state": "available"},
                    {"sample_coverage": 0.80, "confidence": 0.74},
                    {"source_stale": False, "observation_age_minutes": 8.0},
                    {
                        "station_id": "ZUUU",
                        "wu_forecast_peak_c": 24.44,
                        "observed_today_max_c": 22.0,
                        "observation_hour_local": 8.0,
                    },
                    {},
                )
            finally:
                if old_env is None:
                    os.environ.pop("PM_TEMPERATURE_PATH_CALIBRATION_PATH", None)
                else:
                    os.environ["PM_TEMPERATURE_PATH_CALIBRATION_PATH"] = old_env
                _TEMPERATURE_PATH_CALIBRATION_CACHE.clear()

        self.assertEqual(path["peak_estimator_mu_source"], "wu_forecast_peak_c")
        self.assertEqual(path["error_band"]["source"], "historical_error_calibration")
        self.assertEqual(path["error_band"]["sample_n"], 70)
        self.assertEqual(path["error_band"]["requested_peak_mu_source"], "wu_forecast_peak_c")
        self.assertIsNone(path["error_band"]["matched"].get("peak_mu_source"))
        self.assertTrue(path["error_band"]["calibration_coverage"]["near_close_warning"])
        self.assertIn("path_calibration_near_close_dominated", path["risk_codes"])
        self.assertIn("校准样本偏近收盘", path["risk_explanation"])
        self.assertTrue(path["input_coverage"]["historical_error_calibration"]["available"])

    def test_near_close_calibration_raises_trade_follow_lcb_in_early_warming_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            cal_path = Path(tmp) / "temperature_path_calibration.json"
            cal_path.write_text(
                json.dumps(
                    {
                        "version": "temperature_path_error_calibration_v1",
                        "generated_at": "2026-06-20T00:00:00+00:00",
                        "min_sample_n": 10,
                        "by_peak_mu_source": {
                            "forecast_peak_temp_c": {
                                "sigma_c": 1.52,
                                "sample_n": 70,
                                "deployable_candidate": True,
                            }
                        },
                        "coverage_report": {
                            "sample_n": 550,
                            "near_close_sample_n": 340,
                            "near_close_share": 0.6182,
                            "near_close_warning": True,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            old_env = os.environ.get("PM_TEMPERATURE_PATH_CALIBRATION_PATH")
            os.environ["PM_TEMPERATURE_PATH_CALIBRATION_PATH"] = str(cal_path)
            _TEMPERATURE_PATH_CALIBRATION_CACHE.clear()
            try:
                path = _temperature_path_distribution(
                    [
                        {"outcome": "25°C or below", "q_temperature_bucket_posterior": 0.42},
                        {"outcome": "26°C", "q_temperature_bucket_posterior": 0.10},
                    ],
                    {"available": True, "target_bucket": "25°C or below", "second_target_bucket": "26°C"},
                    {"available": True, "state": "available"},
                    {"sample_coverage": 0.80, "confidence": 0.74},
                    {"source_stale": False, "observation_age_minutes": 8.0},
                    {
                        "station_id": "ZUUU",
                        "wu_forecast_peak_c": 25.1,
                        "observed_today_max_c": 22.0,
                        "observation_hour_local": 8.0,
                        "expected_peak_hour_local": 15.5,
                    },
                    {},
                )
            finally:
                if old_env is None:
                    os.environ.pop("PM_TEMPERATURE_PATH_CALIBRATION_PATH", None)
                else:
                    os.environ["PM_TEMPERATURE_PATH_CALIBRATION_PATH"] = old_env
                _TEMPERATURE_PATH_CALIBRATION_CACHE.clear()

        self.assertGreaterEqual(path["target"]["path_probability_lcb"], 0.35)
        self.assertLess(path["target"]["path_probability_lcb"], 0.50)
        self.assertFalse(path["trade_follow"]["allowed"])
        self.assertEqual(path["trade_follow"]["reason"], "blocked_near_close_calibration_early_window_lcb")
        self.assertEqual(path["trade_follow"]["min_target_path_lcb"], 0.35)
        self.assertEqual(path["trade_follow"]["min_target_path_lcb_effective"], 0.50)
        self.assertTrue(path["trade_follow"]["calibration_risk_adjustment"]["applied"])
        self.assertIn("path_calibration_near_close_early_window_lcb_raised", path["risk_codes"])
        self.assertIn("target_path_lcb_below_calibration_adjusted_min", path["risk_codes"])

    def test_near_close_calibration_warning_does_not_raise_lcb_after_warming_window_develops(self):
        with tempfile.TemporaryDirectory() as tmp:
            cal_path = Path(tmp) / "temperature_path_calibration.json"
            cal_path.write_text(
                json.dumps(
                    {
                        "version": "temperature_path_error_calibration_v1",
                        "generated_at": "2026-06-20T00:00:00+00:00",
                        "min_sample_n": 10,
                        "by_peak_mu_source": {
                            "forecast_peak_temp_c": {
                                "sigma_c": 1.52,
                                "sample_n": 70,
                                "deployable_candidate": True,
                            }
                        },
                        "coverage_report": {
                            "sample_n": 550,
                            "near_close_sample_n": 340,
                            "near_close_share": 0.6182,
                            "near_close_warning": True,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            old_env = os.environ.get("PM_TEMPERATURE_PATH_CALIBRATION_PATH")
            os.environ["PM_TEMPERATURE_PATH_CALIBRATION_PATH"] = str(cal_path)
            _TEMPERATURE_PATH_CALIBRATION_CACHE.clear()
            try:
                path = _temperature_path_distribution(
                    [
                        {"outcome": "25°C or below", "q_temperature_bucket_posterior": 0.42},
                        {"outcome": "26°C", "q_temperature_bucket_posterior": 0.10},
                    ],
                    {"available": True, "target_bucket": "25°C or below", "second_target_bucket": "26°C"},
                    {"available": True, "state": "available"},
                    {"sample_coverage": 0.80, "confidence": 0.74},
                    {"source_stale": False, "observation_age_minutes": 8.0},
                    {
                        "station_id": "ZUUU",
                        "wu_forecast_peak_c": 25.1,
                        "observed_today_max_c": 22.0,
                        "observation_hour_local": 14.0,
                        "expected_peak_hour_local": 15.5,
                    },
                    {},
                )
            finally:
                if old_env is None:
                    os.environ.pop("PM_TEMPERATURE_PATH_CALIBRATION_PATH", None)
                else:
                    os.environ["PM_TEMPERATURE_PATH_CALIBRATION_PATH"] = old_env
                _TEMPERATURE_PATH_CALIBRATION_CACHE.clear()

        self.assertGreaterEqual(path["target"]["path_probability_lcb"], 0.35)
        self.assertLess(path["target"]["path_probability_lcb"], 0.50)
        self.assertTrue(path["trade_follow"]["allowed"])
        self.assertEqual(path["trade_follow"]["reason"], "posterior_path_reliability_and_freshness_pass")
        self.assertEqual(path["trade_follow"]["min_target_path_lcb_effective"], 0.35)
        self.assertFalse(path["trade_follow"]["calibration_risk_adjustment"]["applied"])
        self.assertIn("path_calibration_near_close_dominated", path["risk_codes"])
        self.assertNotIn("path_calibration_near_close_early_window_lcb_raised", path["risk_codes"])

    def test_temperature_path_distribution_skips_non_deployable_bucket_calibration(self):
        with tempfile.TemporaryDirectory() as tmp:
            cal_path = Path(tmp) / "temperature_path_calibration.json"
            cal_path.write_text(
                json.dumps(
                    {
                        "version": "temperature_path_error_calibration_v1",
                        "generated_at": "2026-06-20T00:00:00+00:00",
                        "min_sample_n": 10,
                        "min_deployable_sigma_c": 0.35,
                        "default_sigma_c": 1.25,
                        "buckets": [
                            {
                                "station_id": "ZUUU",
                                "peak_mu_source": "peak_estimator_mu_c",
                                "local_hour_min": 14.0,
                                "local_hour_max": 18.0,
                                "sigma_c": 0.16,
                                "sample_n": 143,
                                "deployable_candidate": False,
                                "deploy_block_reason": "sigma_below_conservative_floor",
                            }
                        ],
                        "by_peak_mu_source": {
                            "peak_estimator_mu_c": {
                                "sigma_c": 0.86,
                                "sample_n": 227,
                                "deployable_candidate": True,
                            }
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            old_env = os.environ.get("PM_TEMPERATURE_PATH_CALIBRATION_PATH")
            os.environ["PM_TEMPERATURE_PATH_CALIBRATION_PATH"] = str(cal_path)
            _TEMPERATURE_PATH_CALIBRATION_CACHE.clear()
            try:
                path = _temperature_path_distribution(
                    [
                        {"outcome": "31°C", "q_temperature_bucket_posterior": 0.58},
                        {"outcome": "32°C", "q_temperature_bucket_posterior": 0.31},
                    ],
                    {"available": True, "target_bucket": "31°C", "second_target_bucket": "32°C"},
                    {"available": True, "state": "available"},
                    {"sample_coverage": 0.80, "confidence": 0.74},
                    {"source_stale": False, "observation_age_minutes": 8.0},
                    {
                        "station_id": "ZUUU",
                        "peak_estimator_mu_c": 31.55,
                        "observation_hour_local": 14.0,
                    },
                    {},
                )
            finally:
                if old_env is None:
                    os.environ.pop("PM_TEMPERATURE_PATH_CALIBRATION_PATH", None)
                else:
                    os.environ["PM_TEMPERATURE_PATH_CALIBRATION_PATH"] = old_env
                _TEMPERATURE_PATH_CALIBRATION_CACHE.clear()

        self.assertEqual(path["error_band"]["source"], "historical_error_calibration")
        self.assertEqual(path["error_band"]["match_scope"], "by_peak_mu_source")
        self.assertEqual(path["peak_estimator_sigma_c"], 0.86)
        self.assertEqual(
            path["error_band"]["skipped_calibration_entries"][0]["reason"],
            "sigma_below_conservative_floor",
        )
        self.assertIn("path_calibration_entry_skipped", path["risk_codes"])
        self.assertIn("已跳过不可部署校准", path["risk_explanation"])

    def test_temperature_path_distribution_falls_back_when_no_deployable_calibration(self):
        with tempfile.TemporaryDirectory() as tmp:
            cal_path = Path(tmp) / "temperature_path_calibration.json"
            cal_path.write_text(
                json.dumps(
                    {
                        "version": "temperature_path_error_calibration_v1",
                        "generated_at": "2026-06-20T00:00:00+00:00",
                        "min_sample_n": 10,
                        "min_deployable_sigma_c": 0.35,
                        "default_sigma_c": 0.16,
                        "buckets": [
                            {
                                "station_id": "ZUUU",
                                "peak_mu_source": "peak_estimator_mu_c",
                                "local_hour_min": 14.0,
                                "local_hour_max": 18.0,
                                "sigma_c": 0.16,
                                "sample_n": 143,
                                "deployable_candidate": False,
                            }
                        ],
                        "by_peak_mu_source": {
                            "peak_estimator_mu_c": {
                                "sigma_c": 0.12,
                                "sample_n": 157,
                                "deployable_candidate": False,
                            }
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            old_env = os.environ.get("PM_TEMPERATURE_PATH_CALIBRATION_PATH")
            os.environ["PM_TEMPERATURE_PATH_CALIBRATION_PATH"] = str(cal_path)
            _TEMPERATURE_PATH_CALIBRATION_CACHE.clear()
            try:
                path = _temperature_path_distribution(
                    [{"outcome": "31°C", "q_temperature_bucket_posterior": 0.58}],
                    {"available": True, "target_bucket": "31°C"},
                    {"available": True, "state": "available"},
                    {"sample_coverage": 0.80, "confidence": 0.74},
                    {"source_stale": False, "observation_age_minutes": 8.0},
                    {
                        "station_id": "ZUUU",
                        "peak_estimator_mu_c": 31.55,
                        "observation_hour_local": 14.0,
                    },
                    {},
                )
            finally:
                if old_env is None:
                    os.environ.pop("PM_TEMPERATURE_PATH_CALIBRATION_PATH", None)
                else:
                    os.environ["PM_TEMPERATURE_PATH_CALIBRATION_PATH"] = old_env
                _TEMPERATURE_PATH_CALIBRATION_CACHE.clear()

        self.assertEqual(path["error_band"]["source"], "default_error_band")
        self.assertEqual(path["error_band"]["fallback_reason"], "no_deployable_historical_error_calibration")
        self.assertFalse(path["input_coverage"]["historical_error_calibration"]["available"])
        self.assertEqual(
            path["input_coverage"]["historical_error_calibration"]["na_reason"],
            "no_deployable_historical_error_calibration",
        )
        self.assertEqual(path["peak_estimator_sigma_c"], 1.25)
        self.assertGreaterEqual(len(path["error_band"]["skipped_calibration_entries"]), 2)
        self.assertIn("path_calibration_fallback_no_deployable_entry", path["risk_codes"])
        self.assertIn("回退原因：no_deployable_historical_error_calibration", path["risk_explanation"])

    def test_decision_trade_plan_uses_dynamic_target_selection_not_observed_bucket(self):
        rows = [
            {"outcome": "28°C", "q_temperature_bucket_posterior": 0.0, "q_bayes_posterior": 0.0},
            {"outcome": "30°C", "q_temperature_bucket_posterior": 0.6828, "q_bayes_posterior": 0.6828},
            {"outcome": "31°C", "q_temperature_bucket_posterior": 0.2964, "q_bayes_posterior": 0.2964},
            {"outcome": "29°C", "q_temperature_bucket_posterior": 0.0202, "q_bayes_posterior": 0.0202},
        ]
        target_selection = {
            "target_bucket": "30°C",
            "target_probability_pct": 68.28,
            "target_row": dict(rows[1]),
            "watch_buckets": [dict(rows[1]), dict(rows[2]), dict(rows[3])],
        }
        reliability = {
            "state": "warming",
            "target_bucket": "30°C",
            "warming_label": "升温支持目标桶兑现",
            "cooling_label": "受阻/降温压制目标桶",
            "warming_realization_pct": 75.4,
            "cooling_realization_pct": 9.2,
            "neutral_or_uncertain_pct": 15.4,
            "confidence_pct": 71.4,
            "realization_mode": "warming_needed",
        }
        evidence = [
            {
                "key": "temperature_trajectory",
                "state": "warming",
                "metrics": {
                    "observed_temp_c": 28.33,
                    "observed_today_max_c": 28.33,
                    "rise_rate_c_per_hour": 0.70,
                    "peak_estimator_mu_c": 30.40,
                },
            },
            {"key": "temperature_momentum", "state": "warming"},
        ]

        decision = _decision_text(
            evidence,
            [dict(rows[0]), dict(rows[1]), dict(rows[2])],
            rows,
            high_temp_guard={"active": False},
            temperature_trend={
                "latest_trend_state": "strengthening",
                "temperature_momentum": {
                    "state": "sustained_warming",
                    "rates_c_per_hour": {"1h": 1.58, "30m": 0.70},
                },
            },
            realization_reliability=reliability,
            target_selection=target_selection,
        )

        trade_plan = decision["target_realization_trade_plan"]
        self.assertEqual(decision["target_bucket"], "30°C")
        self.assertEqual(trade_plan["target_bucket"], "30°C")
        self.assertEqual(trade_plan["buckets"][0]["outcome"], "30°C")
        self.assertEqual(decision["watch_buckets"][:3], ["30°C", "31°C", "29°C"])

    def test_decision_trade_plan_emits_real_bilateral_position_legs(self):
        rows = [
            {"outcome": "28°C", "q_temperature_bucket_posterior": 0.08, "q_bayes_posterior": 0.08},
            {"outcome": "30°C", "q_temperature_bucket_posterior": 0.62, "q_bayes_posterior": 0.62},
            {"outcome": "31°C", "q_temperature_bucket_posterior": 0.20, "q_bayes_posterior": 0.20},
        ]
        target_selection = {
            "target_bucket": "30°C",
            "target_probability_pct": 62.0,
            "target_row": dict(rows[1]),
            "watch_buckets": [dict(rows[1]), dict(rows[2]), dict(rows[0])],
        }
        reliability = {
            "state": "warming",
            "target_bucket": "30°C",
            "warming_realization_pct": 71.0,
            "cooling_realization_pct": 8.0,
            "neutral_or_uncertain_pct": 21.0,
            "confidence_pct": 70.0,
        }
        decision = _decision_text(
            [{"key": "temperature_momentum", "state": "warming"}],
            [dict(rows[1]), dict(rows[2]), dict(rows[0])],
            rows,
            high_temp_guard={"active": False},
            temperature_trend={
                "temperature_momentum": {
                    "state": "sustained_warming",
                    "rates_c_per_hour": {"1h": 1.2, "30m": 1.0},
                },
            },
            realization_reliability=reliability,
            target_selection=target_selection,
            active_position_legs=[
                {
                    "outcome": "28°C",
                    "side": "YES",
                    "position_size": 12.5,
                    "avg_price": 0.41,
                    "source": "edge_trades",
                }
            ],
        )

        plan = decision["target_realization_trade_plan"]
        self.assertTrue(plan["position_legs_resolved"])
        self.assertEqual(plan["sell_old_leg"]["outcome"], "28°C")
        self.assertEqual(plan["sell_old_leg"]["side"], "YES")
        self.assertEqual(plan["sell_old_leg"]["target_realization_execution_action"], "SELL")
        self.assertEqual(plan["buy_new_leg"]["outcome"], "30°C")
        self.assertEqual(plan["buy_new_leg"]["side"], "YES")
        self.assertEqual(plan["buy_new_leg"]["target_realization_execution_action"], "BUY")
        self.assertEqual(plan["buy_old_reverse_leg"]["outcome"], "28°C")
        self.assertEqual(plan["buy_old_reverse_leg"]["side"], "NO")
        self.assertEqual(plan["buy_old_reverse_leg"]["paired_exit_outcome"], "28°C")
        self.assertEqual(plan["buy_old_reverse_leg"]["paired_exit_side"], "YES")

    def test_target_bucket_outlook_splits_final_bucket_from_lower_edge_reach(self):
        rows = [
            {"outcome": "28°C", "q_temperature_bucket_posterior": 0.0},
            {"outcome": "29°C", "q_temperature_bucket_posterior": 0.0202},
            {"outcome": "30°C", "q_temperature_bucket_posterior": 0.6828},
            {"outcome": "31°C", "q_temperature_bucket_posterior": 0.2964},
        ]
        target_selection = {
            "target_bucket": "30°C",
            "watch_buckets": [dict(rows[2]), dict(rows[3]), dict(rows[1])],
        }
        reliability = {
            "target_bucket_warming_realization_pct": 75.4,
            "target_bucket_blocked_realization_pct": 9.2,
            "neutral_or_uncertain_pct": 15.4,
        }

        outlook = _target_bucket_outlook(
            rows,
            target_selection,
            reliability,
            {"observed_max_c": 28.33},
            {
                "observed_temp_c": 28.33,
                "observed_today_max_c": 28.33,
                "peak_estimator_mu_c": 30.40,
                "peak_estimator_sigma_c": 0.63,
                "temperature_rise_rate_c_per_hour": 0.70,
            },
        )

        self.assertTrue(outlook["available"])
        self.assertEqual(outlook["version"], "target_bucket_outlook_v2")
        self.assertEqual(outlook["final_bucket"], "30°C")
        self.assertEqual(outlook["target_bucket"], "30°C")
        self.assertEqual(outlook["second_target_bucket"], "31°C")
        self.assertEqual(outlook["current_state"], "below_target")
        self.assertEqual(outlook["final_relation_to_target"], "target")
        self.assertEqual(outlook["dominant_relation"], "target")
        self.assertEqual(outlook["target_reach_state"], "likely")
        self.assertIn("当前后验最可能落在 30°C", outlook["primary_conclusion"])
        self.assertIn("目标桶同为 30°C", outlook["primary_conclusion"])
        self.assertNotIn("路径触达", outlook["primary_conclusion"])
        self.assertEqual(outlook["target_reach_label"], "路径触达较顺")
        self.assertIn("仅作路径/执行参考", outlook["reach_line"])
        self.assertIn("未结算后验集中在目标桶", outlook["settlement_line"])
        self.assertEqual([item["key"] for item in outlook["probability_bands"]], ["below_target", "target", "above_target"])
        self.assertEqual([item["key"] for item in outlook["answer_cards"]], ["final_bucket", "target_reach", "target_gap"])
        self.assertAlmostEqual(outlook["gap_to_target_lower_edge_c"], 1.67)
        self.assertAlmostEqual(outlook["target_final_probability_pct"], 68.28)
        self.assertAlmostEqual(outlook["below_target_probability_pct"], 2.02)
        self.assertAlmostEqual(outlook["above_target_probability_pct"], 29.64)
        self.assertAlmostEqual(outlook["target_reach_lower_edge_probability_pct"], 75.4)
        self.assertIn("不等同于最终结算在目标桶", outlook["definition"])
        second = outlook["second_target_outlook"]
        self.assertTrue(second["available"])
        self.assertEqual(second["version"], "second_target_outlook_v1")
        self.assertEqual(second["second_target_bucket"], "31°C")
        self.assertEqual(second["target_bucket"], "30°C")
        self.assertEqual(second["candidate_relation_to_target"], "above_target")
        self.assertEqual(second["state"], "unreached")
        self.assertIn("不覆盖当前目标 30°C", second["primary_conclusion"])
        self.assertIn("若最终超过 30°C", second["candidate_line"])
        self.assertIn("还差 2.67°C", second["reach_line"])
        self.assertEqual([item["key"] for item in second["answer_cards"]], ["candidate_bucket", "breakout_probability", "candidate_gap"])
        self.assertEqual([item["key"] for item in second["probability_bands"]], ["below_second_target", "second_target", "above_second_target"])
        self.assertAlmostEqual(second["candidate_probability_pct"], 29.64)
        self.assertAlmostEqual(second["second_or_higher_probability_pct"], 29.64)
        self.assertAlmostEqual(second["below_second_target_probability_pct"], 70.3)
        self.assertAlmostEqual(second["second_target_final_probability_pct"], 29.64)
        self.assertAlmostEqual(second["above_second_target_probability_pct"], 0.0)
        self.assertAlmostEqual(second["gap_to_second_target_lower_edge_c"], 2.67)

    def test_target_bucket_outlook_does_not_mark_top_target_as_hard_when_path_lags(self):
        rows = [
            {"outcome": "28°C", "q_temperature_bucket_posterior": 0.2600},
            {"outcome": "29°C", "q_temperature_bucket_posterior": 0.2518},
            {"outcome": "30°C", "q_temperature_bucket_posterior": 0.2776},
            {"outcome": "31°C", "q_temperature_bucket_posterior": 0.1547},
            {"outcome": "32°C", "q_temperature_bucket_posterior": 0.0559},
        ]
        outlook = _target_bucket_outlook(
            rows,
            {"target_bucket": "30°C", "watch_buckets": [dict(rows[2]), dict(rows[3]), dict(rows[1])]},
            {"target_bucket_warming_realization_pct": 24.5},
            {"observed_max_c": None},
            {
                "observed_temp_c": None,
                "observed_today_max_c": None,
                "peak_estimator_mu_c": 29.1,
                "peak_estimator_sigma_c": 1.25,
            },
        )

        self.assertTrue(outlook["available"])
        self.assertEqual(outlook["final_bucket"], "30°C")
        self.assertEqual(outlook["target_bucket"], "30°C")
        self.assertEqual(outlook["final_relation_to_target"], "target")
        self.assertEqual(outlook["dominant_relation"], "below_target")
        self.assertEqual(outlook["target_reach_state"], "unlikely")
        self.assertEqual(outlook["target_reach_label"], "路径触达偏弱")
        self.assertIn("当前后验最可能落在 30°C", outlook["primary_conclusion"])
        self.assertIn("目标桶同为 30°C", outlook["primary_conclusion"])
        self.assertNotIn("偏难", outlook["primary_conclusion"])
        self.assertNotIn("路径触达偏弱", outlook["primary_conclusion"])
        self.assertIn("仅作路径/执行参考", outlook["reach_line"])
        self.assertIn("未结算后验偏低", outlook["settlement_line"])

    def test_target_bucket_outlook_reach_uses_canonical_temperature_path_distribution(self):
        outlook = {
            "available": True,
            "target_bucket": "30°C",
            "target_reach_state": "possible",
            "target_reach_label": "路径仍有机会",
            "reach_line": "轨迹估计达到 30°C 下沿概率 62.3%；仅作路径/执行参考，不改写最高温落桶后验。",
            "target_reach_lower_edge_probability_pct": 62.33,
            "answer_cards": [
                {"key": "final_bucket", "label": "当前后验最可能", "answer": "30°C", "detail": "后验 43.1%。"},
                {
                    "key": "target_reach",
                    "label": "路径触达参考",
                    "answer": "路径仍有机会",
                    "detail": "轨迹估计达到 30°C 下沿概率 62.3%；仅作路径/执行参考，不改写最高温落桶后验。",
                    "tone": "warn",
                },
            ],
        }
        path_distribution = {
            "available": True,
            "target_bucket": "30°C",
            "target": {
                "bucket": "30°C",
                "path_probability_pct": 0.64,
                "path_probability_lcb_pct": 0.0,
                "path_probability_source": "peak_estimator_error_band",
                "path_probability_mode": "reach_at_or_above_lower_edge",
            },
            "trade_follow": {"allowed": False, "reason": "blocked_target_path_lcb_low"},
        }

        synced = _sync_target_bucket_outlook_with_temperature_path_distribution(outlook, path_distribution)

        self.assertAlmostEqual(synced["target_reach_lower_edge_probability_pct"], 0.64)
        self.assertEqual(synced["target_reach_label"], "路径触达偏弱")
        self.assertEqual(synced["target_reach_state"], "unlikely")
        self.assertIn("0.6%", synced["reach_line"])
        self.assertIn("LCB 0.0%", synced["reach_line"])
        self.assertNotIn("62.3%", synced["reach_line"])
        reach_card = next(card for card in synced["answer_cards"] if card["key"] == "target_reach")
        self.assertEqual(reach_card["answer"], "路径触达偏弱")
        self.assertEqual(reach_card["tone"], "bad")
        self.assertIn("0.6%", reach_card["detail"])
        self.assertFalse(synced["temperature_path_trade_follow_allowed"])
        self.assertEqual(synced["temperature_path_trade_follow_reason"], "blocked_target_path_lcb_low")

    def test_target_bucket_outlook_hard_awc_bucket_constrains_posterior_floor(self):
        rows = [
            {"outcome": "29°C", "q_temperature_bucket_posterior": 0.0202},
            {"outcome": "30°C", "q_temperature_bucket_posterior": 0.6828},
            {"outcome": "31°C", "q_temperature_bucket_posterior": 0.2964},
        ]
        outlook = _target_bucket_outlook(
            rows,
            {"target_bucket": "30°C", "watch_buckets": [dict(rows[1]), dict(rows[2]), dict(rows[0])]},
            {
                "target_bucket_warming_realization_pct": 75.4,
                "target_bucket_blocked_realization_pct": 9.2,
                "neutral_or_uncertain_pct": 15.4,
            },
            {"observed_max_c": 31.0},
            {
                "observed_temp_c": 30.5,
                "observed_today_max_c": 31.0,
                "zuuu_metar_raw_awc_today_max_c": 31.0,
                "zuuu_metar_raw_awc_today_max_at": "2026-06-18T07:00:00+00:00",
                "zuuu_metar_raw_awc_fetched_at": "2026-06-18T07:00:01+00:00",
                "zuuu_metar_raw_awc_fetch_duration_ms": 84,
                "peak_estimator_mu_c": 30.4,
                "peak_estimator_sigma_c": 0.63,
            },
            "",
            "C",
        )

        self.assertTrue(outlook["available"])
        self.assertEqual(outlook["final_bucket"], "31°C")
        self.assertEqual(outlook["target_bucket"], "31°C")
        self.assertEqual(outlook["input_target_bucket"], "30°C")
        self.assertEqual(outlook["target_bucket_source"], "hard_source_constrained_posterior_highest_bucket")
        self.assertEqual(outlook["final_bucket_source"], "hard_source_constrained_posterior")
        self.assertEqual(outlook["pre_constraint_posterior_final_bucket"], "30°C")
        self.assertEqual(outlook["posterior_final_bucket"], "31°C")
        self.assertEqual(outlook["final_relation_to_target"], "target")
        self.assertEqual(outlook["hard_source_direct"]["source_id"], "zuuu_metar_raw_awc")
        self.assertEqual(outlook["hard_source_direct"]["bucket"], "31°C")
        self.assertTrue(outlook["hard_source_direct"]["posterior_floor_constraint_applied"])
        self.assertIn("硬源已触达 31°C 下沿", outlook["primary_conclusion"])
        self.assertIn("约束前后验最高 30°C", outlook["answer_cards"][0]["detail"])
        self.assertAlmostEqual(outlook["final_bucket_probability_pct"], 100.0)
        self.assertAlmostEqual(outlook["target_final_probability_pct"], 100.0)
        second = outlook["second_target_outlook"]
        self.assertFalse(second["available"])
        self.assertEqual(second["reason"], "second_target_missing")

    def test_target_bucket_outlook_low_boundary_hard_source_does_not_override_final(self):
        rows = [
            {"outcome": "29°C or below", "q_temperature_bucket_posterior": 0.10, "is_cumulative_bucket": True},
            {"outcome": "30°C", "q_temperature_bucket_posterior": 0.20},
            {"outcome": "31°C", "q_temperature_bucket_posterior": 0.25},
            {"outcome": "32°C", "q_temperature_bucket_posterior": 0.30},
            {"outcome": "33°C", "q_temperature_bucket_posterior": 0.15},
        ]
        outlook = _target_bucket_outlook(
            rows,
            {"target_bucket": "32°C", "watch_buckets": [dict(rows[3]), dict(rows[2]), dict(rows[4])]},
            {},
            {"observed_max_c": 27.2},
            {
                "observed_temp_c": 27.2,
                "observed_today_max_c": 27.2,
                "zuuu_metar_raw_awc_today_max_c": 27.2,
                "zuuu_metar_raw_awc_today_max_at": "2026-06-19T02:00:00+00:00",
                "zuuu_metar_raw_awc_fetched_at": "2026-06-19T02:00:01+00:00",
                "zuuu_metar_raw_awc_fetch_duration_ms": 52,
            },
            "",
            "C",
        )

        self.assertTrue(outlook["available"])
        self.assertEqual(outlook["hard_source_direct"]["bucket"], "29°C or below")
        self.assertFalse(outlook["hard_source_direct"]["posterior_floor_constraint_applied"])
        self.assertEqual(outlook["final_bucket"], "32°C")
        self.assertEqual(outlook["target_bucket"], "32°C")
        self.assertNotEqual(outlook["final_bucket"], "29°C or below")
        self.assertEqual(outlook["second_target_outlook"]["second_target_bucket"], "33°C")

    def test_target_bucket_outlook_promotes_highest_boundary_bucket_as_final_and_target(self):
        rows = [
            {"outcome": "29°C or below", "q_temperature_bucket_posterior": 0.46},
            {"outcome": "32°C", "q_temperature_bucket_posterior": 0.31},
            {"outcome": "33°C", "q_temperature_bucket_posterior": 0.23},
        ]

        outlook = _target_bucket_outlook(
            rows,
            {"target_bucket": "29°C or below", "watch_buckets": [dict(rows[0]), dict(rows[1]), dict(rows[2])]},
            {},
            {"observed_max_c": 27.22},
            {"observed_temp_c": 27.22, "observed_today_max_c": 27.22},
        )

        self.assertTrue(outlook["available"])
        self.assertEqual(outlook["cumulative_posterior_final_bucket"], "29°C or below")
        self.assertEqual(outlook["final_bucket"], "29°C or below")
        self.assertEqual(outlook["target_bucket"], "29°C or below")
        self.assertEqual(outlook["target_bucket_source"], "posterior_highest_bucket")
        self.assertIn("29°C or below", outlook["primary_conclusion"])
        self.assertAlmostEqual(outlook["final_bucket_probability_pct"], 46.0)
        self.assertAlmostEqual(outlook["target_final_probability_pct"], 46.0)
        self.assertAlmostEqual(outlook["above_target_probability_pct"], 54.0)
        self.assertEqual(outlook["second_target_bucket"], "32°C")
        self.assertEqual(outlook["second_target_outlook"]["second_target_bucket"], "32°C")
        self.assertEqual(outlook["second_target_outlook"]["candidate_relation_to_target"], "above_target")

    def test_target_bucket_outlook_lower_boundary_uses_hold_breakout_wording(self):
        rows = [
            {"outcome": "25°C or below", "q_temperature_bucket_posterior": 0.2612},
            {"outcome": "26°C", "q_temperature_bucket_posterior": 0.2517},
            {"outcome": "27°C", "q_temperature_bucket_posterior": 0.2500},
            {"outcome": "28°C", "q_temperature_bucket_posterior": 0.2371},
        ]
        lower_outlook = _target_bucket_outlook(
            rows,
            {"target_bucket": "25°C or below", "watch_buckets": [dict(row) for row in rows]},
            {
                "target_bucket_warming_realization_pct": 26.1,
                "target_bucket_blocked_realization_pct": 73.9,
            },
            {},
            {
                "peak_estimator_mu_c": 26.2,
                "peak_estimator_sigma_c": 1.1,
            },
            rounding_rule="whole_degrees",
            market_unit="C",
        )

        self.assertEqual(lower_outlook["target_bucket"], "25°C or below")
        self.assertEqual(lower_outlook["target_reach_label"], "低边界突破风险高")
        self.assertEqual(lower_outlook["answer_cards"][1]["label"], "能否守住低边界")
        self.assertEqual(lower_outlook["probability_bands"][1]["label"], "守住低边界")
        self.assertEqual(lower_outlook["probability_bands"][2]["label"], "突破低边界")
        self.assertIn("最终超过 25°C or below", lower_outlook["probability_bands"][2]["meaning"])
        self.assertIn("低边界突破风险", lower_outlook["settlement_line"])
        self.assertNotIn("达标偏难", lower_outlook["primary_conclusion"])
        self.assertAlmostEqual(lower_outlook["above_target_probability_pct"], 73.88)

    def test_distribution_rows_keep_fact_gated_posterior_as_final_outlook_ssot(self):
        payload = {
            "rows": [
                {
                    "outcome": "30°C",
                    "market_closed": False,
                    "q_bayes_posterior": 0.0,
                    "q_bayes_posterior_pre_observed_constraints": 0.3614,
                    "q_temperature_bucket_posterior": 0.0,
                    "q_temperature_bucket_posterior_pre_observed_constraints": 0.4096,
                    "q_posterior_observed_constraints_applied": True,
                    "q_posterior_observed_constraints_hard_zero": True,
                    "q_posterior_observed_constraints_reason_codes": [
                        "observed_today_max_above_bucket"
                    ],
                },
                {
                    "outcome": "31°C",
                    "market_closed": False,
                    "q_raw": 0.242,
                    "q_ensemble_qm_bucket": 0.388,
                    "q_bayes_prior_strength": 12.0,
                    "q_temperature_bucket_alpha": 13.5,
                    "q_temperature_bucket_alpha_total": 45.0,
                    "q_temperature_bucket_score_components": {
                        "history": 6.0,
                        "model": 3.0,
                        "wu_forecast": 2.4,
                        "taf_peak": 1.2,
                        "temperature_trajectory": 1.6,
                        "independent_physics_residual": 0.9,
                    },
                    "q_bayes_model_evidence": 10.0,
                    "q_bayes_wu_forecast_evidence": 6.0,
                    "q_bayes_wu_forecast_prob": 0.4,
                    "q_bayes_taf_peak_evidence": 4.0,
                    "q_bayes_taf_peak_prob": 0.3,
                    "q_bayes_temperature_trajectory_evidence": 4.0,
                    "q_bayes_temperature_trajectory_prob": 0.4,
                    "q_bayes_independent_physics_residual_evidence": 1.0,
                    "q_bayes_independent_physics_residual_prob": 0.9,
                    "q_bayes_posterior": 0.840558,
                    "q_bayes_posterior_pre_observed_constraints": 0.28175,
                    "q_temperature_bucket_posterior": 0.840558,
                    "q_temperature_bucket_posterior_pre_observed_constraints": 0.28175,
                    "q_posterior_observed_constraints_applied": True,
                    "q_posterior_observed_constraints_hard_zero": False,
                },
                {
                    "outcome": "32°C",
                    "market_closed": False,
                    "q_bayes_posterior": 0.142653,
                    "q_bayes_posterior_pre_observed_constraints": 0.047816,
                    "q_temperature_bucket_posterior": 0.142653,
                    "q_temperature_bucket_posterior_pre_observed_constraints": 0.047816,
                    "q_posterior_observed_constraints_applied": True,
                    "q_posterior_observed_constraints_hard_zero": False,
                },
            ]
        }

        rows = _distribution_rows(payload)
        by_bucket = {row["outcome"]: row for row in rows}
        self.assertEqual(by_bucket["30°C"]["q_temperature_bucket_posterior"], 0.0)
        self.assertAlmostEqual(
            by_bucket["30°C"]["q_temperature_bucket_posterior_pre_observed_constraints"],
            0.4096,
        )
        self.assertEqual(by_bucket["30°C"]["q_bayes_posterior"], 0.0)
        self.assertTrue(by_bucket["30°C"]["q_posterior_observed_constraints_hard_zero"])
        self.assertEqual(by_bucket["31°C"]["q_temperature_bucket_posterior"], 0.840558)
        self.assertEqual(by_bucket["31°C"]["q_raw"], 0.242)
        self.assertEqual(by_bucket["31°C"]["q_ensemble_qm_bucket"], 0.388)
        chain = by_bucket["31°C"]["bayes_chain"]
        self.assertAlmostEqual(chain["prior_prob"], 0.5)
        self.assertAlmostEqual(chain["likelihood_prob"], 9.1 / 25.0)
        self.assertAlmostEqual(
            chain["likelihood_parts"]["wu_forecast"]["prob"],
            0.4,
        )
        self.assertAlmostEqual(
            chain["likelihood_parts"]["temperature_trajectory"]["prob"],
            0.4,
        )
        self.assertEqual(by_bucket["31°C"]["q_prior"], chain["prior_prob"])
        self.assertEqual(by_bucket["31°C"]["q_likelihood"], chain["likelihood_prob"])

        outlook = _target_bucket_outlook(
            rows,
            {"target_bucket": "30°C", "watch_buckets": [by_bucket["30°C"], by_bucket["31°C"]]},
            {},
            {"observed_max_c": 31.0},
            {"observed_temp_c": 28.89, "observed_today_max_c": 31.0},
        )

        self.assertTrue(outlook["available"])
        self.assertEqual(outlook["final_bucket"], "31°C")
        self.assertEqual(outlook["target_bucket"], "31°C")
        self.assertEqual(outlook["input_target_bucket"], "30°C")
        self.assertEqual(outlook["final_relation_to_target"], "target")
        self.assertAlmostEqual(outlook["target_final_probability_pct"], 84.06)
        self.assertAlmostEqual(outlook["above_target_probability_pct"], 14.27)
        self.assertEqual(outlook["second_target_outlook"]["second_target_bucket"], "32°C")
        self.assertAlmostEqual(
            outlook["second_target_outlook"]["second_or_higher_probability_pct"],
            14.27,
        )

    def test_distribution_rows_prefer_hierarchical_layer_contributions_chain(self):
        payload = {
            "rows": [
                {
                    "outcome": "30°C",
                    "q_temperature_bucket_posterior": 0.20,
                    "q_bayes_meta": {
                        "source": "hierarchical_posterior_pipeline_runtime_v1",
                        "phase": "d0_awc_preparation_window",
                        "beta_weak": 0.4,
                        "beta_strong": 1.2,
                        "phase_reason": "unit_test_phase",
                        "hard_mask_reason": "none",
                        "score_components": {
                            "prior": -1.20,
                            "forecast_weak_likelihood": -0.10,
                            "metar_strong_likelihood": -0.30,
                        },
                        "layer_contributions": {
                            "prior": {
                                "role": "root_prior",
                                "boundary": "historical_climate_date_site_terrain_weather_type_only",
                                "distribution": {"30°C": 0.45, "31°C": 0.55},
                            },
                            "forecast_weak_likelihood": {
                                "role": "prediction_weak_likelihood",
                                "boundary": "models_taf_wu_twc_forecasts_only_no_hard_facts",
                                "beta": 0.4,
                                "distribution": {"30°C": 0.35, "31°C": 0.65},
                            },
                            "metar_strong_likelihood": {
                                "role": "observation_strong_likelihood",
                                "boundary": "metar_temperature_momentum_physics_cloud_rain_state_once_no_double_count",
                                "beta": 1.2,
                                "distribution": {"30°C": 0.20, "31°C": 0.80},
                            },
                        },
                    },
                },
                {
                    "outcome": "31°C",
                    "q_temperature_bucket_posterior": 0.80,
                    "q_bayes_meta": {
                        "source": "hierarchical_posterior_pipeline_runtime_v1",
                        "phase": "d0_awc_preparation_window",
                        "beta_weak": 0.4,
                        "beta_strong": 1.2,
                        "phase_reason": "unit_test_phase",
                        "hard_mask_reason": "none",
                        "score_components": {
                            "prior": -0.90,
                            "forecast_weak_likelihood": -0.05,
                            "metar_strong_likelihood": -0.15,
                        },
                        "layer_contributions": {
                            "prior": {
                                "role": "root_prior",
                                "boundary": "historical_climate_date_site_terrain_weather_type_only",
                                "distribution": {"30°C": 0.45, "31°C": 0.55},
                            },
                            "forecast_weak_likelihood": {
                                "role": "prediction_weak_likelihood",
                                "boundary": "models_taf_wu_twc_forecasts_only_no_hard_facts",
                                "beta": 0.4,
                                "distribution": {"30°C": 0.35, "31°C": 0.65},
                            },
                            "metar_strong_likelihood": {
                                "role": "observation_strong_likelihood",
                                "boundary": "metar_temperature_momentum_physics_cloud_rain_state_once_no_double_count",
                                "beta": 1.2,
                                "distribution": {"30°C": 0.20, "31°C": 0.80},
                            },
                        },
                    },
                    "q_bayes_wu_forecast_evidence": 99.0,
                    "q_bayes_wu_forecast_prob": 0.01,
                },
            ]
        }

        rows = _distribution_rows(payload)
        by_bucket = {row["outcome"]: row for row in rows}
        chain = by_bucket["31°C"]["bayes_chain"]

        self.assertEqual(chain["schema"], "hierarchical_layer_contributions_chain_v1")
        self.assertEqual(chain["role"], "hierarchical_joint_likelihood_explanation")
        self.assertFalse(chain["old_additive_evidence_pool_used"])
        self.assertFalse(chain["legacy_compat_fallback_used"])
        self.assertAlmostEqual(chain["prior_prob"], 0.55)
        self.assertAlmostEqual(chain["likelihood_parts"]["forecast_weak_likelihood"]["prob"], 0.65)
        self.assertAlmostEqual(chain["likelihood_parts"]["metar_strong_likelihood"]["prob"], 0.80)
        self.assertAlmostEqual(chain["beta_weak"], 0.4)
        self.assertAlmostEqual(chain["beta_strong"], 1.2)
        self.assertEqual(chain["phase"], "d0_awc_preparation_window")
        self.assertGreater(chain["likelihood_prob"], 0.5)
        self.assertNotIn("wu_forecast", chain["likelihood_parts"])
        self.assertEqual(by_bucket["31°C"]["q_prior"], chain["prior_prob"])
        self.assertEqual(by_bucket["31°C"]["q_likelihood"], chain["likelihood_prob"])

    def test_keeps_cumulative_bucket_out_of_single_top(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "archive.db"
            conn = _make_db(db)
            payload = {
                "weather_likelihood_refreshed_at": "2026-06-13T05:40:07+00:00",
                "weather_likelihood_feature_quality": "complete",
                "weather_likelihood_features": {
                    "observed_temp_c": 27.78,
                    "observed_today_max_c": 27.78,
                    "temperature_rise_rate_c_per_hour": -1.0,
                    "peak_estimator_mu_c": 29.39,
                    "peak_estimator_sigma_c": 0.81,
                    "forecast_peak_temp": 32.1,
                    "settlement_source_forecast_peak_c": 32.2,
                    "taf_forecast_peak_c": 32.0,
                    "cloud_cover_pct": 18.7,
                    "cloud_cover_low_pct": 0.0,
                    "solar_radiation": 500.0,
                    "dewpoint_c": 18.0,
                    "awc_metar_no_precip_observed": True,
                    "is_raining": True,
                    "is_raining_observed": False,
                    "observed_precip_rate_mm_per_h": 0.0,
                },
                "weather_likelihood_feature_groups": {
                    "precipitation_evaporative_cooling": {
                        "evidence_state": "strong_no_precip",
                        "source_profile": {
                            "evidence_state": "strong_no_precip",
                            "source": "zuuu_metar_raw_awc_cavok_no_precip",
                            "source_class": "metar",
                        },
                    }
                },
                "rows": [
                    {
                        "outcome": "30°C or below",
                        "q_calibrated": 0.20,
                        "q_bayes_posterior": 0.25,
                        "q_bayes_physical_likelihood_enabled": False,
                        "q_bayes_physical_likelihood_evidence": 0.0,
                    },
                    {
                        "outcome": "32°C",
                        "q_calibrated": 0.18,
                        "q_bayes_posterior": 0.24,
                        "q_bayes_physical_likelihood_enabled": False,
                        "q_bayes_physical_likelihood_evidence": 0.0,
                    },
                    {
                        "outcome": "31°C",
                        "q_calibrated": 0.15,
                        "q_bayes_posterior": 0.14,
                        "q_bayes_physical_likelihood_enabled": False,
                        "q_bayes_physical_likelihood_evidence": 0.0,
                    },
                ],
            }
            prior_payload = json.loads(json.dumps(payload))
            prior_payload["weather_likelihood_refreshed_at"] = "2026-06-13T04:31:17+00:00"
            prior_payload["weather_likelihood_features"]["temperature_rise_rate_c_per_hour"] = 2.0
            prior_payload["weather_likelihood_features"]["peak_estimator_mu_c"] = 33.26
            conn.execute(
                """
                INSERT INTO event_runs(run_date, event_date, slug, title, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-06-13",
                    "2026-06-13",
                    "highest-temperature-in-chengdu-on-june-13-2026",
                    "Highest temperature in Chengdu on June 13?",
                    json.dumps(prior_payload),
                    "2026-06-13T04:44:16+00:00",
                ),
            )
            conn.execute(
                """
                INSERT INTO event_runs(run_date, event_date, slug, title, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-06-13",
                    "2026-06-13",
                    "highest-temperature-in-chengdu-on-june-13-2026",
                    "Highest temperature in Chengdu on June 13?",
                    json.dumps(payload),
                    "2026-06-13T05:44:16+00:00",
                ),
            )
            conn.commit()
            conn.close()

            summary = build_chengdu_intraday_summary(db, event_date="2026-06-13")

        self.assertIs(summary["ok"], True)
        self.assertEqual(summary["top_calibrated"]["outcome"], "30°C or below")
        self.assertEqual(summary["top_single_temperature"]["outcome"], "32°C")
        self.assertEqual(summary["target_bucket"], "30°C or below")
        self.assertEqual(summary["target_bucket_source"], "dynamic_posterior_highest_bucket")
        self.assertEqual(summary["target_bucket_selection"]["target_bucket_type"], "cumulative_boundary")
        self.assertEqual(summary["target_bucket_selection"]["version"], "dynamic_daily_target_bucket_selection_v4")
        self.assertEqual(summary["target_bucket_selection"]["selection_priority"], "dynamic_posterior_highest_bucket")
        self.assertEqual(summary["target_bucket_selection"]["dynamic_target_state"], "posterior_highest_bucket")
        self.assertFalse(summary["target_bucket_selection"]["differs_from_posterior_highest"])
        self.assertEqual(summary["target_bucket_selection"]["single_temperature_reference_bucket"]["outcome"], "32°C")
        self.assertEqual(summary["decision"]["stance"], "30°C or below 优先观察")
        self.assertIn("目标桶先看 30°C or below", summary["decision"]["action"])
        self.assertIn("31°C+ 暂不追", summary["decision"]["action"])
        self.assertIn("实况升温落后软预报", summary["decision"]["summary"])
        self.assertNotIn("即使晴空/辐射有利", summary["decision"]["summary"])
        self.assertNotIn("不得因晴空/辐射", summary["decision"]["action"])
        self.assertTrue(summary["high_temp_amplification_guard"]["active"])
        self.assertEqual(summary["temperature_trend"]["latest_trend_state"], "weakening")
        self.assertIn("temperature_momentum", summary["temperature_trend"])
        momentum = next(item for item in summary["evidence_conclusions"] if item["key"] == "temperature_momentum")
        self.assertEqual(momentum["evidence_class"], "observed_realization")
        self.assertIn(momentum["metrics"]["realization_state"], {"flat", "not_realized", "cooling", "mixed"})
        self.assertEqual(summary["refresh_cadence"]["recommended_interval_sec"], 600)
        precip = next(item for item in summary["evidence_conclusions"] if item["key"] == "radar_precip_echo")
        self.assertEqual(precip["state"], "warming")
        self.assertEqual(precip["metrics"]["evidence_state"], "strong_no_precip")
        line = build_log_line(summary)
        self.assertIn("target=30°C or below", line)
        self.assertIn("trend=weakening", line)
        self.assertIn("momentum_state=", line)
        self.assertIn("guard_active=1", line)

    def test_dynamic_target_promotes_early_d0_low_boundary_to_forecast_path_bucket(self):
        rows = [
            {
                "outcome": "24°C or below",
                "q_temperature_bucket_posterior": 0.2181,
                "q_bayes_posterior": 0.2181,
                "is_cumulative_bucket": True,
            },
            {
                "outcome": "27°C",
                "q_temperature_bucket_posterior": 0.2001,
                "q_bayes_posterior": 0.2001,
                "is_cumulative_bucket": False,
            },
            {
                "outcome": "26°C",
                "q_temperature_bucket_posterior": 0.1721,
                "q_bayes_posterior": 0.1721,
                "is_cumulative_bucket": False,
            },
            {
                "outcome": "28°C",
                "q_temperature_bucket_posterior": 0.1357,
                "q_bayes_posterior": 0.1357,
                "is_cumulative_bucket": False,
            },
        ]
        top_all = [dict(rows[0]), dict(rows[1]), dict(rows[2]), dict(rows[3])]
        top_single = [dict(rows[1]), dict(rows[2]), dict(rows[3])]
        features = {
            "weather_likelihood_refreshed_at": "2026-06-23T23:42:30+00:00",
            "observed_today_max_c": 24.0,
            "observed_temp_c": 23.33,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "weather_com_forecast_peak_c": 27.0,
            "weather_com_forecast_peak_at": "2026-06-24T16:00:00+0800",
            "peak_estimator_sigma_c": 0.85,
        }

        selection = _dynamic_target_bucket_selection(
            rows,
            top_all,
            top_single,
            features,
            "whole_degrees",
            "C",
            temperature_trend={},
            event_date="2026-06-24",
        )

        self.assertEqual(selection["posterior_highest_bucket_label"], "24°C or below")
        self.assertEqual(selection["target_bucket"], "24°C or below")
        self.assertEqual(selection["dynamic_target_source"], "dynamic_posterior_highest_bucket")
        self.assertEqual(selection["dynamic_target_state"], "posterior_highest_bucket")
        self.assertFalse(selection["differs_from_posterior_highest"])
        self.assertEqual(selection["trajectory_candidate_source"], "temperature_path_breakout_target")
        self.assertFalse(selection["trajectory_candidate_applied_to_target"])
        self.assertEqual(selection["trajectory_candidate"]["outcome"], "27°C")
        self.assertEqual(
            selection["forecast_path_breakout_context"]["held_target_bucket"],
            "24°C or below",
        )

    def test_d0_refresh_cadence_uses_current_time_when_metar_source_is_stale(self):
        payload = {
            "weather_likelihood_refreshed_at": "2026-06-14T06:50:06+00:00",
        }
        features = {
            "expected_peak_hour_local": 16.0,
            "observation_hour_local": 12.0,
            "zuuu_metar_raw_awc_latest_at": "2026-06-14T04:00:00+00:00",
            "metar_stale": True,
            "metar_target_event_date": "2026-06-14",
        }

        cadence = _refresh_cadence(
            payload,
            features,
            event_date="2026-06-14",
            now_utc=datetime(2026, 6, 14, 7, 15, tzinfo=timezone.utc),
        )

        self.assertEqual(cadence["cadence_basis"], "current_local_time")
        self.assertEqual(cadence["recommended_interval_sec"], 120)
        self.assertAlmostEqual(cadence["hours_to_peak_window"], 0.75)
        self.assertEqual(cadence["time_precision"], "seconds")
        self.assertEqual(cadence["reference_time_local"], "2026-06-14 15:15:00")
        self.assertEqual(cadence["latest_observation_time_local"], "2026-06-14 12:00:00")
        self.assertEqual(cadence["latest_observation_time_utc"], "2026-06-14T04:00:00+00:00")
        self.assertGreater(cadence["observation_age_minutes"], 180)
        self.assertTrue(cadence["source_stale"])

    def test_market_top_bucket_uses_latest_ticks_and_reports_incomplete_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "archive.db"
            conn = _make_db(db)
            conn.execute(
                """
                CREATE TABLE market_tokens (
                    slug TEXT,
                    event_date TEXT,
                    outcome TEXT,
                    yes_token TEXT,
                    no_token TEXT,
                    market_active INTEGER,
                    market_closed INTEGER,
                    market_archived INTEGER,
                    updated_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE event_market_ticks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_date TEXT,
                    event_date TEXT,
                    slug TEXT,
                    outcome TEXT,
                    side TEXT,
                    best_bid REAL,
                    best_ask REAL,
                    mid REAL,
                    spread REAL,
                    source TEXT,
                    source_detail TEXT,
                    created_at TEXT
                )
                """
            )
            slug = "highest-temperature-in-chengdu-on-june-17-2026"
            outcomes = ["24°C or below", "25°C", "26°C", "27°C"]
            for outcome in outcomes:
                conn.execute(
                    """
                    INSERT INTO market_tokens(
                        slug, event_date, outcome, yes_token, no_token,
                        market_active, market_closed, market_archived, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 1, 0, 0, ?)
                    """,
                    (slug, "2026-06-17", outcome, f"yes-{outcome}", f"no-{outcome}", "2026-06-15T16:31:30+00:00"),
                )
            conn.execute(
                """
                INSERT INTO event_market_ticks(
                    run_date, event_date, slug, outcome, side,
                    best_bid, best_ask, mid, spread, source, source_detail, created_at
                ) VALUES (?, ?, ?, ?, 'YES', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-06-15",
                    "2026-06-17",
                    slug,
                    "24°C or below",
                    0.28,
                    0.34,
                    0.31,
                    0.06,
                    "clob_rest_refresh",
                    "test",
                    "2026-06-15T16:40:54+00:00",
                ),
            )
            payload = {
                "weather_likelihood_refreshed_at": "2026-06-15T16:40:00+00:00",
                "weather_likelihood_features": {},
                "rows": [
                    {"outcome": "24°C or below", "q_temperature_bucket_posterior": 0.12, "best_bid": 0.01, "best_ask": 0.02},
                    {"outcome": "25°C", "q_temperature_bucket_posterior": 0.15, "best_bid": 0.18, "best_ask": 0.22},
                    {"outcome": "26°C", "q_temperature_bucket_posterior": 0.18, "best_bid": 0.19, "best_ask": 0.21},
                    {"outcome": "27°C", "q_temperature_bucket_posterior": 0.23, "best_bid": 0.08, "best_ask": 0.14},
                ],
            }
            conn.execute(
                """
                INSERT INTO event_runs(run_date, event_date, slug, title, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-06-15",
                    "2026-06-17",
                    slug,
                    "Highest temperature in Chengdu on June 17?",
                    json.dumps(payload),
                    "2026-06-15T16:41:00+00:00",
                ),
            )
            conn.commit()
            conn.close()

            summary = build_chengdu_intraday_summary(db, event_date="2026-06-17")

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["target_bucket"], "27°C")
        self.assertEqual(summary["market_top_bucket"]["outcome"], "24°C or below")
        self.assertTrue(summary["target_market_divergence"])
        self.assertFalse(summary["market_tick_coverage"]["is_complete"])
        self.assertTrue(summary["market_tick_coverage"]["needs_full_rest_snapshot"])
        self.assertEqual(summary["market_tick_coverage"]["covered_outcomes"], 1)
        self.assertEqual(summary["market_tick_coverage"]["expected_outcomes"], 4)
        ticked = next(row for row in summary["bucket_probabilities"] if row["outcome"] == "24°C or below")
        self.assertTrue(ticked["market_tick_overlay_applied"])
        self.assertEqual(ticked["best_bid"], 0.28)
        self.assertEqual(ticked["best_ask"], 0.34)

    def test_fact_latest_summary_does_not_overlay_market_ticks_over_ssot_distribution(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "archive.db"
            conn = _make_db(db)
            conn.row_factory = sqlite3.Row
            conn.execute(
                """
                CREATE TABLE event_fact_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_date TEXT,
                    event_date TEXT,
                    slug TEXT,
                    fact_as_of TEXT,
                    fact_json TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE event_fact_latest (
                    slug TEXT,
                    event_date TEXT,
                    fact_id INTEGER
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE event_market_ticks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_date TEXT,
                    event_date TEXT,
                    slug TEXT,
                    outcome TEXT,
                    side TEXT,
                    best_bid REAL,
                    best_ask REAL,
                    mid REAL,
                    spread REAL,
                    source TEXT,
                    source_detail TEXT,
                    created_at TEXT
                )
                """
            )
            slug = "highest-temperature-in-chengdu-on-june-24-2026"
            fact = {
                "title": "Highest temperature in Chengdu on June 24?",
                "weather_likelihood_refreshed_at": "2026-06-24T03:00:00+00:00",
                "weather_likelihood_features": {},
                "target_bucket": "26°C",
                "target_bucket_selection": {
                    "target_bucket": "26°C",
                    "target_probability_pct": 30.0,
                    "dynamic_target_state": "temperature_trajectory_switch",
                    "dynamic_target_source": "temperature_trajectory_switch_pressure",
                },
                "posterior_target_bucket_selection": {
                    "target_bucket": "26°C",
                    "target_probability_pct": 30.0,
                },
                "distribution": [
                    {"outcome": "26°C", "q_temperature_bucket_posterior": 0.30, "best_bid": 0.21, "best_ask": 0.25},
                    {"outcome": "27°C", "q_temperature_bucket_posterior": 0.40, "best_bid": 0.10, "best_ask": 0.12},
                    {"outcome": "28°C", "q_temperature_bucket_posterior": 0.20, "best_bid": 0.06, "best_ask": 0.08},
                ],
            }
            cur = conn.execute(
                """
                INSERT INTO event_fact_history(run_date, event_date, slug, fact_as_of, fact_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "2026-06-24",
                    "2026-06-24",
                    slug,
                    "2026-06-24T03:00:00+00:00",
                    json.dumps(fact),
                ),
            )
            fact_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO event_fact_latest(slug, event_date, fact_id) VALUES (?, ?, ?)",
                (slug, "2026-06-24", fact_id),
            )
            conn.execute(
                """
                INSERT INTO event_market_ticks(
                    run_date, event_date, slug, outcome, side,
                    best_bid, best_ask, mid, spread, source, source_detail, created_at
                ) VALUES (?, ?, ?, ?, 'YES', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-06-24",
                    "2026-06-24",
                    slug,
                    "27°C",
                    0.90,
                    0.94,
                    0.92,
                    0.04,
                    "clob_rest_refresh",
                    "newer_tick_must_not_override_fact_latest",
                    "2026-06-24T03:01:00+00:00",
                ),
            )
            conn.commit()
            conn.close()

            summary = build_chengdu_intraday_summary(db, event_date="2026-06-24")

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["source"], "event_fact_latest.payload_json")
        self.assertEqual(summary["temperature_distribution"]["ssot"], "event_fact_latest.payload_json.rows")
        self.assertEqual(summary["target_bucket"], "26°C")
        self.assertEqual(summary["posterior_highest_bucket_label"], "26°C")
        self.assertEqual(
            summary["target_bucket_selection"]["ssot_source"],
            "event_fact_latest.target_bucket_selection",
        )
        self.assertFalse(summary["market_tick_coverage"]["overlay_applied"])
        self.assertEqual(
            summary["market_tick_coverage"]["overlay_skipped_reason"],
            "event_fact_latest_distribution_is_ssot",
        )
        row_27 = next(row for row in summary["bucket_probabilities"] if row["outcome"] == "27°C")
        self.assertNotIn("market_tick_overlay_applied", row_27)
        self.assertEqual(row_27["best_bid"], 0.10)
        self.assertEqual(row_27["best_ask"], 0.12)

    def test_forecast_only_snapshot_reports_temperature_trajectory_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "archive.db"
            conn = _make_db(db)
            payload = {
                "weather_likelihood_refreshed_at": "2026-06-16T14:20:00+00:00",
                "weather_likelihood_feature_quality": "partial",
                "weather_likelihood_features": {
                    "forecast_peak_temp": 25.16,
                    "weather_com_forecast_peak_c": 25.0,
                    "twc_forecast_peak_c": 25.0,
                    "taf_forecast_peak_c": 23.0,
                    "temperature_trajectory_na_reason": "observed_temperature_missing",
                },
                "rows": [
                    {"outcome": "24°C or below", "q_temperature_bucket_posterior": 0.3491},
                    {"outcome": "25°C", "q_temperature_bucket_posterior": 0.2132},
                    {"outcome": "26°C", "q_temperature_bucket_posterior": 0.1539},
                ],
            }
            conn.execute(
                """
                INSERT INTO event_runs(run_date, event_date, slug, title, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-06-16",
                    "2026-06-17",
                    "highest-temperature-in-chengdu-on-june-17-2026",
                    "Highest temperature in Chengdu on June 17?",
                    json.dumps(payload),
                    "2026-06-16T14:21:00+00:00",
                ),
            )
            conn.commit()
            conn.close()

            summary = build_chengdu_intraday_summary(db, event_date="2026-06-17")

        self.assertTrue(summary["ok"])
        trajectory = summary["temperature_trajectory_status"]
        self.assertFalse(trajectory["available"])
        self.assertEqual(trajectory["state"], "partial")
        self.assertEqual(trajectory["label"], "数据不全")
        self.assertIn("observed_temperature_missing", trajectory["missing_fields"])
        self.assertIn("temperature_momentum_or_rise_rate_missing", trajectory["missing_fields"])
        self.assertEqual(trajectory["forecast_peak_temp_c"], 25.16)
        self.assertFalse(summary["temperature_trend"]["available"])
        self.assertEqual(summary["temperature_trend"]["stages"], [])
        self.assertFalse(summary["key_metrics"]["temperature_trajectory_available"])
        self.assertEqual(summary["key_metrics"]["temperature_trajectory_state"], "partial")
        self.assertIn(
            "observed_temperature_missing",
            summary["key_metrics"]["temperature_trajectory_missing_fields"],
        )

    def test_regime_v1_status_fills_missing_line_momentum(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "archive.db"
            conn = _make_db(db)
            payload = {
                "weather_likelihood_refreshed_at": "2026-07-02T03:06:49+00:00",
                "weather_likelihood_features": {
                    "observed_temp_c": 29.0,
                    "observed_today_max_c": 29.0,
                    "forecast_peak_temp": 32.0,
                    "peak_estimator": {
                        "enabled": True,
                        "observed_temperature_trajectory_regime_v1": {
                            "available": True,
                            "path_mu_c": 32.75,
                            "path_sigma_c": 0.77,
                            "effective_evidence": 4.25,
                            "rate_hat_c_per_hour": 2.24,
                            "accel_hat_c_per_hour2": 0.12,
                        },
                    },
                },
                "rows": [
                    {"outcome": "31°C", "q_temperature_bucket_posterior": 0.45},
                    {"outcome": "32°C", "q_temperature_bucket_posterior": 0.40},
                ],
            }
            conn.execute(
                """
                INSERT INTO event_runs(run_date, event_date, slug, title, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-07-02",
                    "2026-07-02",
                    "highest-temperature-in-chengdu-on-july-2-2026",
                    "Highest temperature in Chengdu on July 2?",
                    json.dumps(payload),
                    "2026-07-02T03:07:00+00:00",
                ),
            )
            conn.commit()
            conn.close()

            summary = build_chengdu_intraday_summary(db, event_date="2026-07-02")

        trajectory = summary["temperature_trajectory_status"]
        self.assertTrue(trajectory["available"])
        self.assertTrue(trajectory["trajectory_plumbing_mismatch"])
        self.assertIn("trajectory_plumbing_mismatch", trajectory["diagnostic_flags"])
        self.assertEqual(trajectory["temperature_trajectory_path_mu_c"], 32.75)
        self.assertEqual(trajectory["temperature_trajectory_rate_hat_c_per_hour"], 2.24)
        self.assertNotIn("temperature_momentum_or_rise_rate_missing", trajectory["missing_fields"])

    def test_untrusted_temperature_rise_spike_is_not_displayed_or_guarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "archive.db"
            conn = _make_db(db)
            payload = {
                "weather_likelihood_refreshed_at": "2026-06-19T02:17:00+00:00",
                "weather_likelihood_features": {
                    "observed_temp_c": 27.22,
                    "observed_today_max_c": 28.0,
                    "forecast_peak_temp": 33.0,
                    "temperature_rise_rate_c_per_hour": -14.74,
                    "temperature_rise_rate_raw_pair_c_per_hour": -14.74,
                    "temperature_rise_rate_raw_pair_actual_hours": 2.0 / 60.0,
                    "peak_estimator_mu_c": 28.89,
                    "peak_estimator_sigma_c": 0.30,
                },
                "rows": [
                    {"outcome": "33°C", "q_temperature_bucket_posterior": 0.43},
                    {"outcome": "32°C", "q_temperature_bucket_posterior": 0.31},
                    {"outcome": "31°C", "q_temperature_bucket_posterior": 0.26},
                ],
            }
            conn.execute(
                """
                INSERT INTO event_runs(run_date, event_date, slug, title, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-06-19",
                    "2026-06-19",
                    "highest-temperature-in-chengdu-on-june-19-2026",
                    "Highest temperature in Chengdu on June 19?",
                    json.dumps(payload),
                    "2026-06-19T02:17:05+00:00",
                ),
            )
            conn.commit()
            conn.close()

            summary = build_chengdu_intraday_summary(db, event_date="2026-06-19")

        self.assertTrue(summary["ok"])
        self.assertIsNone(summary["key_metrics"]["temperature_rise_rate_c_per_hour"])
        self.assertFalse(summary["high_temp_amplification_guard"]["active"])
        trajectory = summary["temperature_trajectory_status"]
        self.assertIsNone(trajectory["temperature_rise_rate_c_per_hour"])
        self.assertFalse(trajectory["temperature_rise_rate_trusted"])
        self.assertIn("unstable_temperature_rise_rate_suppressed", trajectory["missing_fields"])

    def test_d0_refresh_cadence_uses_seconds_inside_peak_window(self):
        payload = {
            "weather_likelihood_refreshed_at": "2026-06-14T07:59:06+00:00",
        }
        features = {
            "expected_peak_hour_local": 16.0,
            "zuuu_metar_raw_awc_latest_at": "2026-06-14T08:00:05+00:00",
            "metar_target_event_date": "2026-06-14",
        }

        cadence = _refresh_cadence(
            payload,
            features,
            event_date="2026-06-14",
            now_utc=datetime(2026, 6, 14, 8, 0, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(cadence["recommended_interval_sec"], 1)
        self.assertEqual(cadence["label"], "1s")
        self.assertEqual(cadence["reference_time_local"], "2026-06-14 16:00:30")
        self.assertEqual(cadence["latest_observation_time_local"], "2026-06-14 16:00:05")

    def test_observed_max_guards_impossible_low_cumulative_bucket(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "archive.db"
            conn = _make_db(db)
            payload = {
                "weather_likelihood_refreshed_at": "2026-06-13T07:49:28+00:00",
                "weather_likelihood_feature_quality": "complete",
                "weather_likelihood_features": {
                    "observed_temp_c": 31.0,
                    "observed_today_max_c": 31.0,
                    "temperature_rise_rate_c_per_hour": 3.0,
                    "forecast_peak_temp": 32.1,
                    "settlement_source_forecast_peak_c": 31.67,
                    "taf_forecast_peak_c": 32.0,
                },
                "rows": [
                    {
                        "outcome": "30°C or below",
                        "q_calibrated": 0.30,
                        "q_bayes_posterior": 0.40,
                        "q_temperature_bucket_posterior": 0.60,
                    },
                    {
                        "outcome": "32°C",
                        "q_calibrated": 0.20,
                        "q_bayes_posterior": 0.25,
                        "q_temperature_bucket_posterior": 0.20,
                    },
                ],
            }
            conn.execute(
                """
                INSERT INTO event_runs(run_date, event_date, slug, title, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-06-13",
                    "2026-06-13",
                    "highest-temperature-in-chengdu-on-june-13-2026",
                    "Highest temperature in Chengdu on June 13?",
                    json.dumps(payload),
                    "2026-06-13T07:49:30+00:00",
                ),
            )
            conn.commit()
            conn.close()

            summary = build_chengdu_intraday_summary(db, event_date="2026-06-13")

        low_bucket = summary["bucket_probabilities"][0]
        self.assertEqual(low_bucket["outcome"], "30°C or below")
        self.assertTrue(low_bucket["impossible_by_observed_max"])
        self.assertEqual(low_bucket["q_calibrated"], 0.0)
        self.assertEqual(low_bucket["q_calibrated_raw"], 0.30)
        self.assertEqual(low_bucket["q_bayes_posterior"], 0.0)
        self.assertEqual(low_bucket["q_bayes_posterior_raw"], 0.40)
        self.assertEqual(low_bucket["q_temperature_bucket_posterior"], 0.0)
        self.assertEqual(low_bucket["q_temperature_bucket_posterior_raw"], 0.60)
        self.assertEqual(summary["top_calibrated"]["outcome"], "32°C")
        self.assertEqual(summary["target_bucket"], "32°C")
        self.assertEqual(summary["target_bucket_selection"]["target_bucket_type"], "single_temperature")

    def test_d0_observed_feasibility_suppresses_high_tail_floor(self):
        rows = [
            {
                "outcome": "31°C",
                "q_calibrated": 0.13,
                "q_calibrated_pct": 13.0,
                "q_bayes_posterior": 0.16,
                "q_bayes_posterior_pct": 16.0,
                "q_physical_observed_prob": 0.98,
                "is_cumulative_bucket": False,
            },
            {
                "outcome": "32°C",
                "q_calibrated": 0.13,
                "q_calibrated_pct": 13.0,
                "q_bayes_posterior": 0.16,
                "q_bayes_posterior_pct": 16.0,
                "q_physical_observed_prob": 0.02,
                "is_cumulative_bucket": False,
            },
            {
                "outcome": "34°C",
                "q_calibrated": 0.10,
                "q_calibrated_pct": 10.0,
                "q_bayes_posterior": 0.05,
                "q_bayes_posterior_pct": 5.0,
                "q_physical_observed_prob": 1e-16,
                "is_cumulative_bucket": False,
            },
        ]
        features = {
            "observed_temp_c": 31.0,
            "observed_today_max_c": 31.0,
            "observed_temp_source": "zuuu_metar_raw_awc",
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "observed_today_max_final": True,
        }

        guarded = _apply_d0_observed_feasibility_guard(
            rows,
            features,
            "2026-06-13",
            today_key="2026-06-13",
        )

        by_outcome = {row["outcome"]: row for row in guarded}
        self.assertTrue(by_outcome["34°C"]["observed_feasibility_applied"])
        self.assertTrue(by_outcome["34°C"]["suppressed_by_observed_feasibility"])
        self.assertEqual(by_outcome["34°C"]["q_calibrated_pct"], 0.0)
        self.assertEqual(by_outcome["34°C"]["q_calibrated_pct_raw"], 10.0)
        self.assertGreater(by_outcome["31°C"]["q_calibrated_pct"], 97.0)
        self.assertLess(by_outcome["32°C"]["q_calibrated_pct"], 3.0)

    def test_d0_observed_feasibility_skips_intraday_metar_lower_bound(self):
        rows = [
            {
                "outcome": "24°C or below",
                "q_bayes_posterior": 0.1611,
                "q_temperature_bucket_posterior": 0.1611,
                "q_physical_observed_prob": 0.251444,
            },
            {
                "outcome": "27°C",
                "q_bayes_posterior": 0.1931,
                "q_temperature_bucket_posterior": 0.1931,
                "q_physical_observed_prob": 0.141551,
            },
        ]
        features = {
            "observed_temp_c": 23.0,
            "observed_today_max_c": 24.0,
            "observed_temp_source": "zuuu_metar_raw_awc",
            "observed_today_max_source": "zuuu_metar_raw_awc",
        }

        guarded = _apply_d0_observed_feasibility_guard(
            rows,
            features,
            "2026-06-24",
            today_key="2026-06-24",
        )

        by_outcome = {row["outcome"]: row for row in guarded}
        self.assertFalse(by_outcome["24°C or below"]["observed_feasibility_applied"])
        self.assertEqual(
            by_outcome["24°C or below"]["observed_feasibility_skipped_reason"],
            "intraday_observed_max_not_final",
        )
        self.assertAlmostEqual(by_outcome["24°C or below"]["q_bayes_posterior"], 0.1611)
        self.assertAlmostEqual(by_outcome["27°C"]["q_bayes_posterior"], 0.1931)

    def test_d0_observed_feasibility_skip_restores_materialized_raw_distribution(self):
        rows = [
            {
                "outcome": "24°C or below",
                "q_bayes_posterior": 0.3053,
                "q_bayes_posterior_raw": 0.1611,
                "q_temperature_bucket_posterior": 0.3053,
                "q_temperature_bucket_posterior_raw": 0.1611,
                "q_calibrated": 0.3053,
                "q_calibrated_raw": 0.1611,
                "observed_feasibility_applied": True,
                "observed_feasibility_factor": 0.251444,
            },
            {
                "outcome": "27°C",
                "q_bayes_posterior": 0.2060,
                "q_bayes_posterior_raw": 0.1931,
                "q_temperature_bucket_posterior": 0.2060,
                "q_temperature_bucket_posterior_raw": 0.1931,
                "q_calibrated": 0.2060,
                "q_calibrated_raw": 0.1931,
                "observed_feasibility_applied": True,
                "observed_feasibility_factor": 0.141551,
            },
        ]
        features = {
            "observed_temp_c": 23.0,
            "observed_today_max_c": 24.0,
            "observed_temp_source": "zuuu_metar_raw_awc",
            "observed_today_max_source": "zuuu_metar_raw_awc",
        }

        guarded = _apply_d0_observed_feasibility_guard(
            rows,
            features,
            "2026-06-24",
            today_key="2026-06-24",
        )

        by_outcome = {row["outcome"]: row for row in guarded}
        self.assertFalse(by_outcome["24°C or below"]["observed_feasibility_applied"])
        self.assertEqual(
            by_outcome["24°C or below"]["observed_feasibility_skipped_reason"],
            "intraday_observed_max_not_final",
        )
        self.assertAlmostEqual(by_outcome["24°C or below"]["q_bayes_posterior"], 0.1611)
        self.assertAlmostEqual(by_outcome["24°C or below"]["q_temperature_bucket_posterior"], 0.1611)
        self.assertAlmostEqual(by_outcome["27°C"]["q_bayes_posterior"], 0.1931)

    def test_d0_observed_feasibility_guard_is_idempotent_for_final_observation(self):
        rows = [
            {
                "outcome": "24°C or below",
                "q_bayes_posterior": 0.161099644840474,
                "q_temperature_bucket_posterior": 0.161099644840474,
                "q_physical_observed_prob": 0.251444,
            },
            {
                "outcome": "27°C",
                "q_bayes_posterior": 0.193102181923937,
                "q_temperature_bucket_posterior": 0.193102181923937,
                "q_physical_observed_prob": 0.141551,
            },
            {
                "outcome": "28°C",
                "q_bayes_posterior": 0.135691824832868,
                "q_temperature_bucket_posterior": 0.135691824832868,
                "q_physical_observed_prob": 0.117113,
            },
        ]
        features = {
            "observed_temp_c": 24.0,
            "observed_today_max_c": 24.0,
            "observed_temp_source": "zuuu_metar_raw_awc",
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "observed_today_max_final": True,
        }

        once = _apply_d0_observed_feasibility_guard(
            rows,
            features,
            "2026-06-24",
            today_key="2026-06-24",
        )
        twice = _apply_d0_observed_feasibility_guard(
            once,
            features,
            "2026-06-24",
            today_key="2026-06-24",
        )

        once_by_outcome = {row["outcome"]: row for row in once}
        twice_by_outcome = {row["outcome"]: row for row in twice}
        for outcome in once_by_outcome:
            self.assertAlmostEqual(
                once_by_outcome[outcome]["q_bayes_posterior"],
                twice_by_outcome[outcome]["q_bayes_posterior"],
            )
            self.assertAlmostEqual(
                once_by_outcome[outcome]["q_temperature_bucket_posterior"],
                twice_by_outcome[outcome]["q_temperature_bucket_posterior"],
            )

    def test_source_observation_comparison_uses_wu_display_bucket_not_raw_floor(self):
        rows = [
            {"outcome": "31°C or below"},
            {"outcome": "32°C"},
            {"outcome": "33°C"},
            {"outcome": "34°C"},
        ]
        features = {
            "zuuu_metar_raw_awc_latest_temp_c": 33.0,
            "zuuu_metar_raw_awc_today_max_c": 33.0,
            "zuuu_metar_raw_awc_latest_at": "2026-06-14T08:00:00+00:00",
            "wu_twc_observed_intraday_current_temp_c": 32.7777777778,
            "wu_twc_observed_intraday_today_max_c": 32.7777777778,
            "wu_twc_observed_intraday_today_max_at": "2026-06-14T08:32:40+00:00",
        }

        comparison = _source_observation_comparison(
            rows,
            features,
            rounding_rule="whole_degrees",
            market_unit="C",
            reference_time=datetime(2026, 6, 14, 8, 35, tzinfo=timezone.utc),
            event_date="2026-06-14",
        )

        self.assertEqual(comparison["awc"]["settlement_proxy_bucket"], "33°C")
        self.assertEqual(comparison["awc"]["visual_nearest_bucket"], "33°C")
        self.assertEqual(comparison["wu_twc"]["settlement_proxy_bucket"], "33°C")
        self.assertEqual(comparison["wu_twc"]["raw_decimal_floor_proxy_bucket"], "32°C")
        self.assertEqual(comparison["wu_twc"]["visual_nearest_bucket"], "33°C")
        self.assertTrue(comparison["wu_twc"]["bucket_mismatch_by_visual_rounding"])
        self.assertTrue(comparison["same_settlement_bucket"])
        self.assertEqual(comparison["primary_source"], "zuuu_metar_raw_awc")
        self.assertIn("raw-floor", comparison["warning"])

    def test_source_observation_comparison_does_not_promote_non_event_day_metar(self):
        rows = [
            {"outcome": "24°C or below"},
            {"outcome": "25°C"},
            {"outcome": "29°C"},
            {"outcome": "30°C"},
        ]
        features = {
            "zuuu_metar_raw_awc_latest_temp_c": 22.0,
            "zuuu_metar_raw_awc_latest_at": "2026-06-24T23:00:00+00:00",
            "zuuu_metar_raw_awc_latest_raw": "ZUUU 242300Z 00000MPS CAVOK 22/19 Q1008",
        }

        comparison = _source_observation_comparison(
            rows,
            features,
            rounding_rule="whole_degrees",
            market_unit="C",
            reference_time=datetime(2026, 6, 25, 4, 10, tzinfo=timezone.utc),
            event_date="2026-06-26",
        )

        self.assertEqual(comparison["primary_source"], "")
        self.assertEqual(comparison["primary_bucket"], "")
        self.assertFalse(comparison["event_observation_available"])
        self.assertEqual(comparison["awc"]["settlement_proxy_bucket"], "24°C or below")
        self.assertFalse(comparison["awc"]["usable_for_event"])
        self.assertFalse(comparison["awc"]["event_date_match"])
        self.assertEqual(comparison["awc"]["observed_local_date"], "2026-06-25")
        self.assertIn("等待 2026-06-26 当日可落桶硬实况", comparison["verdict"])
        self.assertIn("不是事件日 2026-06-26", comparison["warning"])

    def test_target_observation_marks_wu_display_single_bucket_as_reached(self):
        rows = [
            {"outcome": "31°C or below"},
            {"outcome": "32°C"},
            {"outcome": "33°C"},
            {"outcome": "34°C"},
        ]
        features = {
            "wu_twc_observed_intraday_today_max_c": 32.7777777778,
            "wu_twc_observed_intraday_today_max_at": "2026-06-14T08:12:40+00:00",
        }

        state = _target_bucket_observation_state(
            rows,
            features,
            "33°C",
            rounding_rule="whole_degrees",
            market_unit="C",
        )

        self.assertEqual(state["source"], "wu_twc_observed_intraday_today_max")
        self.assertEqual(state["settlement_proxy_bucket"], "33°C")
        self.assertEqual(state["raw_bucket"], "32°C")
        self.assertEqual(state["display_bucket"], "33°C")
        self.assertEqual(state["state"], "observed_in_target_bucket")
        self.assertTrue(state["realized"])

    def test_lower_boundary_bucket_alive_is_not_marked_realized(self):
        rows = [
            {"outcome": "30°C or below"},
            {"outcome": "31°C"},
            {"outcome": "32°C"},
        ]
        features = {
            "observed_today_max_c": 27.78,
            "observed_today_max_at": "2026-06-13T05:40:07+00:00",
        }

        state = _target_bucket_observation_state(
            rows,
            features,
            "30°C or below",
            rounding_rule="whole_degrees",
            market_unit="C",
        )

        self.assertEqual(state["settlement_proxy_bucket"], "30°C or below")
        self.assertEqual(state["state"], "observed_within_lower_boundary")
        self.assertTrue(state["alive"])
        self.assertFalse(state["realized"])
        self.assertFalse(state["exceeded"])

    def test_dynamic_target_keeps_lower_boundary_when_not_broken(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "archive.db"
            conn = _make_db(db)
            payload = {
                "weather_likelihood_refreshed_at": "2026-06-15T05:00:00+00:00",
                "rounding_rule": "whole_degrees",
                "weather_likelihood_features": {
                    "observed_temp_c": 29.0,
                    "observed_today_max_c": 29.0,
                    "observed_today_max_source": "zuuu_metar_raw_awc",
                    "zuuu_metar_raw_awc_today_max_c": 29.0,
                    "temperature_rise_rate_c_per_hour": 0.0,
                },
                "rows": [
                    {"outcome": "31°C or below", "q_temperature_bucket_posterior": 0.60, "q_calibrated": 0.55},
                    {"outcome": "32°C", "q_temperature_bucket_posterior": 0.25, "q_calibrated": 0.25},
                    {"outcome": "33°C", "q_temperature_bucket_posterior": 0.15, "q_calibrated": 0.20},
                ],
            }
            conn.execute(
                """
                INSERT INTO event_runs(run_date, event_date, slug, title, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-06-15",
                    "2026-06-15",
                    "highest-temperature-in-chengdu-on-june-15-2026",
                    "Highest temperature in Chengdu on June 15?",
                    json.dumps(payload),
                    "2026-06-15T05:00:10+00:00",
                ),
            )
            conn.commit()
            conn.close()

            summary = build_chengdu_intraday_summary(db, event_date="2026-06-15")

        self.assertEqual(summary["target_bucket"], "31°C or below")
        self.assertEqual(summary["target_bucket_selection"]["dynamic_target_state"], "posterior_highest_bucket")
        self.assertFalse(summary["target_bucket_selection"]["differs_from_posterior_highest"])
        self.assertEqual(summary["temperature_realization_reliability"]["realization_mode"], "lower_boundary_hold_needed")
        self.assertFalse(summary["target_bucket_observation_state"]["realized"])
        self.assertIn("最高可结算桶即当前目标桶", summary["target_bucket_selection"]["dynamic_target_reason"])

    def test_dynamic_target_switches_on_d0_posterior_trajectory_before_hard_observed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "archive.db"
            conn = _make_db(db)
            conn.execute(
                """
                CREATE TABLE market_tokens (
                    slug TEXT,
                    event_date TEXT,
                    outcome TEXT,
                    yes_token TEXT,
                    no_token TEXT,
                    market_active INTEGER,
                    market_closed INTEGER,
                    market_archived INTEGER,
                    updated_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE event_market_ticks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_date TEXT,
                    event_date TEXT,
                    slug TEXT,
                    outcome TEXT,
                    side TEXT,
                    best_bid REAL,
                    best_ask REAL,
                    mid REAL,
                    spread REAL,
                    source TEXT,
                    source_detail TEXT,
                    created_at TEXT
                )
                """
            )
            slug = "highest-temperature-in-chengdu-on-june-17-2026"
            outcomes = ["24°C or below", "25°C", "26°C"]
            for outcome in outcomes:
                conn.execute(
                    """
                    INSERT INTO market_tokens(
                        slug, event_date, outcome, yes_token, no_token,
                        market_active, market_closed, market_archived, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 1, 0, 0, ?)
                    """,
                    (slug, "2026-06-17", outcome, f"yes-{outcome}", f"no-{outcome}", "2026-06-17T02:25:40+00:00"),
                )
            for outcome, bid, ask, mid, spread in (
                ("24°C or below", 0.20, 0.31, 0.255, 0.11),
                ("25°C", 0.41, 0.42, 0.415, 0.01),
                ("26°C", 0.16, 0.21, 0.185, 0.05),
            ):
                conn.execute(
                    """
                    INSERT INTO event_market_ticks(
                        run_date, event_date, slug, outcome, side,
                        best_bid, best_ask, mid, spread, source, source_detail, created_at
                    ) VALUES (?, ?, ?, ?, 'YES', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "2026-06-17",
                        "2026-06-17",
                        slug,
                        outcome,
                        bid,
                        ask,
                        mid,
                        spread,
                        "clob_rest_refresh",
                        "test",
                        "2026-06-17T02:25:40+00:00",
                    ),
                )
            payload = {
                "weather_likelihood_refreshed_at": "2026-06-17T02:25:42+00:00",
                "rounding_rule": "whole_degrees",
                "market_unit": "C",
                "weather_likelihood_features": {
                    "observed_temp_c": 23.0,
                    "observed_today_max_c": 23.0,
                    "observed_today_max_source": "zuuu_metar_raw_awc",
                    "zuuu_metar_raw_awc_today_max_c": 23.0,
                    "zuuu_metar_raw_awc_today_max_at": "2026-06-17T02:00:00+00:00",
                    "zuuu_metar_raw_awc_latest_at": "2026-06-17T02:00:00+00:00",
                    "temperature_rise_rate_c_per_hour": 0.8,
                    "temperature_momentum_1h_c_per_hour": 0.8,
                    "temperature_momentum_2h_c_per_hour": 0.7,
                    "temperature_momentum_realization_state": "emerging_warming",
                },
                "rows": [
                    {
                        "outcome": "24°C or below",
                        "q_temperature_bucket_posterior": 0.35,
                        "q_calibrated": 0.35,
                        "best_bid": 0.66,
                        "best_ask": 0.72,
                        "outcome_volume": 500,
                    },
                    {
                        "outcome": "25°C",
                        "q_temperature_bucket_posterior": 0.30,
                        "q_calibrated": 0.30,
                        "best_bid": 0.22,
                        "best_ask": 0.30,
                        "outcome_volume": 500,
                    },
                    {
                        "outcome": "26°C",
                        "q_temperature_bucket_posterior": 0.20,
                        "q_calibrated": 0.20,
                        "best_bid": 0.10,
                        "best_ask": 0.18,
                        "outcome_volume": 200,
                    },
                ],
            }
            conn.execute(
                """
                INSERT INTO event_runs(run_date, event_date, slug, title, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-06-17",
                    "2026-06-17",
                    slug,
                    "Highest temperature in Chengdu on June 17?",
                    json.dumps(payload),
                    "2026-06-17T02:25:42+00:00",
                ),
            )
            conn.commit()
            conn.close()

            summary = build_chengdu_intraday_summary(db, event_date="2026-06-17")

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["posterior_highest_bucket_label"], "26°C")
        self.assertEqual(summary["market_top_bucket"]["outcome"], "25°C")
        selection = summary["target_bucket_selection"]
        self.assertEqual(summary["target_bucket"], "26°C")
        self.assertEqual(selection["dynamic_target_state"], "posterior_highest_bucket")
        self.assertEqual(selection["dynamic_target_source"], "dynamic_posterior_highest_bucket")
        self.assertFalse(selection["differs_from_posterior_highest"])
        self.assertTrue(summary["observation_led_likelihood"]["applied"])
        self.assertEqual(summary["observation_led_likelihood"]["target_bucket_before"], "24°C or below")
        self.assertEqual(summary["observation_led_likelihood"]["target_bucket_after"], "26°C")

    def test_dynamic_target_switches_to_temperature_trajectory_bucket_when_momentum_confirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "archive.db"
            conn = _make_db(db)
            slug = "highest-temperature-in-chengdu-on-june-17-2026"
            payload = {
                "weather_likelihood_refreshed_at": "2026-06-17T02:25:42+00:00",
                "rounding_rule": "whole_degrees",
                "market_unit": "C",
                "weather_likelihood_features": {
                    "observed_temp_c": 23.0,
                    "observed_today_max_c": 23.0,
                    "observed_today_max_source": "zuuu_metar_raw_awc",
                    "zuuu_metar_raw_awc_today_max_c": 23.0,
                    "zuuu_metar_raw_awc_today_max_at": "2026-06-17T02:00:00+00:00",
                    "zuuu_metar_raw_awc_latest_at": "2026-06-17T02:00:00+00:00",
                    "weather_com_forecast_peak_at": "2026-06-17T16:00:00+08:00",
                    "temperature_rise_rate_c_per_hour": 0.8,
                    "temperature_rise_rate_source": "zuuu_metar_raw_awc:30m",
                    "temperature_rise_rate_window": "30m",
                    "temperature_momentum_30m_c_per_hour": 0.8,
                    "temperature_momentum_45m_c_per_hour": 0.75,
                    "temperature_momentum_1h_c_per_hour": 0.8,
                    "temperature_momentum_2h_c_per_hour": 0.7,
                    "temperature_momentum_3h_c_per_hour": 0.6,
                    "temperature_momentum_6h_c_per_hour": 0.4,
                    "temperature_momentum_effective_c_per_hour": 0.68,
                    "temperature_momentum_realization_state": "sustained_warming",
                    "temperature_momentum_sustained_warming": True,
                    "temperature_momentum_posterior_gate_allowed": True,
                },
                "rows": [
                    {
                        "outcome": "24°C or below",
                        "q_temperature_bucket_posterior": 0.35,
                        "q_calibrated": 0.35,
                        "best_bid": 0.66,
                        "best_ask": 0.72,
                        "outcome_volume": 500,
                    },
                    {
                        "outcome": "25°C",
                        "q_temperature_bucket_posterior": 0.30,
                        "q_calibrated": 0.30,
                        "best_bid": 0.22,
                        "best_ask": 0.30,
                        "outcome_volume": 500,
                    },
                    {
                        "outcome": "26°C",
                        "q_temperature_bucket_posterior": 0.20,
                        "q_calibrated": 0.20,
                        "best_bid": 0.10,
                        "best_ask": 0.18,
                        "outcome_volume": 200,
                    },
                ],
            }
            conn.execute(
                """
                INSERT INTO event_runs(run_date, event_date, slug, title, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-06-17",
                    "2026-06-17",
                    slug,
                    "Highest temperature in Chengdu on June 17?",
                    json.dumps(payload),
                    "2026-06-17T02:25:42+00:00",
                ),
            )
            conn.commit()
            conn.close()

            summary = build_chengdu_intraday_summary(db, event_date="2026-06-17")

        selection = summary["target_bucket_selection"]
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["posterior_highest_bucket_label"], "26°C")
        self.assertEqual(summary["target_bucket"], "26°C")
        self.assertEqual(selection["dynamic_target_state"], "posterior_highest_bucket")
        self.assertEqual(selection["dynamic_target_source"], "dynamic_posterior_highest_bucket")
        self.assertFalse(selection["differs_from_posterior_highest"])
        self.assertTrue(summary["observation_led_likelihood"]["applied"])
        self.assertEqual(summary["observation_led_likelihood"]["target_bucket_before"], "24°C or below")
        self.assertEqual(summary["observation_led_likelihood"]["target_bucket_after"], "26°C")
        self.assertEqual(summary["observation_led_bayes"]["target_bucket"], "26°C")

    def test_dynamic_target_switches_single_temperature_bucket_when_stable_warming_projects_higher(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "archive.db"
            conn = _make_db(db)
            slug = "highest-temperature-in-chengdu-on-june-23-2026"
            payload = {
                "weather_likelihood_refreshed_at": "2026-06-23T05:20:00+00:00",
                "rounding_rule": "whole_degrees",
                "market_unit": "C",
                "weather_likelihood_features": {
                    "observed_temp_c": 29.0,
                    "observed_today_max_c": 29.0,
                    "observed_today_max_source": "zuuu_metar_raw_awc",
                    "zuuu_metar_raw_awc_today_max_c": 29.0,
                    "zuuu_metar_raw_awc_today_max_at": "2026-06-23T05:00:00+00:00",
                    "zuuu_metar_raw_awc_latest_at": "2026-06-23T05:00:00+00:00",
                    "weather_com_forecast_peak_at": "2026-06-23T16:00:00+08:00",
                    "temperature_rise_rate_c_per_hour": 0.9,
                    "temperature_rise_rate_source": "zuuu_metar_raw_awc:30m",
                    "temperature_rise_rate_window": "30m",
                    "temperature_momentum_30m_c_per_hour": 0.55,
                    "temperature_momentum_1h_c_per_hour": 0.35,
                    "temperature_momentum_2h_c_per_hour": 0.45,
                    "temperature_momentum_3h_c_per_hour": 0.40,
                    "temperature_momentum_6h_c_per_hour": 0.35,
                    "temperature_momentum_effective_c_per_hour": 0.42,
                    "temperature_momentum_realization_state": "mixed",
                    "temperature_momentum_sustained_warming": False,
                },
                "rows": [
                    {
                        "outcome": "29°C",
                        "q_temperature_bucket_posterior": 0.55,
                        "q_calibrated": 0.55,
                        "best_bid": 0.52,
                        "best_ask": 0.56,
                        "outcome_volume": 500,
                    },
                    {
                        "outcome": "30°C",
                        "q_temperature_bucket_posterior": 0.24,
                        "q_calibrated": 0.24,
                        "best_bid": 0.19,
                        "best_ask": 0.25,
                        "outcome_volume": 400,
                    },
                    {
                        "outcome": "31°C",
                        "q_temperature_bucket_posterior": 0.08,
                        "q_calibrated": 0.08,
                        "best_bid": 0.04,
                        "best_ask": 0.08,
                        "outcome_volume": 200,
                    },
                ],
            }
            conn.execute(
                """
                INSERT INTO event_runs(run_date, event_date, slug, title, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-06-23",
                    "2026-06-23",
                    slug,
                    "Highest temperature in Chengdu on June 23?",
                    json.dumps(payload),
                    "2026-06-23T05:20:00+00:00",
                ),
            )
            conn.commit()
            conn.close()

            summary = build_chengdu_intraday_summary(db, event_date="2026-06-23")

        selection = summary["target_bucket_selection"]
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["posterior_highest_bucket_label"], "30°C")
        self.assertEqual(summary["target_bucket"], "30°C")
        self.assertEqual(selection["dynamic_target_state"], "posterior_highest_bucket")
        self.assertEqual(selection["dynamic_target_source"], "dynamic_posterior_highest_bucket")
        self.assertFalse(selection["differs_from_posterior_highest"])
        self.assertTrue(summary["observation_led_likelihood"]["applied"])
        self.assertEqual(summary["observation_led_likelihood"]["target_bucket_before"], "29°C")
        self.assertEqual(summary["observation_led_likelihood"]["target_bucket_after"], "30°C")
        self.assertEqual(
            selection["target_momentum_gate"]["posterior_gate_reason"],
            "stable_multi_window_confirmed",
        )

    def test_dynamic_target_prefers_projected_trajectory_bucket_over_higher_posterior_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "archive.db"
            conn = _make_db(db)
            slug = "highest-temperature-in-chengdu-on-june-23-2026"
            payload = {
                "weather_likelihood_refreshed_at": "2026-06-23T05:20:00+00:00",
                "rounding_rule": "whole_degrees",
                "market_unit": "C",
                "weather_likelihood_features": {
                    "observed_temp_c": 29.0,
                    "observed_today_max_c": 29.0,
                    "observed_today_max_source": "zuuu_metar_raw_awc",
                    "zuuu_metar_raw_awc_today_max_c": 29.0,
                    "zuuu_metar_raw_awc_today_max_at": "2026-06-23T05:00:00+00:00",
                    "zuuu_metar_raw_awc_latest_at": "2026-06-23T05:00:00+00:00",
                    "weather_com_forecast_peak_at": "2026-06-23T16:00:00+08:00",
                    "temperature_rise_rate_c_per_hour": 0.9,
                    "temperature_rise_rate_source": "zuuu_metar_raw_awc:30m",
                    "temperature_rise_rate_window": "30m",
                    "temperature_momentum_30m_c_per_hour": 0.55,
                    "temperature_momentum_1h_c_per_hour": 0.35,
                    "temperature_momentum_2h_c_per_hour": 0.45,
                    "temperature_momentum_3h_c_per_hour": 0.40,
                    "temperature_momentum_6h_c_per_hour": 0.35,
                    "temperature_momentum_effective_c_per_hour": 0.42,
                    "temperature_momentum_realization_state": "mixed",
                    "temperature_momentum_sustained_warming": False,
                },
                "rows": [
                    {
                        "outcome": "29°C",
                        "q_temperature_bucket_posterior": 0.55,
                        "q_calibrated": 0.55,
                        "best_bid": 0.52,
                        "best_ask": 0.56,
                        "outcome_volume": 500,
                    },
                    {
                        "outcome": "30°C",
                        "q_temperature_bucket_posterior": 0.20,
                        "q_calibrated": 0.20,
                        "best_bid": 0.18,
                        "best_ask": 0.24,
                        "outcome_volume": 400,
                    },
                    {
                        "outcome": "31°C",
                        "q_temperature_bucket_posterior": 0.24,
                        "q_calibrated": 0.24,
                        "best_bid": 0.20,
                        "best_ask": 0.27,
                        "outcome_volume": 300,
                    },
                ],
            }
            conn.execute(
                """
                INSERT INTO event_runs(run_date, event_date, slug, title, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-06-23",
                    "2026-06-23",
                    slug,
                    "Highest temperature in Chengdu on June 23?",
                    json.dumps(payload),
                    "2026-06-23T05:20:00+00:00",
                ),
            )
            conn.commit()
            conn.close()

            summary = build_chengdu_intraday_summary(db, event_date="2026-06-23")

        selection = summary["target_bucket_selection"]
        context = selection["early_switch_context"]
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["posterior_highest_bucket_label"], "30°C")
        self.assertEqual(summary["target_bucket"], "30°C")
        self.assertEqual(selection["dynamic_target_state"], "posterior_highest_bucket")
        self.assertEqual(selection["dynamic_target_source"], "dynamic_posterior_highest_bucket")
        self.assertFalse(selection["differs_from_posterior_highest"])
        self.assertTrue(summary["observation_led_likelihood"]["applied"])
        self.assertEqual(summary["observation_led_likelihood"]["target_bucket_before"], "29°C")
        self.assertEqual(summary["observation_led_likelihood"]["target_bucket_after"], "30°C")
        self.assertEqual(summary["observation_led_bayes"]["target_bucket"], "30°C")

    def test_dynamic_target_uses_wu_soft_display_without_hard_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "archive.db"
            conn = _make_db(db)
            payload = {
                "weather_likelihood_refreshed_at": "2026-06-14T08:12:11+00:00",
                "rounding_rule": "whole_degrees",
                "weather_likelihood_features": {
                    "observed_temp_c": 32.78,
                    "observed_today_max_c": 32.7777777778,
                    "observed_today_max_source": "wu_twc_observed_intraday_max_since7am",
                    "zuuu_metar_raw_awc_today_max_c": 32.0,
                    "zuuu_metar_raw_awc_today_max_at": "2026-06-14T07:00:00+00:00",
                    "wu_twc_observed_intraday_today_max_c": 32.7777777778,
                    "wu_twc_observed_intraday_today_max_at": "2026-06-14T08:12:00+00:00",
                    "temperature_rise_rate_c_per_hour": 0.49,
                },
                "rows": [
                    {"outcome": "31°C or below", "q_temperature_bucket_posterior": 0.70, "q_calibrated": 0.70},
                    {"outcome": "32°C", "q_temperature_bucket_posterior": 0.20, "q_calibrated": 0.20},
                    {"outcome": "33°C", "q_temperature_bucket_posterior": 0.08, "q_calibrated": 0.08},
                    {"outcome": "34°C", "q_temperature_bucket_posterior": 0.02, "q_calibrated": 0.02},
                ],
            }
            conn.execute(
                """
                INSERT INTO event_runs(run_date, event_date, slug, title, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-06-14",
                    "2026-06-14",
                    "highest-temperature-in-chengdu-on-june-14-2026",
                    "Highest temperature in Chengdu on June 14?",
                    json.dumps(payload),
                    "2026-06-14T08:12:11+00:00",
                ),
            )
            conn.commit()
            conn.close()

            summary = build_chengdu_intraday_summary(db, event_date="2026-06-14")

        self.assertEqual(summary["posterior_highest_bucket_label"], "32°C")
        self.assertEqual(summary["target_bucket"], "32°C")
        self.assertEqual(summary["target_bucket_selection"]["dynamic_target_state"], "posterior_highest_bucket")
        self.assertEqual(
            summary["target_bucket_selection"]["observed_bucket_candidate"]["settlement_proxy_bucket"],
            "33°C",
        )
        self.assertTrue(summary["target_bucket_selection"]["observed_bucket_candidate"]["is_soft_display_source"])

    def test_dynamic_target_falls_back_when_single_bucket_market_is_not_tradeable(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "archive.db"
            conn = _make_db(db)
            payload = {
                "weather_likelihood_refreshed_at": "2026-06-15T05:00:10+00:00",
                "rounding_rule": "whole_degrees",
                "weather_likelihood_features": {},
                "rows": [
                    {
                        "outcome": "33°C",
                        "q_temperature_bucket_posterior": 0.50,
                        "q_calibrated": 0.50,
                        "display_probability_market_dead": True,
                        "display_probability_source": "dead_yes_market_zero",
                    },
                    {
                        "outcome": "32°C",
                        "q_temperature_bucket_posterior": 0.40,
                        "q_calibrated": 0.40,
                        "best_bid": 0.28,
                        "best_ask": 0.34,
                        "outcome_volume": 120,
                    },
                    {
                        "outcome": "34°C",
                        "q_temperature_bucket_posterior": 0.10,
                        "q_calibrated": 0.10,
                        "best_bid": 0.04,
                        "best_ask": 0.08,
                        "outcome_volume": 20,
                    },
                ],
            }
            conn.execute(
                """
                INSERT INTO event_runs(run_date, event_date, slug, title, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-06-15",
                    "2026-06-15",
                    "highest-temperature-in-chengdu-on-june-15-2026",
                    "Highest temperature in Chengdu on June 15?",
                    json.dumps(payload),
                    "2026-06-15T05:00:10+00:00",
                ),
            )
            conn.commit()
            conn.close()

            summary = build_chengdu_intraday_summary(db, event_date="2026-06-15")

        self.assertEqual(summary["posterior_highest_bucket_label"], "33°C")
        self.assertEqual(summary["target_bucket"], "33°C")
        self.assertEqual(summary["target_bucket_selection"]["dynamic_target_state"], "posterior_highest_bucket")
        self.assertEqual(summary["target_bucket_selection"]["dynamic_target_source"], "dynamic_posterior_highest_bucket")
        self.assertEqual(summary["target_bucket_selection"]["target_actionability_state"], "observe_only")
        self.assertFalse(summary["target_bucket_selection"]["target_is_tradeable"])
        self.assertEqual(
            summary["target_bucket_selection"]["unactionable_posterior_target"]["market_tradeability"]["state"],
            "market_dead_observe_only",
        )

    def test_dynamic_target_uses_defensive_bucket_when_momentum_not_realized(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "archive.db"
            conn = _make_db(db)
            payload = {
                "weather_likelihood_refreshed_at": "2026-06-15T05:00:10+00:00",
                "rounding_rule": "whole_degrees",
                "weather_likelihood_features": {
                    "observed_temp_c": 29.0,
                    "observed_today_max_c": 29.0,
                    "observed_today_max_source": "zuuu_metar_raw_awc",
                    "zuuu_metar_raw_awc_today_max_c": 29.0,
                    "zuuu_metar_raw_awc_today_max_at": "2026-06-15T05:00:00+00:00",
                    "temperature_rise_rate_c_per_hour": 0.0,
                    "temperature_momentum_1h_c_per_hour": 0.0,
                    "temperature_momentum_2h_c_per_hour": 0.0,
                    "temperature_momentum_3h_c_per_hour": -0.2,
                    "temperature_momentum_realization_state": "not_realized",
                },
                "rows": [
                    {
                        "outcome": "31°C or below",
                        "q_temperature_bucket_posterior": 0.20,
                        "q_calibrated": 0.20,
                        "best_bid": 0.18,
                        "best_ask": 0.24,
                        "outcome_volume": 200,
                    },
                    {
                        "outcome": "32°C",
                        "q_temperature_bucket_posterior": 0.30,
                        "q_calibrated": 0.30,
                        "best_bid": 0.25,
                        "best_ask": 0.31,
                        "outcome_volume": 200,
                    },
                    {
                        "outcome": "33°C",
                        "q_temperature_bucket_posterior": 0.50,
                        "q_calibrated": 0.50,
                        "best_bid": 0.42,
                        "best_ask": 0.48,
                        "outcome_volume": 200,
                    },
                ],
            }
            conn.execute(
                """
                INSERT INTO event_runs(run_date, event_date, slug, title, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-06-15",
                    "2026-06-15",
                    "highest-temperature-in-chengdu-on-june-15-2026",
                    "Highest temperature in Chengdu on June 15?",
                    json.dumps(payload),
                    "2026-06-15T05:00:10+00:00",
                ),
            )
            conn.commit()
            conn.close()

            summary = build_chengdu_intraday_summary(db, event_date="2026-06-15")

        self.assertEqual(summary["posterior_highest_bucket_label"], "33°C")
        self.assertEqual(summary["target_bucket"], "33°C")
        self.assertEqual(
            summary["target_bucket_selection"]["dynamic_target_state"],
            "posterior_highest_bucket",
        )
        self.assertFalse(summary["target_bucket_selection"]["target_momentum_gate"]["supports_warming"])
        self.assertFalse(summary["target_bucket_selection"]["differs_from_posterior_highest"])

    def test_dynamic_target_keeps_dead_lower_boundary_as_risk_monitor_not_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "archive.db"
            conn = _make_db(db)
            payload = {
                "weather_likelihood_refreshed_at": "2026-06-15T05:00:10+00:00",
                "rounding_rule": "whole_degrees",
                "weather_likelihood_features": {
                    "observed_temp_c": 27.0,
                    "observed_today_max_c": 27.0,
                    "observed_today_max_source": "zuuu_metar_raw_awc",
                    "zuuu_metar_raw_awc_today_max_c": 27.0,
                    "zuuu_metar_raw_awc_today_max_at": "2026-06-14T23:00:00+00:00",
                },
                "rows": [
                    {
                        "outcome": "31°C or below",
                        "q_temperature_bucket_posterior": 0.90,
                        "q_calibrated": 0.90,
                        "display_probability_market_dead": True,
                        "display_probability_source": "dead_yes_market_zero",
                    },
                    {
                        "outcome": "32°C",
                        "q_temperature_bucket_posterior": 0.08,
                        "q_calibrated": 0.08,
                        "best_bid": 0.18,
                        "best_ask": 0.20,
                        "outcome_volume": 120,
                    },
                    {
                        "outcome": "33°C",
                        "q_temperature_bucket_posterior": 0.02,
                        "q_calibrated": 0.02,
                        "best_bid": 0.03,
                        "best_ask": 0.06,
                        "outcome_volume": 20,
                    },
                ],
            }
            conn.execute(
                """
                INSERT INTO event_runs(run_date, event_date, slug, title, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-06-15",
                    "2026-06-15",
                    "highest-temperature-in-chengdu-on-june-15-2026",
                    "Highest temperature in Chengdu on June 15?",
                    json.dumps(payload),
                    "2026-06-15T05:00:10+00:00",
                ),
            )
            conn.commit()
            conn.close()

            summary = build_chengdu_intraday_summary(db, event_date="2026-06-15")

        self.assertEqual(summary["target_bucket"], "31°C or below")
        self.assertEqual(summary["target_bucket_selection"]["dynamic_target_state"], "posterior_highest_bucket")
        self.assertEqual(summary["target_bucket_selection"]["target_actionability_state"], "risk_monitor_only")
        self.assertFalse(summary["target_bucket_selection"]["target_is_tradeable"])
        self.assertIn("最高可结算桶即当前目标桶", summary["target_bucket_selection"]["dynamic_target_reason"])

    def test_created_at_replay_tracks_dynamic_target_state_switches(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "archive.db"
            conn = _make_db(db)

            def insert_snapshot(created_at, features, rows):
                payload = {
                    "weather_likelihood_refreshed_at": created_at,
                    "rounding_rule": "whole_degrees",
                    "weather_likelihood_features": features,
                    "rows": rows,
                }
                conn.execute(
                    """
                    INSERT INTO event_runs(run_date, event_date, slug, title, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "2026-06-14",
                        "2026-06-14",
                        "highest-temperature-in-chengdu-on-june-14-2026",
                        "Highest temperature in Chengdu on June 14?",
                        json.dumps(payload),
                        created_at,
                    ),
                )

            low_rows = [
                {"outcome": "31°C or below", "q_temperature_bucket_posterior": 0.72, "q_calibrated": 0.72},
                {"outcome": "32°C", "q_temperature_bucket_posterior": 0.18, "q_calibrated": 0.18},
                {"outcome": "33°C", "q_temperature_bucket_posterior": 0.08, "q_calibrated": 0.08},
                {"outcome": "34°C", "q_temperature_bucket_posterior": 0.02, "q_calibrated": 0.02},
            ]
            insert_snapshot(
                "2026-06-14T04:34:10+00:00",
                {
                    "observed_temp_c": 29.0,
                    "observed_today_max_c": 29.0,
                    "observed_today_max_source": "zuuu_metar_raw_awc",
                    "zuuu_metar_raw_awc_today_max_c": 29.0,
                    "zuuu_metar_raw_awc_today_max_at": "2026-06-14T04:00:00+00:00",
                    "temperature_rise_rate_c_per_hour": 0.0,
                },
                low_rows,
            )
            insert_snapshot(
                "2026-06-14T07:34:10+00:00",
                {
                    "observed_temp_c": 32.0,
                    "observed_today_max_c": 32.0,
                    "observed_today_max_source": "zuuu_metar_raw_awc",
                    "zuuu_metar_raw_awc_today_max_c": 32.0,
                    "zuuu_metar_raw_awc_today_max_at": "2026-06-14T07:00:00+00:00",
                    "temperature_rise_rate_c_per_hour": 1.0,
                },
                low_rows,
            )
            insert_snapshot(
                "2026-06-14T08:12:10+00:00",
                {
                    "observed_temp_c": 32.78,
                    "observed_today_max_c": 32.7777777778,
                    "observed_today_max_source": "wu_twc_observed_intraday_max_since7am",
                    "zuuu_metar_raw_awc_today_max_c": 32.0,
                    "zuuu_metar_raw_awc_today_max_at": "2026-06-14T07:00:00+00:00",
                    "wu_twc_observed_intraday_today_max_c": 32.7777777778,
                    "wu_twc_observed_intraday_today_max_at": "2026-06-14T08:12:00+00:00",
                    "temperature_rise_rate_c_per_hour": 0.49,
                },
                low_rows,
            )
            insert_snapshot(
                "2026-06-14T08:35:10+00:00",
                {
                    "observed_temp_c": 33.0,
                    "observed_today_max_c": 33.0,
                    "observed_today_max_source": "zuuu_metar_raw_awc",
                    "zuuu_metar_raw_awc_today_max_c": 33.0,
                    "zuuu_metar_raw_awc_today_max_at": "2026-06-14T08:00:00+00:00",
                    "wu_twc_observed_intraday_today_max_c": 32.7777777778,
                    "wu_twc_observed_intraday_today_max_at": "2026-06-14T08:12:00+00:00",
                    "temperature_rise_rate_c_per_hour": 0.20,
                },
                [
                    {"outcome": "31°C or below", "q_temperature_bucket_posterior": 0.05, "q_calibrated": 0.05},
                    {"outcome": "32°C", "q_temperature_bucket_posterior": 0.20, "q_calibrated": 0.20},
                    {"outcome": "33°C", "q_temperature_bucket_posterior": 0.70, "q_calibrated": 0.70},
                    {"outcome": "34°C", "q_temperature_bucket_posterior": 0.05, "q_calibrated": 0.05},
                ],
            )
            conn.commit()
            conn.close()

            summary = build_chengdu_intraday_summary(db, event_date="2026-06-14")

        replay = summary["created_at_replay"]
        by_run = {row["event_run_id"]: row for row in replay}
        ordered = sorted(replay, key=lambda row: row["created_at"])
        self.assertGreaterEqual(len(ordered), 4)
        self.assertEqual(ordered[0]["target_bucket"], "31°C or below")
        self.assertEqual(ordered[0]["dynamic_target_state"], "posterior_highest_bucket")
        self.assertEqual(ordered[1]["target_bucket"], "32°C")
        self.assertEqual(ordered[1]["dynamic_target_state"], "posterior_highest_bucket")
        soft_rows = [
            row for row in ordered
            if row["target_observation_state"].get("is_soft_display_source")
        ]
        self.assertTrue(soft_rows)
        self.assertEqual(soft_rows[0]["target_bucket"], "32°C")
        hard_rows = [
            row for row in ordered
            if row["target_bucket"] == "33°C" and row["dynamic_target_state"] == "posterior_highest_bucket"
        ]
        self.assertTrue(hard_rows)
        self.assertFalse(hard_rows[-1]["target_observation_state"]["is_soft_display_source"])

    def test_proxy_precip_does_not_trigger_strong_cooling_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "archive.db"
            conn = _make_db(db)
            payload = {
                "weather_likelihood_refreshed_at": "2026-06-13T08:14:06+00:00",
                "weather_likelihood_feature_quality": "partial",
                "weather_likelihood_features": {
                    "forecast_peak_temp": 33.8,
                    "wu_forecast_peak_c": 33.3,
                    "taf_forecast_peak_c": 32.0,
                    "cloud_cover_pct": 40.1,
                    "cloud_cover_low_pct": 4.4,
                    "solar_radiation": 463.8,
                    "dewpoint_c": 19.1,
                    "observed_precip_rate_mm_per_h": 0.1,
                    "is_raining": True,
                    "is_raining_observed": None,
                },
                "weather_likelihood_feature_groups": {
                    "precipitation_evaporative_cooling": {
                        "evidence_state": "proxy",
                        "source_profile": {
                            "evidence_state": "proxy",
                            "source": "open_meteo_current_hourly_precip",
                            "source_class": "weak_proxy",
                        },
                    }
                },
                "rows": [
                    {"outcome": "33°C", "q_calibrated": 0.13, "q_bayes_posterior": 0.28},
                    {"outcome": "34°C", "q_calibrated": 0.12, "q_bayes_posterior": 0.21},
                ],
            }
            conn.execute(
                """
                INSERT INTO event_runs(run_date, event_date, slug, title, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-06-13",
                    "2026-06-14",
                    "highest-temperature-in-chengdu-on-june-14-2026",
                    "Highest temperature in Chengdu on June 14?",
                    json.dumps(payload),
                    "2026-06-13T08:25:54+00:00",
                ),
            )
            conn.commit()
            conn.close()

            summary = build_chengdu_intraday_summary(db, event_date="2026-06-14")

        precip = next(item for item in summary["evidence_conclusions"] if item["key"] == "radar_precip_echo")
        self.assertEqual(precip["state"], "neutral")
        self.assertIn("代理降水", precip["conclusion"])
        self.assertNotEqual(summary["decision"]["summary"], "强降水证据成立，蒸发冷却主导，降温概率显著上调。")

    def test_latest_without_date_skips_slug_event_date_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "archive.db"
            conn = _make_db(db)
            good_payload = {
                "weather_likelihood_features": {
                    "observed_temp_c": 31.0,
                    "observed_today_max_c": 31.0,
                    "forecast_peak_temp": 32.1,
                },
                "rows": [{"outcome": "32°C", "q_calibrated": 0.2, "q_bayes_posterior": 0.25}],
            }
            bad_payload = {
                "weather_likelihood_features": {
                    "observed_temp_c": 28.9,
                    "observed_today_max_c": 28.9,
                    "forecast_peak_temp": 21.0,
                },
                "rows": [{"outcome": "30°C or below", "q_calibrated": 0.3, "q_bayes_posterior": 0.8}],
            }
            rows = [
                ("2026-06-13", "2026-06-13", "highest-temperature-in-chengdu-on-june-13-2026", good_payload, "2026-06-13T07:49:30+00:00"),
                ("2026-06-13", "2026-06-12", "highest-temperature-in-chengdu-on-june-13-2026", bad_payload, "2026-06-13T07:51:14+00:00"),
            ]
            for run_date, event_date, slug, payload, created_at in rows:
                conn.execute(
                    """
                    INSERT INTO event_runs(run_date, event_date, slug, title, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_date,
                        event_date,
                        slug,
                        "Highest temperature in Chengdu?",
                        json.dumps(payload),
                        created_at,
                    ),
                )
            conn.commit()
            conn.close()

            summary = build_chengdu_intraday_summary(db)

        self.assertIs(summary["ok"], True)
        self.assertEqual(summary["event_date"], "2026-06-13")
        self.assertEqual(summary["created_at"], "2026-06-13T07:49:30+00:00")
        self.assertEqual(summary["key_metrics"]["observed_temp_c"], 31.0)

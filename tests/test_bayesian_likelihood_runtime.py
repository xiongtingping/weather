import gzip
import math
import os
import struct
import unittest
import zlib
from datetime import datetime, timezone
from unittest.mock import patch

from bayesian_likelihood_runtime import (
    compute_weather_likelihood_vector,
    detect_bimodal_temperature_distribution,
    dynamic_likelihood_config,
    estimate_peak_temperature_from_features,
    peak_kalman_gain,
    parse_temperature_bucket,
    precipitation_evidence_profile,
)
from bayesian_probability_runtime import apply_bayesian_probability_update
from scan_weather_market import (
    _build_weather_likelihood_features,
    _compact_weather_likelihood_features,
    _metar_confirms_no_precip,
    _metar_precip_rate_mm_per_h,
    _parse_awc_raw_metar_line,
    _temperature_peak_likelihood_refresh_interval_sec,
    _temperature_momentum_from_pairs,
    _weather_likelihood_feature_group_availability,
    _weather_likelihood_feature_quality,
    fetch_metar_likelihood_features,
    fetch_mrms_qpe_metadata_features,
    fetch_nws_latest_observation_likelihood_features,
    fetch_open_meteo_likelihood_features,
    fetch_open_meteo_satellite_radiation_features,
    fetch_zuuu_taf_forecast_peak_metrics,
    fetch_zuuu_metar_raw_awc_features,
    fetch_wu_twc_observed_intraday_features,
)


class BayesianLikelihoodRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_env = os.environ.copy()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._old_env)

    def test_dewpoint_hard_constraint_zeroes_impossible_cool_bucket(self) -> None:
        report = compute_weather_likelihood_vector(
            ["24°C or below", "25°C", "26°C or higher"],
            {"dewpoint_c": 25.2, "forecast_peak_temp_c": 27.0},
            hours_to_event=1.0,
        )

        self.assertTrue(report["enabled"])
        self.assertTrue(report["rows"][0]["hard_zero"])
        self.assertIn("dewpoint_above_bucket", report["rows"][0]["hard_reasons"])
        self.assertLess(report["likelihood_probs"][0], 1e-6)

    def test_open_meteo_precip_proxy_is_not_strong_radar_likelihood(self) -> None:
        features = {
            "forecast_peak_temp_c": 32.0,
            "observed_temp": 30.0,
            "observation_hour_local": 11.0,
            "observed_precip_rate_mm_per_h": 0.1,
            "precipitation_rate_mm_per_h": 0.1,
            "rain_rate_mm_per_h": 0.1,
            "is_raining": True,
            "radar_qpe_proxy_source": "open_meteo_current_hourly_precip",
        }

        profile = precipitation_evidence_profile(features)
        report = compute_weather_likelihood_vector(["31°C", "33°C"], features, hours_to_event=4.0)
        precip_group = report["rows"][0]["component_groups"]["precipitation_evaporative_cooling"]

        self.assertFalse(profile["available_strong"])
        self.assertTrue(profile["available_proxy"])
        self.assertEqual(profile["evidence_state"], "proxy")
        self.assertFalse(precip_group["available"])
        self.assertEqual(precip_group["evidence_state"], "proxy")
        self.assertNotIn("precip_evaporative_cooling", report["rows"][0]["components"])

    def test_real_qpe_zero_precip_is_strong_observed_evidence(self) -> None:
        features = {
            "forecast_peak_temp_c": 32.0,
            "observed_temp": 30.0,
            "observation_hour_local": 11.0,
            "radar_precip_rate_mm_per_h": 0.0,
            "qpe_precip_rate_mm_per_h": 0.0,
            "radar_qpe_proxy_source": "mrms_qpe",
        }

        profile = precipitation_evidence_profile(features)
        report = compute_weather_likelihood_vector(["31°C", "33°C"], features, hours_to_event=4.0)
        precip_group = report["rows"][0]["component_groups"]["precipitation_evaporative_cooling"]

        self.assertTrue(profile["available_strong"])
        self.assertEqual(profile["source_class"], "radar_qpe")
        self.assertEqual(profile["evidence_state"], "strong_no_precip")
        self.assertTrue(profile["no_precip_confirmed"])
        self.assertFalse(profile["precip_present"])
        self.assertEqual(profile["rate_mm_per_h"], 0.0)
        self.assertTrue(precip_group["available"])
        self.assertEqual(precip_group["evidence_state"], "strong_no_precip")
        self.assertIn("precip_evaporative_cooling", report["rows"][0]["components"])
        self.assertAlmostEqual(report["rows"][0]["components"]["precip_evaporative_cooling"], 0.0)

    def test_awc_metar_cavok_is_strong_no_precip_evidence(self) -> None:
        features = {
            "forecast_peak_temp_c": 32.0,
            "observed_temp": 30.0,
            "observation_hour_local": 11.0,
            "metar_no_precip_observed": True,
            "awc_metar_no_precip_observed": True,
            "metar_cavok_observed": True,
            "observed_precip_rate_mm_per_h": 0.0,
            "precipitation_rate_mm_per_h": 0.0,
            "radar_qpe_proxy_source": "zuuu_metar_raw_awc_cavok_no_precip",
        }

        profile = precipitation_evidence_profile(features)
        report = compute_weather_likelihood_vector(["31°C", "33°C"], features, hours_to_event=4.0)
        precip_group = report["rows"][0]["component_groups"]["precipitation_evaporative_cooling"]

        self.assertTrue(profile["available_strong"])
        self.assertEqual(profile["source_class"], "metar")
        self.assertEqual(profile["source_field"], "awc_metar_no_precip_observed")
        self.assertEqual(profile["evidence_state"], "strong_no_precip")
        self.assertTrue(profile["no_precip_confirmed"])
        self.assertFalse(profile["precip_present"])
        self.assertEqual(profile["rate_mm_per_h"], 0.0)
        self.assertTrue(precip_group["available"])
        self.assertEqual(precip_group["evidence_state"], "strong_no_precip")
        self.assertAlmostEqual(report["rows"][0]["components"]["precip_evaporative_cooling"], 0.0)

    def test_metar_precip_and_no_precip_conflict_is_not_strong_evidence(self) -> None:
        features = {
            "forecast_peak_temp_c": 32.0,
            "observed_temp": 30.0,
            "observation_hour_local": 11.0,
            "metar_rain_observed": True,
            "awc_metar_no_precip_observed": True,
            "observed_precip_rate_mm_per_h": 0.1,
            "radar_qpe_proxy_source": "zuuu_metar_raw_awc_present_weather",
        }

        profile = precipitation_evidence_profile(features)
        report = compute_weather_likelihood_vector(["31°C", "33°C"], features, hours_to_event=4.0)
        precip_group = report["rows"][0]["component_groups"]["precipitation_evaporative_cooling"]

        self.assertFalse(profile["available_strong"])
        self.assertFalse(profile["precip_present"])
        self.assertFalse(profile["no_precip_confirmed"])
        self.assertEqual(profile["evidence_state"], "conflict")
        self.assertEqual(precip_group["evidence_state"], "conflict")
        self.assertNotIn("precip_evaporative_cooling", report["rows"][0]["components"])

    def test_metar_parser_treats_cavok_shra_as_conflict_not_precip_rate(self) -> None:
        row = {"raw": "METAR ZUUU 290200Z VRB02MPS CAVOK -SHRA 27/20 Q1010 NOSIG"}

        self.assertIsNone(_metar_precip_rate_mm_per_h(row))
        self.assertFalse(_metar_confirms_no_precip(row))

    def test_parse_polymarket_between_temperature_bucket(self) -> None:
        bucket = parse_temperature_bucket("between 80-81°F")

        self.assertIsNotNone(bucket)
        self.assertEqual(bucket.kind, "range")
        self.assertEqual(bucket.low, 80.0)
        self.assertEqual(bucket.high, 81.0)
        self.assertEqual(bucket.unit, "F")
        self.assertEqual(bucket.representative, 80.5)

    def test_final_observed_today_max_is_temperature_trajectory_evidence(self) -> None:
        report = compute_weather_likelihood_vector(
            ["28°C or below", "29°C", "30°C or higher"],
            {
                "observed_today_max_c": 29.8,
                "observed_temp_c": 29.8,
                "temperature_observation_final_daily_max": True,
            },
        )

        self.assertTrue(report["enabled"])
        self.assertTrue(report["rows"][0]["hard_zero"])
        self.assertTrue(report["rows"][1]["hard_zero"])
        self.assertIn("observed_today_max_above_bucket", report["rows"][0]["hard_reasons"])
        self.assertIn("observed_today_max_final", report["rows"][2]["components"])
        self.assertGreater(report["likelihood_probs"][2], report["likelihood_probs"][1])

    def test_proxy_observed_today_max_does_not_hard_constrain(self) -> None:
        report = compute_weather_likelihood_vector(
            ["28°C or below", "29°C", "30°C or higher"],
            {
                "observed_today_max_c": 29.8,
                "observed_today_max_source": "open_meteo_archive_dry_run",
                "temperature_observation_final_daily_max": True,
            },
        )

        self.assertTrue(report["enabled"])
        self.assertFalse(report["rows"][0]["hard_zero"])
        self.assertNotIn("observed_today_max_above_bucket", report["rows"][0]["hard_reasons"])
        self.assertNotIn("observed_today_max_final", report["rows"][2]["components"])

    def test_deformed_wu_twc_intraday_max_is_not_strong_hard_constraint(self) -> None:
        report = compute_weather_likelihood_vector(
            ["28°C", "29°C", "30°C"],
            {
                "observed_today_max_c": 29.0,
                "observed_today_max_source": "wu_twc_observed_intraday_current_metric_max_since7am",
                "observed_today_max_trusted": True,
                "wu_twc_observed_intraday_today_max_c": 29.0,
                "wu_twc_observed_intraday_current_error": "current_observation_event_date_mismatch",
                "wu_twc_observed_intraday_max_error": "current_observation_event_date_mismatch",
                "weather_source_delay_deformation_alert": {
                    "alert": True,
                    "codes": ["wu_nowcast_temperature_deformation"],
                },
            },
        )

        self.assertTrue(report["enabled"])
        self.assertFalse(report["rows"][0]["hard_zero"])
        self.assertNotIn("observed_today_max_above_bucket", report["rows"][0]["hard_reasons"])

    def test_low_cti_and_cloud_suppress_hot_tail(self) -> None:
        report = compute_weather_likelihood_vector(
            ["28°C", "31°C", "34°C or higher"],
            {
                "forecast_peak_temp_c": 30.0,
                "cti": 0.25,
                "cloud_cover_pct": 92.0,
            },
            hours_to_event=8.0,
        )

        probs = report["likelihood_probs"]
        self.assertTrue(report["enabled"])
        self.assertGreater(probs[0], probs[2])
        self.assertIn("radiation_cti", report["rows"][2]["components"])
        self.assertIn("cloud", report["rows"][2]["components"])

    def test_observed_radiation_without_clear_sky_is_independent_component(self) -> None:
        report = compute_weather_likelihood_vector(
            ["28°C", "31°C", "34°C or higher"],
            {
                "forecast_peak_temp_c": 30.0,
                "solar_radiation_observed": 650.0,
                "shortwave_radiation_observed": 650.0,
            },
            hours_to_event=8.0,
        )

        row = report["rows"][2]
        radiation_group = row["component_groups"]["radiation"]
        self.assertTrue(report["enabled"])
        self.assertIn("radiation_cti", row["components"])
        self.assertTrue(radiation_group["available"])
        self.assertEqual(radiation_group["na_reason"], "")
        self.assertIn("radiation_cti", radiation_group["components"])

    def test_wind_airmass_distinguishes_direction_at_same_speed(self) -> None:
        labels = ["29°C", "33°C or higher"]
        cool = compute_weather_likelihood_vector(
            labels,
            {"forecast_peak_temp_c": 31.0, "wind_speed_mps": 8.0, "wind_direction_deg": 330.0},
            hours_to_event=10.0,
        )
        warm = compute_weather_likelihood_vector(
            labels,
            {"forecast_peak_temp_c": 31.0, "wind_speed_mps": 8.0, "wind_direction_deg": 220.0},
            hours_to_event=10.0,
        )

        self.assertLess(cool["likelihood_probs"][1], warm["likelihood_probs"][1])

    def test_bimodal_detection_sets_risk_scale(self) -> None:
        report = compute_weather_likelihood_vector(
            ["24°C", "29°C", "34°C"],
            {
                "forecast_peak_temp_c": 29.0,
                "temperature_distribution_c": [23.9, 24.2, 24.1, 24.4, 33.8, 34.2, 34.0, 34.4],
            },
            hours_to_event=20.0,
        )

        self.assertTrue(report["bimodal"]["bimodal"])
        self.assertLess(report["risk_size_scale"], 1.0)
        self.assertTrue(detect_bimodal_temperature_distribution([20, 20.2, 20.1, 26, 26.2, 26.1])["bimodal"])

    def test_dynamic_config_matches_time_philosophy(self) -> None:
        near = dynamic_likelihood_config(1.0)
        far = dynamic_likelihood_config(72.0)

        self.assertGreater(near.dewpoint_weight, near.cloud_weight)
        self.assertGreater(far.pressure_weight, far.cloud_weight)
        self.assertLess(far.observed_temp_weight, near.observed_temp_weight)
        self.assertGreater(near.peak_estimator_weight, far.peak_estimator_weight)

    def test_peak_likelihood_refresh_interval_tightens_near_local_high_window(self) -> None:
        os.environ["PM_BAYES_LIKELIHOOD_REFRESH_START_HOUR"] = "10"
        os.environ["PM_BAYES_LIKELIHOOD_REFRESH_PEAK_HOUR"] = "16"
        os.environ["PM_BAYES_LIKELIHOOD_REFRESH_END_HOUR"] = "18"
        os.environ["PM_BAYES_LIKELIHOOD_REFRESH_PRE_PEAK_MIN"] = "12"
        os.environ["PM_BAYES_LIKELIHOOD_REFRESH_PEAK_MIN"] = "2"
        os.environ["PM_BAYES_LIKELIHOOD_REFRESH_POST_PEAK_MIN"] = "6"

        early = _temperature_peak_likelihood_refresh_interval_sec(
            "2026-06-10",
            "Asia/Shanghai",
            "temperature_2m_max",
            now_dt=datetime(2026, 6, 10, 3, 0, tzinfo=timezone.utc),
        )
        near_peak = _temperature_peak_likelihood_refresh_interval_sec(
            "2026-06-10",
            "Asia/Shanghai",
            "temperature_2m_max",
            now_dt=datetime(2026, 6, 10, 7, 30, tzinfo=timezone.utc),
        )
        post_peak = _temperature_peak_likelihood_refresh_interval_sec(
            "2026-06-10",
            "Asia/Shanghai",
            "temperature_2m_max",
            now_dt=datetime(2026, 6, 10, 9, 0, tzinfo=timezone.utc),
        )

        self.assertIsNotNone(early)
        self.assertIsNotNone(near_peak)
        self.assertIsNotNone(post_peak)
        self.assertGreater(float(early), float(near_peak))
        self.assertLessEqual(float(near_peak), 2.0 * 60.0)
        self.assertLessEqual(float(post_peak), 2.0 * 60.0)
        self.assertIsNone(
            _temperature_peak_likelihood_refresh_interval_sec(
                "2026-06-11",
                "Asia/Shanghai",
                "temperature_2m_max",
                now_dt=datetime(2026, 6, 10, 7, 30, tzinfo=timezone.utc),
            )
        )
        self.assertIsNone(
            _temperature_peak_likelihood_refresh_interval_sec(
                "2026-06-10",
                "Asia/Shanghai",
                "temperature_2m_min",
                now_dt=datetime(2026, 6, 10, 7, 30, tzinfo=timezone.utc),
            )
        )
        self.assertIsNotNone(
            _temperature_peak_likelihood_refresh_interval_sec(
                "2026-06-10",
                "UTC",
                "temperature_2m_max",
                now_dt=datetime(2026, 6, 10, 7, 30, tzinfo=timezone.utc),
                lon=103.945966,
                resolution_source="https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
            )
        )

    def test_peak_likelihood_refresh_defaults_are_high_frequency_on_d0(self) -> None:
        early = _temperature_peak_likelihood_refresh_interval_sec(
            "2026-06-10",
            "Asia/Shanghai",
            "temperature_2m_max",
            now_dt=datetime(2026, 6, 10, 3, 0, tzinfo=timezone.utc),
        )
        near_peak = _temperature_peak_likelihood_refresh_interval_sec(
            "2026-06-10",
            "Asia/Shanghai",
            "temperature_2m_max",
            now_dt=datetime(2026, 6, 10, 8, 0, tzinfo=timezone.utc),
        )
        post_peak = _temperature_peak_likelihood_refresh_interval_sec(
            "2026-06-10",
            "Asia/Shanghai",
            "temperature_2m_max",
            now_dt=datetime(2026, 6, 10, 9, 0, tzinfo=timezone.utc),
        )

        self.assertIsNotNone(early)
        self.assertIsNotNone(near_peak)
        self.assertIsNotNone(post_peak)
        self.assertLessEqual(float(early), 5.0 * 60.0)
        self.assertLessEqual(float(near_peak), 1.0 * 60.0)
        self.assertLessEqual(float(post_peak), 2.0 * 60.0)

    def test_peak_likelihood_refresh_continues_after_local_event_day(self) -> None:
        os.environ["PM_BAYES_LIKELIHOOD_REFRESH_POST_EVENT_SEC"] = "300"

        post_event = _temperature_peak_likelihood_refresh_interval_sec(
            "2026-06-10",
            "Asia/Shanghai",
            "temperature_2m_max",
            now_dt=datetime(2026, 6, 10, 16, 30, tzinfo=timezone.utc),
        )
        future_event = _temperature_peak_likelihood_refresh_interval_sec(
            "2026-06-12",
            "Asia/Shanghai",
            "temperature_2m_max",
            now_dt=datetime(2026, 6, 10, 16, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(float(post_event), 300.0)
        self.assertIsNone(future_event)

    def test_peak_likelihood_refresh_continues_after_local_high_window(self) -> None:
        os.environ["PM_BAYES_LIKELIHOOD_REFRESH_START_HOUR"] = "10"
        os.environ["PM_BAYES_LIKELIHOOD_REFRESH_PEAK_HOUR"] = "16"
        os.environ["PM_BAYES_LIKELIHOOD_REFRESH_END_HOUR"] = "18"
        os.environ["PM_BAYES_LIKELIHOOD_REFRESH_AFTER_WINDOW_SEC"] = "120"

        after_window = _temperature_peak_likelihood_refresh_interval_sec(
            "2026-06-10",
            "Asia/Shanghai",
            "temperature_2m_max",
            now_dt=datetime(2026, 6, 10, 13, 30, tzinfo=timezone.utc),
        )
        before_window = _temperature_peak_likelihood_refresh_interval_sec(
            "2026-06-10",
            "Asia/Shanghai",
            "temperature_2m_max",
            now_dt=datetime(2026, 6, 10, 1, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(float(after_window), 120.0)
        self.assertIsNone(before_window)

    def test_peak_likelihood_refresh_accepts_second_level_cadence(self) -> None:
        os.environ["PM_BAYES_LIKELIHOOD_REFRESH_START_HOUR"] = "10"
        os.environ["PM_BAYES_LIKELIHOOD_REFRESH_PEAK_HOUR"] = "16"
        os.environ["PM_BAYES_LIKELIHOOD_REFRESH_END_HOUR"] = "18"
        os.environ["PM_BAYES_LIKELIHOOD_REFRESH_PRE_PEAK_SEC"] = "180"
        os.environ["PM_BAYES_LIKELIHOOD_REFRESH_PEAK_SEC"] = "30"
        os.environ["PM_BAYES_LIKELIHOOD_REFRESH_POST_PEAK_SEC"] = "45"
        os.environ["PM_BAYES_LIKELIHOOD_REFRESH_MIN_FLOOR_SEC"] = "10"

        peak_window = _temperature_peak_likelihood_refresh_interval_sec(
            "2026-06-10",
            "Asia/Shanghai",
            "temperature_2m_max",
            now_dt=datetime(2026, 6, 10, 8, 0, 30, tzinfo=timezone.utc),
        )
        post_peak = _temperature_peak_likelihood_refresh_interval_sec(
            "2026-06-10",
            "Asia/Shanghai",
            "temperature_2m_max",
            now_dt=datetime(2026, 6, 10, 8, 30, 30, tzinfo=timezone.utc),
        )

        self.assertIsNotNone(peak_window)
        self.assertIsNotNone(post_peak)
        self.assertLessEqual(float(peak_window), 30.0)
        self.assertLessEqual(float(post_peak), 45.0)

    def test_peak_likelihood_refresh_hard_caps_slow_env_by_peak_distance(self) -> None:
        os.environ["PM_BAYES_LIKELIHOOD_REFRESH_START_HOUR"] = "8"
        os.environ["PM_BAYES_LIKELIHOOD_REFRESH_PEAK_HOUR"] = "16"
        os.environ["PM_BAYES_LIKELIHOOD_REFRESH_END_HOUR"] = "18"
        os.environ["PM_BAYES_LIKELIHOOD_REFRESH_PRE_PEAK_MIN"] = "60"
        os.environ["PM_BAYES_LIKELIHOOD_REFRESH_PEAK_MIN"] = "60"
        os.environ["PM_BAYES_LIKELIHOOD_REFRESH_POST_PEAK_MIN"] = "60"
        os.environ["PM_BAYES_LIKELIHOOD_REFRESH_MIN_FLOOR_MIN"] = "30"

        over_4h = _temperature_peak_likelihood_refresh_interval_sec(
            "2026-06-10",
            "Asia/Shanghai",
            "temperature_2m_max",
            now_dt=datetime(2026, 6, 10, 3, 0, tzinfo=timezone.utc),
        )
        two_to_4h = _temperature_peak_likelihood_refresh_interval_sec(
            "2026-06-10",
            "Asia/Shanghai",
            "temperature_2m_max",
            now_dt=datetime(2026, 6, 10, 4, 30, tzinfo=timezone.utc),
        )
        under_2h = _temperature_peak_likelihood_refresh_interval_sec(
            "2026-06-10",
            "Asia/Shanghai",
            "temperature_2m_max",
            now_dt=datetime(2026, 6, 10, 6, 30, tzinfo=timezone.utc),
        )
        peak_window = _temperature_peak_likelihood_refresh_interval_sec(
            "2026-06-10",
            "Asia/Shanghai",
            "temperature_2m_max",
            now_dt=datetime(2026, 6, 10, 8, 0, tzinfo=timezone.utc),
        )

        self.assertLessEqual(float(over_4h), 15.0 * 60.0)
        self.assertLessEqual(float(two_to_4h), 10.0 * 60.0)
        self.assertLessEqual(float(under_2h), 5.0 * 60.0)
        self.assertLessEqual(float(peak_window), 2.0 * 60.0)

    def test_peak_estimator_blends_observed_extrapolation_with_forecast(self) -> None:
        peak = estimate_peak_temperature_from_features(
            {
                "unit": "C",
                "forecast_peak_temp": 35.0,
                "temperature_distribution": [34.0, 35.0, 36.0],
                "observed_temp": 31.0,
                "observation_hour_local": 11.5,
                "observation_month": 6,
                "cloud_cover_pct": 90.0,
                "wind_speed_mps": 10.0,
            }
        )

        self.assertTrue(peak["enabled"])
        self.assertEqual(peak["source"], "energy_balance_nowcast")
        self.assertEqual(peak["legacy_source"], "kalman_blend")
        self.assertGreater(peak["kalman_gain"], peak_kalman_gain(8.0))
        self.assertLess(peak["combined_discount"], 1.0)
        self.assertGreater(peak["mu_c"], 31.0)
        self.assertLess(peak["mu_c"], 35.5)

    def test_high_temp_guard_blocks_favorable_physics_amplification(self) -> None:
        features = {
            "unit": "C",
            "forecast_peak_temp": 33.0,
            "settlement_source_forecast_peak_c": 33.0,
            "temperature_distribution": [32.5, 33.0, 33.5],
            "observed_temp": 28.0,
            "observed_today_max_c": 28.0,
            "observation_hour_local": 13.0,
            "observation_month": 6,
            "temperature_rise_rate_c_per_hour": 0.0,
            "cloud_cover_pct": 5.0,
            "cloud_cover_low_pct": 0.0,
            "solar_radiation_observed": 780.0,
            "cti": 1.0,
        }

        peak = estimate_peak_temperature_from_features(features)
        report = compute_weather_likelihood_vector(["29°C", "32°C", "34°C or higher"], features, hours_to_event=1.0)

        self.assertTrue(peak["enabled"])
        self.assertTrue(peak["high_temp_amplification_guard_active"])
        self.assertLess(peak["mu_c"], 30.0)
        self.assertIn("high_temp_amplification_guard", report["rows"][1]["components"])
        self.assertLess(report["likelihood_probs"][2], report["likelihood_probs"][0])

    def test_precipitation_enters_independent_evaporative_cooling_group(self) -> None:
        labels = ["30°C", "35°C or higher"]
        dry = compute_weather_likelihood_vector(
            labels,
            {
                "forecast_peak_temp_c": 34.0,
                "observed_temp": 31.0,
                "observation_hour_local": 12.0,
            },
            hours_to_event=2.0,
        )
        wet = compute_weather_likelihood_vector(
            labels,
            {
                "forecast_peak_temp_c": 34.0,
                "observed_temp": 31.0,
                "observation_hour_local": 12.0,
                "radar_precip_rate_mm_per_h": 6.0,
                "is_raining_observed": True,
            },
            hours_to_event=2.0,
        )

        self.assertLess(wet["likelihood_probs"][1], dry["likelihood_probs"][1])
        hot_groups = wet["rows"][1]["component_groups"]
        self.assertIn("precip_evaporative_cooling", hot_groups["precipitation_evaporative_cooling"]["components"])
        self.assertEqual(hot_groups["cloud_shading"]["na_reason"], "low_mid_total_cloud_missing")
        self.assertEqual(
            dry["physical_evidence"]["availability"]["precipitation_evaporative_cooling"]["na_reason"],
            "real_radar_qpe_metar_or_verified_precip_missing",
        )

    def test_peak_estimator_likelihood_suppresses_unreachable_hot_bucket(self) -> None:
        report = compute_weather_likelihood_vector(
            ["31°C", "34°C", "37°C or higher"],
            {
                "unit": "C",
                "forecast_peak_temp": 36.0,
                "temperature_distribution": [35.0, 36.0, 37.0],
                "observed_temp": 30.5,
                "observation_hour_local": 12.5,
                "observation_month": 6,
                "cloud_cover_pct": 95.0,
                "wind_speed_mps": 12.0,
            },
            hours_to_event=1.0,
        )

        self.assertTrue(report["enabled"])
        self.assertIn("peak_estimator", report["rows"][0]["components"])
        self.assertLess(report["likelihood_probs"][2], report["likelihood_probs"][0])
        self.assertIn("temperature_trajectory", report["rows"][0]["component_groups"])
        self.assertIn("physical_evidence", report)

    def test_tail_support_caps_extreme_ge_bucket(self) -> None:
        report = compute_weather_likelihood_vector(
            ["25°C", "30°C or higher"],
            {
                "observed_temp": 25.0,
                "forecast_peak_temp_c": 25.0,
                "forecast_sigma_c": 0.0,
                "peak_estimator": {
                    "enabled": True,
                    "source": "test",
                    "mu_c": 24.0,
                    "sigma_c": 0.0,
                },
            },
            hours_to_event=0.5,
        )

        self.assertTrue(report["enabled"])
        self.assertTrue(report["rows"][1]["hard_zero"])
        self.assertIn("tail_support_below_bucket", report["rows"][1]["hard_reasons"])
        self.assertLess(report["likelihood_probs"][1], 1e-8)
        self.assertGreater(report["likelihood_probs"][0], report["likelihood_probs"][1] * 1e6)

    def test_peak_estimator_locks_observed_max_after_lock_hour(self) -> None:
        peak = estimate_peak_temperature_from_features(
            {
                "unit": "C",
                "forecast_peak_temp": 33.0,
                "observed_temp": 29.0,
                "observed_today_max_c": 31.2,
                "observation_hour_local": 14.25,
            }
        )

        self.assertTrue(peak["enabled"])
        self.assertEqual(peak["source"], "locked_observed")
        self.assertAlmostEqual(peak["mu_c"], 31.2)
        self.assertLessEqual(peak["sigma_c"], 0.3)

    def test_peak_estimator_uses_peak_window_before_locking_observed_max(self) -> None:
        peak = estimate_peak_temperature_from_features(
            {
                "unit": "C",
                "forecast_peak_temp": 33.0,
                "observed_temp": 31.0,
                "observed_today_max_c": 31.0,
                "observation_hour_local": 14.25,
                "expected_peak_hour_local": 15.0,
                "observation_month": 6,
            }
        )

        self.assertTrue(peak["enabled"])
        self.assertEqual(peak["source"], "energy_balance_nowcast")
        self.assertEqual(peak["peak_window_start_hour_local"], 14.0)
        self.assertEqual(peak["peak_window_end_hour_local"], 17.0)
        self.assertAlmostEqual(peak["hours_to_peak_window"], 2.75)
        self.assertGreater(peak["mu_c"], 31.0)

    def test_peak_estimator_live_regime_uses_curvature_and_peak_distribution(self) -> None:
        peak = estimate_peak_temperature_from_features(
            {
                "unit": "C",
                "forecast_peak_temp_c": 33.0,
                "forecast_sigma_c": 1.0,
                "observed_temp_c": 30.0,
                "observed_today_max_c": 30.0,
                "observed_today_max_source": "zuuu_metar_raw_awc",
                "observed_today_max_trusted": True,
                "observation_hour_local": 11.5,
                "observation_month": 7,
                "expected_peak_hour_local": 16.0,
                "expected_peak_window_start_hour_local": 15.0,
                "expected_peak_window_end_hour_local": 17.0,
                "temperature_momentum_source": "zuuu_metar_raw_awc",
                "temperature_momentum_1h_c_per_hour": 1.0,
                "temperature_momentum_2h_c_per_hour": 0.7,
                "temperature_momentum_3h_c_per_hour": 0.5,
                "temperature_momentum_posterior_gate_allowed": True,
            }
        )

        regime = peak["observed_temperature_trajectory_regime_v1"]

        self.assertFalse(peak["observed_temperature_trajectory_regime_v1_applied_to_mu"])
        self.assertTrue(regime["available"])
        self.assertEqual(regime["group"], "temperature_trajectory")
        self.assertEqual(regime["role"], "observed_realization")
        self.assertEqual(regime["source_class"], "airport_temperature_path")
        self.assertEqual(regime["regime_model_status"], "heuristic_live_no_bundle")
        self.assertGreater(len(regime["peak_hour_distribution"]), 1)
        self.assertGreater(regime["rate_hat_c_per_hour"], 0.0)
        self.assertGreater(regime["accel_hat_c_per_hour2"], 0.0)
        self.assertGreater(regime["path_mu_c"], peak["observed_temp_c"])
        self.assertEqual(regime["mean_c"], regime["path_mu_c"])
        self.assertEqual(regime["sigma_c"], regime["path_sigma_c"])
        self.assertIn("kinematic_baseline_mu", regime)
        self.assertGreater(peak["mu_c"], peak["observed_temp_c"])

    def test_peak_estimator_live_regime_flags_forced_turn_from_cloud_rain_radiation(self) -> None:
        peak = estimate_peak_temperature_from_features(
            {
                "unit": "C",
                "forecast_peak_temp_c": 33.0,
                "forecast_sigma_c": 1.0,
                "observed_temp_c": 30.0,
                "observed_today_max_c": 30.0,
                "observed_today_max_source": "zuuu_metar_raw_awc",
                "observed_today_max_trusted": True,
                "observation_hour_local": 11.5,
                "observation_month": 7,
                "expected_peak_hour_local": 16.0,
                "expected_peak_window_start_hour_local": 15.0,
                "expected_peak_window_end_hour_local": 17.0,
                "temperature_momentum_source": "zuuu_metar_raw_awc",
                "temperature_momentum_1h_c_per_hour": 0.9,
                "temperature_momentum_2h_c_per_hour": 0.7,
                "temperature_momentum_3h_c_per_hour": 0.6,
                "temperature_momentum_posterior_gate_allowed": True,
                "cloud_cover_low_pct": 90.0,
                "cloud_cover_mid_pct": 80.0,
                "radar_precip_rate_mm_per_h": 2.0,
                "is_raining_observed": True,
                "cti": 0.25,
            }
        )

        regime = peak["observed_temperature_trajectory_regime_v1"]
        probs = regime["regime_probs"]

        self.assertFalse(peak["observed_temperature_trajectory_regime_v1_applied_to_mu"])
        self.assertGreater(regime["forcing_onset_probs"]["forced_turn_probability"], 0.5)
        self.assertGreater(probs["forced_turn_risk"], probs["accelerating_warming"])

    def test_peak_estimator_soft_wu_twc_intraday_consistency_boosts_regime_confidence_only(self) -> None:
        base_features = {
            "unit": "C",
            "forecast_peak_temp_c": 33.0,
            "forecast_sigma_c": 1.0,
            "observed_temp_c": 30.0,
            "observed_today_max_c": 30.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "observed_today_max_trusted": True,
            "observation_hour_local": 11.5,
            "observation_month": 7,
            "expected_peak_hour_local": 16.0,
            "expected_peak_window_start_hour_local": 15.0,
            "expected_peak_window_end_hour_local": 17.0,
            "zuuu_metar_raw_awc_available": True,
            "zuuu_metar_raw_awc_temperature_momentum_1h_c_per_hour": 0.7,
            "zuuu_metar_raw_awc_temperature_momentum_2h_c_per_hour": 0.55,
            "zuuu_metar_raw_awc_temperature_momentum_3h_c_per_hour": 0.45,
        }
        baseline = estimate_peak_temperature_from_features(base_features)
        with_soft = estimate_peak_temperature_from_features(
            {
                **base_features,
                "wu_twc_observed_intraday_available": True,
                "wu_twc_observed_intraday_history_sample_count": 5,
                "wu_twc_observed_intraday_temperature_momentum_15m_c_per_hour": 1.2,
                "wu_twc_observed_intraday_temperature_momentum_10m_c_per_hour": 1.0,
                "wu_twc_observed_intraday_temperature_momentum_5m_c_per_hour": 0.8,
            }
        )

        baseline_regime = baseline["observed_temperature_trajectory_regime_v1"]
        soft_regime = with_soft["observed_temperature_trajectory_regime_v1"]
        soft_profile = soft_regime["soft_intraday_momentum"]
        wu_source = next(row for row in soft_profile["sources"] if row["source"] == "wu_twc_observed_intraday")

        self.assertTrue(soft_regime["available"])
        self.assertEqual(soft_profile["status"], "consistent_confidence_boost")
        self.assertEqual(soft_profile["consistent_sources"], ["wu_twc_observed_intraday"])
        self.assertGreater(soft_regime["soft_intraday_confidence_boost"], 0.0)
        self.assertGreater(soft_regime["curvature_confidence"], baseline_regime["curvature_confidence"])
        self.assertFalse(wu_source["used_for_live_momentum"])
        self.assertTrue(wu_source["used_for_regime_confidence_boost"])
        self.assertNotIn("15m", with_soft["temperature_momentum"]["rates_c_per_hour"])

    def test_peak_estimator_soft_wu_twc_intraday_conflict_does_not_boost(self) -> None:
        peak = estimate_peak_temperature_from_features(
            {
                "unit": "C",
                "forecast_peak_temp_c": 33.0,
                "forecast_sigma_c": 1.0,
                "observed_temp_c": 30.0,
                "observed_today_max_c": 30.0,
                "observed_today_max_source": "zuuu_metar_raw_awc",
                "observed_today_max_trusted": True,
                "observation_hour_local": 11.5,
                "observation_month": 7,
                "expected_peak_hour_local": 16.0,
                "expected_peak_window_start_hour_local": 15.0,
                "expected_peak_window_end_hour_local": 17.0,
                "zuuu_metar_raw_awc_available": True,
                "zuuu_metar_raw_awc_temperature_momentum_1h_c_per_hour": 0.7,
                "zuuu_metar_raw_awc_temperature_momentum_2h_c_per_hour": 0.55,
                "wu_twc_observed_intraday_available": True,
                "wu_twc_observed_intraday_history_sample_count": 5,
                "wu_twc_observed_intraday_temperature_momentum_15m_c_per_hour": -1.2,
                "wu_twc_observed_intraday_temperature_momentum_10m_c_per_hour": -1.0,
                "wu_twc_observed_intraday_temperature_momentum_5m_c_per_hour": -0.8,
            }
        )

        regime = peak["observed_temperature_trajectory_regime_v1"]
        soft_profile = regime["soft_intraday_momentum"]
        wu_source = next(row for row in soft_profile["sources"] if row["source"] == "wu_twc_observed_intraday")

        self.assertEqual(soft_profile["status"], "direction_conflict_no_boost")
        self.assertEqual(soft_profile["conflicting_sources"], ["wu_twc_observed_intraday"])
        self.assertEqual(regime["soft_intraday_confidence_boost"], 0.0)
        self.assertFalse(wu_source["used_for_regime_confidence_boost"])
        self.assertFalse(wu_source["used_for_live_momentum"])

    def test_peak_estimator_regime_path_mu_is_weighted_not_global_peak_hour_max(self) -> None:
        peak = estimate_peak_temperature_from_features(
            {
                "unit": "C",
                "forecast_peak_temp_c": 34.0,
                "forecast_sigma_c": 1.0,
                "observed_temp_c": 30.0,
                "observed_today_max_c": 30.0,
                "observed_today_max_source": "zuuu_metar_raw_awc",
                "observed_today_max_trusted": True,
                "observation_hour_local": 11.0,
                "observation_month": 7,
                "expected_peak_hour_local": 16.0,
                "peak_hour_distribution": {15: 0.9, 17: 0.1},
                "temperature_momentum_source": "zuuu_metar_raw_awc",
                "temperature_momentum_1h_c_per_hour": 1.1,
                "temperature_momentum_2h_c_per_hour": 0.8,
                "temperature_momentum_3h_c_per_hour": 0.6,
                "temperature_momentum_posterior_gate_allowed": True,
            }
        )

        regime = peak["observed_temperature_trajectory_regime_v1"]

        self.assertTrue(regime["available"])
        self.assertEqual(regime["path_aggregation"], "deterministic_path_monte_carlo_max")
        self.assertGreaterEqual(regime["path_draw_count"], 64)
        self.assertLess(regime["path_mu_c"], regime["path_upper_candidate_c"])
        self.assertGreater(regime["path_upper_candidate_c"], peak["observed_temp_c"])

    def test_peak_estimator_regime_uses_local_quadratic_observed_temperature_path(self) -> None:
        peak = estimate_peak_temperature_from_features(
            {
                "unit": "C",
                "forecast_peak_temp_c": 33.0,
                "forecast_sigma_c": 1.0,
                "observed_temp_c": 30.0,
                "observed_today_max_c": 30.0,
                "observed_today_max_source": "zuuu_metar_raw_awc",
                "observed_today_max_trusted": True,
                "observation_hour_local": 12.0,
                "observation_month": 7,
                "expected_peak_hour_local": 16.0,
                "expected_peak_window_start_hour_local": 15.0,
                "expected_peak_window_end_hour_local": 17.0,
                "temperature_path_observations": [
                    {"hour_local": 10.0, "temp_c": 27.8},
                    {"hour_local": 11.0, "temp_c": 29.1},
                    {"hour_local": 12.0, "temp_c": 30.0},
                ],
            }
        )

        regime = peak["observed_temperature_trajectory_regime_v1"]
        fit = regime["local_quadratic_fit"]

        self.assertFalse(peak["observed_temperature_trajectory_regime_v1_applied_to_mu"])
        self.assertTrue(fit["available"])
        self.assertEqual(fit["source"], "local_quadratic_observed_temperature_path")
        self.assertGreater(regime["rate_hat_c_per_hour"], 0.0)
        self.assertLess(regime["accel_hat_c_per_hour2"], 0.0)

    def test_peak_estimator_regime_reads_awc_today_temperature_points_with_utc_at(self) -> None:
        peak = estimate_peak_temperature_from_features(
            {
                "unit": "C",
                "forecast_peak_temp_c": 32.0,
                "forecast_sigma_c": 1.0,
                "observed_temp_c": 28.0,
                "observed_today_max_c": 28.0,
                "observed_today_max_source": "zuuu_metar_raw_awc",
                "observed_today_max_trusted": True,
                "observation_hour_local": 10.0,
                "observation_hour_local_timezone": "Asia/Shanghai",
                "observation_month": 7,
                "expected_peak_hour_local": 15.0,
                "expected_peak_window_start_hour_local": 14.0,
                "expected_peak_window_end_hour_local": 17.0,
                "zuuu_metar_raw_awc_today_temperature_points": [
                    {"at": "2026-07-02T00:00:00+00:00", "temp_c": 26.0},
                    {"at": "2026-07-02T01:00:00+00:00", "temp_c": 27.0},
                    {"at": "2026-07-02T02:00:00+00:00", "temp_c": 28.0},
                ],
            }
        )

        fit = peak["observed_temperature_trajectory_regime_v1"]["local_quadratic_fit"]

        self.assertTrue(fit["available"])
        self.assertEqual(fit["point_count"], 3)
        self.assertEqual([row["hour_local"] for row in fit["points"]], [8.0, 9.0, 10.0])
        self.assertGreater(peak["observed_temperature_trajectory_regime_v1"]["rate_hat_c_per_hour"], 0.0)

    def test_peak_estimator_zero_precip_has_no_evaporative_cooling_floor(self) -> None:
        peak = estimate_peak_temperature_from_features(
            {
                "unit": "C",
                "forecast_peak_temp_c": 32.0,
                "forecast_sigma_c": 1.0,
                "observed_temp_c": 28.0,
                "observed_today_max_c": 28.0,
                "observed_today_max_source": "zuuu_metar_raw_awc",
                "observed_today_max_trusted": True,
                "observation_hour_local": 10.0,
                "observation_month": 7,
                "expected_peak_hour_local": 15.0,
                "precipitation_rate_mm_per_h": 0.0,
                "temperature_path_observations": [
                    {"hour_local": 8.0, "temp_c": 26.0},
                    {"hour_local": 9.0, "temp_c": 27.0},
                    {"hour_local": 10.0, "temp_c": 28.0},
                ],
            }
        )

        self.assertEqual(peak["precip_evaporative_cooling_c"], 0.0)
        regime = peak["observed_temperature_trajectory_regime_v1"]
        self.assertEqual(regime["forcing_onset_probs"]["precip_pressure"], 0.0)

    def test_peak_estimator_regime_uses_future_hourly_forcing_pressure(self) -> None:
        peak = estimate_peak_temperature_from_features(
            {
                "unit": "C",
                "forecast_peak_temp_c": 33.0,
                "forecast_sigma_c": 1.0,
                "observed_temp_c": 30.0,
                "observed_today_max_c": 30.0,
                "observed_today_max_source": "zuuu_metar_raw_awc",
                "observed_today_max_trusted": True,
                "observation_hour_local": 11.5,
                "observation_month": 7,
                "expected_peak_hour_local": 16.0,
                "expected_peak_window_start_hour_local": 15.0,
                "expected_peak_window_end_hour_local": 17.0,
                "temperature_momentum_source": "zuuu_metar_raw_awc",
                "temperature_momentum_1h_c_per_hour": 0.9,
                "temperature_momentum_2h_c_per_hour": 0.7,
                "temperature_momentum_3h_c_per_hour": 0.6,
                "temperature_momentum_posterior_gate_allowed": True,
                "precip_rate_hourly_mm_per_h": {14: 2.5, 15: 1.5},
            }
        )

        forcing = peak["observed_temperature_trajectory_regime_v1"]["forcing_onset_probs"]

        self.assertGreater(forcing["precip_pressure"], 0.5)
        self.assertEqual(forcing["precipitation"]["hourly_source"], "precip_rate_hourly_mm_per_h")
        self.assertGreater(forcing["forced_turn_probability"], 0.5)

    def test_peak_estimator_regime_uses_layered_cloud_forcing_pressure(self) -> None:
        base_features = {
            "unit": "C",
            "forecast_peak_temp_c": 33.0,
            "forecast_sigma_c": 1.0,
            "observed_temp_c": 30.0,
            "observed_today_max_c": 30.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "observed_today_max_trusted": True,
            "observation_hour_local": 11.5,
            "observation_month": 7,
            "expected_peak_hour_local": 16.0,
            "expected_peak_window_start_hour_local": 15.0,
            "expected_peak_window_end_hour_local": 17.0,
            "temperature_momentum_source": "zuuu_metar_raw_awc",
            "temperature_momentum_1h_c_per_hour": 0.9,
            "temperature_momentum_2h_c_per_hour": 0.7,
            "temperature_momentum_3h_c_per_hour": 0.6,
            "temperature_momentum_posterior_gate_allowed": True,
        }
        clear_peak = estimate_peak_temperature_from_features(base_features)
        cloudy_peak = estimate_peak_temperature_from_features(
            {
                **base_features,
                "low_cloud_hourly_pct": [
                    {"hour_local": 13.0, "low_cloud_pct": 82.0},
                    {"hour_local": 15.0, "low_cloud_pct": 88.0},
                ],
                "mid_cloud_hourly_pct": [{"hour_local": 14.0, "mid_cloud_pct": 70.0}],
                "high_cloud_hourly_pct": [{"hour_local": 15.0, "high_cloud_pct": 95.0}],
            }
        )

        clear_regime = clear_peak["observed_temperature_trajectory_regime_v1"]
        cloudy_regime = cloudy_peak["observed_temperature_trajectory_regime_v1"]
        forcing = cloudy_regime["forcing_onset_probs"]

        self.assertGreater(forcing["low_cloud_pressure"], forcing["high_cloud_pressure"])
        self.assertGreater(forcing["cloud_pressure"], 0.7)
        self.assertEqual(
            forcing["cloud_layers"]["low"]["hourly_source"],
            "low_cloud_hourly_pct",
        )
        self.assertGreater(forcing["forced_turn_probability"], 0.7)
        self.assertLessEqual(cloudy_regime["path_mu_c"], clear_regime["path_mu_c"])

    def test_peak_estimator_regime_parses_hourly_precip_list_timing(self) -> None:
        peak = estimate_peak_temperature_from_features(
            {
                "unit": "C",
                "forecast_peak_temp_c": 33.0,
                "forecast_sigma_c": 1.0,
                "observed_temp_c": 30.0,
                "observed_today_max_c": 30.0,
                "observed_today_max_source": "zuuu_metar_raw_awc",
                "observed_today_max_trusted": True,
                "observation_hour_local": 11.5,
                "observation_month": 7,
                "expected_peak_hour_local": 16.0,
                "expected_peak_window_start_hour_local": 15.0,
                "expected_peak_window_end_hour_local": 17.0,
                "temperature_momentum_source": "zuuu_metar_raw_awc",
                "temperature_momentum_1h_c_per_hour": 0.9,
                "temperature_momentum_2h_c_per_hour": 0.7,
                "temperature_momentum_3h_c_per_hour": 0.6,
                "temperature_momentum_posterior_gate_allowed": True,
                "precip_rate_hourly_mm_per_h": [
                    {"hour_local": 14.0, "precip_rate_mm_per_h": 2.5},
                    {"hour_local": 15.0, "precip_rate_mm_per_h": 1.5},
                ],
            }
        )

        forcing = peak["observed_temperature_trajectory_regime_v1"]["forcing_onset_probs"]

        self.assertGreater(forcing["precip_pressure"], 0.5)
        self.assertEqual(forcing["precipitation"]["hourly_source"], "precip_rate_hourly_mm_per_h")
        self.assertEqual(
            forcing["precipitation"]["onset_distribution"][0]["hour_local"],
            14.0,
        )
        self.assertGreater(forcing["forced_turn_probability"], 0.5)

    def test_peak_estimator_uses_awc_context_momentum_watch_before_peak_window_end(self) -> None:
        peak = estimate_peak_temperature_from_features(
            {
                "unit": "C",
                "forecast_peak_temp": 31.0,
                "observed_temp_c": 31.0,
                "observed_temp_source": "zuuu_metar_raw_awc",
                "observed_today_max_c": 31.0,
                "observed_today_max_source": "zuuu_metar_raw_awc",
                "observed_today_max_trusted": True,
                "observation_hour_local": 14.0,
                "expected_peak_hour_local": 15.0,
                "observation_month": 6,
                "temperature_momentum_source": "zuuu_metar_raw_awc",
                "temperature_momentum_1h_c_per_hour": 0.0,
                "temperature_momentum_2h_c_per_hour": 1.0,
                "temperature_momentum_3h_c_per_hour": 0.67,
                "temperature_momentum_6h_c_per_hour": 1.0,
                "temperature_momentum_12h_c_per_hour": 0.92,
                "temperature_rise_rate_c_per_hour": 0.0,
            }
        )

        self.assertTrue(peak["enabled"])
        self.assertEqual(peak["source"], "energy_balance_nowcast")
        self.assertEqual(peak["temperature_momentum_realization_state"], "context_warming_watch")
        self.assertTrue(peak["temperature_momentum_context_warming_watch"])
        self.assertTrue(peak["energy_balance_terms"]["temperature_trajectory_uses_context_momentum"])
        self.assertFalse(peak["observed_temperature_trajectory_regime_v1_applied_to_mu"])
        self.assertTrue(peak["energy_balance_terms"]["observed_temperature_trajectory_regime_v1_source_component_enabled"])
        self.assertFalse(peak["energy_balance_terms"]["legacy_peak_trajectory_enabled"])
        self.assertGreater(
            peak["observed_temperature_trajectory_regime_v1"]["path_delta_c"],
            0.0,
        )

    def test_peak_estimator_mu_never_below_hard_observed_today_max(self) -> None:
        peak = estimate_peak_temperature_from_features(
            {
                "unit": "C",
                "forecast_peak_temp": 30.0,
                "observed_temp_c": 33.0,
                "observed_temp_source": "zuuu_metar_raw_awc",
                "observed_today_max_c": 33.0,
                "observed_today_max_source": "zuuu_metar_raw_awc",
                "observed_today_max_trusted": True,
                "observation_hour_local": 16.75,
                "expected_peak_hour_local": 15.0,
                "observation_month": 6,
                "temperature_momentum_source": "zuuu_metar_raw_awc",
                "temperature_momentum_1h_c_per_hour": 0.0,
                "temperature_momentum_2h_c_per_hour": 1.0,
                "temperature_momentum_3h_c_per_hour": 0.67,
                "temperature_momentum_6h_c_per_hour": 1.0,
            }
        )

        self.assertTrue(peak["enabled"])
        self.assertGreaterEqual(peak["mu_c"], 33.0)

    def test_peak_estimator_projects_breakout_after_lock_when_hard_momentum_warms(self) -> None:
        peak = estimate_peak_temperature_from_features(
            {
                "unit": "C",
                "forecast_peak_temp": 31.0,
                "observed_temp_c": 31.0,
                "observed_temp_source": "zuuu_metar_raw_awc",
                "observed_today_max_c": 31.0,
                "observed_today_max_source": "zuuu_metar_raw_awc",
                "observed_today_max_trusted": True,
                "zuuu_metar_raw_awc_available": True,
                "zuuu_metar_raw_awc_latest_temp_c": 31.0,
                "zuuu_metar_raw_awc_today_max_c": 31.0,
                "observation_hour_local": 14.25,
                "expected_peak_hour_local": 16.0,
                "observation_month": 6,
                "temperature_momentum_source": "zuuu_metar_raw_awc",
                "temperature_momentum_1h_c_per_hour": 1.2,
                "temperature_momentum_effective_c_per_hour": 1.2,
                "temperature_momentum_realization_state": "emerging_warming",
                "temperature_rise_rate_c_per_hour": 1.2,
                "temperature_rise_rate_source": "zuuu_metar_raw_awc:1h",
            }
        )

        self.assertTrue(peak["enabled"])
        self.assertEqual(peak["source"], "energy_balance_nowcast")
        self.assertFalse(peak["observed_temperature_trajectory_regime_v1_applied_to_mu"])
        self.assertTrue(peak["energy_balance_terms"]["observed_temperature_trajectory_regime_v1_source_component_enabled"])
        self.assertGreater(
            peak["observed_temperature_trajectory_regime_v1"]["path_delta_c"],
            0.0,
        )

    def test_peak_estimator_does_not_lock_deformed_wu_twc_intraday_max(self) -> None:
        peak = estimate_peak_temperature_from_features(
            {
                "unit": "C",
                "forecast_peak_temp": 27.6,
                "observed_temp": 27.78,
                "observed_today_max_c": 29.0,
                "observed_today_max_source": "wu_twc_observed_intraday_current_metric_max_since7am",
                "wu_twc_observed_intraday_today_max_c": 29.0,
                "zuuu_metar_raw_awc_today_max_c": 28.0,
                "observation_hour_local": 15.25,
                "weather_source_delay_deformation_alert": {
                    "alert": True,
                    "codes": ["wu_nowcast_temperature_deformation"],
                },
            }
        )

        self.assertTrue(peak["enabled"])
        self.assertEqual(peak["source"], "locked_trusted_observed")
        self.assertAlmostEqual(peak["mu_c"], 28.0)
        self.assertEqual(peak["observed_today_max_source"], "zuuu_metar_raw_awc_today_max_c")
        self.assertEqual(peak["raw_observed_today_max_demoted_reason"], "wu_twc_intraday_deformation_alert")

    def test_probability_runtime_uses_physical_likelihood_as_extra_evidence(self) -> None:
        os.environ["PM_BAYES_PROBABILITY_ENABLED"] = "1"
        os.environ["PM_BAYES_LIKELIHOOD_ENABLED"] = "1"
        os.environ["PM_BAYES_LIKELIHOOD_EVIDENCE"] = "24"
        items = [
            {
                "label": "24°C or below",
                "q": {"q_round": 0.50, "q_raw": 0.50},
                "per_model_q": {"m1": {"q_round": 0.50}},
                "active_models": ["m1"],
                "bayes_likelihood_features": {"dewpoint_c": 25.1, "forecast_peak_temp_c": 28.0},
            },
            {
                "label": "26°C or higher",
                "q": {"q_round": 0.50, "q_raw": 0.50},
                "per_model_q": {"m1": {"q_round": 0.50}},
                "active_models": ["m1"],
            },
        ]

        meta = apply_bayesian_probability_update(items, weights={"m1": 1.0}, days_to_event=0.1)

        self.assertTrue(meta["physical_likelihood_enabled"])
        self.assertEqual(meta["physical_likelihood_source"], "weather_physical_likelihood")
        self.assertGreater(items[1]["q"]["q_bayes_posterior"], items[0]["q"]["q_bayes_posterior"])
        self.assertTrue(items[0]["q_bayes_meta"]["physical_likelihood_enabled"])
        self.assertLess(items[0]["q_bayes_meta"]["physical_likelihood_prob"], 1e-6)
        physical_row = items[0]["q_bayes_meta"]["physical_likelihood_row"]
        self.assertTrue(physical_row["hard_zero"])
        self.assertIn("dewpoint_above_bucket", physical_row["hard_reasons"])
        self.assertIn("dewpoint", physical_row["components"])
        self.assertTrue(items[0]["q_bayes_physical_likelihood_hard_zero"])
        self.assertIn("dewpoint_above_bucket", items[0]["q_bayes_physical_likelihood_hard_reasons"])
        self.assertIn("dewpoint", items[0]["q_bayes_physical_likelihood_components"])
        self.assertIn("dewpoint", items[0]["q"]["q_bayes_physical_likelihood_components"])
        self.assertIn("wet_bulb_constraint", items[0]["q_bayes_physical_likelihood_component_groups"])
        self.assertIn(
            "wet_bulb_constraint",
            items[0]["q"]["q_bayes_physical_likelihood_component_groups"],
        )
        self.assertTrue(math.isclose(sum(item["q"]["q_raw"] for item in items), 1.0, rel_tol=0, abs_tol=1e-12))

    def test_probability_runtime_allows_small_physical_likelihood_for_chengdu_d0_scope(self) -> None:
        os.environ["PM_BAYES_PROBABILITY_ENABLED"] = "1"
        os.environ["PM_BAYES_LIKELIHOOD_ENABLED"] = "1"
        os.environ["PM_BAYES_LIKELIHOOD_EVIDENCE"] = "1"
        os.environ["PM_BAYES_LIKELIHOOD_CITY_ALLOWLIST"] = "chengdu"
        os.environ["PM_BAYES_LIKELIHOOD_STATION_ALLOWLIST"] = "ZUUU"
        os.environ["PM_BAYES_LIKELIHOOD_D0_ONLY"] = "1"
        os.environ["PM_BAYES_LIKELIHOOD_D0_MAX_DAYS"] = "0.75"
        items = [
            {
                "label": "30°C",
                "q": {"q_round": 0.50, "q_raw": 0.50},
                "per_model_q": {"m1": {"q_round": 0.50}},
                "active_models": ["m1"],
                "weather_likelihood_features": {
                    "city": "chengdu",
                    "station_id": "ZUUU",
                    "forecast_peak_temp_c": 31.0,
                    "observed_temp_c": 30.0,
                    "observed_today_max_c": 30.5,
                    "observation_hour_local": 14.0,
                    "dewpoint_c": 22.0,
                },
            },
            {
                "label": "31°C",
                "q": {"q_round": 0.50, "q_raw": 0.50},
                "per_model_q": {"m1": {"q_round": 0.50}},
                "active_models": ["m1"],
            },
        ]

        meta = apply_bayesian_probability_update(items, weights={"m1": 1.0}, days_to_event=0.2)

        self.assertTrue(meta["physical_likelihood_scope_allowed"])
        self.assertEqual(meta["physical_likelihood_scope_reason"], "station_allowlist")
        self.assertTrue(meta["physical_likelihood_stage_allowed"])
        self.assertEqual(meta["physical_likelihood_stage_reason"], "d0_window")
        self.assertGreater(meta["physical_likelihood_evidence"], 0.0)
        self.assertLessEqual(meta["physical_likelihood_evidence"], 1.0)
        self.assertTrue(items[0]["q_bayes_meta"]["physical_likelihood_enabled"])

    def test_probability_runtime_blocks_physical_likelihood_outside_d0_scope(self) -> None:
        os.environ["PM_BAYES_PROBABILITY_ENABLED"] = "1"
        os.environ["PM_BAYES_LIKELIHOOD_ENABLED"] = "1"
        os.environ["PM_BAYES_LIKELIHOOD_EVIDENCE"] = "1"
        os.environ["PM_BAYES_LIKELIHOOD_CITY_ALLOWLIST"] = "chengdu"
        os.environ["PM_BAYES_LIKELIHOOD_STATION_ALLOWLIST"] = "ZUUU"
        os.environ["PM_BAYES_LIKELIHOOD_D0_ONLY"] = "1"
        os.environ["PM_BAYES_LIKELIHOOD_D0_MAX_DAYS"] = "0.75"
        items = [
            {
                "label": "30°C",
                "q": {"q_round": 0.50, "q_raw": 0.50},
                "per_model_q": {"m1": {"q_round": 0.50}},
                "active_models": ["m1"],
                "weather_likelihood_features": {
                    "city": "chengdu",
                    "station_id": "ZUUU",
                    "forecast_peak_temp_c": 31.0,
                    "observed_temp_c": 30.0,
                    "observed_today_max_c": 30.5,
                    "observation_hour_local": 14.0,
                    "dewpoint_c": 22.0,
                },
            },
            {
                "label": "31°C",
                "q": {"q_round": 0.50, "q_raw": 0.50},
                "per_model_q": {"m1": {"q_round": 0.50}},
                "active_models": ["m1"],
            },
        ]

        meta = apply_bayesian_probability_update(items, weights={"m1": 1.0}, days_to_event=1.2)

        self.assertTrue(meta["physical_likelihood_scope_allowed"])
        self.assertFalse(meta["physical_likelihood_stage_allowed"])
        self.assertEqual(meta["physical_likelihood_stage_reason"], "outside_d0_window")
        self.assertEqual(meta["physical_likelihood_evidence"], 0.0)
        self.assertFalse(items[0]["q_bayes_meta"]["physical_likelihood_enabled"])

    def test_model_distribution_only_features_do_not_add_uniform_evidence(self) -> None:
        os.environ["PM_BAYES_PROBABILITY_ENABLED"] = "1"
        os.environ["PM_BAYES_LIKELIHOOD_ENABLED"] = "1"
        items = [
            {
                "label": "24°C",
                "q": {"q_round": 0.20, "q_raw": 0.20},
                "per_model_q": {"m1": {"q_round": 0.20}},
                "active_models": ["m1"],
                "weather_likelihood_features": {
                    "unit": "C",
                    "forecast_peak_temp": 27.0,
                    "temperature_distribution": [26.5, 27.0, 27.5],
                },
            },
            {
                "label": "27°C",
                "q": {"q_round": 0.80, "q_raw": 0.80},
                "per_model_q": {"m1": {"q_round": 0.80}},
                "active_models": ["m1"],
            },
        ]

        meta = apply_bayesian_probability_update(items, weights={"m1": 1.0}, days_to_event=1.0)

        self.assertFalse(meta["physical_likelihood_enabled"])
        self.assertEqual(meta["physical_likelihood_evidence"], 0.0)

    def test_wu_forecast_peak_is_not_physical_residual_by_default(self) -> None:
        report = compute_weather_likelihood_vector(
            ["29°C", "31°C"],
            {
                "settlement_source_forecast_peak_c": 31.0,
                "wu_forecast_peak_c": 31.0,
                "wu_forecast_peak_soft": True,
                "wu_forecast_peak_usable_for_settlement": False,
            },
            hours_to_event=1.0,
            base_probs=[0.5, 0.5],
        )

        self.assertTrue(report["enabled"])
        self.assertFalse(report["probability_evidence_enabled"])
        self.assertEqual(report["active_evidence_count"], 0)
        for row in report["rows"]:
            groups = row["component_groups"]
            residual = groups["residual_correction"]["components"]
            self.assertNotIn("settlement_source_forecast_peak", residual)

    def test_scan_builds_standard_weather_likelihood_features(self) -> None:
        features = _build_weather_likelihood_features(
            model_members={"m1": [28.0, 29.0], "m2": [30.0]},
            market_var="temperature_2m_max",
            market_unit="C",
            observed_nowcast_value=29.5,
            observed_nowcast_share=0.8,
            auxiliary_features={
                "source": "open_meteo_forecast_aux",
                "dewpoint_c": 26.0,
                "cti": 0.7,
                "precipitation_rate_mm_per_h": 1.2,
                "observation_hour_local": 11.0,
                "observation_month": 6,
            },
        )

        self.assertEqual(features["source"], "scan_model_members")
        self.assertEqual(features["market_var"], "temperature_2m_max")
        self.assertEqual(features["observed_temp"], 29.5)
        self.assertEqual(features["dewpoint_c"], 26.0)
        self.assertEqual(features["cti"], 0.7)
        self.assertEqual(features["precipitation_rate_mm_per_h"], 1.2)
        self.assertEqual(features["auxiliary_source"], "open_meteo_forecast_aux")
        self.assertEqual(features["temperature_distribution"], [28.0, 29.0, 30.0])
        self.assertIn("peak_estimator", features)
        self.assertTrue(features["peak_estimator_enabled"])
        self.assertEqual(features["cloud_shading_na_reason"], "low_mid_total_cloud_missing")
        groups = _weather_likelihood_feature_group_availability(features)
        self.assertFalse(groups["precipitation_evaporative_cooling"]["available"])
        self.assertEqual(groups["precipitation_evaporative_cooling"]["evidence_state"], "proxy")
        self.assertEqual(_weather_likelihood_feature_quality(features), "partial")
        complete_features = dict(
            features,
            cloud_cover_low_pct=30.0,
            wind_speed_mps=4.0,
            wind_direction_deg=210.0,
        )
        self.assertEqual(_weather_likelihood_feature_quality(complete_features), "partial")
        complete_features["radar_precip_rate_mm_per_h"] = 0.0
        complete_features["radar_qpe_proxy_source"] = "mrms_qpe"
        self.assertEqual(_weather_likelihood_feature_quality(complete_features), "complete")
        compact = _compact_weather_likelihood_features(
            {
                **complete_features,
                "temperature_distribution": list(range(700)),
                "raw_response": {"ignored": True},
            }
        )
        self.assertNotIn("raw_response", compact)
        self.assertLessEqual(len(compact["temperature_distribution"]), 512)
        self.assertEqual(compact["temperature_distribution_truncated_count"], 188)
        self.assertEqual(
            _build_weather_likelihood_features(
                model_members={"m1": [28.0]},
                market_var="temperature_2m_min",
                market_unit="C",
            ),
            {},
        )

    def test_wu_nowcast_observation_is_not_overwritten_by_aux_temperature(self) -> None:
        features = _build_weather_likelihood_features(
            model_members={"m1": [29.0, 30.0], "m2": [31.0]},
            market_var="temperature_2m_max",
            market_unit="C",
            observed_nowcast_value=30.0,
            observed_nowcast_share=0.5,
            observed_nowcast_source="wunderground_current_station_max_since7am",
            auxiliary_features={
                "source": "metar_aviationweather",
                "observed_temp_c": 22.0,
                "metar_temperature_c": 22.0,
                "observation_hour_local": 14.0,
            },
        )

        self.assertEqual(features["observed_temp_c"], 30.0)
        self.assertEqual(features["observed_today_max_c"], 30.0)
        self.assertEqual(features["observed_today_max_source"], "wunderground_current_station_max_since7am")
        self.assertEqual(features["metar_temperature_c"], 22.0)

    def test_zuuu_awc_hard_observed_overrides_wu_history_nowcast(self) -> None:
        features = _build_weather_likelihood_features(
            model_members={"m1": [31.0, 32.0], "m2": [33.0]},
            market_var="temperature_2m_max",
            market_unit="C",
            observed_nowcast_value=27.77777777777778,
            observed_nowcast_share=0.6,
            observed_nowcast_source="wunderground_history",
            auxiliary_features={
                "source": "zuuu_metar_raw_awc",
                "zuuu_metar_raw_awc_available": True,
                "zuuu_metar_raw_awc_latest_raw": "METAR ZUUU 130700Z 18003MPS 150V230 CAVOK 31/18 Q1008 NOSIG",
                "zuuu_metar_raw_awc_latest_temp_c": 31.0,
                "zuuu_metar_raw_awc_today_max_c": 31.0,
                "observed_temp": 31.0,
                "observed_temp_c": 31.0,
                "observed_temp_source": "zuuu_metar_raw_awc",
                "metar_temperature_c": 31.0,
                "observed_today_max_c": 31.0,
                "observed_today_max_source": "zuuu_metar_raw_awc",
                "observed_today_max_trusted": True,
            },
        )

        self.assertEqual(features["observed_temp_c"], 31.0)
        self.assertEqual(features["observed_temp_source"], "zuuu_metar_raw_awc")
        self.assertEqual(features["observed_today_max_c"], 31.0)
        self.assertEqual(features["observed_today_max_source"], "zuuu_metar_raw_awc")
        self.assertEqual(features["metar_temperature_c"], 31.0)

    def test_hard_awc_namespace_promotes_observation_path_over_wu_generic(self) -> None:
        features = _build_weather_likelihood_features(
            model_members={"m1": [31.0, 32.0], "m2": [33.0]},
            market_var="temperature_2m_max",
            market_unit="C",
            observed_nowcast_value=None,
            auxiliary_features={
                "source": "combined_observed_features",
                "observed_temp": 32.2,
                "observed_temp_c": 32.2,
                "observed_temp_source": "wu_twc_observed_intraday_current",
                "observed_today_max_c": 32.2,
                "observed_today_max_source": "wu_twc_observed_intraday_max_since7am",
                "temperature_momentum_source": "wu_twc_observed_intraday",
                "temperature_momentum_1h_c_per_hour": 1.0,
                "temperature_trajectory_stable_rate_c_per_hour": 1.0,
                "zuuu_metar_raw_awc_available": True,
                "zuuu_metar_raw_awc_latest_temp_c": 33.0,
                "zuuu_metar_raw_awc_today_max_c": 33.0,
                "zuuu_metar_raw_awc_temperature_momentum_1h_c_per_hour": 2.0,
                "zuuu_metar_raw_awc_temperature_momentum_effective_c_per_hour": 2.0,
                "zuuu_metar_raw_awc_temperature_momentum_realization_state": "emerging_warming",
                "zuuu_metar_raw_awc_temperature_momentum_posterior_gate_allowed": True,
                "zuuu_metar_raw_awc_temperature_trajectory_stable_rate_c_per_hour": 2.0,
                "zuuu_metar_raw_awc_temperature_trajectory_stable_rate_source": "zuuu_metar_raw_awc:1h",
                "zuuu_metar_raw_awc_temperature_trajectory_stable_rate_window": "1h",
                "observation_hour_local": 15.0,
                "observation_month": 6,
            },
        )

        self.assertEqual(features["observed_temp_c"], 33.0)
        self.assertEqual(features["observed_temp_source"], "zuuu_metar_raw_awc")
        self.assertEqual(features["observed_today_max_c"], 33.0)
        self.assertEqual(features["observed_today_max_source"], "zuuu_metar_raw_awc")
        self.assertEqual(features["temperature_momentum_source"], "zuuu_metar_raw_awc")
        self.assertEqual(features["temperature_momentum_1h_c_per_hour"], 2.0)
        self.assertEqual(features["temperature_rise_rate_source"], "zuuu_metar_raw_awc:1h")
        self.assertEqual(features["observed_temp_preserved_source"], "wu_twc_observed_intraday_current")

    def test_hourly_metar_momentum_does_not_fabricate_subhour_windows(self) -> None:
        pairs = [
            (datetime(2026, 6, 29, 6, 0, tzinfo=timezone.utc), 31.0),
            (datetime(2026, 6, 29, 7, 0, tzinfo=timezone.utc), 33.0),
        ]
        momentum = _temperature_momentum_from_pairs(pairs, source="zuuu_metar_raw_awc")

        self.assertEqual(momentum["temperature_momentum_1h_c_per_hour"], 2.0)
        self.assertNotIn("temperature_momentum_45m_c_per_hour", momentum)
        self.assertNotIn("temperature_momentum_30m_c_per_hour", momentum)
        self.assertFalse(momentum["temperature_momentum_posterior_gate_allowed"])
        self.assertEqual(
            momentum["temperature_momentum_posterior_gate_reason"],
            "one_hour_requires_30m_45m_confirmation",
        )

    def test_hourly_metar_momentum_allows_one_hour_with_positive_background(self) -> None:
        pairs = [
            (datetime(2026, 6, 29, 4, 0, tzinfo=timezone.utc), 23.0),
            (datetime(2026, 6, 29, 5, 0, tzinfo=timezone.utc), 24.0),
            (datetime(2026, 6, 29, 6, 0, tzinfo=timezone.utc), 24.0),
            (datetime(2026, 6, 29, 7, 0, tzinfo=timezone.utc), 26.0),
        ]
        momentum = _temperature_momentum_from_pairs(pairs, source="zuuu_metar_raw_awc")

        self.assertEqual(momentum["temperature_momentum_1h_c_per_hour"], 2.0)
        self.assertGreater(momentum["temperature_momentum_2h_c_per_hour"], 0.0)
        self.assertGreater(momentum["temperature_momentum_3h_c_per_hour"], 0.0)
        self.assertTrue(momentum["temperature_momentum_posterior_gate_allowed"])
        self.assertEqual(
            momentum["temperature_momentum_posterior_gate_reason"],
            "one_hour_context_confirmed",
        )
        self.assertEqual(momentum["temperature_momentum_posterior_confirmed_windows"], "1h,2h,3h")

    def test_peak_estimator_does_not_use_short_window_momentum_without_confirmed_window(self) -> None:
        peak = estimate_peak_temperature_from_features(
            {
                "observed_temp_c": 30.0,
                "observed_today_max_c": 30.0,
                "observed_today_max_source": "zuuu_metar_raw_awc",
                "forecast_peak_temp_c": 31.0,
                "forecast_sigma_c": 1.0,
                "observation_hour_local": 10.0,
                "observation_month": 6,
                "expected_peak_hour_local": 14.0,
                "temperature_momentum_15m_c_per_hour": 4.0,
                "temperature_momentum_10m_c_per_hour": 4.5,
                "temperature_momentum_realization_state": "emerging_warming",
            }
        )

        terms = peak["energy_balance_terms"]
        self.assertTrue(peak["enabled"])
        self.assertFalse(terms["temperature_trajectory_uses_confirmed_momentum"])
        self.assertEqual(terms["temperature_trajectory_confirmed_momentum_windows"], "")
        self.assertIsNone(terms["temperature_trajectory_rate_c_per_hour"])
        self.assertIsNone(terms["temperature_trajectory_delta_c"])

    def test_peak_estimator_uses_one_hour_awc_momentum_with_background_context_in_regime(self) -> None:
        peak = estimate_peak_temperature_from_features(
            {
                "observed_temp_c": 30.0,
                "observed_today_max_c": 30.0,
                "observed_today_max_source": "zuuu_metar_raw_awc",
                "forecast_peak_temp_c": 32.46,
                "forecast_sigma_c": 1.0,
                "observation_hour_local": 11.0,
                "observation_month": 7,
                "expected_peak_hour_local": 15.0,
                "temperature_momentum_source": "zuuu_metar_raw_awc",
                "temperature_momentum_6h_c_per_hour": 0.7,
                "temperature_momentum_3h_c_per_hour": 0.8,
                "temperature_momentum_2h_c_per_hour": 0.9,
                "temperature_momentum_1h_c_per_hour": 2.0,
                "temperature_momentum_posterior_gate_allowed": True,
                "temperature_momentum_realization_state": "emerging_warming",
                "temperature_rise_rate_c_per_hour": 2.0,
                "temperature_rise_rate_source": "zuuu_metar_raw_awc:1h",
                "temperature_rise_rate_window": "1h",
            }
        )

        terms = peak["energy_balance_terms"]
        self.assertTrue(peak["enabled"])
        self.assertTrue(terms["temperature_trajectory_uses_confirmed_momentum"])
        self.assertEqual(terms["temperature_trajectory_confirmed_momentum_windows"], "1h,2h,3h,6h")
        self.assertFalse(terms["temperature_trajectory_short_window_suppressed"])
        self.assertEqual(terms["temperature_trajectory_suppressed_reason"], "")
        self.assertFalse(peak["observed_temperature_trajectory_regime_v1_applied_to_mu"])
        self.assertIsNone(terms["temperature_trajectory_delta_c"])
        self.assertTrue(terms["observed_temperature_trajectory_regime_v1_source_component_enabled"])
        self.assertFalse(terms["legacy_peak_trajectory_enabled"])
        self.assertGreater(
            peak["observed_temperature_trajectory_regime_v1"]["rate_hat_c_per_hour"],
            1.0,
        )
        self.assertTrue(
            peak["observed_temperature_trajectory_regime_v1"][
                "warming_acceleration_confirmed_by_context"
            ]
        )
        self.assertTrue(
            peak["observed_temperature_trajectory_regime_v1"]["strong_warming_confirmed"]
        )
        self.assertGreater(
            peak["observed_temperature_trajectory_regime_v1"]["evidence_scale"],
            1.0,
        )
        self.assertGreater(
            peak["observed_temperature_trajectory_regime_v1"]["effective_evidence"],
            peak["observed_temperature_trajectory_regime_v1"]["evidence_base"],
        )
        self.assertGreater(
            peak["observed_temperature_trajectory_regime_v1"]["path_delta_c"],
            0.0,
        )

    def test_peak_estimator_blocks_explicit_gate_when_only_short_windows_exist(self) -> None:
        peak = estimate_peak_temperature_from_features(
            {
                "observed_temp_c": 30.0,
                "observed_today_max_c": 30.0,
                "observed_today_max_source": "zuuu_metar_raw_awc",
                "forecast_peak_temp_c": 31.0,
                "forecast_sigma_c": 1.0,
                "observation_hour_local": 10.0,
                "observation_month": 6,
                "expected_peak_hour_local": 14.0,
                "temperature_momentum_5m_c_per_hour": 6.0,
                "temperature_momentum_3m_c_per_hour": 5.0,
                "temperature_momentum_2m_c_per_hour": 4.0,
                "temperature_momentum_1m_c_per_hour": 3.0,
                "temperature_momentum_posterior_gate_allowed": True,
                "temperature_rise_rate_c_per_hour": 6.0,
                "temperature_rise_rate_window": "5m",
                "temperature_rise_rate_source": "unit_test:5m",
            }
        )

        terms = peak["energy_balance_terms"]
        self.assertTrue(peak["enabled"])
        self.assertFalse(terms["temperature_trajectory_uses_confirmed_momentum"])
        self.assertEqual(terms["temperature_trajectory_confirmed_momentum_windows"], "")
        self.assertTrue(terms["temperature_trajectory_short_window_suppressed"])
        self.assertEqual(
            terms["temperature_trajectory_suppressed_reason"],
            "short_window_requires_30m_45m_1h_confirmation",
        )
        self.assertIsNone(terms["temperature_trajectory_rate_c_per_hour"])
        self.assertIsNone(terms["temperature_trajectory_delta_c"])

    def test_peak_estimator_allows_momentum_after_thirty_minute_confirmation(self) -> None:
        peak = estimate_peak_temperature_from_features(
            {
                "observed_temp_c": 30.0,
                "observed_today_max_c": 30.0,
                "observed_today_max_source": "zuuu_metar_raw_awc",
                "forecast_peak_temp_c": 31.0,
                "forecast_sigma_c": 1.0,
                "observation_hour_local": 10.0,
                "observation_month": 6,
                "expected_peak_hour_local": 14.0,
                "temperature_momentum_30m_c_per_hour": 1.0,
                "temperature_momentum_5m_c_per_hour": 3.0,
                "temperature_momentum_posterior_gate_allowed": True,
            }
        )

        terms = peak["energy_balance_terms"]
        self.assertTrue(peak["enabled"])
        self.assertTrue(terms["temperature_trajectory_uses_confirmed_momentum"])
        self.assertEqual(terms["temperature_trajectory_confirmed_momentum_windows"], "30m")
        self.assertGreater(terms["temperature_trajectory_rate_c_per_hour"], 0.0)
        self.assertIsNone(terms["temperature_trajectory_delta_c"])
        self.assertTrue(terms["observed_temperature_trajectory_regime_v1_source_component_enabled"])
        self.assertFalse(terms["legacy_peak_trajectory_enabled"])
        self.assertGreater(
            peak["observed_temperature_trajectory_regime_v1"]["path_delta_c"],
            0.0,
        )

    def test_wu_twc_observed_intraday_fetches_current_max_and_momentum(self) -> None:
        current_ts = datetime(2026, 6, 14, 7, 0, tzinfo=timezone.utc).timestamp()
        max_ts = datetime(2026, 6, 14, 6, 30, tzinfo=timezone.utc).timestamp()
        history_pairs = [
            (datetime(2026, 6, 14, 1, 0, tzinfo=timezone.utc), 25.0),
            (datetime(2026, 6, 14, 4, 0, tzinfo=timezone.utc), 29.0),
            (datetime(2026, 6, 14, 6, 30, tzinfo=timezone.utc), 30.5),
        ]

        def fake_current_metrics(_source, _station, _event_date, _unit):
            return (
                {
                    "ok": True,
                    "value": 31.0,
                    "time_gmt": current_ts,
                    "source": "wunderground_current_station_current_temperature",
                },
                {
                    "ok": True,
                    "value": 30.5,
                    "time_gmt": max_ts,
                    "source": "wunderground_current_station_max_since7am",
                },
            )

        with patch(
            "scan_weather_market.fetch_wunderground_current_and_max_observation_metrics",
            side_effect=fake_current_metrics,
        ), patch(
            "scan_weather_market._wunderground_history_temp_pairs_c",
            return_value=(history_pairs, {"method": "weather_com_history_api"}),
        ):
            features = fetch_wu_twc_observed_intraday_features(
                "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU/date/2026-06-14",
                "ZUUU",
                "2026-06-14",
                "temperature_2m_max",
                "C",
                tz="Asia/Shanghai",
                reference_observed_at="2026-06-14T04:00:00+00:00",
            )

        self.assertTrue(features["wu_twc_observed_intraday_available"])
        self.assertTrue(features["wu_twc_observed_intraday_primary_current"])
        self.assertTrue(features["wu_twc_observed_intraday_primary_max"])
        self.assertEqual(features["observed_temp_c"], 31.0)
        self.assertEqual(features["observed_temp_source"], "wu_twc_observed_intraday_current")
        self.assertEqual(features["observed_today_max_c"], 31.0)
        self.assertEqual(features["observed_today_max_source"], "wu_twc_observed_intraday_current")
        self.assertGreater(features["temperature_rise_rate_c_per_hour"], 0.0)
        self.assertIn("wu_twc_observed_intraday_temperature_momentum_30m_c_per_hour", features)
        self.assertEqual(
            features["wu_twc_observed_intraday_temperature_momentum_30m_c_per_hour"],
            features["temperature_momentum_30m_c_per_hour"],
        )
        self.assertEqual(features["wu_twc_observed_intraday_history_sample_count"], 3)

    def test_wu_twc_current_endpoint_raw_max_participates_in_today_max(self) -> None:
        current_dt = datetime(2026, 6, 18, 7, 0, tzinfo=timezone.utc)
        current_ts = current_dt.timestamp()
        history_pairs = [
            (datetime(2026, 6, 18, 2, 0, tzinfo=timezone.utc), 23.0),
            (datetime(2026, 6, 18, 4, 0, tzinfo=timezone.utc), 24.0),
        ]

        def fake_current_metrics(_source, _station, _event_date, _unit):
            return (
                {
                    "ok": True,
                    "value": 24.0,
                    "time_gmt": current_ts,
                    "source": "wunderground_current_station_current_temperature",
                    "source_url": "https://api.weather.com/v3/wx/observations/current?stationId=ZUUU",
                },
                {
                    "ok": True,
                    "value": 24.0,
                    "time_gmt": current_ts,
                    "source": "wunderground_current_station_max_since7am",
                },
            )

        def fake_raw(_source_url, *, units):
            if units == "m":
                return {
                    "ok": True,
                    "raw_unit": "C",
                    "max_since7am_raw_value": 31.0,
                    "observed_at": current_dt.isoformat(),
                }
            return {
                "ok": True,
                "raw_unit": "F",
                "max_since7am_raw_value": 87.8,
                "observed_at": current_dt.isoformat(),
            }

        with patch(
            "scan_weather_market.fetch_wunderground_current_and_max_observation_metrics",
            side_effect=fake_current_metrics,
        ), patch(
            "scan_weather_market._wunderground_history_temp_pairs_c",
            return_value=(history_pairs, {"method": "weather_com_history_api"}),
        ), patch(
            "scan_weather_market._weather_com_current_endpoint_raw_observation",
            side_effect=fake_raw,
        ):
            features = fetch_wu_twc_observed_intraday_features(
                "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU/date/2026-06-18",
                "ZUUU",
                "2026-06-18",
                "temperature_2m_max",
                "C",
                tz="Asia/Shanghai",
                reference_observed_at="2026-06-18T04:00:00+00:00",
            )

        self.assertEqual(features["wu_twc_observed_intraday_today_max_c"], 31.0)
        self.assertEqual(
            features["wu_twc_observed_intraday_today_max_source"],
            "wu_twc_observed_intraday_current_metric_max_since7am",
        )
        self.assertEqual(features["observed_today_max_c"], 31.0)

    def test_wu_twc_observed_intraday_aux_overrides_stale_wu_history_nowcast(self) -> None:
        features = _build_weather_likelihood_features(
            model_members={"m1": [31.0, 32.0], "m2": [33.0]},
            market_var="temperature_2m_max",
            market_unit="C",
            observed_nowcast_value=29.0,
            observed_nowcast_share=0.6,
            observed_nowcast_source="wunderground_history",
            auxiliary_features={
                "source": "wu_twc_observed_intraday",
                "wu_twc_observed_intraday_available": True,
                "wu_twc_observed_intraday_primary_current": True,
                "wu_twc_observed_intraday_primary_max": True,
                "observed_temp": 31.0,
                "observed_temp_c": 31.0,
                "observed_temp_source": "wu_twc_observed_intraday_current",
                "observed_today_max_c": 31.0,
                "observed_today_max_source": "wu_twc_observed_intraday_current",
                "observed_today_max_trusted": True,
            },
        )

        self.assertEqual(features["observed_temp_c"], 31.0)
        self.assertEqual(features["observed_temp_source"], "wu_twc_observed_intraday_current")
        self.assertEqual(features["observed_today_max_c"], 31.0)
        self.assertEqual(features["observed_today_max_source"], "wu_twc_observed_intraday_current")

    def test_wu_forecast_peak_does_not_become_observed_nowcast(self) -> None:
        features = _build_weather_likelihood_features(
            model_members={"m1": [29.0, 30.0], "m2": [31.0]},
            market_var="temperature_2m_max",
            market_unit="C",
            auxiliary_features={
                "source": "wunderground_forecast_daily_peak",
                "settlement_source_forecast_peak_c": 30.0,
                "wu_forecast_peak_c": 30.0,
                "wu_forecast_peak_soft": True,
                "wu_forecast_peak_usable_for_settlement": False,
            },
        )

        self.assertNotIn("observed_temp", features)
        self.assertNotIn("observed_temp_c", features)
        self.assertNotIn("observed_today_max_c", features)
        self.assertEqual(features["settlement_source_forecast_peak_c"], 30.0)
        self.assertEqual(features["wu_forecast_peak_usable_for_settlement"], False)

    def test_fetch_open_meteo_likelihood_features_extracts_physical_fields(self) -> None:
        class FakeResponse:
            status_code = 200
            url = "https://api.open-meteo.com/v1/forecast?test=1"
            headers = {"Date": "Sun, 31 May 2026 00:00:00 GMT"}

            def raise_for_status(self) -> None:
                return None

            def json(self):
                return {
                    "hourly": {
                        "temperature_2m": [25.0, 28.0, 31.0, 30.0],
                        "dew_point_2m": [21.0, 22.0, 24.0, 23.0],
                        "cloud_cover": [80.0, 70.0, 50.0, 40.0],
                        "cloud_cover_low": [80.0, 70.0, 20.0, 10.0],
                        "cloud_cover_mid": [0.0, 20.0, 30.0, 20.0],
                        "cloud_cover_high": [10.0, 10.0, 40.0, 60.0],
                        "shortwave_radiation": [0.0, 100.0, 600.0, 300.0],
                        "precipitation": [0.0, 0.0, 2.0, 0.5],
                        "rain": [0.0, 0.0, 1.5, 0.2],
                        "showers": [0.0, 0.0, 0.5, 0.0],
                        "weather_code": [0, 3, 61, 61],
                        "wind_speed_10m": [2.0, 4.0, 8.0, 6.0],
                        "wind_direction_10m": [180.0, 200.0, 220.0, 240.0],
                        "surface_pressure": [1008.0, 1007.0, 1006.0, 1005.0],
                    },
                    "current": {
                        "precipitation": 3.0,
                        "rain": 2.5,
                        "showers": 0.0,
                        "weather_code": 61,
                    },
                    "daily": {
                        "uv_index_max": [5.0],
                        "uv_index_clear_sky_max": [10.0],
                        "shortwave_radiation_sum": [18.0],
                    },
                }

        provenance = {}
        with patch("scan_weather_market.requests_get", return_value=FakeResponse()) as mocked:
            features = fetch_open_meteo_likelihood_features(
                30.0,
                104.0,
                "2026-06-01",
                "Asia/Shanghai",
                provenance=provenance,
            )

        self.assertTrue(mocked.called)
        params = mocked.call_args.kwargs["params"]
        self.assertIn("dew_point_2m", params["hourly"])
        self.assertIn("precipitation", params["hourly"])
        self.assertIn("precipitation", params["current"])
        self.assertIn("cloud_cover_low", params["hourly"])
        self.assertIn("uv_index_clear_sky_max", params["daily"])
        self.assertEqual(features["source"], "open_meteo_forecast_aux")
        self.assertEqual(features["dewpoint_c"], 24.0)
        self.assertIn("cloud_cover_low_pct", features)
        self.assertEqual(features["wind_speed_mps"], 8.0)
        self.assertAlmostEqual(features["cti"], 0.5)
        self.assertEqual(features["precipitation_rate_mm_per_h"], 3.0)
        self.assertEqual(features["rain_rate_mm_per_h"], 2.5)
        self.assertTrue(features["is_raining"])
        self.assertEqual(features["pressure_trend_hpa_3h"], -3.0)
        self.assertIn("dewpoint_c", provenance["feature_keys"])

    def test_fetch_mrms_qpe_metadata_decodes_precip_rate_grib2_png_point(self) -> None:
        def chunk(kind: bytes, payload: bytes) -> bytes:
            return len(payload).to_bytes(4, "big") + kind + payload + b"\x00\x00\x00\x00"

        def synthetic_png() -> bytes:
            rows = [
                b"\x00" + (30).to_bytes(2, "big") + (45).to_bytes(2, "big"),
                b"\x00" + (70).to_bytes(2, "big") + (82).to_bytes(2, "big"),
            ]
            ihdr = struct.pack(">IIBBBBB", 2, 2, 16, 0, 0, 0, 0)
            return (
                b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", ihdr)
                + chunk(b"IDAT", zlib.compress(b"".join(rows)))
                + chunk(b"IEND", b"")
            )

        def section(number: int, payload: bytes) -> bytes:
            length = 5 + len(payload)
            return length.to_bytes(4, "big") + bytes([number]) + payload

        def synthetic_grib2_gz() -> bytes:
            sec1 = section(1, b"\x00" * 16)
            sec3_payload = bytearray(67)
            sec3_payload[0] = 0
            sec3_payload[1:5] = (4).to_bytes(4, "big")
            sec3_payload[5] = 0
            sec3_payload[6] = 0
            sec3_payload[7:9] = (0).to_bytes(2, "big")
            sec3_payload[25:29] = (2).to_bytes(4, "big")
            sec3_payload[29:33] = (2).to_bytes(4, "big")
            sec3_payload[41:45] = int(41.0 * 1_000_000).to_bytes(4, "big", signed=True)
            sec3_payload[45:49] = int(286.0 * 1_000_000).to_bytes(4, "big", signed=True)
            sec3_payload[50:54] = int(40.99 * 1_000_000).to_bytes(4, "big", signed=True)
            sec3_payload[54:58] = int(286.01 * 1_000_000).to_bytes(4, "big", signed=True)
            sec3_payload[58:62] = int(0.01 * 1_000_000).to_bytes(4, "big")
            sec3_payload[62:66] = int(0.01 * 1_000_000).to_bytes(4, "big")
            sec3_payload[66] = 0
            sec3 = section(3, bytes(sec3_payload))
            sec4 = section(4, b"\x00\x00\x00\x00")
            sec5_payload = (
                (4).to_bytes(4, "big")
                + (41).to_bytes(2, "big")
                + struct.pack(">f", -30.0)
                + (0).to_bytes(2, "big", signed=True)
                + (1).to_bytes(2, "big", signed=True)
                + bytes([16, 0])
            )
            sec5 = section(5, sec5_payload)
            sec6 = section(6, bytes([255]))
            sec7 = section(7, synthetic_png())
            body = sec1 + sec3 + sec4 + sec5 + sec6 + sec7 + b"7777"
            total = 16 + len(body)
            message = b"GRIB\x00\x00\xd1\x02" + total.to_bytes(8, "big") + body
            return gzip.compress(message)

        class FakeResponse:
            def __init__(self, content: bytes, url: str, status_code: int = 200):
                self.status_code = status_code
                self.url = url
                self.headers = {}
                self.content = content

        list_payload = b"""<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Contents>
    <Key>CONUS/PrecipRate_00.00/20260531/MRMS_PrecipRate_00.00_20260531-130000.grib2.gz</Key>
    <LastModified>2026-05-31T13:01:00.000Z</LastModified>
    <Size>123</Size>
  </Contents>
  <Contents>
    <Key>CONUS/PrecipRate_00.00/20260531/MRMS_PrecipRate_00.00_20260531-130200.grib2.gz</Key>
    <LastModified>2026-05-31T13:03:00.000Z</LastModified>
    <Size>456</Size>
  </Contents>
</ListBucketResult>"""

        def fake_requests_get(url, **kwargs):
            if kwargs.get("params"):
                return FakeResponse(list_payload, "https://noaa-mrms-pds.s3.amazonaws.com/?list-type=2")
            return FakeResponse(
                synthetic_grib2_gz(),
                "https://noaa-mrms-pds.s3.amazonaws.com/CONUS/PrecipRate_00.00/20260531/MRMS_PrecipRate_00.00_20260531-130200.grib2.gz",
            )

        provenance = {}
        with patch("scan_weather_market.requests_get", side_effect=fake_requests_get) as mocked:
            features = fetch_mrms_qpe_metadata_features(
                40.99,
                -73.99,
                tz="America/New_York",
                target_event_date="2026-05-31",
                now_dt=datetime(2026, 5, 31, 13, 5, tzinfo=timezone.utc),
                provenance=provenance,
        )

        self.assertTrue(mocked.called)
        prefixes = [
            call.kwargs["params"]["prefix"]
            for call in mocked.call_args_list
            if call.kwargs.get("params")
        ]
        self.assertIn("CONUS/PrecipRate_00.00/20260531/", prefixes)
        self.assertEqual(features["source"], "noaa_mrms_qpe_metadata")
        self.assertTrue(features["radar_qpe_available"])
        self.assertTrue(features["radar_qpe_latest_key"].endswith("130200.grib2.gz"))
        self.assertEqual(features["radar_qpe_latest_at"], "2026-05-31T13:02:00+00:00")
        self.assertNotIn("radar_qpe_value_na_reason", features)
        self.assertEqual(features["radar_qpe_value_source"], "noaa_mrms_precip_rate_grib2_png")
        self.assertAlmostEqual(features["radar_precip_rate_mm_per_h"], 5.2)
        self.assertAlmostEqual(features["qpe_precip_rate_mm_per_h"], 5.2)
        self.assertTrue(features["is_raining_observed"])
        self.assertEqual(features["radar_qpe_grid_x"], 1)
        self.assertEqual(features["radar_qpe_grid_y"], 1)
        self.assertEqual(features["radar_qpe_raw_code"], 82)
        self.assertIn("radar_qpe_latest_key", provenance["feature_keys"])

    def test_fetch_metar_likelihood_features_extracts_primary_evidence(self) -> None:
        class FakeResponse:
            status_code = 200
            url = "https://aviationweather.gov/api/data/metar?ids=KJFK"
            headers = {"Date": "Sun, 31 May 2026 00:00:00 GMT"}

            def raise_for_status(self) -> None:
                return None

            def json(self):
                return [
                    {
                        "icaoId": "KJFK",
                        "obsTime": "2026-05-31T10:00:00Z",
                        "temp": 24.0,
                        "dewp": 20.0,
                        "wdir": 80,
                        "wspd": 10,
                        "altim": 29.80,
                        "rawOb": "KJFK 311000Z 08010KT FEW020 SCT060 24/20 A2980",
                    },
                    {
                        "icaoId": "KJFK",
                        "obsTime": "2026-05-31T13:00:00Z",
                        "temp": 28.0,
                        "dewp": 22.0,
                        "wdir": 90,
                        "wspd": 16,
                        "wgst": 24,
                        "altim": 29.70,
                        "rawOb": "KJFK 311300Z 09016G24KT -RA FEW020 SCT060 OVC100 28/22 A2970",
                    },
                ]

        provenance = {}
        with patch("scan_weather_market.requests_get", return_value=FakeResponse()) as mocked:
            features = fetch_metar_likelihood_features("KJFK", provenance=provenance)

        self.assertTrue(mocked.called)
        params = mocked.call_args.kwargs["params"]
        self.assertEqual(params["ids"], "KJFK")
        self.assertEqual(features["source"], "metar_aviationweather")
        self.assertEqual(features["observed_temp"], 28.0)
        self.assertEqual(features["dewpoint_c"], 22.0)
        self.assertAlmostEqual(features["wind_speed_mps"], 16 * 0.514444)
        self.assertEqual(features["wind_direction_deg"], 90.0)
        self.assertGreater(features["cloud_cover_pct"], 80.0)
        self.assertTrue(features["is_raining_observed"])
        self.assertGreater(features["observed_precip_rate_mm_per_h"], 0.0)
        self.assertLess(features["pressure_trend_hpa_3h"], 0.0)
        self.assertEqual(features["observation_hour_local"], 13.0)
        self.assertEqual(features["observed_today_max_c"], 28.0)
        self.assertIn("cloud_cover_pct", provenance["feature_keys"])

    def test_fetch_metar_likelihood_features_suppresses_observed_evidence_for_other_event_date(self) -> None:
        class FakeResponse:
            status_code = 200
            url = "https://aviationweather.gov/api/data/metar?ids=ZUUU"
            headers = {"Date": "Wed, 10 Jun 2026 10:00:00 GMT"}

            def raise_for_status(self) -> None:
                return None

            def json(self):
                return [
                    {
                        "icaoId": "ZUUU",
                        "obsTime": "2026-06-10T10:00:00Z",
                        "temp": 29.0,
                        "dewp": 16.0,
                        "wdir": 180,
                        "wspd": 8,
                        "rawOb": "ZUUU 101000Z 18008KT 29/16 Q1008",
                    }
                ]

        provenance = {}
        with patch("scan_weather_market.requests_get", return_value=FakeResponse()):
            features = fetch_metar_likelihood_features(
                "ZUUU",
                tz="Asia/Shanghai",
                target_event_date="2026-06-11",
                provenance=provenance,
            )

        self.assertEqual(features["source"], "metar_aviationweather")
        self.assertTrue(features["metar_observation_date_mismatch"])
        self.assertEqual(features["metar_observation_local_date"], "2026-06-10")
        self.assertNotIn("observed_temp", features)
        self.assertNotIn("metar_temperature_c", features)
        self.assertNotIn("observed_today_max_c", features)
        self.assertNotIn("observation_hour_local", features)
        self.assertNotIn("dewpoint_c", features)

    def test_fetch_zuuu_metar_raw_awc_features_extracts_raw_audit_and_wu_delta(self) -> None:
        class FakeResponse:
            status_code = 200
            url = "https://aviationweather.gov/api/data/metar?ids=ZUUU&format=raw"
            headers = {"Date": "Fri, 12 Jun 2026 01:05:00 GMT"}
            text = "\n".join(
                [
                    "METAR ZUUU 120100Z 29002MPS 250V320 CAVOK 24/14 Q1014 NOSIG",
                    "METAR ZUUU 120000Z 31002MPS 280V350 CAVOK 22/14 Q1014 NOSIG",
                    "METAR ZUUU 112300Z 31001MPS CAVOK 21/15 Q1014 NOSIG",
                ]
            )

            def raise_for_status(self) -> None:
                return None

        provenance = {}
        with patch("scan_weather_market.requests_get", return_value=FakeResponse()) as mocked:
            features = fetch_zuuu_metar_raw_awc_features(
                "ZUUU",
                tz="Asia/Shanghai",
                target_event_date="2026-06-12",
                market_unit="C",
                observed_nowcast_value=24.4,
                observed_nowcast_source="wunderground_current_station_current_temperature",
                wu_history_metrics={"value": 24.0, "time_gmt": 1781226000},
                provenance=provenance,
            )

        params = mocked.call_args.kwargs["params"]
        self.assertEqual(params["ids"], "ZUUU")
        self.assertEqual(params["format"], "raw")
        self.assertEqual(features["source"], "zuuu_metar_raw_awc")
        self.assertTrue(features["zuuu_metar_raw_awc_available"])
        self.assertEqual(features["zuuu_metar_raw_awc_latest_temp_c"], 24.0)
        self.assertFalse(features["zuuu_metar_raw_awc_date_mismatch"])
        self.assertEqual(features["zuuu_metar_raw_awc_date_mismatch_reason"], "")
        self.assertEqual(features["observed_temp_c"], 24.0)
        self.assertEqual(features["observed_today_max_c"], 24.0)
        self.assertEqual(features["observed_today_max_source"], "zuuu_metar_raw_awc")
        self.assertFalse(features["is_raining_observed"])
        self.assertTrue(features["metar_no_precip_observed"])
        self.assertTrue(features["awc_metar_no_precip_observed"])
        self.assertTrue(features["metar_cavok_observed"])
        self.assertEqual(features["observed_precip_rate_mm_per_h"], 0.0)
        self.assertEqual(features["precipitation_rate_mm_per_h"], 0.0)
        self.assertEqual(features["radar_qpe_proxy_source"], "zuuu_metar_raw_awc_cavok_no_precip")
        self.assertAlmostEqual(features["zuuu_metar_raw_awc_vs_wu_nowcast_delta_c"], 0.4)
        self.assertEqual(features["zuuu_metar_raw_awc_vs_wu_history_status"], "aligned")
        self.assertEqual(features["weather_source_alert_status"], "aligned")
        self.assertEqual(features["weather_source_alert_level"], "ok")
        self.assertFalse(features["weather_source_delay_deformation_alert"]["alert"])
        self.assertEqual(features["wu_settlement_page"], "Wunderground settlement display page")
        self.assertEqual(features["twc_provider"], "Weather.com/TWC")
        self.assertEqual(features["zuuu_metar_observed"], "ZUUU airport METAR observed")
        self.assertEqual(features["zuuu_metar_archive_iem"], "historical_archive_not_realtime")
        self.assertEqual(features["awc_wind_dir_deg"], 290.0)
        self.assertEqual(features["awc_wind_speed_mps"], 2.0)
        self.assertTrue(features["awc_wind_variable"])
        self.assertEqual(features["awc_wind_variable_from_deg"], 250.0)
        self.assertEqual(features["awc_wind_variable_to_deg"], 320.0)
        self.assertEqual(features["awc_wind_token"], "29002MPS")
        self.assertEqual(features["zuuu_metar_raw_awc_latest_wind_dir_deg"], 290.0)
        self.assertEqual(features["zuuu_metar_raw_awc_latest_pressure_hpa"], 1014.0)
        self.assertEqual(features["awc_pressure_hpa"], 1014.0)
        self.assertEqual(features["pressure_hpa"], 1014.0)
        self.assertEqual(features["awc_previous_dewpoint_c"], 14.0)
        self.assertEqual(features["dewpoint_trend_c"], 0.0)
        self.assertEqual(features["awc_previous_pressure_hpa"], 1014.0)
        self.assertEqual(features["awc_pressure_trend_hpa"], 0.0)
        self.assertIn("awc_metar_no_precip_observed", provenance["feature_keys"])
        self.assertIn("zuuu_metar_raw_awc_latest_raw", provenance["feature_keys"])

    def test_parse_awc_raw_metar_line_extracts_wind_group(self) -> None:
        reference_dt = datetime(2026, 7, 2, 12, 10, tzinfo=timezone.utc)
        fixed = _parse_awc_raw_metar_line(
            "METAR ZUUU 021200Z 08005G08MPS 040V110 9999 -SHRA FEW033CB SCT033 27/21 Q1009 BECMG TL1330 02003MPS SCT040",
            reference_dt,
        )

        self.assertEqual(fixed["wind_dir_deg"], 80.0)
        self.assertEqual(fixed["wind_speed_mps"], 5.0)
        self.assertEqual(fixed["wind_gust_mps"], 8.0)
        self.assertTrue(fixed["wind_variable"])
        self.assertEqual(fixed["wind_variable_from_deg"], 40.0)
        self.assertEqual(fixed["wind_variable_to_deg"], 110.0)
        self.assertEqual(fixed["pressure_hpa"], 1009.0)
        self.assertEqual(fixed["wind_token"], "08005G08MPS")

        variable = _parse_awc_raw_metar_line(
            "METAR ZUUU 021000Z VRB02MPS 9999 FEW033TCU SCT033 34/17 Q1007 NOSIG",
            reference_dt,
        )

        self.assertNotIn("wind_dir_deg", variable)
        self.assertTrue(variable["wind_variable"])
        self.assertEqual(variable["wind_dir_text"], "VRB")
        self.assertEqual(variable["wind_speed_mps"], 2.0)

    def test_fetch_zuuu_metar_raw_awc_features_can_skip_cross_checks_for_fast_path(self) -> None:
        class FakeResponse:
            status_code = 200
            url = "https://aviationweather.gov/api/data/metar?ids=ZUUU&format=raw"
            headers = {"Date": "Fri, 12 Jun 2026 01:05:00 GMT"}
            text = "METAR ZUUU 120100Z 29002MPS 250V320 CAVOK 24/14 Q1014 NOSIG"

            def raise_for_status(self) -> None:
                return None

        with patch("scan_weather_market.fetch_zuuu_metar_cross_check_features") as cross_check:
            cross_check.side_effect = RuntimeError("slow cross-check must not run")
            with patch("scan_weather_market.requests_get", return_value=FakeResponse()):
                features = fetch_zuuu_metar_raw_awc_features(
                    "ZUUU",
                    tz="Asia/Shanghai",
                    target_event_date="2026-06-12",
                    include_cross_checks=False,
                )

        cross_check.assert_not_called()
        self.assertEqual(features["source"], "zuuu_metar_raw_awc")
        self.assertEqual(features["zuuu_metar_raw_awc_latest_temp_c"], 24.0)
        self.assertFalse(features["zuuu_metar_raw_awc_cross_checks_included"])
        self.assertNotIn("metar_cross_check_error", features)

    def test_fetch_zuuu_metar_raw_awc_features_flags_wu_delay_and_deformation(self) -> None:
        class FakeResponse:
            status_code = 200
            url = "https://aviationweather.gov/api/data/metar?ids=ZUUU&format=raw"
            headers = {"Date": "Fri, 12 Jun 2026 01:05:00 GMT"}
            text = "\n".join(
                [
                    "METAR ZUUU 120100Z 29002MPS 250V320 CAVOK 24/14 Q1014 NOSIG",
                    "METAR ZUUU 120000Z 31002MPS 280V350 CAVOK 22/14 Q1014 NOSIG",
                ]
            )

            def raise_for_status(self) -> None:
                return None

        with patch("scan_weather_market.requests_get", return_value=FakeResponse()):
            features = fetch_zuuu_metar_raw_awc_features(
                "ZUUU",
                tz="Asia/Shanghai",
                target_event_date="2026-06-12",
                market_unit="C",
                observed_nowcast_value=26.0,
                observed_nowcast_source="wunderground_current_station_current_temperature",
                wu_history_metrics={"value": 24.0, "time_gmt": 0},
            )

        alert = features["weather_source_delay_deformation_alert"]
        self.assertTrue(alert["alert"])
        self.assertEqual(alert["level"], "critical")
        self.assertIn("wu_nowcast_temperature_deformation", alert["codes"])
        self.assertIn("wu_settlement_page_delay", alert["codes"])
        self.assertEqual(features["weather_source_alert_status"], "delay_or_deformation_alert")
        self.assertIn("wu_nowcast_temperature_deformation", features["weather_source_alert_codes"])
        self.assertEqual(features["awc_wind_dir_deg"], 290.0)
        self.assertEqual(features["awc_previous_wind_dir_deg"], 310.0)
        self.assertEqual(features["awc_previous_wind_variable_from_deg"], 280.0)
        self.assertEqual(features["awc_previous_wind_variable_to_deg"], 350.0)

    def test_fetch_zuuu_metar_raw_awc_features_is_chengdu_only(self) -> None:
        with patch("scan_weather_market.requests_get") as mocked:
            features = fetch_zuuu_metar_raw_awc_features("KJFK")

        self.assertEqual(features, {})
        self.assertFalse(mocked.called)

    def test_fetch_zuuu_taf_forecast_peak_metrics_extracts_tx_as_strong_soft(self) -> None:
        class FakeResponse:
            status_code = 200
            url = "https://aviationweather.gov/api/data/taf?ids=ZUUU&format=raw"
            headers = {"Date": "Fri, 12 Jun 2026 01:10:00 GMT"}
            text = "TAF ZUUU 112101Z 1200/1224 22004MPS 6000 SCT050 TX30/1207Z TN20/1222Z"

            def raise_for_status(self) -> None:
                return None

        provenance = {}
        with patch("scan_weather_market.requests_get", return_value=FakeResponse()) as mocked:
            metrics = fetch_zuuu_taf_forecast_peak_metrics(
                "ZUUU",
                "2026-06-12",
                "temperature_2m_max",
                "C",
                tz="Asia/Shanghai",
                provenance=provenance,
            )

        params = mocked.call_args.kwargs["params"]
        self.assertEqual(params["ids"], "ZUUU")
        self.assertEqual(params["format"], "raw")
        self.assertEqual(mocked.call_args.kwargs["timeout"], (1.5, 3.0))
        self.assertEqual(mocked.call_args.kwargs["retries"], 1)
        self.assertTrue(metrics["ok"])
        self.assertEqual(metrics["source"], "zuuu_taf_awc_tx_peak")
        self.assertEqual(metrics["value_c"], 30.0)
        self.assertEqual(metrics["peak_at"], "2026-06-12T07:00:00+00:00")
        self.assertEqual(metrics["valid_end_at"], "2026-06-13T00:00:00+00:00")

    def test_fetch_zuuu_taf_forecast_peak_metrics_accepts_amd_report(self) -> None:
        class FakeResponse:
            status_code = 200
            url = "https://aviationweather.gov/api/data/taf?ids=ZUUU&format=raw"
            headers = {"Date": "Thu, 02 Jul 2026 16:00:00 GMT"}
            text = (
                "TAF AMD ZUUU 021554Z 0212/0312 08005MPS 6000 FEW030CB SCT040 "
                "TX33/0307Z TN23/0223Z BECMG 0213/0214 -TSRA"
            )

            def raise_for_status(self) -> None:
                return None

        provenance = {}
        with patch("scan_weather_market.requests_get", return_value=FakeResponse()):
            metrics = fetch_zuuu_taf_forecast_peak_metrics(
                "ZUUU",
                "2026-07-03",
                "temperature_2m_max",
                "C",
                tz="Asia/Shanghai",
                provenance=provenance,
            )

        self.assertTrue(metrics["ok"])
        self.assertEqual(metrics["value_c"], 33.0)
        self.assertEqual(metrics["peak_token"], "TX33/0307Z")
        self.assertEqual(metrics["minimum_c"], 23.0)
        self.assertTrue(metrics["strong_soft"])
        self.assertFalse(metrics["source_usable_for_settlement"])
        self.assertTrue(metrics["not_observed_nowcast"])
        self.assertFalse(metrics["usable_for_bayes"])
        self.assertEqual(
            metrics["bayes_channel_status"],
            "pending_awc_wu_quality_gate_and_created_at_walk_forward_calibration",
        )
        self.assertIn("value_c", provenance["feature_keys"])
        self.assertEqual(provenance["request_timeout"], [1.5, 3.0])
        self.assertEqual(provenance["request_retries"], 1)

    def test_fetch_nws_latest_observation_extracts_precip_and_physical_fields(self) -> None:
        class FakeResponse:
            status_code = 200
            url = "https://api.weather.gov/stations/KJFK/observations/latest"
            headers = {"Date": "Sun, 31 May 2026 13:05:00 GMT"}

            def json(self):
                return {
                    "properties": {
                        "timestamp": "2026-05-31T13:00:00+00:00",
                        "temperature": {"value": 28.0, "unitCode": "wmoUnit:degC"},
                        "dewpoint": {"value": 22.0, "unitCode": "wmoUnit:degC"},
                        "windSpeed": {"value": 7.0, "unitCode": "wmoUnit:m_s-1"},
                        "windDirection": {"value": 90.0, "unitCode": "wmoUnit:degree_(angle)"},
                        "barometricPressure": {"value": 100800.0, "unitCode": "wmoUnit:Pa"},
                        "precipitationLastHour": {"value": 2.0, "unitCode": "wmoUnit:mm"},
                        "precipitationLast3Hours": {"value": 4.0, "unitCode": "wmoUnit:mm"},
                        "cloudLayers": [
                            {"amount": "BKN", "base": {"value": 800.0, "unitCode": "wmoUnit:m"}},
                        ],
                        "presentWeather": [
                            {"intensity": "light", "weather": "rain"},
                        ],
                    }
                }

        provenance = {}
        with patch("scan_weather_market.requests_get", return_value=FakeResponse()) as mocked:
            features = fetch_nws_latest_observation_likelihood_features(
                "KJFK",
                tz="America/New_York",
                target_event_date="2026-05-31",
                provenance=provenance,
            )

        self.assertTrue(mocked.called)
        self.assertEqual(features["source"], "nws_latest_station_observation")
        self.assertEqual(features["observed_temp"], 28.0)
        self.assertEqual(features["dewpoint_c"], 22.0)
        self.assertEqual(features["wind_speed_mps"], 7.0)
        self.assertEqual(features["wind_direction_deg"], 90.0)
        self.assertEqual(features["pressure_hpa"], 1008.0)
        self.assertEqual(features["qpe_precip_rate_mm_per_h"], 2.0)
        self.assertEqual(features["precipitation_rate_mm_per_h"], 2.0)
        self.assertEqual(features["qpe_precip_mm"], 2.0)
        self.assertGreater(features["surface_wetness_proxy"], 0.0)
        self.assertTrue(features["is_raining_observed"])
        self.assertGreater(features["cloud_cover_pct"], 70.0)
        self.assertIn("qpe_precip_rate_mm_per_h", provenance["feature_keys"])

    def test_stale_metar_keeps_core_evidence(self) -> None:
        os.environ["PM_BAYES_LIKELIHOOD_METAR_MAX_AGE_MIN"] = "1"

        class FakeResponse:
            status_code = 200
            url = "https://aviationweather.gov/api/data/metar?ids=KJFK"
            headers = {}

            def raise_for_status(self) -> None:
                return None

            def json(self):
                return [
                    {
                        "icaoId": "KJFK",
                        "obsTime": "2020-01-01T00:00:00Z",
                        "temp": 28.0,
                        "dewp": 22.0,
                        "wdir": 90,
                        "wspd": 16,
                        "rawOb": "KJFK 010000Z 09016KT OVC020 28/22 A2970",
                    }
                ]

        with patch("scan_weather_market.requests_get", return_value=FakeResponse()):
            features = fetch_metar_likelihood_features("KJFK")

        self.assertTrue(features["metar_stale"])
        self.assertEqual(features["source"], "metar_aviationweather")
        self.assertIn("dewpoint_c", features)
        self.assertIn("observed_temp", features)
        self.assertAlmostEqual(features["observed_temp"], 28.0)

    def test_fetch_satellite_radiation_features_extracts_observed_shortwave(self) -> None:
        class FakeResponse:
            status_code = 200
            url = "https://satellite-api.open-meteo.com/v1/satellite-radiation?test=1"
            headers = {}

            def raise_for_status(self) -> None:
                return None

            def json(self):
                return {
                    "hourly": {
                        "shortwave_radiation": [0.0, 120.0, 600.0, 420.0],
                        "direct_radiation": [0.0, 70.0, 500.0, 300.0],
                        "diffuse_radiation": [0.0, 50.0, 100.0, 120.0],
                    }
                }

        provenance = {}
        with patch("scan_weather_market.requests_get", return_value=FakeResponse()) as mocked:
            features = fetch_open_meteo_satellite_radiation_features(
                30.0,
                104.0,
                "2026-06-01",
                "Asia/Shanghai",
                provenance=provenance,
            )

        self.assertTrue(mocked.called)
        self.assertIn("shortwave_radiation", mocked.call_args.kwargs["params"]["hourly"])
        self.assertEqual(features["source"], "open_meteo_satellite_radiation")
        self.assertAlmostEqual(features["solar_radiation_observed"], (120.0 + 600.0 + 420.0) / 3.0)
        self.assertIn("solar_radiation_observed", provenance["feature_keys"])


if __name__ == "__main__":
    unittest.main()

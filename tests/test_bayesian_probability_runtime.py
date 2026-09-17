import os
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from posterior_pipeline_entry_runtime import (
    _temperature_trajectory_channel_probs,
    _temperature_momentum_variable,
    apply_bayesian_probability_update,
    apply_hard_source_posterior_override_to_distribution,
    build_historical_bayesian_context,
    resolve_hard_source_posterior_override,
)
from climo_regime_runtime import (
    basin_heat_mechanism_model,
    cap_climo_evidence_by_pp_delta,
    build_climo_regime_features,
    climo_regime_evidence_scale,
    climo_high_temp_potential_gate,
    load_climo_regime_context,
)
from bayesian_data_role_contract import (
    ROLE_BACKGROUND,
    ROLE_CALIBRATION,
    ROLE_DISPLAY_TRADEABILITY,
    ROLE_LIKELIHOOD,
    ROLE_MODEL_EVIDENCE,
    ROLE_PRIOR,
    audit_bayesian_data_role_contract,
    contract_by_source_id,
)


class BayesianProbabilityProductionDefaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_env = os.environ.copy()
        os.environ.pop("PM_BAYES_LEGACY_ADDITIVE_COMPAT_ENABLED", None)
        os.environ.pop("PM_BAYES_LEGACY_ADDITIVE_COMPAT_ALLOW_UNSAFE_RUNTIME", None)
        os.environ.pop("PM_BAYES_HIERARCHICAL_PRODUCTION_ENABLED", None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._old_env)

    def test_default_probability_update_uses_hierarchical_pipeline(self) -> None:
        os.environ["PM_BAYES_PROBABILITY_ENABLED"] = "1"
        os.environ["PM_LCB_STAGE_Z_DEFAULTS_ENABLED"] = "0"
        items = [
            {
                "label": "30°C",
                "rounding_rule": "whole_degrees",
                "q": {"q_round": 0.40, "q_raw": 0.40},
                "per_model_q": {
                    "gfs_seamless": {"q_round": 0.35},
                    "ecmwf_ifs025": {"q_round": 0.45},
                },
                "active_models": ["gfs_seamless", "ecmwf_ifs025"],
                "weather_likelihood_features": {
                    "event_date": "2026-07-03",
                    "zuuu_metar_raw_awc_latest_at": "2026-07-03T12:00:00+08:00",
                    "observed_temp_c": 28.0,
                    "dewpoint_c": 22.0,
                    "visibility": "9999",
                    "cloud_text": "FEW011 SCT020",
                    "temperature_momentum_effective_c_per_hour": 3.0,
                },
            },
            {
                "label": "31°C",
                "rounding_rule": "whole_degrees",
                "q": {"q_round": 0.35, "q_raw": 0.35},
                "per_model_q": {
                    "gfs_seamless": {"q_round": 0.40},
                    "ecmwf_ifs025": {"q_round": 0.35},
                },
                "active_models": ["gfs_seamless", "ecmwf_ifs025"],
            },
            {
                "label": "32°C",
                "rounding_rule": "whole_degrees",
                "q": {"q_round": 0.25, "q_raw": 0.25},
                "per_model_q": {
                    "gfs_seamless": {"q_round": 0.25},
                    "ecmwf_ifs025": {"q_round": 0.20},
                },
                "active_models": ["gfs_seamless", "ecmwf_ifs025"],
            },
        ]

        meta = apply_bayesian_probability_update(
            items,
            weights={"gfs_seamless": 0.25, "ecmwf_ifs025": 0.75},
            model_cache_freshness=1.0,
            days_to_event=0.0,
            lcb_stage_tz="Asia/Shanghai",
        )

        self.assertTrue(meta["enabled"])
        self.assertEqual(meta["source"], "hierarchical_posterior_pipeline_runtime_v1")
        self.assertEqual(meta["posterior_engine"], "sequential_remaining_warming_bayes_runtime_v31")
        self.assertNotIn("old_additive_evidence_pool_used", meta)
        self.assertNotIn("production_replacement_required", meta)
        self.assertNotIn("legacy_additive_evidence_bridge", meta)
        self.assertIn("layer_contributions", meta)
        self.assertIn("forecast_weak_likelihood", meta["layer_contributions"])
        self.assertIn("metar_strong_likelihood", meta["layer_contributions"])
        self.assertAlmostEqual(sum(item["q"]["q_raw"] for item in items), 1.0, places=12)
        self.assertEqual(items[0]["q_bayes_meta"]["source"], "hierarchical_posterior_pipeline_runtime_v1")
        self.assertNotIn("old_additive_evidence_pool_used", items[0]["q_bayes_meta"])


    def test_legacy_flag_alone_does_not_disable_hierarchical_pipeline(self) -> None:
        os.environ["PM_BAYES_PROBABILITY_ENABLED"] = "1"
        os.environ["PM_BAYES_LEGACY_ADDITIVE_COMPAT_ENABLED"] = "1"
        os.environ.pop("PM_BAYES_LEGACY_ADDITIVE_COMPAT_ALLOW_UNSAFE_RUNTIME", None)
        items = [
            {
                "label": "30°C",
                "rounding_rule": "whole_degrees",
                "q": {"q_round": 0.45, "q_raw": 0.45},
                "per_model_q": {"gfs_seamless": {"q_round": 0.45}},
                "active_models": ["gfs_seamless"],
            },
            {
                "label": "31°C",
                "rounding_rule": "whole_degrees",
                "q": {"q_round": 0.55, "q_raw": 0.55},
                "per_model_q": {"gfs_seamless": {"q_round": 0.55}},
                "active_models": ["gfs_seamless"],
            },
        ]

        meta = apply_bayesian_probability_update(
            items,
            weights={"gfs_seamless": 1.0},
            model_cache_freshness=1.0,
            days_to_event=0.0,
            lcb_stage_tz="Asia/Shanghai",
        )

        self.assertEqual(meta["source"], "hierarchical_posterior_pipeline_runtime_v1")
        self.assertNotIn("old_additive_evidence_pool_used", meta)

    def test_intraday_helpers_no_longer_import_legacy_additive_runtime(self) -> None:
        root = Path(__file__).resolve().parents[1]
        runtime = (root / "posterior_pipeline_entry_runtime.py").read_text(encoding="utf-8")

        helper_block = runtime.split("def _parse_event_date", 1)[0]
        helper_block += runtime.split("def _format_bucket_number", 1)[0].rsplit("def _temperature_trajectory_channel_probs", 1)[-1]
        helper_block += runtime.split("def _taf_time_scale", 1)[0].rsplit("def _build_physical_d0_explanation", 1)[-1]

        self.assertIn("from intraday_likelihood_runtime import physical_likelihood_quality_scale", helper_block)
        self.assertIn("from intraday_likelihood_runtime import temperature_trajectory_realization_probs", helper_block)
        self.assertIn("from intraday_likelihood_runtime import build_physical_d0_explanation", helper_block)
        self.assertNotIn(
            "from legacy_additive_bayesian_probability_runtime import _physical_likelihood" + "_evidence_scale",
            helper_block,
        )
        self.assertNotIn("from legacy_additive_bayesian_probability_runtime import _temperature_trajectory_channel_probs", helper_block)
        self.assertNotIn("from legacy_additive_bayesian_probability_runtime import _build_physical_d0_explanation", helper_block)


class BayesianProbabilityRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_env = os.environ.copy()
        os.environ.pop("PM_BAYES_LEGACY_ADDITIVE_COMPAT_ENABLED", None)
        os.environ.pop("PM_BAYES_LEGACY_ADDITIVE_COMPAT_ALLOW_UNSAFE_RUNTIME", None)
        os.environ.pop("PM_BAYES_HIERARCHICAL_PRODUCTION_ENABLED", None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._old_env)

    def _force_hierarchical_production_runtime(self) -> None:
        os.environ.pop("PM_BAYES_LEGACY_ADDITIVE_COMPAT_ENABLED", None)
        os.environ.pop("PM_BAYES_LEGACY_ADDITIVE_COMPAT_ALLOW_UNSAFE_RUNTIME", None)
        os.environ.pop("PM_BAYES_HIERARCHICAL_PRODUCTION_ENABLED", None)

    def _pipeline_runtime_output(self, meta: dict, key: str) -> dict:
        pipeline = meta["hierarchical_posterior_pipeline"]
        outputs = pipeline["runtime_outputs"]
        output = outputs[key]
        self.assertFalse(output["production_entry_enabled"])
        self.assertFalse(output["guards"]["writes_archive_db"])
        self.assertFalse(output["guards"]["places_orders"])
        return output

    def _source_trace_by_id(self, output: dict, source_id: str) -> dict:
        for row in output["source_trace"]:
            if row.get("source_id") == source_id:
                return row
        self.fail(f"source_trace missing source_id={source_id}")

    def _source_group_trace(self, output: dict, group: str) -> dict:
        for row in output["source_group_trace"]:
            if row.get("group") == group:
                return row
        self.fail(f"source_group_trace missing group={group}")

    def _assert_hierarchical_probability_runtime(self, meta: dict) -> None:
        self.assertEqual(meta["source"], "hierarchical_posterior_pipeline_runtime_v1")
        self.assertEqual(meta["posterior_engine"], "sequential_remaining_warming_bayes_runtime_v31")
        self.assertNotIn("old_additive_evidence_pool_used", meta)
        self.assertNotIn("production_replacement_required", meta)

    def test_temperature_momentum_suppresses_untrusted_minute_spikes(self) -> None:
        variable = _temperature_momentum_variable(
            {
                "temperature_momentum_24h_c_per_hour": 0.62,
                "temperature_momentum_12h_c_per_hour": 0.92,
                "temperature_momentum_6h_c_per_hour": 1.17,
                "temperature_momentum_3h_c_per_hour": 0.67,
                "temperature_momentum_2h_c_per_hour": 1.0,
                "temperature_momentum_1h_c_per_hour": 1.0,
                "temperature_momentum_45m_c_per_hour": 1.0,
                "temperature_momentum_30m_c_per_hour": 1.0,
                "temperature_momentum_15m_c_per_hour": -2.17,
                "temperature_momentum_5m_c_per_hour": -23.37,
                "temperature_momentum_3m_c_per_hour": -23.37,
                "temperature_momentum_effective_c_per_hour": -6.11,
                "temperature_momentum_realization_state": "cooling",
            }
        )

        self.assertNotEqual(variable["metrics"]["realization_state"], "cooling")
        self.assertGreater(variable["metrics"]["effective_rate_c_per_hour"], 0.0)
        self.assertIn("5m", variable["metrics"]["suppressed_untrusted_rates_c_per_hour"])
        self.assertIn("3m", variable["metrics"]["suppressed_untrusted_rates_c_per_hour"])

    def test_updates_probability_vector_and_preserves_static_q(self) -> None:
        os.environ["PM_BAYES_PROBABILITY_ENABLED"] = "1"
        os.environ["PM_LCB_STAGE_Z_DEFAULTS_ENABLED"] = "0"
        os.environ.pop("PM_BAYES_LEGACY_ADDITIVE_COMPAT_ENABLED", None)
        os.environ.pop("PM_BAYES_LEGACY_ADDITIVE_COMPAT_ALLOW_UNSAFE_RUNTIME", None)
        os.environ.pop("PM_BAYES_HIERARCHICAL_PRODUCTION_ENABLED", None)
        items = [
            {
                "q": {"q_round": 0.20, "q_raw": 0.20},
                "per_model_q": {
                    "gfs_seamless": {"q_round": 0.10},
                    "ecmwf_ifs025": {"q_round": 0.30},
                },
                "active_models": ["gfs_seamless", "ecmwf_ifs025"],
            },
            {
                "q": {"q_round": 0.80, "q_raw": 0.80},
                "per_model_q": {
                    "gfs_seamless": {"q_round": 0.90},
                    "ecmwf_ifs025": {"q_round": 0.70},
                },
                "active_models": ["gfs_seamless", "ecmwf_ifs025"],
            },
        ]

        meta = apply_bayesian_probability_update(
            items,
            weights={"gfs_seamless": 0.25, "ecmwf_ifs025": 0.75},
            model_cache_freshness=1.0,
            days_to_event=1.0,
        )

        self.assertTrue(meta["enabled"])
        self.assertEqual(meta["source"], "hierarchical_posterior_pipeline_runtime_v1")
        self.assertEqual(meta["posterior_engine"], "sequential_remaining_warming_bayes_runtime_v31")
        self.assertNotIn("old_additive_evidence_pool_used", meta)
        self.assertNotIn("production_replacement_required", meta)
        self.assertNotIn("legacy_additive_evidence_bridge", meta)
        self.assertNotIn("legacy_additive_evidence_bridge", items[0]["q_bayes_meta"])
        self.assertIn("posterior_temperature_distribution", meta)
        self.assertAlmostEqual(sum(item["q"]["q_raw"] for item in items), 1.0, places=12)
        self.assertEqual(items[0]["q"]["q_raw_static"], 0.20)
        self.assertEqual(items[1]["q"]["q_raw_static"], 0.80)
        self.assertAlmostEqual(items[0]["q"]["q_raw_no"], 1.0 - items[0]["q"]["q_raw"], places=12)
        self.assertAlmostEqual(
            items[0]["q"]["q_bayes_posterior_no"],
            1.0 - items[0]["q"]["q_bayes_posterior"],
            places=12,
        )
        self.assertLessEqual(items[0]["q"]["q_bayes_lcb"], items[0]["q"]["q_bayes_posterior"])
        self.assertGreaterEqual(items[0]["q"]["q_bayes_ucb"], items[0]["q"]["q_bayes_posterior"])

    def test_hard_source_observation_constrains_lower_buckets(self) -> None:
        self._force_hierarchical_production_runtime()
        os.environ["PM_BAYES_PROBABILITY_ENABLED"] = "1"
        os.environ["PM_BAYES_LIKELIHOOD_ENABLED"] = "0"
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_ENABLED"] = "1"
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_ENABLED"] = "0"
        os.environ["PM_LCB_STAGE_Z_DEFAULTS_ENABLED"] = "0"
        items = [
            {
                "label": "30°C",
                "rounding_rule": "whole_degrees",
                "q": {"q_round": 0.70, "q_raw": 0.70},
                "per_model_q": {"m": {"q_round": 0.70}},
                "active_models": ["m"],
                "weather_likelihood_features": {
                    "market_var": "temperature_2m_max",
                    "event_date": "2026-06-18",
                    "expected_peak_hour_local": 16.0,
                    "source": "zuuu_metar_raw_awc",
                    "zuuu_metar_raw_awc_today_max_c": 31.0,
                    "zuuu_metar_raw_awc_today_max_at": "2026-06-18T07:59:00+00:00",
                    "zuuu_metar_raw_awc_latest_at": "2026-06-18T07:59:00+00:00",
                    "zuuu_metar_raw_awc_fetched_at": "2026-06-18T08:00:00+00:00",
                    "zuuu_metar_raw_awc_target_event_date": "2026-06-18",
                    "zuuu_metar_raw_awc_date_mismatch": False,
                },
            },
            {
                "label": "31°C",
                "rounding_rule": "whole_degrees",
                "q": {"q_round": 0.20, "q_raw": 0.20},
                "per_model_q": {"m": {"q_round": 0.20}},
                "active_models": ["m"],
            },
            {
                "label": "32°C",
                "rounding_rule": "whole_degrees",
                "q": {"q_round": 0.10, "q_raw": 0.10},
                "per_model_q": {"m": {"q_round": 0.10}},
                "active_models": ["m"],
            },
        ]

        meta = apply_bayesian_probability_update(
            items,
            weights={"m": 1.0},
            model_cache_freshness=1.0,
            days_to_event=0.0,
            lcb_stage_tz="Asia/Shanghai",
        )

        self.assertEqual(meta["source"], "hierarchical_posterior_pipeline_runtime_v1")
        self.assertEqual(meta["posterior_engine"], "sequential_remaining_warming_bayes_runtime_v31")
        self.assertNotIn("old_additive_evidence_pool_used", meta)
        self.assertEqual(meta["hard_constraints"]["combined_hard_mask"], {"30°C": 0.0, "31°C": 1.0, "32°C": 1.0})
        self.assertEqual(meta["hard_constraints"]["posterior"]["masked_zero_buckets"], ["30°C"])
        self.assertIn("observed_today_max_c", meta["hard_mask_reason"])
        self.assertEqual(items[0]["q"]["q_bayes_posterior"], 0.0)
        self.assertGreater(items[1]["q"]["q_bayes_posterior"], 0.0)
        self.assertGreater(items[2]["q"]["q_bayes_posterior"], 0.0)
        self.assertAlmostEqual(
            items[1]["q"]["q_bayes_posterior"] + items[2]["q"]["q_bayes_posterior"],
            1.0,
            places=12,
        )
        self.assertEqual(items[1]["q_bayes_meta"]["source"], "hierarchical_posterior_pipeline_runtime_v1")
        self.assertEqual(items[1]["q_bayes_meta"]["temperature_bucket_probability_mode"], "hierarchical_joint_likelihood")

    def test_nonfinal_hard_source_keeps_atomic_distribution_not_single_point(self) -> None:
        self._force_hierarchical_production_runtime()
        os.environ["PM_BAYES_PROBABILITY_ENABLED"] = "1"
        os.environ["PM_BAYES_LIKELIHOOD_ENABLED"] = "0"
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_ENABLED"] = "1"
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_ENABLED"] = "0"
        os.environ["PM_BAYES_ATOMIC_TEMPERATURE_BUCKET_DISPLAY_ENABLED"] = "1"
        os.environ["PM_LCB_STAGE_Z_DEFAULTS_ENABLED"] = "0"
        items = [
            {
                "label": "30°C or below",
                "rounding_rule": "whole_degrees",
                "q": {"q_round": 0.60, "q_raw": 0.60},
                "per_model_q": {"m": {"q_round": 0.60}},
                "active_models": ["m"],
                "weather_likelihood_features": {
                    "market_var": "temperature_2m_max",
                    "event_date": "2026-06-18",
                    "source": "zuuu_metar_raw_awc",
                    "zuuu_metar_raw_awc_today_max_c": 31.0,
                    "zuuu_metar_raw_awc_today_max_at": "2026-06-18T07:59:00+00:00",
                    "zuuu_metar_raw_awc_target_event_date": "2026-06-18",
                },
            },
            {
                "label": "31°C",
                "rounding_rule": "whole_degrees",
                "q": {"q_round": 0.25, "q_raw": 0.25},
                "per_model_q": {"m": {"q_round": 0.25}},
                "active_models": ["m"],
            },
            {
                "label": "32°C",
                "rounding_rule": "whole_degrees",
                "q": {"q_round": 0.15, "q_raw": 0.15},
                "per_model_q": {"m": {"q_round": 0.15}},
                "active_models": ["m"],
            },
        ]

        meta = apply_bayesian_probability_update(
            items,
            weights={"m": 1.0},
            model_cache_freshness=1.0,
            days_to_event=0.0,
            lcb_stage_tz="Asia/Shanghai",
        )

        self.assertEqual(meta["source"], "hierarchical_posterior_pipeline_runtime_v1")
        self.assertEqual(meta["posterior_engine"], "sequential_remaining_warming_bayes_runtime_v31")
        self.assertNotIn("old_additive_evidence_pool_used", meta)
        self.assertEqual(
            meta["hard_constraints"]["combined_hard_mask"],
            {"30°C or below": 0.0, "31°C": 1.0, "32°C": 1.0},
        )
        self.assertFalse(meta["hard_constraints"]["metar_strong"]["hard_lock"])
        self.assertEqual(items[0]["q"]["q_bayes_posterior"], 0.0)
        self.assertEqual(items[0]["q_temperature_bucket_posterior"], 0.0)
        self.assertGreater(items[1]["q"]["q_bayes_posterior"], 0.0)
        self.assertGreater(items[2]["q"]["q_bayes_posterior"], 0.0)
        self.assertLess(items[1]["q"]["q_bayes_posterior"], 1.0)
        self.assertLess(items[2]["q"]["q_bayes_posterior"], 1.0)
        self.assertAlmostEqual(
            items[1]["q"]["q_bayes_posterior"] + items[2]["q"]["q_bayes_posterior"],
            1.0,
            places=12,
        )
        self.assertGreater(items[1]["q_temperature_bucket_posterior"], 0.0)
        self.assertGreater(items[2]["q_temperature_bucket_posterior"], 0.0)
        self.assertLess(items[1]["q_temperature_bucket_posterior"], 1.0)
        self.assertLess(items[2]["q_temperature_bucket_posterior"], 1.0)


    def test_wu_twc_intraday_alert_does_not_hard_zero_above_awc_floor(self) -> None:
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_ENABLED"] = "1"
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_ENABLED"] = "0"
        rows = [
            {"outcome": "27°C", "q_bayes_posterior": 0.40, "q_raw": 0.40},
            {"outcome": "28°C", "q_bayes_posterior": 0.30, "q_raw": 0.30},
            {"outcome": "29°C", "q_bayes_posterior": 0.20, "q_raw": 0.20},
            {"outcome": "30°C", "q_bayes_posterior": 0.10, "q_raw": 0.10},
        ]
        meta = apply_hard_source_posterior_override_to_distribution(
            rows,
            {
                "market_var": "temperature_2m_max",
                "event_date": "2026-06-27",
                "zuuu_metar_raw_awc_today_max_c": 28.0,
                "zuuu_metar_raw_awc_today_max_at": "2026-06-27T04:00:00+00:00",
                "zuuu_metar_raw_awc_target_event_date": "2026-06-27",
                "wu_twc_observed_intraday_today_max_c": 29.0,
                "wu_twc_observed_intraday_today_max_at": "2026-06-27T06:36:51+00:00",
                "wu_twc_observed_intraday_current_temp_c": 27.77777777777778,
                "wu_twc_observed_intraday_current_error": "current_observation_event_date_mismatch",
                "wu_twc_observed_intraday_max_error": "current_observation_event_date_mismatch",
                "weather_source_delay_deformation_alert": {
                    "alert": True,
                    "codes": ["wu_nowcast_temperature_deformation"],
                },
            },
            base_probs=[0.40, 0.30, 0.20, 0.10],
            rounding_rule="whole_degrees",
            market_var="temperature_2m_max",
            event_tz="Asia/Shanghai",
            event_date="2026-06-27",
        )

        self.assertTrue(meta["applied"])
        self.assertEqual(meta["source"], "zuuu_metar_raw_awc")
        self.assertEqual(meta["target_bucket"], "28°C")
        self.assertTrue(rows[0]["q_posterior_observed_constraints_hard_zero"])
        self.assertFalse(rows[1].get("q_posterior_observed_constraints_hard_zero", False))
        self.assertGreater(rows[1]["q_bayes_posterior"], 0.0)

    def test_station_observation_intraday_constrains_lower_buckets_without_final_lock(self) -> None:
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_ENABLED"] = "1"
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_ENABLED"] = "0"
        rows = [
            {"outcome": "32°C", "q_bayes_posterior": 0.60, "q_raw": 0.60},
            {"outcome": "33°C", "q_bayes_posterior": 0.25, "q_raw": 0.25},
            {"outcome": "34°C", "q_bayes_posterior": 0.15, "q_raw": 0.15},
        ]
        meta = apply_hard_source_posterior_override_to_distribution(
            rows,
            {
                "market_var": "temperature_2m_max",
                "event_date": "2026-06-19",
                "observed_today_max_c": 34.0,
                "observed_today_max_source": "station_observations.wunderground_history",
                "observed_today_max_trusted": True,
                "station_observation_triggered": True,
                "station_observation_observed_at": "2026-06-19T08:00:00+00:00",
                "station_observation_created_at": "2026-06-19T08:08:01+00:00",
            },
            base_probs=[0.60, 0.25, 0.15],
            rounding_rule="whole_degrees",
            market_var="temperature_2m_max",
            event_tz="Asia/Shanghai",
            event_date="2026-06-19",
        )

        self.assertTrue(meta["applied"])
        self.assertEqual(meta["source"], "station_observations.wunderground_history")
        self.assertFalse(meta["final_bucket_lock"])
        self.assertEqual(meta["constraint_mode"], "hard_source_lower_bound_constraint")
        self.assertEqual([row["q_bayes_posterior"] for row in rows], [0.0, 0.0, 1.0])
        self.assertTrue(rows[0]["q_posterior_observed_constraints_hard_zero"])

    def test_hard_source_final_lock_sets_single_bucket(self) -> None:
        self._force_hierarchical_production_runtime()
        os.environ["PM_BAYES_PROBABILITY_ENABLED"] = "1"
        os.environ["PM_BAYES_LIKELIHOOD_ENABLED"] = "0"
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_ENABLED"] = "1"
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_ENABLED"] = "0"
        os.environ["PM_LCB_STAGE_Z_DEFAULTS_ENABLED"] = "0"
        features = {
            "market_var": "temperature_2m_max",
            "event_date": "2026-06-18",
            "source": "zuuu_metar_raw_awc",
            "zuuu_metar_raw_awc_today_max_c": 31.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-18T07:59:00+00:00",
            "zuuu_metar_raw_awc_target_event_date": "2026-06-18",
            "hard_source_final_daily_max": True,
        }
        items = [
            {
                "label": "30°C",
                "rounding_rule": "whole_degrees",
                "q": {"q_round": 0.70, "q_raw": 0.70},
                "per_model_q": {"m": {"q_round": 0.70}},
                "active_models": ["m"],
                "weather_likelihood_features": features,
            },
            {
                "label": "31°C",
                "rounding_rule": "whole_degrees",
                "q": {"q_round": 0.20, "q_raw": 0.20},
                "per_model_q": {"m": {"q_round": 0.20}},
                "active_models": ["m"],
            },
            {
                "label": "32°C",
                "rounding_rule": "whole_degrees",
                "q": {"q_round": 0.10, "q_raw": 0.10},
                "per_model_q": {"m": {"q_round": 0.10}},
                "active_models": ["m"],
            },
        ]

        meta = apply_bayesian_probability_update(
            items,
            weights={"m": 1.0},
            model_cache_freshness=1.0,
            days_to_event=0.0,
            lcb_stage_tz="Asia/Shanghai",
        )

        self.assertEqual(meta["source"], "hierarchical_posterior_pipeline_runtime_v1")
        self.assertEqual(meta["posterior_engine"], "sequential_remaining_warming_bayes_runtime_v31")
        self.assertEqual(meta["phase"], "D0_hard")
        self.assertEqual(meta["hard_constraints"]["combined_hard_mask"], {"30°C": 0.0, "31°C": 1.0, "32°C": 0.0})
        self.assertTrue(meta["hard_constraints"]["metar_strong"]["hard_lock"])
        self.assertEqual(meta["hard_constraints"]["metar_strong"]["official_final_result"], 31.0)
        self.assertIn("official_final_result", meta["hard_mask_reason"])
        self.assertIn("final_daily_max_bucket_lock", meta["hard_mask_reason"])
        self.assertEqual(items[0]["q"]["q_bayes_posterior"], 0.0)
        self.assertEqual(items[1]["q"]["q_bayes_posterior"], 1.0)
        self.assertEqual(items[2]["q"]["q_bayes_posterior"], 0.0)

    def test_wu_twc_intraday_observation_constrains_lower_buckets_without_final_lock(self) -> None:
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_ENABLED"] = "1"
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_ENABLED"] = "0"
        features = {
            "market_var": "temperature_2m_max",
            "event_date": "2026-06-27",
            "source": "wu_twc_observed_intraday",
            "wu_twc_observed_intraday_current_temp_c": 27.78,
            "wu_twc_observed_intraday_today_max_c": 28.0,
            "wu_twc_observed_intraday_current_at": "2026-06-27T03:46:33+00:00",
            "wu_twc_observed_intraday_today_max_at": "2026-06-27T03:46:33+00:00",
            "wu_twc_observed_intraday_fetched_at": "2026-06-27T03:48:44+00:00",
            "wu_twc_observed_intraday_stale": False,
        }

        meta = resolve_hard_source_posterior_override(
            ["27°C or below", "28°C", "29°C", "30°C"],
            features,
            base_probs=[0.4, 0.3, 0.2, 0.1],
            rounding_rule="whole_degrees",
            market_var="temperature_2m_max",
            event_tz="Asia/Shanghai",
            event_date="2026-06-27",
        )

        self.assertTrue(meta["applied"])
        self.assertEqual(meta["source"], "wu_twc_observed_intraday")
        self.assertEqual(meta["target_bucket"], "28°C")
        self.assertFalse(meta["final_bucket_lock"])
        self.assertEqual(meta["constraint_mode"], "hard_source_lower_bound_constraint")
        self.assertEqual(meta["posterior_probs"][0], 0.0)
        self.assertAlmostEqual(meta["posterior_probs"][1], 0.5, places=12)
        self.assertAlmostEqual(meta["posterior_probs"][2], 1.0 / 3.0, places=12)
        self.assertAlmostEqual(meta["posterior_probs"][3], 1.0 / 6.0, places=12)

    def test_wu_twc_intraday_observation_bypasses_missing_peak_window_as_lower_bound(self) -> None:
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_ENABLED"] = "1"
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_ENABLED"] = "1"
        features = {
            "market_var": "temperature_2m_max",
            "event_date": "2026-06-27",
            "source": "wu_twc_observed_intraday",
            "wu_twc_observed_intraday_current_temp_c": 27.78,
            "wu_twc_observed_intraday_today_max_c": 28.0,
            "wu_twc_observed_intraday_current_at": "2026-06-27T03:46:33+00:00",
            "wu_twc_observed_intraday_today_max_at": "2026-06-27T03:46:33+00:00",
            "wu_twc_observed_intraday_fetched_at": "2026-06-27T03:48:44+00:00",
            "wu_twc_observed_intraday_stale": False,
        }

        meta = resolve_hard_source_posterior_override(
            ["27°C or below", "28°C", "29°C", "30°C"],
            features,
            base_probs=[0.4, 0.3, 0.2, 0.1],
            rounding_rule="whole_degrees",
            market_var="temperature_2m_max",
            event_tz="Asia/Shanghai",
            event_date="2026-06-27",
        )

        self.assertTrue(meta["applied"])
        self.assertEqual(meta["source"], "wu_twc_observed_intraday")
        self.assertEqual(meta["target_bucket"], "28°C")
        self.assertEqual(meta["window"]["reason"], "wu_forecast_peak_time_missing")
        self.assertTrue(meta["window_bypassed_by_strong_observation"])
        self.assertFalse(meta["final_bucket_lock"])
        self.assertEqual(meta["constraint_mode"], "hard_source_lower_bound_constraint")
        self.assertEqual(meta["posterior_probs"][0], 0.0)

    def test_metar_hard_observation_bypasses_peak_window_as_strong_likelihood(self) -> None:
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_ENABLED"] = "1"
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_ENABLED"] = "1"
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_MODE"] = "wu_forecast"
        features = {
            "market_var": "temperature_2m_max",
            "event_date": "2026-06-18",
            "source": "zuuu_metar_raw_awc",
            "zuuu_metar_raw_awc_today_max_c": 33.0,
            "zuuu_metar_raw_awc_target_event_date": "2026-06-18",
            "zuuu_metar_raw_awc_today_max_at": "2026-06-18T02:00:00+00:00",
            "wu_forecast_peak_at": "2026-06-18T07:00:00+00:00",
        }

        meta = resolve_hard_source_posterior_override(
            ["32°C", "33°C", "34°C"],
            features,
            base_probs=[0.55, 0.30, 0.15],
            rounding_rule="whole_degrees",
            market_var="temperature_2m_max",
            event_tz="Asia/Shanghai",
            event_date="2026-06-18",
            now_dt=datetime(2026, 6, 18, 4, 0, tzinfo=timezone.utc),
        )

        self.assertTrue(meta["applied"])
        self.assertFalse(meta["window"]["allowed"])
        self.assertTrue(meta["window_bypassed_by_strong_observation"])
        self.assertFalse(meta["final_bucket_lock"])
        self.assertEqual(meta["constraint_mode"], "hard_source_lower_bound_constraint")
        self.assertEqual(meta["posterior_probs"][0], 0.0)
        self.assertAlmostEqual(meta["posterior_probs"][1], 2.0 / 3.0, places=12)
        self.assertAlmostEqual(meta["posterior_probs"][2], 1.0 / 3.0, places=12)

    def test_hard_source_final_lock_bypasses_peak_window(self) -> None:
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_ENABLED"] = "1"
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_ENABLED"] = "1"
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_MODE"] = "wu_forecast"
        features = {
            "market_var": "temperature_2m_max",
            "event_date": "2026-06-18",
            "source": "zuuu_metar_raw_awc",
            "zuuu_metar_raw_awc_today_max_c": 31.0,
            "zuuu_metar_raw_awc_target_event_date": "2026-06-18",
            "settlement_actual": 31.0,
            "market_closed": True,
            "wu_forecast_peak_at": "2026-06-18T07:00:00+00:00",
        }

        meta = resolve_hard_source_posterior_override(
            ["30°C", "31°C"],
            features,
            base_probs=[0.7, 0.3],
            rounding_rule="whole_degrees",
            market_var="temperature_2m_max",
            event_tz="Asia/Shanghai",
            event_date="2026-06-18",
            now_dt=datetime(2026, 6, 18, 4, 0, tzinfo=timezone.utc),
        )

        self.assertTrue(meta["applied"])
        self.assertTrue(meta["final_bucket_lock"])
        self.assertEqual(meta["constraint_mode"], "final_bucket_lock")
        self.assertEqual(meta["final_lock_reason"], "settlement_actual_known")
        self.assertEqual(meta["posterior_probs"], [0.0, 1.0])

    def test_deformed_wu_intraday_settlement_actual_does_not_final_lock(self) -> None:
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_ENABLED"] = "1"
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_ENABLED"] = "0"
        os.environ["PM_BAYES_BLOCK_DEFORMED_WU_INTRADAY_SETTLEMENT_LOCK"] = "1"
        features = {
            "market_var": "temperature_2m_max",
            "event_date": "2026-06-27",
            "settlement_actual": 29.0,
            "settlement_actual_source": "event_runs.settlement_actual",
            "zuuu_metar_raw_awc_today_max_c": 28.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-27T04:00:00+00:00",
            "zuuu_metar_raw_awc_target_event_date": "2026-06-27",
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

        meta = resolve_hard_source_posterior_override(
            ["27°C", "28°C", "29°C", "30°C"],
            features,
            base_probs=[0.10, 0.50, 0.25, 0.15],
            rounding_rule="whole_degrees",
            market_var="temperature_2m_max",
            event_tz="Asia/Shanghai",
            event_date="2026-06-27",
        )

        self.assertTrue(meta["applied"])
        self.assertFalse(meta["final_bucket_lock"])
        self.assertEqual(meta["source"], "zuuu_metar_raw_awc")
        self.assertEqual(meta["target_bucket"], "28°C")
        self.assertEqual(meta["blocked_candidate_count"], 1)
        self.assertEqual(meta["constraint_mode"], "hard_source_lower_bound_constraint")
        self.assertEqual(meta["posterior_probs"][0], 0.0)
        self.assertGreater(meta["posterior_probs"][1], 0.0)
        self.assertGreater(meta["posterior_probs"][2], 0.0)

    def test_settlement_actual_reverse_fahrenheit_boundary_does_not_final_lock(self) -> None:
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_ENABLED"] = "1"
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_ENABLED"] = "0"
        os.environ["PM_BAYES_BLOCK_REVERSE_F_SETTLEMENT_BOUNDARY"] = "1"
        features = {
            "market_var": "temperature_2m_max",
            "event_date": "2026-06-18",
            "settlement_actual": 32.7777777778,
            "settlement_actual_source": "event_runs.settlement_actual",
        }

        meta = resolve_hard_source_posterior_override(
            ["32°C", "33°C", "34°C"],
            features,
            base_probs=[0.2, 0.6, 0.2],
            rounding_rule="whole_degrees",
            market_var="temperature_2m_max",
            event_tz="Asia/Shanghai",
            event_date="2026-06-18",
        )

        self.assertTrue(meta["applied"])
        self.assertTrue(meta["final_bucket_lock"])
        self.assertEqual(meta["target_bucket"], "33°C")
        self.assertEqual(meta["constraint_mode"], "final_bucket_lock")

    def test_settlement_actual_below_awc_observation_does_not_final_lock(self) -> None:
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_ENABLED"] = "1"
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_ENABLED"] = "0"
        os.environ["PM_BAYES_BLOCK_SETTLEMENT_BELOW_AWC_OBSERVED"] = "1"
        features = {
            "market_var": "temperature_2m_max",
            "event_date": "2026-06-23",
            "settlement_actual": 27.5,
            "settlement_actual_source": "event_runs.settlement_actual",
            "zuuu_metar_raw_awc_today_max_c": 30.0,
            "zuuu_metar_raw_awc_target_event_date": "2026-06-23",
        }

        meta = resolve_hard_source_posterior_override(
            ["27°C", "28°C", "29°C", "30°C"],
            features,
            base_probs=[0.25, 0.25, 0.25, 0.25],
            rounding_rule="whole_degrees",
            market_var="temperature_2m_max",
            event_tz="Asia/Shanghai",
            event_date="2026-06-23",
        )

        self.assertTrue(meta["applied"])
        self.assertFalse(meta["final_bucket_lock"])
        self.assertEqual(meta["source"], "zuuu_metar_raw_awc")
        self.assertEqual(meta["constraint_mode"], "hard_source_lower_bound_constraint")
        self.assertEqual(meta["blocked_candidate_count"], 1)
        self.assertEqual(meta["posterior_probs"], [0.0, 0.0, 0.0, 1.0])

    def test_hard_source_override_window_accepts_local_time_override(self) -> None:
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_ENABLED"] = "1"
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_ENABLED"] = "1"
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_MODE"] = "wu_forecast"
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_LOCAL_DATE_OVERRIDE"] = "2026-06-18"
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_LOCAL_HOUR_OVERRIDE"] = "14.0"
        features = {
            "market_var": "temperature_2m_max",
            "event_date": "2026-06-18",
            "source": "zuuu_metar_raw_awc",
            "zuuu_metar_raw_awc_today_max_c": 31.0,
            "zuuu_metar_raw_awc_target_event_date": "2026-06-18",
            "zuuu_metar_raw_awc_fetched_at": "2026-06-18T04:00:00+00:00",
            "wu_forecast_peak_at": "2026-06-18T07:00:00+00:00",
        }

        meta = resolve_hard_source_posterior_override(
            ["30°C", "31°C"],
            features,
            base_probs=[0.7, 0.3],
            rounding_rule="whole_degrees",
            market_var="temperature_2m_max",
            event_tz="Asia/Shanghai",
            event_date="2026-06-18",
        )

        self.assertTrue(meta["applied"])
        self.assertEqual(meta["window"]["local_hour"], 14.0)
        self.assertEqual(meta["window"]["local_time"], "2026-06-18T14:00:00+08:00")

    def test_hard_source_override_window_prefers_peak_hour_distribution(self) -> None:
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_ENABLED"] = "1"
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_ENABLED"] = "1"
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_MODE"] = "wu_forecast"
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_LOCAL_DATE_OVERRIDE"] = "2026-06-18"
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_LOCAL_HOUR_OVERRIDE"] = "18.49"
        features = {
            "market_var": "temperature_2m_max",
            "event_date": "2026-06-18",
            "source": "zuuu_metar_raw_awc",
            "zuuu_metar_raw_awc_today_max_c": 31.0,
            "zuuu_metar_raw_awc_target_event_date": "2026-06-18",
            "wu_forecast_peak_at": "2026-06-18T07:00:00+00:00",
            "expected_peak_hour_distribution": [
                {"hour_local": 15.0, "prob": 0.3, "valid_time_local": "2026-06-18T15:00:00+08:00"},
                {"hour_local": 16.0, "prob": 0.4, "valid_time_local": "2026-06-18T16:00:00+08:00"},
                {"hour_local": 17.0, "prob": 0.3, "valid_time_local": "2026-06-18T17:00:00+08:00"},
            ],
            "expected_peak_window_start_hour_local": 15.0,
            "expected_peak_window_end_hour_local": 17.0,
            "expected_peak_hour_source": "weather_com_forecast_hourly_peak_plateau:hourly_near_peak_plateau",
        }

        meta = resolve_hard_source_posterior_override(
            ["30°C", "31°C"],
            features,
            base_probs=[0.7, 0.3],
            rounding_rule="whole_degrees",
            market_var="temperature_2m_max",
            event_tz="Asia/Shanghai",
            event_date="2026-06-18",
        )

        self.assertTrue(meta["applied"])
        self.assertEqual(meta["window"]["start_hour_local"], 14.0)
        self.assertEqual(meta["window"]["end_hour_local"], 18.5)
        self.assertEqual(meta["window"]["peak_window_start_hour_local"], 15.0)
        self.assertEqual(meta["window"]["peak_window_end_hour_local"], 17.0)
        self.assertEqual(
            meta["window"]["peak_hour_source"],
            "weather_com_forecast_hourly_peak_plateau:hourly_near_peak_plateau",
        )

    def test_wu_twc_final_settlement_uses_settlement_actual_for_final_lock(self) -> None:
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_ENABLED"] = "1"
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_ENABLED"] = "1"
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_MODE"] = "wu_forecast"
        features = {
            "market_var": "temperature_2m_max",
            "event_date": "2026-06-18",
            "source": "wu_twc_observed_intraday",
            "wu_twc_observed_intraday_today_max_c": 31.0,
            "wu_twc_observed_intraday_event_date": "2026-06-18",
            "wu_twc_observed_intraday_primary_max": True,
            "wu_twc_observed_intraday_stale": False,
            "settlement_actual": 31.0,
            "settlement_actual_source": "wu_twc_final_settlement",
            "market_closed": True,
        }

        meta = resolve_hard_source_posterior_override(
            ["30°C", "31°C", "32°C"],
            features,
            base_probs=[0.55, 0.35, 0.10],
            rounding_rule="whole_degrees",
            market_var="temperature_2m_max",
            event_tz="Asia/Shanghai",
            event_date="2026-06-18",
            now_dt=datetime(2026, 6, 18, 7, 0, tzinfo=timezone.utc),
        )

        self.assertTrue(meta["applied"])
        self.assertTrue(meta["final_bucket_lock"])
        self.assertEqual(meta["source"], "wu_twc_final_settlement")
        self.assertEqual(meta["constraint_mode"], "final_bucket_lock")
        self.assertEqual(meta["posterior_probs"], [0.0, 1.0, 0.0])

    def test_awc_final_daily_max_flag_locks_bucket_without_settlement_actual(self) -> None:
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_ENABLED"] = "1"
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_ENABLED"] = "1"
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_MODE"] = "wu_forecast"
        features = {
            "market_var": "temperature_2m_max",
            "event_date": "2026-07-05",
            "source": "zuuu_metar_raw_awc",
            "zuuu_metar_raw_awc_today_max_c": 37.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-07-05T08:00:00+00:00",
            "zuuu_metar_raw_awc_target_event_date": "2026-07-05",
            "observed_today_max_c": 37.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "observed_today_max_final": True,
        }

        meta = resolve_hard_source_posterior_override(
            ["36°C", "37°C", "38°C"],
            features,
            base_probs=[0.50, 0.03, 0.47],
            rounding_rule="whole_degrees",
            market_var="temperature_2m_max",
            event_tz="Asia/Shanghai",
            event_date="2026-07-05",
            now_dt=datetime(2026, 7, 5, 8, 6, tzinfo=timezone.utc),
        )

        self.assertTrue(meta["applied"])
        self.assertTrue(meta["final_bucket_lock"])
        self.assertEqual(meta["source"], "zuuu_metar_raw_awc")
        self.assertEqual(meta["constraint_mode"], "final_bucket_lock")
        self.assertEqual(meta["final_lock_reason"], "hard_source_final_flag")
        self.assertEqual(meta["posterior_probs"], [0.0, 1.0, 0.0])

    def test_amsc_post_peak_cooling_remains_lower_bound_until_final_evidence(self) -> None:
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_ENABLED"] = "1"
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_ENABLED"] = "1"
        features = {
            "market_var": "temperature_2m_max",
            "event_date": "2026-07-08",
            "tz": "Asia/Shanghai",
            "source": "zuuu_metar_raw_amsc",
            "observed_today_max_c": 39.0,
            "observed_today_max_source": "zuuu_metar_raw_amsc",
            "observed_today_max_trusted": True,
            "zuuu_metar_raw_amsc_today_max_c": 39.0,
            "zuuu_metar_raw_amsc_today_max_at": "2026-07-08T09:00:00+00:00",
            "zuuu_metar_raw_amsc_target_event_date": "2026-07-08",
            "zuuu_metar_raw_amsc_latest_temp_c": 35.0,
            "zuuu_metar_raw_amsc_latest_at": "2026-07-08T13:30:00+00:00",
            "zuuu_metar_raw_amsc_fetched_at": "2026-07-08T13:31:00+00:00",
            "expected_peak_window_end_hour_local": 21.5,
        }

        meta = resolve_hard_source_posterior_override(
            ["38°C", "39°C", "40°C"],
            features,
            base_probs=[0.62, 0.33, 0.05],
            rounding_rule="whole_degrees",
            market_var="temperature_2m_max",
            event_tz="Asia/Shanghai",
            event_date="2026-07-08",
            now_dt=datetime(2026, 7, 8, 13, 40, tzinfo=timezone.utc),
        )

        self.assertTrue(meta["applied"])
        self.assertEqual(meta["source"], "zuuu_metar_raw_amsc")
        self.assertFalse(meta["final_bucket_lock"])
        self.assertEqual(meta["final_lock_reason"], "hard_source_lower_bound_only")
        self.assertEqual(meta["constraint_mode"], "hard_source_lower_bound_constraint")
        self.assertEqual(meta["posterior_probs"][0], 0.0)
        self.assertGreater(meta["posterior_probs"][1], 0.0)
        self.assertGreater(meta["posterior_probs"][2], 0.0)

    def test_amsc_small_post_peak_integer_drop_does_not_lock_daily_max_bucket(self) -> None:
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_ENABLED"] = "1"
        os.environ["PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_ENABLED"] = "1"
        features = {
            "market_var": "temperature_2m_max",
            "event_date": "2026-07-08",
            "tz": "Asia/Shanghai",
            "source": "zuuu_metar_raw_amsc",
            "observed_today_max_c": 39.0,
            "observed_today_max_source": "zuuu_metar_raw_amsc",
            "observed_today_max_trusted": True,
            "zuuu_metar_raw_amsc_today_max_c": 39.0,
            "zuuu_metar_raw_amsc_today_max_at": "2026-07-08T09:00:00+00:00",
            "zuuu_metar_raw_amsc_target_event_date": "2026-07-08",
            "zuuu_metar_raw_amsc_latest_temp_c": 38.0,
            "zuuu_metar_raw_amsc_latest_at": "2026-07-08T13:30:00+00:00",
            "expected_peak_window_end_hour_local": 21.5,
        }

        meta = resolve_hard_source_posterior_override(
            ["38°C", "39°C", "40°C"],
            features,
            base_probs=[0.62, 0.33, 0.05],
            rounding_rule="whole_degrees",
            market_var="temperature_2m_max",
            event_tz="Asia/Shanghai",
            event_date="2026-07-08",
            now_dt=datetime(2026, 7, 8, 13, 40, tzinfo=timezone.utc),
        )

        self.assertTrue(meta["applied"])
        self.assertFalse(meta["final_bucket_lock"])
        self.assertEqual(meta["constraint_mode"], "hard_source_lower_bound_constraint")
        self.assertEqual(meta["posterior_probs"][0], 0.0)
        self.assertAlmostEqual(meta["posterior_probs"][1], 0.868421052631579)
        self.assertAlmostEqual(meta["posterior_probs"][2], 0.13157894736842105)

    def test_bayesian_probability_lcb_z_follows_stage(self) -> None:
        os.environ["PM_BAYES_PROBABILITY_ENABLED"] = "1"
        os.environ["PM_BAYES_PROBABILITY_Z_BY_STAGE"] = "d2_plus=1.64,d1=1.28,d0=0.8"
        items = [
            {"q": {"q_round": 0.40, "q_raw": 0.40}, "per_model_q": {"m": {"q_round": 0.40}}, "active_models": ["m"]},
            {"q": {"q_round": 0.60, "q_raw": 0.60}, "per_model_q": {"m": {"q_round": 0.60}}, "active_models": ["m"]},
        ]

        meta = apply_bayesian_probability_update(
            items,
            weights={"m": 1.0},
            model_cache_freshness=1.0,
            days_to_event=0.0,
            lcb_stage_tz="Asia/Shanghai",
        )

        self.assertAlmostEqual(float(meta["z"]), 0.8)
        self.assertIn(meta["z_stage"], {"d0_morning", "d0_afternoon"})
        self.assertEqual(items[0]["q_bayes_meta"]["z_source"], "PM_BAYES_PROBABILITY_Z_BY_STAGE:d0")

    def test_disabled_leaves_items_unchanged(self) -> None:
        os.environ["PM_BAYES_PROBABILITY_ENABLED"] = "0"
        items = [{"q": {"q_round": 0.42, "q_raw": 0.42}, "per_model_q": {}, "active_models": []}]

        meta = apply_bayesian_probability_update(items, weights={})

        self.assertFalse(meta["enabled"])
        self.assertEqual(items[0]["q"], {"q_round": 0.42, "q_raw": 0.42})





    def test_bayesian_data_role_contract_covers_all_sources_without_role_mismatch(self) -> None:
        audit = audit_bayesian_data_role_contract()
        contracts = contract_by_source_id()

        self.assertEqual(audit["coverage_count"], 19)
        self.assertEqual(audit["coverage_ratio"], 1.0)
        self.assertEqual(audit["mismatch_count"], 0, audit["mismatches"])
        self.assertEqual(contracts["wu_settlement_page"].contract_role, ROLE_PRIOR)
        self.assertEqual(contracts["market_quote_history"].contract_role, ROLE_CALIBRATION)
        self.assertEqual(contracts["model_historical_performance"].contract_role, ROLE_CALIBRATION)
        self.assertEqual(contracts["wu_settlement_page_realtime_display"].contract_role, ROLE_LIKELIHOOD)
        self.assertEqual(contracts["twc_realtime_display"].contract_role, ROLE_LIKELIHOOD)
        self.assertEqual(contracts["market_realtime_orderbook"].contract_role, ROLE_LIKELIHOOD)
        self.assertEqual(contracts["physical_model_realtime"].contract_role, ROLE_LIKELIHOOD)
        self.assertEqual(contracts["satellite_radar_realtime"].contract_role, ROLE_LIKELIHOOD)
        self.assertEqual(contracts["model_derived_physics"].contract_role, ROLE_LIKELIHOOD)
        self.assertEqual(contracts["independent_physics_observed"].contract_role, ROLE_LIKELIHOOD)
        self.assertEqual(contracts["observed_temperature_trajectory"].contract_role, ROLE_LIKELIHOOD)
        self.assertEqual(contracts["zuuu_metar_raw_awc"].contract_role, ROLE_LIKELIHOOD)
        self.assertEqual(contracts["zuuu_metar_archive_iem"].contract_role, ROLE_CALIBRATION)
        self.assertEqual(contracts["twc_provider"].contract_role, ROLE_MODEL_EVIDENCE)
        self.assertEqual(contracts["twc_forecast_peak"].contract_role, ROLE_MODEL_EVIDENCE)
        self.assertEqual(contracts["wu_forecast_high"].contract_role, ROLE_MODEL_EVIDENCE)
        self.assertEqual(contracts["zuuu_taf_forecast_peak"].contract_role, ROLE_MODEL_EVIDENCE)
        self.assertEqual(contracts["climo_regime_background"].contract_role, ROLE_BACKGROUND)
        self.assertEqual(contracts["current_model_ensemble"].contract_role, ROLE_MODEL_EVIDENCE)

    def test_bayesian_data_role_contract_matches_current_pipeline_boundaries(self) -> None:
        contracts = contract_by_source_id()

        historical_roles = {
            role
            for entry in contracts.values()
            if entry.temporal_class == "historical"
            for role in entry.implemented_roles
        }
        realtime_roles = {
            role
            for entry in contracts.values()
            if entry.temporal_class == "realtime"
            for role in entry.implemented_roles
        }
        self.assertLessEqual(historical_roles, {ROLE_PRIOR, ROLE_CALIBRATION})
        self.assertFalse({ROLE_PRIOR, ROLE_CALIBRATION}.intersection(realtime_roles))
        self.assertEqual(
            contracts["market_realtime_orderbook"].implemented_roles,
            (ROLE_DISPLAY_TRADEABILITY,),
        )
        self.assertIn("q_observed_nowcast", contracts["wu_settlement_page_realtime_display"].fields)
        self.assertIn("weather_likelihood_features.observed_temp", contracts["wu_settlement_page_realtime_display"].fields)
        self.assertIn("not_sensor_origin", contracts["wu_settlement_page"].safeguards)
        self.assertIn("provider_dedup_required", contracts["twc_realtime_display"].safeguards)
        self.assertIn("observed_lower_bound_only", contracts["zuuu_metar_raw_awc"].safeguards)
        self.assertIn("historical_archive_only", contracts["zuuu_metar_archive_iem"].safeguards)
        self.assertIn("provider_wrapper_only", contracts["twc_provider"].safeguards)
        self.assertIn("provider_dedup_required", contracts["twc_forecast_peak"].safeguards)
        self.assertIn(
            "temperature_trajectory_required_for_high_temp_amplification",
            contracts["observed_temperature_trajectory"].safeguards,
        )
        self.assertIn(
            "temperature_momentum_answers_physical_potential_realization",
            contracts["observed_temperature_trajectory"].safeguards,
        )
        self.assertIn("temperature_momentum_1h_c_per_hour", contracts["observed_temperature_trajectory"].fields)
        self.assertIn("physical_potential_only", contracts["independent_physics_observed"].safeguards)
        self.assertIn("requires_temperature_trajectory_realization", contracts["model_derived_physics"].safeguards)
        self.assertLess(
            contracts["observed_temperature_trajectory"].priority_rank,
            contracts["independent_physics_observed"].priority_rank,
        )
        self.assertIn("q_bayes_weather_com_forecast_prob", contracts["twc_forecast_peak"].fields)
        self.assertIn(
            "forecast_weak_likelihood.source_distributions.weather_com_forecast",
            contracts["twc_forecast_peak"].fields,
        )
        self.assertIn("settlement_source_forecast_peak_c", contracts["wu_forecast_high"].fields)
        self.assertIn("q_bayes_wu_forecast_prob", contracts["wu_forecast_high"].fields)
        self.assertIn(
            "forecast_weak_likelihood.source_distributions.wu_forecast",
            contracts["wu_forecast_high"].fields,
        )
        self.assertIn("not_observed_nowcast", contracts["wu_forecast_high"].safeguards)
        self.assertIn("not_independent_physics_residual", contracts["wu_forecast_high"].safeguards)
        self.assertIn("taf_forecast_peak_c", contracts["zuuu_taf_forecast_peak"].fields)
        self.assertIn("not_physical_likelihood_component", contracts["zuuu_taf_forecast_peak"].safeguards)
        self.assertEqual(contracts["zuuu_taf_forecast_peak"].implemented_roles, ())
        self.assertEqual(contracts["twc_realtime_display"].implemented_roles, ())
        self.assertEqual(contracts["twc_forecast_peak"].implemented_roles, (ROLE_MODEL_EVIDENCE,))
        self.assertEqual(contracts["model_derived_physics"].implemented_roles, ())
        self.assertIn("climo_reference_used", contracts["climo_regime_background"].fields)
        self.assertIn(
            "reference_uses_only_years_before_target_event_year",
            contracts["climo_regime_background"].safeguards,
        )
        self.assertIn("dedupe_with_p_model", contracts["model_derived_physics"].safeguards)
        self.assertIn("independent_physics_residual_only", contracts["satellite_radar_realtime"].safeguards)
        self.assertIn("physical_residual_only", contracts["independent_physics_observed"].safeguards)
        self.assertIn("independent_physics_residual_only", contracts["independent_physics_observed"].safeguards)
        self.assertIn(
            "q_bayes_physical_likelihood_hard_reasons",
            contracts["physical_model_realtime"].fields,
        )
        self.assertIn(
            "split_model_context_from_observed_residual_required",
            contracts["physical_model_realtime"].safeguards,
        )

    def test_climo_regime_shrinks_extreme_decade_sample_to_same_month_baseline(self) -> None:
        history = [
            {"event_date": "2025-06-09", "actual": 33.0},
            {"event_date": "2025-06-10", "actual": 36.7},
            {"event_date": "2025-06-11", "actual": 37.8},
            {"event_date": "2025-06-12", "actual": 39.0},
            {"event_date": "2025-06-13", "actual": 39.3},
            {"event_date": "2025-06-14", "actual": 34.0},
            {"event_date": "2025-06-15", "actual": 36.3},
            {"event_date": "2025-06-16", "actual": 39.8},
            {"event_date": "2025-06-17", "actual": 40.0},
            {"event_date": "2025-06-18", "actual": 27.1},
            {"event_date": "2025-06-23", "actual": 28.5},
            {"event_date": "2025-06-24", "actual": 21.4},
            {"event_date": "2025-06-25", "actual": 28.2},
            {"event_date": "2025-06-26", "actual": 29.3},
            {"event_date": "2025-06-27", "actual": 29.5},
            {"event_date": "2025-06-28", "actual": 31.1},
            {"event_date": "2025-06-29", "actual": 27.2},
            {"event_date": "2025-06-30", "actual": 30.6},
        ]

        features = build_climo_regime_features(
            tmax_history=history,
            target_event_date="2026-06-13",
            min_samples=3,
        )

        self.assertEqual(features["climo_tmax_basis"], "same_month_robust_shrink_from_decade")
        self.assertLess(float(features["climo_tmax_p50_c"]), 33.0)
        self.assertEqual(features["status"], "ok")

    def test_climo_regime_reference_uses_only_prior_years_without_db_backfill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ref_path = Path(tmp) / "chengdu_open_meteo_reference.json"
            ref_path.write_text(
                json.dumps(
                    {
                        "source": "open_meteo_archive_reference_prior_years",
                        "provider": "Open-Meteo Archive API",
                        "daily": {
                            "time": [
                                "2024-06-11",
                                "2024-06-12",
                                "2025-06-11",
                                "2025-06-12",
                                "2026-06-11",
                            ],
                            "temperature_2m_max": [28.0, 29.0, 30.0, 31.0, 45.0],
                            "dew_point_2m_mean": [20.0, 21.0, 22.0, 23.0, 30.0],
                        },
                    }
                ),
                encoding="utf-8",
            )

            context = load_climo_regime_context(
                db_path=Path(tmp) / "missing.db",
                labels=["28°C", "29°C", "30°C", "31°C"],
                target_event_date="2026-06-13",
                as_of="2026-06-12T00:00:00+00:00",
                city="chengdu",
                station_id="ZUUU",
                market_var="temperature_2m_max",
                market_unit="C",
                rounding_rule="whole_degrees",
                reference_path=ref_path,
                reference_min_samples=4,
            )

        features = context["features"]
        self.assertTrue(features["climo_reference_used"])
        self.assertEqual(features["created_at_guard"], "reference_uses_only_years_before_target_event_year")
        self.assertEqual(features["climo_tmax_sample_count"], 4)
        self.assertLess(float(features["climo_tmax_p95_c"]), 45.0)
        self.assertEqual(features["climo_reference_meta"]["year_max"], 2025)

    def test_basin_heat_mechanism_model_combines_common_heating_drivers(self) -> None:
        model = basin_heat_mechanism_model(
            {
                "forecast_peak_temp_c": 35.0,
                "taf_forecast_peak_c": 35.2,
                "temperature_850hpa_anomaly_c": 2.2,
                "shortwave_radiation_observed": 760.0,
                "cloud_cover_low_pct": 18.0,
                "boundary_layer_height_m": 1300.0,
                "temperature_momentum_effective_c_per_hour": 0.55,
                "wind_speed_10m_mps": 1.2,
                "dewpoint_c": 17.5,
                "radar_precip_rate_mm_per_h": 0.0,
            },
            p90_c=34.0,
        )

        self.assertEqual(model["state"], "active")
        self.assertGreater(model["heat_uplift_index"], 0.7)
        self.assertGreater(model["temperature_adjustment_c"], 0.0)
        self.assertIn("subtropical_high_warm_ridge", model["active_support_keys"])
        self.assertIn("clear_sky_shortwave_radiation", model["active_support_keys"])
        self.assertIn("boundary_layer_growth_mixing", model["active_support_keys"])
        self.assertIn("weak_wind_static_heating", model["active_support_keys"])
        self.assertTrue(model["model_boundaries"]["weak_prior_or_explanation_only"])

    def test_basin_heat_mechanism_model_suppresses_cloud_precip_southwest_vortex(self) -> None:
        model = basin_heat_mechanism_model(
            {
                "forecast_peak_temp_c": 33.0,
                "cloud_cover_pct": 92.0,
                "precip_nowcast_1_3h_max_rate_mm_per_h": 1.2,
                "southwest_vortex_active": True,
                "wind_speed_10m_mps": 5.1,
                "dewpoint_c": 23.0,
            },
            p90_c=34.0,
        )

        self.assertEqual(model["state"], "off")
        self.assertLess(model["temperature_adjustment_c"], 0.0)
        self.assertIn("precipitation_evaporative_cloud_damping", model["active_suppression_keys"])
        self.assertIn("southwest_vortex_precip_regime", model["active_suppression_keys"])

    def test_climo_regime_features_expose_basin_heat_mechanism_model(self) -> None:
        features = build_climo_regime_features(
            tmax_history=[
                {"event_date": "2025-06-11", "actual": 31.0},
                {"event_date": "2025-06-12", "actual": 32.0},
                {"event_date": "2025-06-13", "actual": 33.0},
                {"event_date": "2025-06-14", "actual": 34.0},
                {"event_date": "2025-06-15", "actual": 35.0},
            ],
            target_event_date="2026-06-13",
            weather_features={
                "forecast_peak_temp_c": 35.0,
                "shortwave_radiation_observed": 720.0,
                "cloud_cover_low_pct": 15.0,
                "temperature_momentum_effective_c_per_hour": 0.5,
                "wind_speed_10m_mps": 1.4,
            },
            min_samples=3,
        )

        self.assertEqual(features["basin_heat_mechanism_state"], "active")
        self.assertIn("basin_heat_mechanism_model", features)
        self.assertIn("clear_sky_shortwave_radiation", features["basin_heat_mechanism_active_support_keys"])
        self.assertEqual(features["primary_regime_label"], "warm_ridge_proxy")

    def test_climo_regime_d0_within_2h_is_explanation_only(self) -> None:
        scale = climo_regime_evidence_scale(
            days_to_event=1.9 / 24.0,
            features={"hours_to_peak_window": 1.9, "climo_tmax_sample_count": 30},
        )

        self.assertEqual(scale["stage"], "d0_within_2h_explanation_only")
        self.assertEqual(scale["scale"], 0.0)

    def test_climo_regime_pp_cap_limits_single_bucket_delta(self) -> None:
        capped = cap_climo_evidence_by_pp_delta(
            base_scores=[30.0, 40.0, 30.0],
            climo_probs=[0.0, 0.0, 1.0],
            raw_evidence=50.0,
            max_abs_pp=0.01,
        )

        self.assertTrue(capped["cap_hit"])
        self.assertLessEqual(float(capped["max_abs_delta"]), 0.0100001)
        self.assertLess(float(capped["evidence"]), 50.0)

    def test_climo_high_temp_gate_blocks_unrealized_temperature_momentum(self) -> None:
        gate = climo_high_temp_potential_gate(
            {
                "primary_regime_label": "warm_ridge_proxy",
                "subtropical_high_regime": "warm_ridge_proxy",
                "climo_tmax_p90_c": 34.0,
                "forecast_peak_temp_c": 35.0,
                "taf_forecast_peak_c": 35.0,
                "current_temp_c": 31.0,
                "temperature_momentum_1h_c_per_hour": 0.0,
                "cloud_cover_low_pct": 10.0,
                "radar_precip_rate_mm_per_h": 0.0,
            }
        )

        self.assertFalse(gate["allow_high_temp_uplift"])
        self.assertTrue(gate["hard_blocked"])
        self.assertTrue(gate["temperature_trajectory_priority"])


    def test_weak_proxy_physical_likelihood_is_quality_gated(self) -> None:
        self._force_hierarchical_production_runtime()
        os.environ["PM_BAYES_PROBABILITY_ENABLED"] = "1"
        os.environ["PM_BAYES_LIKELIHOOD_ENABLED"] = "1"
        os.environ["PM_BAYES_LIKELIHOOD_EVIDENCE"] = "12"
        os.environ["PM_BAYES_LIKELIHOOD_PROXY_ONLY_EVIDENCE_SCALE"] = "0.35"
        os.environ["PM_BAYES_LIKELIHOOD_EXTREME_WEAK_EVIDENCE_SCALE"] = "0.25"
        os.environ["PM_LCB_STAGE_Z_DEFAULTS_ENABLED"] = "0"
        items = [
            {
                "label": "20°C",
                "q": {"q_round": 0.10, "q_raw": 0.10},
                "per_model_q": {"m": {"q_round": 0.10}},
                "active_models": ["m"],
            },
            {
                "label": "30°C",
                "q": {"q_round": 0.90, "q_raw": 0.90},
                "per_model_q": {"m": {"q_round": 0.90}},
                "active_models": ["m"],
                "weather_likelihood_features": {
                    "source": "scan_model_members",
                    "forecast_peak_temp_c": 20.0,
                    "temperature_distribution_c": [19.8, 20.0, 20.2],
                    "cloud_cover_pct": 95.0,
                    "cti": 0.2,
                    "auxiliary_source": "satellite_radiation_error:HTTPError",
                },
            },
        ]

        meta = apply_bayesian_probability_update(
            items,
            weights={"m": 1.0},
            model_cache_freshness=1.0,
            days_to_event=1.0,
        )

        self._assert_hierarchical_probability_runtime(meta)
        metar_output = self._pipeline_runtime_output(meta, "metar_strong_likelihood")
        self.assertEqual(metar_output["observation_features"]["cloud_cover_pct"], 95.0)
        self.assertEqual(metar_output["observation_features"]["low_cloud_cover_pct"], 95.0)
        self.assertGreater(
            metar_output["state_probs"]["S0_wet_layer_closed"],
            metar_output["state_probs"]["S2_open_boundary_layer"],
        )
        self.assertTrue(metar_output["boundary"]["metar_internal_fields_not_independent_likelihoods"])
        self.assertIn(
            {
                "legacy_channel": "physical_likelihood",
                "new_location": "state_probs",
                "policy": "cloud_visibility_dewpoint_precip_wind_state_inference_once",
            },
            metar_output["dedupe_trace"],
        )



    def test_momentum_rate_enters_metar_strong_once_without_source_component_cap(self) -> None:
        self._force_hierarchical_production_runtime()
        os.environ["PM_BAYES_PROBABILITY_ENABLED"] = "1"
        os.environ["PM_BAYES_LIKELIHOOD_ENABLED"] = "1"
        os.environ["PM_BAYES_LIKELIHOOD_EVIDENCE"] = "0"
        os.environ["PM_BAYES_CLIMO_REGIME_ENABLED"] = "0"
        os.environ["PM_BAYES_WU_FORECAST_ENABLED"] = "0"
        os.environ["PM_BAYES_TAF_PEAK_ENABLED"] = "0"
        os.environ["PM_BAYES_TRAJECTORY_REGIME_V1_PRECISION_CAP"] = "0.75"
        os.environ["PM_LCB_STAGE_Z_DEFAULTS_ENABLED"] = "0"
        items = [
            {
                "label": "31°C",
                "rounding_rule": "rounded_degrees",
                "q": {"q_round": 0.50, "q_raw": 0.50},
                "per_model_q": {"m": {"q_round": 0.50}},
                "active_models": ["m"],
                "weather_likelihood_features": {
                    "market_var": "temperature_2m_max",
                    "forecast_peak_temp_c": 32.0,
                    "observed_temp_c": 31.0,
                    "observed_today_max_c": 31.0,
                    "observation_hour_local": 13.0,
                    "expected_peak_hour_local": 16.0,
                    "temperature_momentum_30m_c_per_hour": 0.8,
                    "temperature_momentum_1h_c_per_hour": 0.7,
                    "temperature_momentum_realization_state": "sustained_warming",
                },
            },
            {
                "label": "32°C",
                "rounding_rule": "rounded_degrees",
                "q": {"q_round": 0.40, "q_raw": 0.40},
                "per_model_q": {"m": {"q_round": 0.40}},
                "active_models": ["m"],
            },
            {
                "label": "33°C",
                "rounding_rule": "rounded_degrees",
                "q": {"q_round": 0.10, "q_raw": 0.10},
                "per_model_q": {"m": {"q_round": 0.10}},
                "active_models": ["m"],
            },
        ]

        meta = apply_bayesian_probability_update(
            items,
            weights={"m": 1.0},
            model_cache_freshness=1.0,
            days_to_event=0.0,
            lcb_stage_tz="Asia/Shanghai",
        )

        self._assert_hierarchical_probability_runtime(meta)
        metar_output = self._pipeline_runtime_output(meta, "metar_strong_likelihood")
        self.assertEqual(metar_output["observation_features"]["current_temp_c"], 31.0)
        self.assertEqual(metar_output["observation_features"]["observed_today_max_c"], 31.0)
        self.assertEqual(metar_output["observation_features"]["momentum_rate_c_per_hour"], 0.7)
        model_trace = self._source_trace_by_id(metar_output, "model_distribution:m")
        self.assertFalse(model_trace["included"])
        self.assertEqual(model_trace["exclusion_reason"], "source_class_not_hard_observation")
        self.assertTrue(metar_output["remaining_warming_by_state"])



    def test_weather_type_prior_enters_root_prior_distribution(self) -> None:
        self._force_hierarchical_production_runtime()
        os.environ["PM_BAYES_PROBABILITY_ENABLED"] = "1"
        os.environ["PM_BAYES_LIKELIHOOD_ENABLED"] = "0"
        os.environ["PM_BAYES_CLIMO_REGIME_ENABLED"] = "0"
        os.environ["PM_BAYES_WU_FORECAST_ENABLED"] = "0"
        os.environ["PM_BAYES_TAF_PEAK_ENABLED"] = "0"
        os.environ["PM_BAYES_TEMPERATURE_TRAJECTORY_POSTERIOR_ENABLED"] = "0"
        os.environ["PM_LCB_STAGE_Z_DEFAULTS_ENABLED"] = "0"
        items = [
            {
                "label": "28°C",
                "rounding_rule": "rounded_degrees",
                "q": {"q_round": 0.70, "q_raw": 0.70},
                "per_model_q": {"m": {"q_round": 0.70}},
                "active_models": ["m"],
            },
            {
                "label": "29°C",
                "rounding_rule": "rounded_degrees",
                "q": {"q_round": 0.25, "q_raw": 0.25},
                "per_model_q": {"m": {"q_round": 0.25}},
                "active_models": ["m"],
            },
            {
                "label": "30°C",
                "rounding_rule": "rounded_degrees",
                "q": {"q_round": 0.05, "q_raw": 0.05},
                "per_model_q": {"m": {"q_round": 0.05}},
                "active_models": ["m"],
            },
        ]

        meta = apply_bayesian_probability_update(
            items,
            weights={"m": 1.0},
            model_cache_freshness=1.0,
            days_to_event=1.0,
            historical_context={
                "prior_probs": [0.62, 0.26, 0.12],
                "prior_sample_scale": 1.0,
                "weather_likelihood_features": {
                    "market_var": "temperature_2m_max",
                    "market_unit": "C",
                },
            },
        )

        self._assert_hierarchical_probability_runtime(meta)
        prior = meta["layer_contributions"]["prior"]
        self.assertEqual(prior["role"], "root_prior")
        self.assertEqual(prior["boundary"], "historical_climate_date_site_terrain_weather_type_only")
        self.assertEqual(prior["distribution"], {"28°C": 0.62, "29°C": 0.26, "30°C": 0.12})
        self.assertGreater(items[2]["q"]["q_bayes_posterior"], items[2]["q"]["q_round_static"])

    def test_root_prior_without_history_does_not_use_current_market_q(self) -> None:
        self._force_hierarchical_production_runtime()
        os.environ["PM_BAYES_PROBABILITY_ENABLED"] = "1"
        os.environ["PM_BAYES_LIKELIHOOD_ENABLED"] = "0"
        os.environ["PM_BAYES_CLIMO_REGIME_ENABLED"] = "0"
        os.environ["PM_BAYES_WU_FORECAST_ENABLED"] = "0"
        os.environ["PM_BAYES_TAF_PEAK_ENABLED"] = "0"
        os.environ["PM_BAYES_TEMPERATURE_TRAJECTORY_POSTERIOR_ENABLED"] = "0"
        os.environ["PM_LCB_STAGE_Z_DEFAULTS_ENABLED"] = "0"
        items = [
            {
                "label": "29°C",
                "rounding_rule": "rounded_degrees",
                "q": {"q_round": 0.80, "q_raw": 0.80},
                "per_model_q": {"m": {"q_round": 0.80}},
                "active_models": ["m"],
            },
            {
                "label": "30°C",
                "rounding_rule": "rounded_degrees",
                "q": {"q_round": 0.20, "q_raw": 0.20},
                "per_model_q": {"m": {"q_round": 0.20}},
                "active_models": ["m"],
            },
        ]

        meta = apply_bayesian_probability_update(
            items,
            weights={"m": 1.0},
            model_cache_freshness=1.0,
            days_to_event=1.0,
            historical_context={
                "source": "history_insufficient",
                "prior_probs": None,
                "prior_sample_scale": 0.0,
                "weather_likelihood_features": {
                    "market_var": "temperature_2m_max",
                    "market_unit": "C",
                },
            },
        )

        self._assert_hierarchical_probability_runtime(meta)
        prior = meta["layer_contributions"]["prior"]
        self.assertEqual(prior["role"], "root_prior")
        self.assertEqual(prior["distribution"], {"29°C": 0.5, "30°C": 0.5})
        self.assertNotEqual(prior["distribution"], {"29°C": 0.8, "30°C": 0.2})

    def test_low_sample_root_prior_shrinks_to_neutral_not_current_market_q(self) -> None:
        self._force_hierarchical_production_runtime()
        os.environ["PM_BAYES_PROBABILITY_ENABLED"] = "1"
        os.environ["PM_BAYES_LIKELIHOOD_ENABLED"] = "0"
        os.environ["PM_BAYES_CLIMO_REGIME_ENABLED"] = "0"
        os.environ["PM_BAYES_WU_FORECAST_ENABLED"] = "0"
        os.environ["PM_BAYES_TAF_PEAK_ENABLED"] = "0"
        os.environ["PM_BAYES_TEMPERATURE_TRAJECTORY_POSTERIOR_ENABLED"] = "0"
        os.environ["PM_LCB_STAGE_Z_DEFAULTS_ENABLED"] = "0"
        items = [
            {
                "label": "29°C",
                "rounding_rule": "rounded_degrees",
                "q": {"q_round": 0.90, "q_raw": 0.90},
                "per_model_q": {"m": {"q_round": 0.90}},
                "active_models": ["m"],
            },
            {
                "label": "30°C",
                "rounding_rule": "rounded_degrees",
                "q": {"q_round": 0.10, "q_raw": 0.10},
                "per_model_q": {"m": {"q_round": 0.10}},
                "active_models": ["m"],
            },
        ]

        meta = apply_bayesian_probability_update(
            items,
            weights={"m": 1.0},
            model_cache_freshness=1.0,
            days_to_event=1.0,
            historical_context={
                "source": "station_history_low_sample",
                "prior_probs": [0.20, 0.80],
                "prior_sample_scale": 0.25,
                "weather_likelihood_features": {
                    "market_var": "temperature_2m_max",
                    "market_unit": "C",
                },
            },
        )

        self._assert_hierarchical_probability_runtime(meta)
        prior = meta["layer_contributions"]["prior"]
        self.assertEqual(prior["distribution"], {"29°C": 0.425, "30°C": 0.575})
        self.assertNotEqual(prior["distribution"], {"29°C": 0.375, "30°C": 0.625})

    def test_intraday_rebound_context_enters_metar_strong_remaining_warming(self) -> None:
        self._force_hierarchical_production_runtime()
        os.environ["PM_BAYES_PROBABILITY_ENABLED"] = "1"
        os.environ["PM_BAYES_LIKELIHOOD_ENABLED"] = "0"
        os.environ["PM_BAYES_CLIMO_REGIME_ENABLED"] = "0"
        os.environ["PM_BAYES_WU_FORECAST_ENABLED"] = "0"
        os.environ["PM_BAYES_TAF_PEAK_ENABLED"] = "0"
        os.environ["PM_BAYES_TEMPERATURE_TRAJECTORY_POSTERIOR_ENABLED"] = "0"
        os.environ["PM_LCB_STAGE_Z_DEFAULTS_ENABLED"] = "0"
        items = [
            {
                "label": "28°C",
                "rounding_rule": "rounded_degrees",
                "q": {"q_round": 0.70, "q_raw": 0.70},
                "per_model_q": {"m": {"q_round": 0.70}},
                "active_models": ["m"],
                "weather_likelihood_features": {
                    "market_var": "temperature_2m_max",
                    "market_unit": "C",
                    "observed_temp_c": 28.8,
                    "observed_today_max_c": 28.8,
                    "observation_hour_local": 12.0,
                    "visibility": "9999",
                    "cloud_text": "FEW011 SCT020",
                    "dewpoint_c": 22.0,
                    "dewpoint_trend_c": -1.0,
                    "temperature_momentum_1h_c_per_hour": 1.5,
                },
            },
            {
                "label": "29°C",
                "rounding_rule": "rounded_degrees",
                "q": {"q_round": 0.25, "q_raw": 0.25},
                "per_model_q": {"m": {"q_round": 0.25}},
                "active_models": ["m"],
            },
            {
                "label": "30°C",
                "rounding_rule": "rounded_degrees",
                "q": {"q_round": 0.05, "q_raw": 0.05},
                "per_model_q": {"m": {"q_round": 0.05}},
                "active_models": ["m"],
            },
        ]

        meta = apply_bayesian_probability_update(
            items,
            weights={"m": 1.0},
            model_cache_freshness=1.0,
            days_to_event=0.0,
        )

        self._assert_hierarchical_probability_runtime(meta)
        metar_output = self._pipeline_runtime_output(meta, "metar_strong_likelihood")
        self.assertEqual(metar_output["observation_features"]["current_temp_c"], 28.8)
        self.assertEqual(metar_output["observation_features"]["observed_today_max_c"], 28.8)
        self.assertEqual(metar_output["observation_features"]["momentum_rate_c_per_hour"], 1.5)
        self.assertTrue(metar_output["observation_features"]["visibility_good"])
        self.assertGreater(
            metar_output["state_probs"]["S2_open_boundary_layer"],
            metar_output["state_probs"]["S0_wet_layer_closed"],
        )
        self.assertEqual(metar_output["hard_constraints"]["masked_zero_buckets"], ["28°C"])
        self.assertGreater(items[2]["q"]["q_bayes_posterior"], items[2]["q"]["q_round_static"])

    def test_weather_type_prior_and_hard_source_use_single_mask(self) -> None:
        self._force_hierarchical_production_runtime()
        os.environ["PM_BAYES_PROBABILITY_ENABLED"] = "1"
        os.environ["PM_BAYES_LIKELIHOOD_ENABLED"] = "0"
        os.environ["PM_BAYES_CLIMO_REGIME_ENABLED"] = "0"
        os.environ["PM_BAYES_WU_FORECAST_ENABLED"] = "0"
        os.environ["PM_BAYES_TAF_PEAK_ENABLED"] = "0"
        os.environ["PM_BAYES_TEMPERATURE_TRAJECTORY_POSTERIOR_ENABLED"] = "0"
        os.environ["PM_LCB_STAGE_Z_DEFAULTS_ENABLED"] = "0"
        items = [
            {
                "label": "29°C",
                "rounding_rule": "rounded_degrees",
                "q": {"q_round": 0.60, "q_raw": 0.60},
                "per_model_q": {"m": {"q_round": 0.60}},
                "active_models": ["m"],
            },
            {
                "label": "30°C",
                "rounding_rule": "rounded_degrees",
                "q": {"q_round": 0.30, "q_raw": 0.30},
                "per_model_q": {"m": {"q_round": 0.30}},
                "active_models": ["m"],
            },
            {
                "label": "31°C",
                "rounding_rule": "rounded_degrees",
                "q": {"q_round": 0.08, "q_raw": 0.08},
                "per_model_q": {"m": {"q_round": 0.08}},
                "active_models": ["m"],
            },
            {
                "label": "32°C",
                "rounding_rule": "rounded_degrees",
                "q": {"q_round": 0.02, "q_raw": 0.02},
                "per_model_q": {"m": {"q_round": 0.02}},
                "active_models": ["m"],
            },
        ]

        meta = apply_bayesian_probability_update(
            items,
            weights={"m": 1.0},
            model_cache_freshness=1.0,
            days_to_event=0.0,
            historical_context={
                "prior_probs": [0.50, 0.25, 0.20, 0.05],
                "prior_sample_scale": 1.0,
                "weather_likelihood_features": {
                    "market_var": "temperature_2m_max",
                    "market_unit": "C",
                    "source": "zuuu_metar_raw_awc",
                    "zuuu_metar_raw_awc_today_max_c": 31.0,
                    "zuuu_metar_raw_awc_today_max_at": "2026-06-24T08:00:00+00:00",
                    "zuuu_metar_raw_awc_target_event_date": "2026-06-24",
                },
            },
        )

        self._assert_hierarchical_probability_runtime(meta)
        self.assertEqual(meta["layer_contributions"]["prior"]["distribution"], {"29°C": 0.5, "30°C": 0.25, "31°C": 0.2, "32°C": 0.05})
        self.assertEqual(
            meta["hard_constraints"]["combined_hard_mask"],
            {"29°C": 0.0, "30°C": 0.0, "31°C": 1.0, "32°C": 1.0},
        )
        self.assertEqual(
            meta["hard_constraints"]["posterior"]["masked_zero_buckets"],
            ["29°C", "30°C"],
        )
        self.assertEqual(items[0]["q"]["q_bayes_posterior"], 0.0)
        self.assertEqual(items[1]["q"]["q_bayes_posterior"], 0.0)

    def test_rebound_watch_context_diagnostic_only_under_hierarchical_runtime(self) -> None:
        self._force_hierarchical_production_runtime()
        os.environ["PM_BAYES_PROBABILITY_ENABLED"] = "1"
        os.environ["PM_BAYES_LIKELIHOOD_ENABLED"] = "0"
        os.environ["PM_BAYES_CLIMO_REGIME_ENABLED"] = "0"
        os.environ["PM_BAYES_TEMPERATURE_TRAJECTORY_POSTERIOR_ENABLED"] = "0"
        os.environ["PM_LCB_STAGE_Z_DEFAULTS_ENABLED"] = "0"
        items = [
            {
                "label": "29°C",
                "rounding_rule": "rounded_degrees",
                "q": {"q_round": 0.8, "q_raw": 0.8},
                "per_model_q": {"m": {"q_round": 0.8}},
                "active_models": ["m"],
            },
            {
                "label": "30°C",
                "rounding_rule": "rounded_degrees",
                "q": {"q_round": 0.2, "q_raw": 0.2},
                "per_model_q": {"m": {"q_round": 0.2}},
                "active_models": ["m"],
            },
        ]

        meta = apply_bayesian_probability_update(
            items,
            weights={"m": 1.0},
            model_cache_freshness=1.0,
            days_to_event=0.0,
            historical_context={
                "weather_likelihood_features": {
                    "market_var": "temperature_2m_max",
                    "market_unit": "C",
                },
            },
        )

        self._assert_hierarchical_probability_runtime(meta)
        metar_output = self._pipeline_runtime_output(meta, "metar_strong_likelihood")
        self.assertEqual(metar_output["hard_constraints"]["hard_mask"], {"29°C": 1.0, "30°C": 1.0})
        self.assertEqual(metar_output["hard_constraints"]["hard_mask_reason"], "none")
        self.assertEqual(
            set(items[1]["q_bayes_meta"]["score_components"]),
            {"prior", "forecast_weak_likelihood", "metar_strong_likelihood", "hard_mask"},
        )

    def test_short_window_warming_does_not_enter_temperature_trajectory_posterior(self) -> None:
        labels = ["31°C", "32°C", "33°C"]
        probs, atomic_probs, meta = _temperature_trajectory_channel_probs(
            labels,
            {
                "market_var": "temperature_2m_max",
                "forecast_peak_temp_c": 32.5,
                "observed_temp_c": 31.0,
                "observation_hour_local": 13.0,
                "expected_peak_hour_local": 16.0,
                "temperature_momentum_10m_c_per_hour": 1.2,
                "temperature_momentum_15m_c_per_hour": 1.0,
                "temperature_momentum_effective_c_per_hour": 1.1,
                "temperature_momentum_realization_state": "emerging_warming",
                "temperature_momentum_posterior_gate_allowed": False,
                "temperature_momentum_posterior_gate_state": "short_window_watch",
            },
            rounding_rule="rounded_degrees",
        )

        self.assertFalse(meta["enabled"])
        self.assertEqual(meta["status"], "temperature_momentum_posterior_gate_blocked")
        self.assertEqual(meta["momentum_posterior_gate"]["state"], "short_window_watch")
        self.assertTrue(all(value == 0.0 for value in probs))
        self.assertTrue(all(value == 0.0 for value in atomic_probs))







    def test_strong_observed_physical_anchor_keeps_full_likelihood_weight(self) -> None:
        self._force_hierarchical_production_runtime()
        os.environ["PM_BAYES_PROBABILITY_ENABLED"] = "1"
        os.environ["PM_BAYES_LIKELIHOOD_ENABLED"] = "1"
        os.environ["PM_BAYES_LIKELIHOOD_EVIDENCE"] = "12"
        os.environ["PM_LCB_STAGE_Z_DEFAULTS_ENABLED"] = "0"
        items = [
            {
                "label": "28°C or below",
                "q": {"q_round": 0.50, "q_raw": 0.50},
                "per_model_q": {"m": {"q_round": 0.50}},
                "active_models": ["m"],
            },
            {
                "label": "30°C or higher",
                "q": {"q_round": 0.50, "q_raw": 0.50},
                "per_model_q": {"m": {"q_round": 0.50}},
                "active_models": ["m"],
                "weather_likelihood_features": {
                    "source": "scan_model_members",
                    "observed_today_max_c": 30.2,
                    "temperature_observation_final_daily_max": True,
                    "forecast_peak_temp_c": 30.2,
                },
            },
        ]

        meta = apply_bayesian_probability_update(
            items,
            weights={"m": 1.0},
            model_cache_freshness=1.0,
            days_to_event=0.0,
        )

        self._assert_hierarchical_probability_runtime(meta)
        self.assertEqual(meta["phase"], "D0_hard")
        metar_output = self._pipeline_runtime_output(meta, "metar_strong_likelihood")
        self.assertTrue(metar_output["hard_constraints"]["hard_lock"])
        self.assertEqual(metar_output["hard_constraints"]["official_final_result"], 30.2)
        self.assertEqual(
            metar_output["hard_constraints"]["hard_mask"],
            {"28°C or below": 0.0, "30°C or higher": 1.0},
        )
        self.assertEqual(items[0]["q"]["q_bayes_posterior"], 0.0)
        self.assertEqual(items[1]["q"]["q_bayes_posterior"], 1.0)

    def test_metar_strong_outputs_d0_variables_without_forcing_old_channel(self) -> None:
        self._force_hierarchical_production_runtime()
        os.environ["PM_BAYES_PROBABILITY_ENABLED"] = "1"
        os.environ["PM_BAYES_LIKELIHOOD_ENABLED"] = "1"
        os.environ["PM_BAYES_LIKELIHOOD_EVIDENCE"] = "0"
        os.environ["PM_LCB_STAGE_Z_DEFAULTS_ENABLED"] = "0"
        items = [
            {
                "label": "30°C",
                "rounding_rule": "rounded_degrees",
                "q": {"q_round": 0.50, "q_raw": 0.50},
                "per_model_q": {"m": {"q_round": 0.50}},
                "active_models": ["m"],
                "weather_likelihood_features": {
                    "source": "scan_model_members",
                    "forecast_peak_temp_c": 32.0,
                    "observed_temp_c": 29.5,
                    "observation_hour_local": 13.0,
                    "cloud_cover_pct": 82.0,
                    "cloud_cover_growth_pct_per_h": 14.0,
                    "radar_precip_rate_mm_per_h": 2.4,
                    "is_raining_observed": True,
                    "dewpoint_c": 24.0,
                    "temperature_momentum_1h_c_per_hour": 0.8,
                    "temperature_momentum_2h_c_per_hour": 0.6,
                    "temperature_momentum_3h_c_per_hour": 0.4,
                },
            },
            {
                "label": "33°C",
                "rounding_rule": "rounded_degrees",
                "q": {"q_round": 0.50, "q_raw": 0.50},
                "per_model_q": {"m": {"q_round": 0.50}},
                "active_models": ["m"],
            },
        ]

        meta = apply_bayesian_probability_update(
            items,
            weights={"m": 1.0},
            model_cache_freshness=1.0,
            days_to_event=0.0,
        )

        self._assert_hierarchical_probability_runtime(meta)
        metar_output = self._pipeline_runtime_output(meta, "metar_strong_likelihood")
        self.assertEqual(metar_output["observation_features"]["current_temp_c"], 29.5)
        self.assertEqual(metar_output["observation_features"]["cloud_cover_pct"], 82.0)
        self.assertEqual(metar_output["observation_features"]["dewpoint_c"], 24.0)
        self.assertTrue(metar_output["observation_features"]["high_dewpoint"])
        self.assertEqual(metar_output["observation_features"]["momentum_rate_c_per_hour"], 0.8)
        self.assertTrue(metar_output["boundary"]["metar_internal_fields_not_independent_likelihoods"])
        self.assertEqual(
            set(items[0]["q_bayes_meta"]["score_components"]),
            {"prior", "forecast_weak_likelihood", "metar_strong_likelihood", "hard_mask"},
        )

    def test_physical_d0_explanation_does_not_count_open_meteo_proxy_as_radar_echo(self) -> None:
        os.environ["PM_BAYES_PROBABILITY_ENABLED"] = "1"
        os.environ["PM_BAYES_LIKELIHOOD_ENABLED"] = "1"
        os.environ["PM_BAYES_LIKELIHOOD_EVIDENCE"] = "0"
        os.environ["PM_LCB_STAGE_Z_DEFAULTS_ENABLED"] = "0"
        items = [
            {
                "label": "31°C",
                "rounding_rule": "rounded_degrees",
                "q": {"q_round": 0.50, "q_raw": 0.50},
                "per_model_q": {"m": {"q_round": 0.50}},
                "active_models": ["m"],
                "weather_likelihood_features": {
                    "forecast_peak_temp_c": 32.0,
                    "observed_temp_c": 29.5,
                    "observation_hour_local": 10.0,
                    "cloud_cover_low_pct": 0.0,
                    "cloud_cover_mid_pct": 25.0,
                    "observed_precip_rate_mm_per_h": 0.1,
                    "precipitation_rate_mm_per_h": 0.1,
                    "rain_rate_mm_per_h": 0.1,
                    "is_raining": True,
                    "radar_qpe_proxy_source": "open_meteo_current_hourly_precip",
                    "dewpoint_c": 17.0,
                },
            },
            {
                "label": "33°C",
                "rounding_rule": "rounded_degrees",
                "q": {"q_round": 0.50, "q_raw": 0.50},
                "per_model_q": {"m": {"q_round": 0.50}},
                "active_models": ["m"],
            },
        ]

        meta = apply_bayesian_probability_update(
            items,
            weights={"m": 1.0},
            model_cache_freshness=1.0,
            days_to_event=0.0,
        )

        self._assert_hierarchical_probability_runtime(meta)
        metar_output = self._pipeline_runtime_output(meta, "metar_strong_likelihood")
        self.assertEqual(metar_output["observation_features"]["current_temp_c"], 29.5)
        self.assertEqual(metar_output["observation_features"]["dewpoint_c"], 17.0)
        self.assertFalse(metar_output["observation_features"]["precip_observed"])
        self.assertFalse(metar_output["observation_features"]["showery_or_convective"])
        self.assertTrue(metar_output["observation_features"]["no_precip_confirmed"])
        self.assertTrue(metar_output["boundary"]["metar_internal_fields_not_independent_likelihoods"])

    def test_metar_no_precip_overrides_open_meteo_proxy_radar_echo_current(self) -> None:
        os.environ["PM_BAYES_PROBABILITY_ENABLED"] = "1"
        os.environ["PM_BAYES_LIKELIHOOD_ENABLED"] = "1"
        os.environ["PM_BAYES_LIKELIHOOD_EVIDENCE"] = "0"
        os.environ["PM_LCB_STAGE_Z_DEFAULTS_ENABLED"] = "0"
        items = [
            {
                "label": "31°C",
                "rounding_rule": "rounded_degrees",
                "q": {"q_round": 0.50, "q_raw": 0.50},
                "per_model_q": {"m": {"q_round": 0.50}},
                "active_models": ["m"],
                "weather_likelihood_features": {
                    "forecast_peak_temp_c": 32.0,
                    "observed_temp_c": 29.5,
                    "observation_hour_local": 10.0,
                    "cloud_cover_low_pct": 0.0,
                    "cloud_cover_mid_pct": 10.0,
                    "observed_precip_rate_mm_per_h": 5.8,
                    "precipitation_rate_mm_per_h": 5.8,
                    "is_raining": True,
                    "is_raining_observed": False,
                    "metar_no_precip_observed": True,
                    "awc_metar_no_precip_observed": True,
                    "radar_qpe_proxy_source": "zuuu_metar_raw_awc_no_precip",
                    "dewpoint_c": 17.0,
                },
            },
            {
                "label": "33°C",
                "rounding_rule": "rounded_degrees",
                "q": {"q_round": 0.50, "q_raw": 0.50},
                "per_model_q": {"m": {"q_round": 0.50}},
                "active_models": ["m"],
            },
        ]

        meta = apply_bayesian_probability_update(
            items,
            weights={"m": 1.0},
            model_cache_freshness=1.0,
            days_to_event=0.0,
        )

        self._assert_hierarchical_probability_runtime(meta)
        metar_output = self._pipeline_runtime_output(meta, "metar_strong_likelihood")
        self.assertEqual(metar_output["observation_features"]["current_temp_c"], 29.5)
        self.assertEqual(metar_output["observation_features"]["dewpoint_c"], 17.0)
        self.assertFalse(metar_output["observation_features"]["precip_observed"])
        self.assertFalse(metar_output["observation_features"]["showery_or_convective"])
        self.assertTrue(metar_output["observation_features"]["no_precip_confirmed"])
        self.assertTrue(metar_output["boundary"]["metar_internal_fields_not_independent_likelihoods"])


if __name__ == "__main__":
    unittest.main()

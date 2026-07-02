import unittest

from scan_weather_market import _compact_decision_details


class DecisionDetailsBayesCompactTests(unittest.TestCase):
    def test_compact_entry_details_preserves_bayesian_probability_fields(self) -> None:
        details = {
            "ask": 0.16,
            "bid": 0.15,
            "q_entry": 0.299,
            "q_quote": 0.28,
            "q_raw_static": 0.24,
            "q_bayes_enabled": True,
            "q_bayes_source": "dirichlet_bma",
            "q_bayes_posterior": 0.299,
            "q_bayes_lcb": 0.23,
            "q_bayes_ucb": 0.36,
            "q_bayes_sigma": 0.05,
            "q_bayes_alpha": 17.2,
            "q_bayes_alpha_total": 57.4,
            "q_bayes_z": 0.8,
            "q_bayes_z_stage": "d0_afternoon",
            "q_bayes_z_source": "stage_default",
            "q_bayes_prior_strength": 12.0,
            "q_bayes_prior_source": "market_history_low_sample",
            "q_bayes_historical_samples": 1,
            "q_bayes_historical_prior_sample_scale": 0.333,
            "q_bayes_historical_bucket_count": 1,
            "q_bayes_bias_correction_enabled": True,
            "q_bayes_model_evidence": 45.4,
            "q_bayes_nowcast_evidence": 0.0,
            "q_bayes_physical_likelihood_evidence": 12.0,
            "q_bayes_physical_likelihood_enabled": True,
            "q_bayes_physical_likelihood_source": "weather_physical_likelihood",
            "q_bayes_physical_likelihood_prob": 0.71,
            "q_bayes_physical_likelihood_hard_zero": True,
            "q_bayes_physical_likelihood_hard_reasons": ["dewpoint_above_bucket"],
            "q_bayes_physical_likelihood_components": {"dewpoint": -36.8},
            "q_bayes_physical_likelihood_component_groups": {
                "wet_bulb_constraint": {"log_likelihood": -36.8}
            },
            "q_bayes_bimodal_risk": False,
            "q_bayes_bimodal_size_scale": 1.0,
            "weather_likelihood_features_source": "scan_model_members",
            "weather_likelihood_aux_source": "open_meteo_forecast_aux,metar_aviationweather",
            "weather_likelihood_feature_count": 40,
            "peak_estimator_source": "kalman_blend",
            "peak_estimator_mu_c": 30.4,
            "peak_estimator_sigma_c": 0.8,
            "peak_estimator_kalman_gain": 0.75,
            "peak_estimator_combined_discount": 0.62,
            "q_calibration_z": 0.8,
            "q_calibration_z_stage": "d0_afternoon",
            "q_calibration_z_source": "stage_default",
            "q_quote_calibration_z": 0.8,
            "q_quote_calibration_z_stage": "d0_afternoon",
            "q_quote_calibration_z_source": "stage_default",
            "edge_tradeability_pass": True,
            "edge_tradeability_profit_value": 0.04,
            "exec_edge_entry": 0.13,
            "entry_usable_edge": 0.02,
            "strategy": "lcb_global",
            "source_fit_tier": "settlement_source",
        }

        compact = _compact_decision_details(details, action="entry", will_order=False)

        self.assertTrue(compact["q_bayes_enabled"])
        self.assertEqual(compact["q_bayes_source"], "dirichlet_bma")
        self.assertEqual(compact["q_raw_static"], 0.24)
        self.assertEqual(compact["q_bayes_posterior"], 0.299)
        self.assertEqual(compact["q_bayes_lcb"], 0.23)
        self.assertEqual(compact["q_bayes_ucb"], 0.36)
        self.assertEqual(compact["q_bayes_z"], 0.8)
        self.assertEqual(compact["q_bayes_z_stage"], "d0_afternoon")
        self.assertEqual(compact["q_calibration_z"], 0.8)
        self.assertEqual(compact["q_quote_calibration_z"], 0.8)
        self.assertEqual(compact["q_bayes_prior_source"], "market_history_low_sample")
        self.assertEqual(compact["q_bayes_historical_samples"], 1)
        self.assertTrue(compact["q_bayes_bias_correction_enabled"])
        self.assertTrue(compact["q_bayes_physical_likelihood_enabled"])
        self.assertEqual(compact["q_bayes_physical_likelihood_source"], "weather_physical_likelihood")
        self.assertEqual(compact["q_bayes_physical_likelihood_prob"], 0.71)
        self.assertTrue(compact["q_bayes_physical_likelihood_hard_zero"])
        self.assertEqual(compact["q_bayes_physical_likelihood_hard_reasons"], ["dewpoint_above_bucket"])
        self.assertEqual(compact["q_bayes_physical_likelihood_components"], {"dewpoint": -36.8})
        self.assertEqual(
            compact["q_bayes_physical_likelihood_component_groups"],
            {"wet_bulb_constraint": {"log_likelihood": -36.8}},
        )
        self.assertEqual(compact["weather_likelihood_feature_count"], 40)
        self.assertEqual(compact["peak_estimator_source"], "kalman_blend")
        self.assertEqual(compact["peak_estimator_mu_c"], 30.4)

    def test_compact_entry_details_does_not_null_bayesian_likelihood_when_payload_is_large(self) -> None:
        details = {
            "ask": 0.16,
            "bid": 0.15,
            "q_entry": 0.299,
            "q_quote": 0.28,
            "q_raw_static": 0.24,
            "q_bayes_enabled": True,
            "q_bayes_source": "dirichlet_bma",
            "q_bayes_posterior": 0.299,
            "q_bayes_lcb": 0.23,
            "q_bayes_ucb": 0.36,
            "q_bayes_sigma": 0.05,
            "q_bayes_alpha": 17.2,
            "q_bayes_alpha_total": 57.4,
            "q_bayes_z": 0.8,
            "q_bayes_z_stage": "d0_afternoon",
            "q_bayes_z_source": "stage_default",
            "q_bayes_prior_strength": 12.0,
            "q_bayes_prior_source": "market_history_low_sample",
            "q_bayes_historical_samples": 3,
            "q_bayes_historical_prior_sample_scale": 1.0,
            "q_bayes_historical_bucket_count": 2,
            "q_bayes_bias_correction_enabled": True,
            "q_bayes_model_evidence": 45.4,
            "q_bayes_nowcast_evidence": 9.9,
            "q_bayes_physical_likelihood_evidence": 12.0,
            "q_bayes_physical_likelihood_enabled": True,
            "q_bayes_physical_likelihood_source": "weather_physical_likelihood",
            "q_bayes_physical_likelihood_prob": 0.71,
            "q_bayes_physical_likelihood_hard_zero": True,
            "q_bayes_physical_likelihood_hard_reasons": ["tail_support_below_bucket"],
            "q_bayes_physical_likelihood_components": {"tail_support": -36.8},
            "weather_likelihood_features_source": "scan_model_members",
            "weather_likelihood_aux_source": "open_meteo_forecast_aux,metar_aviationweather",
            "weather_likelihood_feature_count": 40,
            "peak_estimator_source": "kalman_blend",
            "peak_estimator_mu_c": 30.4,
            "peak_estimator_sigma_c": 0.8,
            "peak_estimator_kalman_gain": 0.75,
            "peak_estimator_combined_discount": 0.62,
            "q_calibration_z": 0.8,
            "q_calibration_z_stage": "d0_afternoon",
            "q_calibration_z_source": "stage_default",
            "q_quote_calibration_z": 0.8,
            "q_quote_calibration_z_stage": "d0_afternoon",
            "q_quote_calibration_z_source": "stage_default",
            "edge_tradeability_pass": False,
            "edge_tradeability_profit_value": 0.04,
            "exec_edge_entry": 0.13,
            "entry_usable_edge": 0.02,
            "entry_soft_reason_codes": ["strategy_size_ratio_zero"],
            "strategy": "lcb_global",
            "source_fit_tier": "settlement_source",
        }
        for idx in range(80):
            details[f"edge_tradeability_large_debug_{idx}"] = "x" * 80

        compact = _compact_decision_details(details, action="entry", will_order=False)

        self.assertTrue(compact["q_bayes_enabled"])
        self.assertEqual(compact["q_bayes_posterior"], 0.299)
        self.assertEqual(compact["q_bayes_z_stage"], "d0_afternoon")
        self.assertEqual(compact["q_calibration_z"], 0.8)
        self.assertEqual(compact["q_bayes_prior_source"], "market_history_low_sample")
        self.assertTrue(compact["q_bayes_physical_likelihood_enabled"])
        self.assertEqual(compact["q_bayes_physical_likelihood_source"], "weather_physical_likelihood")
        self.assertTrue(compact["q_bayes_physical_likelihood_hard_zero"])
        self.assertEqual(compact["q_bayes_physical_likelihood_hard_reasons"], ["tail_support_below_bucket"])
        self.assertEqual(compact["q_bayes_physical_likelihood_components"], {"tail_support": -36.8})
        self.assertEqual(compact["weather_likelihood_feature_count"], 40)
        self.assertEqual(compact["peak_estimator_mu_c"], 30.4)

    def test_compact_entry_order_intent_preserves_paper_trading_fields(self) -> None:
        details = {
            "ask": 0.16,
            "q_entry": 0.299,
            "q_raw_static": 0.24,
            "q_bayes_enabled": True,
            "q_bayes_posterior": 0.299,
            "edge_tradeability_pass": True,
            "edge_tradeability_profit_value": 0.04,
            "exec_edge_entry": 0.13,
            "entry_usable_edge": 0.02,
            "strategy": "lcb_global",
            "source_fit_tier": "settlement_source",
            "order_id": "",
            "token_id": "token-123",
            "order_price": 0.16,
            "order_size": 8.5,
            "order_type": "GTC",
            "price": 0.16,
            "size": 8.5,
            "dry_run": True,
            "status": "dry_run",
            "large_debug_blob": {"items": list(range(200))},
        }

        compact = _compact_decision_details(details, action="entry", will_order=True)

        self.assertEqual(compact["token_id"], "token-123")
        self.assertEqual(compact["order_price"], 0.16)
        self.assertEqual(compact["order_size"], 8.5)
        self.assertEqual(compact["order_type"], "GTC")
        self.assertEqual(compact["price"], 0.16)
        self.assertEqual(compact["size"], 8.5)
        self.assertTrue(compact["dry_run"])
        self.assertEqual(compact["status"], "dry_run")


if __name__ == "__main__":
    unittest.main()

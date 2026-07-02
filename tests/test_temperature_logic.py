import unittest
import json
import os
import tempfile

import numpy as np

from scan_weather_market import (
    CHENGDU_WU_MODEL_BIAS_CACHE,
    _archive_write_allowed_for_chengdu_zuuu,
    _bucket_expected_value_from_label,
    _bucket_label_for_value,
    apply_chengdu_wu_model_temperature_bias,
    apply_rounding,
    compute_q_band,
    model_member_bucket_rule_meta,
    parse_outcome_label,
    temperature_settlement_precision_profile,
    temperature_settlement_profile_from_wu_twc_features,
)


class TestTemperatureLogic(unittest.TestCase):
    def test_open_interval_expected_value_no_offset(self) -> None:
        self.assertEqual(_bucket_expected_value_from_label("10 C or higher", None), 10.0)
        self.assertEqual(_bucket_expected_value_from_label("5 C or below", None), 5.0)

    def test_whole_degrees_floor_rounding(self) -> None:
        self.assertEqual(apply_rounding(-2.1, "whole_degrees", "C"), -3.0)
        self.assertEqual(apply_rounding(2.9, "whole_degrees", "C"), 2.0)

    def test_model_member_bucket_rule_defaults_to_source_aligned_rounding(self) -> None:
        old_rule = os.environ.pop("PM_MODEL_MEMBER_BUCKET_RULE", None)
        try:
            meta = model_member_bucket_rule_meta(
                "whole_degrees",
                market_var="temperature_2m_max",
                market_unit="C",
                resolution_source="https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
            )
        finally:
            if old_rule is not None:
                os.environ["PM_MODEL_MEMBER_BUCKET_RULE"] = old_rule
        self.assertEqual(meta["rounding_rule"], "rounded_degrees")
        self.assertEqual(meta["settlement_rounding_rule"], "whole_degrees")

        spec = parse_outcome_label("28°C")
        band = compute_q_band(np.array([27.6, 28.4], dtype=float), spec, meta["rounding_rule"])

        self.assertEqual(band["q_round"], 1.0)
        self.assertEqual(band["q_nearest"], 1.0)
        self.assertEqual(band["q_floor"], 0.5)
        self.assertEqual(band["q_ceil"], 0.5)
        self.assertEqual(band["bucket_rounding_rule"], "rounded_degrees")

    def test_model_member_bucket_rule_can_be_forced_to_contract_floor(self) -> None:
        old_rule = os.environ.get("PM_MODEL_MEMBER_BUCKET_RULE")
        os.environ["PM_MODEL_MEMBER_BUCKET_RULE"] = "settlement"
        try:
            meta = model_member_bucket_rule_meta(
                "whole_degrees",
                market_var="temperature_2m_max",
                market_unit="C",
            )
        finally:
            if old_rule is None:
                os.environ.pop("PM_MODEL_MEMBER_BUCKET_RULE", None)
            else:
                os.environ["PM_MODEL_MEMBER_BUCKET_RULE"] = old_rule
        self.assertEqual(meta["rounding_rule"], "whole_degrees")

        spec = parse_outcome_label("28°C")
        band = compute_q_band(np.array([27.6, 28.4], dtype=float), spec, meta["rounding_rule"])

        self.assertEqual(band["q_round"], 0.5)
        self.assertEqual(band["q_floor"], 0.5)
        self.assertEqual(band["q_nearest"], 1.0)

    def test_range_expected_value_floor_negative(self) -> None:
        expected = _bucket_expected_value_from_label("-1-0 C", "whole_degrees")
        self.assertEqual(expected, -1.0)

    def test_sparse_buckets_return_empty_label(self) -> None:
        rows = [{"outcome": "0-1 C"}, {"outcome": "3-4 C"}]
        self.assertEqual(_bucket_label_for_value(rows, 2.0, None), "")

    def test_bucket_label_uses_floor_for_negative(self) -> None:
        rows = [{"outcome": "-1-0 C"}, {"outcome": "0-1 C"}]
        self.assertEqual(_bucket_label_for_value(rows, -0.1, "whole_degrees"), "-1-0 C")

    def test_fahrenheit_integer_display_crossing_celsius_floor_bucket_is_ambiguous(self) -> None:
        profile = temperature_settlement_precision_profile(
            native_value=91,
            native_unit="F",
            settlement_unit="C",
            settlement_policy="whole_degrees",
            source="weather_com_history_api",
        )

        self.assertTrue(profile["boundary_ambiguous"])
        self.assertEqual(profile["ambiguous_reason"], "native_f_integer_display_crosses_c_floor_bucket")
        self.assertEqual(profile["settlement_interval_floor_buckets"], [32, 33])

    def test_wu_twc_metric_history_source_is_not_ambiguous_even_with_imperial_pair(self) -> None:
        profile = temperature_settlement_profile_from_wu_twc_features(
            {
                "wu_twc_observed_intraday_today_max_c": 30.0,
                "wu_twc_observed_intraday_today_max_source": "wu_twc_observed_intraday_history",
                "wu_twc_observed_intraday_history_raw_value": 30.0,
                "wu_twc_observed_intraday_history_raw_unit": "C",
                "wu_twc_observed_intraday_history_raw_imperial_value": 86.0,
                "wu_twc_observed_intraday_history_raw_imperial_unit": "F",
            },
            rounding_rule="whole_degrees",
            settlement_unit="C",
        )

        self.assertFalse(profile["boundary_ambiguous"])
        self.assertEqual(profile["native_unit"], "C")

    def test_wu_twc_imperial_only_source_is_ambiguous_at_celsius_boundary(self) -> None:
        profile = temperature_settlement_profile_from_wu_twc_features(
            {
                "wu_twc_observed_intraday_today_max_c": 32.7777777778,
                "wu_twc_observed_intraday_today_max_source": "wu_twc_observed_intraday_current_imperial_max_since7am",
                "wu_twc_observed_intraday_current_imperial_max_since7am_raw_value": 91,
                "wu_twc_observed_intraday_current_imperial_raw_unit": "F",
            },
            rounding_rule="whole_degrees",
            settlement_unit="C",
        )

        self.assertTrue(profile["boundary_ambiguous"])
        self.assertEqual(profile["settlement_interval_floor_buckets"], [32, 33])

    def test_chengdu_wu_temperature_bias_applies_only_to_chengdu(self) -> None:
        report = {
            "generated_at": "2026-06-08T00:00:00+00:00",
            "history_window_policy": "available_history",
            "start_date": "2025-06-09",
            "end_date": "2026-06-08",
            "bias_correction": {
                "recommended_scope": "wu_only",
                "basis": "all_available_chengdu_history_in_window",
                "sample_quality": {"tier": "low", "min_model_sample_count": 7},
                "wu_only": {
                    "stats_by_model": {
                        "gfs_seamless": {"recommended_temp_shift_c": 0.5},
                        "icon_seamless": {"recommended_temp_shift_c": -1.0},
                    }
                },
            },
        }
        old_path = os.environ.get("PM_CHENGDU_WU_MODEL_BIAS_REPORT_PATH")
        old_enabled = os.environ.get("PM_CHENGDU_WU_MODEL_BIAS_ENABLED")
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
                json.dump(report, tmp)
                tmp_path = tmp.name
            os.environ["PM_CHENGDU_WU_MODEL_BIAS_REPORT_PATH"] = tmp_path
            os.environ["PM_CHENGDU_WU_MODEL_BIAS_ENABLED"] = "1"
            CHENGDU_WU_MODEL_BIAS_CACHE.clear()
            CHENGDU_WU_MODEL_BIAS_CACHE.update({"path": "", "mtime": None, "data": None})

            members = {
                "gfs_seamless": np.array([28.0, 29.0]),
                "icon_seamless": np.array([27.0]),
                "ecmwf_ifs025": np.array([26.0]),
            }
            shifted, meta = apply_chengdu_wu_model_temperature_bias(
                members,
                market_var="temperature_2m_max",
                market_unit="C",
                slug="highest-temperature-in-chengdu-on-june-9-2026",
                station_id="ZUUU",
            )

            self.assertTrue(meta["applied"])
            np.testing.assert_allclose(shifted["gfs_seamless"], np.array([28.5, 29.5]))
            np.testing.assert_allclose(shifted["icon_seamless"], np.array([26.0]))
            np.testing.assert_allclose(shifted["ecmwf_ifs025"], np.array([26.0]))

            non_chengdu, non_meta = apply_chengdu_wu_model_temperature_bias(
                members,
                market_var="temperature_2m_max",
                market_unit="C",
                slug="highest-temperature-in-london-on-june-9-2026",
                station_id="EGLC",
            )
            self.assertFalse(non_meta["applied"])
            self.assertEqual(non_meta["reason"], "not_chengdu_zuuu")
            np.testing.assert_allclose(non_chengdu["gfs_seamless"], members["gfs_seamless"])
        finally:
            if old_path is None:
                os.environ.pop("PM_CHENGDU_WU_MODEL_BIAS_REPORT_PATH", None)
            else:
                os.environ["PM_CHENGDU_WU_MODEL_BIAS_REPORT_PATH"] = old_path
            if old_enabled is None:
                os.environ.pop("PM_CHENGDU_WU_MODEL_BIAS_ENABLED", None)
            else:
                os.environ["PM_CHENGDU_WU_MODEL_BIAS_ENABLED"] = old_enabled
            CHENGDU_WU_MODEL_BIAS_CACHE.clear()
            CHENGDU_WU_MODEL_BIAS_CACHE.update({"path": "", "mtime": None, "data": None})
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    def test_archive_write_scope_allows_only_chengdu_zuuu_when_enabled(self) -> None:
        old_scope = os.environ.get("PM_ARCHIVE_WRITE_SCOPE")
        try:
            os.environ["PM_ARCHIVE_WRITE_SCOPE"] = "chengdu_zuuu"
            self.assertTrue(
                _archive_write_allowed_for_chengdu_zuuu(
                    slug="highest-temperature-in-chengdu-on-june-9-2026",
                    station_id="ZUUU",
                )
            )
            self.assertTrue(
                _archive_write_allowed_for_chengdu_zuuu(
                    title="Highest temperature in Chengdu",
                    city="Chengdu",
                )
            )
            self.assertFalse(
                _archive_write_allowed_for_chengdu_zuuu(
                    slug="highest-temperature-in-london-on-june-9-2026",
                    station_id="EGLC",
                    resolution_source="https://www.wunderground.com/history/daily/gb/london/EGLC",
                    lat=51.505,
                    lon=0.055,
                )
            )
        finally:
            if old_scope is None:
                os.environ.pop("PM_ARCHIVE_WRITE_SCOPE", None)
            else:
                os.environ["PM_ARCHIVE_WRITE_SCOPE"] = old_scope


if __name__ == "__main__":
    unittest.main()

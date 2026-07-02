import argparse
import unittest

from scripts import weather_market_anomaly_monitor as monitor


def classify(result):
    return monitor.classify_bucket(
        result,
        gap_threshold=0.10,
        jump_threshold=0.05,
        wallet_share_threshold=0.40,
        thin_depth_threshold=100.0,
        turnover_threshold=0.30,
        cross_wallet_threshold=50.0,
        core_price_change_5m=0.03,
        book_inducement_5m_max_notional=25.0,
        book_inducement_30m_max_notional=75.0,
        wallet_flow_min_notional=100.0,
    )


class WeatherMarketAnomalyMonitorTest(unittest.TestCase):
    def test_market_context_analysis_is_reference_only(self):
        analysis = monitor.analyze_market_context_text(
            (
                "Forecast models from sources including the National Meteorological Center of China "
                "and international agencies have converged on a daily maximum of 31°C for Chengdu, "
                "supported by stable high-pressure conditions, light winds, and limited cloud cover. "
                "Outliers remain possible only if late-day convective activity or sensor calibration "
                "adjustments alter the official peak reading."
            )
        )

        self.assertTrue(analysis["is_reference_only"])
        self.assertTrue(analysis["does_not_change_probability"])
        self.assertTrue(analysis["does_not_change_trade_decision"])
        self.assertEqual(analysis["target_temperature_range_c"], {"low_c": 31, "high_c": 31})
        self.assertIn("National Meteorological Center of China", analysis["source_mentions"])
        self.assertIn("高压/稳定形势", analysis["support_factors"])
        self.assertIn("午后对流/雷暴", analysis["risk_factors"])
        self.assertIn("页面 Context 倾向 31°C", analysis["reference_hint"])

    def test_market_context_target_ignores_small_delta_temperature(self):
        analysis = monitor.analyze_market_context_text(
            (
                "Forecast consensus from major models places Chengdu’s June 14 maximum in the "
                "low-to-mid 30s Celsius, producing the tight clustering of market-implied odds "
                "around 32–34 °C. Afternoon convective clouds or scattered showers can cap peak "
                "temperatures by 1–2 °C depending on timing and coverage."
            )
        )

        self.assertEqual(analysis["target_temperature_range_c"], {"low_c": 32, "high_c": 34})
        self.assertIn("页面 Context 倾向 32-34°C", analysis["reference_hint"])
        self.assertNotIn("1-34°C", analysis["reference_hint"])

    def test_climo_regime_summary_is_reference_layer(self):
        rows = monitor.climo_regime_summaries(
            [
                {
                    "slug": "highest-temperature-in-chengdu-on-june-14-2026",
                    "event_url": "https://polymarket.com/event/highest-temperature-in-chengdu-on-june-14-2026",
                    "climo_regime_summary": {
                        "available": True,
                        "display_label": "气候/环流弱参考，不覆盖实况温度轨迹",
                        "potential_label": "支持升温潜力 ≠ 已兑现升温",
                        "trajectory_priority_label": "实况温度轨迹优先",
                        "climo_tmax_p50_c": 30.0,
                        "climo_tmax_p90_c": 34.0,
                        "evidence": 1.2,
                        "top_label": "31°C",
                        "top_prob": 0.24,
                    },
                }
            ]
        )

        self.assertEqual(rows[0]["event_date"], "2026-06-14")
        self.assertEqual(rows[0]["display_label"], "气候/环流弱参考，不覆盖实况温度轨迹")
        self.assertEqual(rows[0]["potential_label"], "支持升温潜力 ≠ 已兑现升温")
        self.assertEqual(rows[0]["trajectory_priority_label"], "实况温度轨迹优先")
        self.assertEqual(rows[0]["top_label"], "31°C")

    def test_low_trade_flow_price_jump_is_book_inducement_not_large_money(self):
        assessment = classify(
            {
                "market_mid": 0.56,
                "model": {"q_primary": 0.2734},
                "price_change_5m": 0.325,
                "price_change_30m": 0.365,
                "liquidity": 520.0,
                "book": {"bid_depth_top5": 115.0, "ask_depth_top5": 372.0},
                "trade_aggregate": {
                    "total_notional": 1388.0,
                    "yes_direction_net": -850.0,
                    "top3_wallet_share": 0.29,
                },
                "recent_trade_windows": {
                    "5m": {"total_notional": 0.0, "top3_wallet_share": None},
                    "30m": {"total_notional": 16.59, "top3_wallet_share": 0.827},
                },
            }
        )

        self.assertIn("thin_book_price_inducement", assessment["classification"])
        self.assertNotIn("large_money_rational_reverse_trading", assessment["classification"])
        self.assertTrue(assessment["book_inducement"]["triggered"])
        self.assertFalse(assessment["wallet_concentration"]["triggered"])
        self.assertEqual(
            monitor.alert_evidence_type(
                assessment["classification"],
                [key for key, enabled in assessment["triggers"].items() if enabled],
            ),
            "book_inducement",
        )

    def test_wallet_concentration_requires_recent_notional_floor(self):
        low_flow = classify(
            {
                "market_mid": 0.21,
                "model": {"q_primary": 0.24},
                "price_change_5m": 0.0,
                "price_change_30m": 0.0,
                "liquidity": 480.0,
                "book": {"bid_depth_top5": 190.0, "ask_depth_top5": 200.0},
                "trade_aggregate": {
                    "total_notional": 500.0,
                    "yes_direction_net": 40.0,
                    "top3_wallet_share": 0.90,
                },
                "recent_trade_windows": {
                    "5m": {"total_notional": 0.0, "top3_wallet_share": None},
                    "30m": {"total_notional": 2.5, "top3_wallet_share": 1.0},
                },
            }
        )
        self.assertFalse(low_flow["wallet_concentration"]["triggered"])
        self.assertNotIn("wallet_concentration_trade_flow", low_flow["classification"])

        high_flow = classify(
            {
                "market_mid": 0.07,
                "model": {"q_primary": 0.237},
                "price_change_5m": -0.01,
                "price_change_30m": -0.01,
                "liquidity": 725.0,
                "book": {"bid_depth_top5": 528.0, "ask_depth_top5": 76.0},
                "trade_aggregate": {
                    "total_notional": 1266.0,
                    "yes_direction_net": 159.0,
                    "top3_wallet_share": 0.49,
                },
                "recent_trade_windows": {
                    "5m": {"total_notional": 0.0, "top3_wallet_share": None},
                    "30m": {"total_notional": 164.49, "top3_wallet_share": 0.997},
                },
            }
        )

        self.assertTrue(high_flow["wallet_concentration"]["triggered"])
        self.assertIn("wallet_concentration_trade_flow", high_flow["classification"])
        self.assertEqual(
            monitor.alert_evidence_type(
                high_flow["classification"],
                [key for key, enabled in high_flow["triggers"].items() if enabled],
            ),
            "wallet_concentration",
        )

    def test_build_alert_marks_book_inducement_direction_as_insufficient_flow(self):
        assessment = classify(
            {
                "market_mid": 0.56,
                "model": {"q_primary": 0.2734},
                "price_change_5m": 0.325,
                "price_change_30m": 0.365,
                "liquidity": 520.0,
                "book": {"bid_depth_top5": 115.0, "ask_depth_top5": 372.0},
                "trade_aggregate": {
                    "total_notional": 1388.0,
                    "yes_direction_net": -850.0,
                    "top3_wallet_share": 0.29,
                    "top_wallets": [],
                },
                "recent_trade_windows": {
                    "5m": {"total_notional": 0.0, "top3_wallet_share": None},
                    "30m": {"total_notional": 16.59, "top3_wallet_share": 0.827},
                },
            }
        )
        alerts = monitor.build_alerts(
            [
                {
                    "slug": "highest-temperature-in-chengdu-on-june-13-2026",
                    "event_url": "https://polymarket.com/event/highest-temperature-in-chengdu-on-june-13-2026",
                    "event_triggers": {"core_bucket_price_probability_change": {"changed_buckets": []}},
                    "buckets": [
                        {
                            "label": "30°C or below",
                            "market_mid": 0.56,
                            "price_change_5m": 0.325,
                            "price_change_30m": 0.365,
                            "model": {"q_bayes_posterior": 0.2734},
                            "trade_aggregate": {"yes_direction_net": -850.0, "top_wallets": []},
                            "assessment": assessment,
                        }
                    ],
                }
            ],
            argparse.Namespace(alert_risk_threshold=40.0),
        )

        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["alert_type"], "book_inducement")
        self.assertEqual(alerts[0]["large_money_direction"], "insufficient_recent_trade_flow")


if __name__ == "__main__":
    unittest.main()

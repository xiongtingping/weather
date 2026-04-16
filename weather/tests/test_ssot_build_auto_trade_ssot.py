import unittest
from unittest.mock import patch


class TestSsotBuildAutoTradeSsot(unittest.TestCase):
    def test_lcb_global_ssot_entry_reasons_follow_tradeability_gate(self) -> None:
        from ui.server import _ssot_build_auto_trade_ssot

        payload = {
            "market_var": "temperature_2m_max",
            "run_date": "2026-04-15",
            "event_date": "2026-04-15",
            "market_closed": 0,
            "distribution": [
                {
                    "outcome": "23C",
                    "best_ask": 0.30,
                    "best_bid": 0.25,
                    "spread": 0.05,
                    "q_round": 0.55,
                    "q_entry": 0.55,
                    "yes_token": "yes-token",
                    "edge_score_yes": 5.0,
                    "exec_edge_entry_yes": 0.20,
                    "exec_edge_exit_yes": 0.20,
                    "exec_edge_score_yes": 5.0,
                    "edge_realizable_yes": 0.20,
                    "edge_realizable_score_yes": 5.0,
                    "exec_fee_cost_yes": 0.0,
                    "exec_fee_bps": 100.0,
                    "edge_signal_lcb_yes": 0.11,
                    "edge_path_p_reach_lcb_yes": 0.22,
                    "edge_tradeability_profit_value_yes": 0.12,
                    "edge_tradeability_signal_lcb_min_yes": 0.35,
                    "edge_tradeability_path_lcb_min_yes": 0.03,
                    "edge_tradeability_profit_min_yes": 0.0005,
                    "edge_tradeability_pass_yes": False,
                    "edge_tradeability_reasons_yes": "signal_lcb_below_min",
                }
            ],
            "auto_trades": [],
        }

        with patch.dict(
            "os.environ",
            {
                "PM_LCB_GLOBAL_ENABLED": "1",
                "PM_AUTO_TRADE_ENTRY_ONLY_REALIZABLE": "1",
            },
            clear=False,
        ):
            ssot = _ssot_build_auto_trade_ssot(payload)

        self.assertIsInstance(ssot, dict)
        self.assertIs(ssot["main_entry_ok"], False)
        self.assertIn("signal_lcb=0.110<0.350", ssot["main_entry_reasons"])

    def test_compute_edge_fields_backfills_lcb_runtime_fields(self) -> None:
        from ui.server import _compute_edge_fields_in_place

        row = {
            "outcome": "23C",
            "run_date": "2026-04-15",
            "event_date": "2026-04-15",
            "best_ask": 0.30,
            "best_bid": 0.25,
            "spread": 0.05,
            "ask_depth_topN": 100.0,
            "ask_depth_total": 100.0,
            "entry_size_yes": 10.0,
            "q_round": 0.65,
            "q_raw": 0.65,
            "q_calibrated": 0.65,
            "q_entry": 0.65,
            "q_edge": 0.65,
            "q_round_model_spread": 0.01,
            "time_factor_yes": 0.80,
            "time_factor": 0.80,
            "time_trend": 0.0,
            "time_freshness": 1.0,
            "structure_factor": 1.0,
        }

        with patch.dict(
            "os.environ",
            {
                "PM_EDGE_TRADEABILITY_ENABLED": "1",
                "PM_EDGE_REALIZABLE_V2_ENABLED": "1",
                "PM_LCB_GLOBAL_ENABLED": "1",
                "PM_LCB_GLOBAL_SIGNAL_LCB_MIN": "0",
                "PM_LCB_GLOBAL_PATH_LCB_MIN": "0",
                "PM_LCB_GLOBAL_EDGE_LCB_MIN": "0",
                "PM_LCB_GLOBAL_EDGE_LCB_MIN_D0": "0",
                "PM_LCB_GLOBAL_EDGE_LCB_MIN_D1": "0",
                "PM_EDGE_POLICY_REQUIRE_EXEC_AUDIT": "0",
                "PM_EDGE_POLICY_REQUIRE_V2": "0",
                "PM_EDGE_PATH_CALIBRATION_PATH": "",
                "PM_EDGE_REALIZABLE_CALIBRATION_PATH": "",
                "PM_EDGE_REALIZABLE_CALIBRATION_PATH_ORDERS": "",
            },
            clear=False,
        ):
            _compute_edge_fields_in_place(row, 0.1)

        self.assertIsNotNone(row.get("exec_edge_entry_yes"))
        self.assertIsNotNone(row.get("edge_signal_lcb_yes"))
        self.assertIsNotNone(row.get("edge_path_p_reach_lcb_yes"))
        self.assertIsNotNone(row.get("edge_tradeability_profit_value_yes"))
        self.assertIs(row.get("edge_tradeability_pass_yes"), True)

    def test_builds_candidate_from_side_specific_lcb_fields(self) -> None:
        from ui.server import _ssot_build_auto_trade_ssot

        payload = {
            "market_var": "temperature_2m_max",
            "run_date": "2026-04-15",
            "event_date": "2026-04-15",
            "market_closed": 0,
            "distribution": [
                {
                    "outcome": "23C",
                    "best_ask": 0.30,
                    "best_bid": 0.25,
                    "spread": 0.05,
                    "q_round": 0.55,
                    "q_entry": 0.55,
                    "yes_token": "yes-token",
                    "edge_score_yes": 5.0,
                    "exec_edge_entry_yes": 0.20,
                    "exec_edge_exit_yes": 0.20,
                    "exec_edge_score_yes": 5.0,
                    "edge_realizable_yes": 0.20,
                    "edge_realizable_score_yes": 5.0,
                    "exec_fee_cost_yes": 0.0,
                    "exec_fee_bps": 100.0,
                    "edge_signal_lcb_yes": 0.11,
                    "edge_path_p_reach_lcb_yes": 0.22,
                    "edge_tradeability_profit_value_yes": 0.12,
                    "edge_tradeability_signal_lcb_min_yes": 0.01,
                    "edge_tradeability_path_lcb_min_yes": 0.02,
                    "edge_tradeability_profit_min_yes": 0.03,
                    "edge_tradeability_pass_yes": True,
                }
            ],
            "auto_trades": [],
        }

        ssot = _ssot_build_auto_trade_ssot(payload)

        self.assertIsInstance(ssot, dict)
        cand = ssot["main_candidate"]
        self.assertIsInstance(cand, dict)
        self.assertAlmostEqual(float(cand["edge_signal_lcb"]), 0.11, places=9)
        self.assertAlmostEqual(float(cand["edge_path_p_reach_lcb"]), 0.22, places=9)
        self.assertAlmostEqual(float(cand["edge_tradeability_profit_value"]), 0.12, places=9)
        self.assertIs(cand["edge_tradeability_pass"], True)

    def test_lcb_global_ssot_blocks_plain_peak_no_bucket(self) -> None:
        from ui.server import _ssot_build_auto_trade_ssot

        payload = {
            "market_var": "temperature_2m_max",
            "run_date": "2026-04-15",
            "event_date": "2026-04-16",
            "market_closed": 0,
            "market_top_label": "25C+",
            "distribution": [
                {
                    "outcome": "25C+",
                    "best_ask_no": 0.50,
                    "best_bid_no": 0.50,
                    "spread_no": 0.00,
                    "q_round_no": 0.89,
                    "q_entry_no": 0.88,
                    "no_token": "no-token",
                    "edge_score_no": 15.0,
                    "exec_edge_entry_no": 0.30,
                    "exec_edge_exit_no": 0.30,
                    "exec_edge_score_no": 15.0,
                    "edge_realizable_no": 0.30,
                    "edge_realizable_score_no": 15.0,
                    "exec_fee_cost_no": 0.0,
                    "exec_fee_bps": 100.0,
                    "edge_signal_lcb_no": 0.60,
                    "edge_path_p_reach_lcb_no": 0.70,
                    "edge_tradeability_profit_value_no": 0.20,
                    "edge_tradeability_signal_lcb_min_no": 0.35,
                    "edge_tradeability_path_lcb_min_no": 0.03,
                    "edge_tradeability_profit_min_no": 0.0005,
                    "edge_tradeability_pass_no": True,
                }
            ],
            "auto_trades": [],
        }

        with patch.dict(
            "os.environ",
            {
                "PM_LCB_GLOBAL_ENABLED": "1",
                "PM_AUTO_TRADE_ENTRY_ONLY_REALIZABLE": "1",
                "PM_LCB_GLOBAL_PLAIN_NO_PEAK_EXCLUDE": "1",
            },
            clear=False,
        ):
            ssot = _ssot_build_auto_trade_ssot(payload)

        self.assertIsInstance(ssot, dict)
        self.assertIs(ssot["main_entry_ok"], False)
        self.assertIn("Plain LCB不做Q峰值桶NO", ssot["main_entry_reasons"])

    def test_get_or_build_rebuilds_legacy_ssot_missing_lcb_fields(self) -> None:
        from ui.server import _get_or_build_auto_trade_ssot

        payload = {
            "market_var": "temperature_2m_max",
            "run_date": "2026-04-15",
            "event_date": "2026-04-15",
            "market_closed": 0,
            "distribution": [
                {
                    "outcome": "23C",
                    "best_ask": 0.30,
                    "best_bid": 0.25,
                    "spread": 0.05,
                    "ask_depth_topN": 100.0,
                    "ask_depth_total": 100.0,
                    "entry_size_yes": 10.0,
                    "q_round": 0.65,
                    "q_raw": 0.65,
                    "q_calibrated": 0.65,
                    "q_entry": 0.65,
                    "q_edge": 0.65,
                    "q_round_model_spread": 0.01,
                    "time_factor_yes": 0.80,
                    "time_factor": 0.80,
                    "time_trend": 0.0,
                    "time_freshness": 1.0,
                    "structure_factor": 1.0,
                    "yes_token": "yes-token",
                    "edge_score_yes": 5.0,
                    "exec_edge_entry_yes": 0.20,
                    "exec_edge_exit_yes": 0.20,
                    "exec_edge_score_yes": 5.0,
                    "edge_realizable_yes": 0.20,
                    "edge_realizable_score_yes": 5.0,
                    "exec_fee_cost_yes": 0.0,
                    "exec_fee_bps": 100.0,
                }
            ],
            "auto_trades": [],
            "auto_trade_ssot": {
                "version": 2,
                "main_candidate": {
                    "side": "YES",
                    "outcome": "23C",
                    "ask": 0.30,
                    "exec_edge_score": 5.0,
                    "exec_edge_entry": 0.20,
                    "exec_edge_exit": 0.20,
                    "edge_realizable": 0.20,
                    "edge_realizable_score": 5.0,
                    "exec_fee_cost": 0.0,
                    "exec_fee_bps": 100.0,
                },
                "main_entry_ok": True,
                "main_entry_reasons": [],
            },
        }

        with patch.dict(
            "os.environ",
            {
                "PM_EDGE_TRADEABILITY_ENABLED": "1",
                "PM_EDGE_REALIZABLE_V2_ENABLED": "1",
                "PM_LCB_GLOBAL_ENABLED": "1",
                "PM_LCB_GLOBAL_SIGNAL_LCB_MIN": "0",
                "PM_LCB_GLOBAL_PATH_LCB_MIN": "0",
                "PM_LCB_GLOBAL_EDGE_LCB_MIN": "0",
                "PM_LCB_GLOBAL_EDGE_LCB_MIN_D0": "0",
                "PM_LCB_GLOBAL_EDGE_LCB_MIN_D1": "0",
                "PM_EDGE_POLICY_REQUIRE_EXEC_AUDIT": "0",
                "PM_EDGE_POLICY_REQUIRE_V2": "0",
                "PM_EDGE_PATH_CALIBRATION_PATH": "",
                "PM_EDGE_REALIZABLE_CALIBRATION_PATH": "",
                "PM_EDGE_REALIZABLE_CALIBRATION_PATH_ORDERS": "",
            },
            clear=False,
        ):
            ssot = _get_or_build_auto_trade_ssot(payload)

        self.assertIsInstance(ssot, dict)
        cand = ssot["main_candidate"]
        self.assertIsInstance(cand, dict)
        self.assertIsNotNone(cand.get("edge_signal_lcb"))
        self.assertIsNotNone(cand.get("edge_path_p_reach_lcb"))
        self.assertIsNotNone(cand.get("edge_tradeability_profit_value"))

    def test_get_or_build_rebuilds_stale_lcb_entry_ok_mismatch(self) -> None:
        from ui.server import _get_or_build_auto_trade_ssot

        payload = {
            "market_var": "temperature_2m_max",
            "run_date": "2026-04-15",
            "event_date": "2026-04-15",
            "market_closed": 0,
            "distribution": [
                {
                    "outcome": "23C",
                    "best_ask": 0.30,
                    "best_bid": 0.25,
                    "spread": 0.05,
                    "q_round": 0.55,
                    "q_entry": 0.55,
                    "yes_token": "yes-token",
                    "edge_score_yes": 5.0,
                    "exec_edge_entry_yes": 0.20,
                    "exec_edge_exit_yes": 0.20,
                    "exec_edge_score_yes": 5.0,
                    "edge_realizable_yes": 0.20,
                    "edge_realizable_score_yes": 5.0,
                    "exec_fee_cost_yes": 0.0,
                    "exec_fee_bps": 100.0,
                    "edge_signal_lcb_yes": 0.11,
                    "edge_path_p_reach_lcb_yes": 0.22,
                    "edge_tradeability_profit_value_yes": 0.12,
                    "edge_tradeability_signal_lcb_min_yes": 0.35,
                    "edge_tradeability_path_lcb_min_yes": 0.03,
                    "edge_tradeability_profit_min_yes": 0.0005,
                    "edge_tradeability_pass_yes": False,
                    "edge_tradeability_reasons_yes": "signal_lcb_below_min",
                }
            ],
            "auto_trades": [],
            "auto_trade_ssot": {
                "version": 2,
                "main_candidate": {
                    "side": "YES",
                    "outcome": "23C",
                    "ask": 0.30,
                    "exec_edge_score": 5.0,
                    "exec_edge_entry": 0.20,
                    "exec_edge_exit": 0.20,
                    "edge_realizable": 0.20,
                    "edge_realizable_score": 5.0,
                    "exec_fee_cost": 0.0,
                    "exec_fee_bps": 100.0,
                    "edge_signal_lcb": 0.11,
                    "edge_path_p_reach_lcb": 0.22,
                    "edge_tradeability_profit_value": 0.12,
                    "edge_tradeability_signal_lcb_min": 0.35,
                    "edge_tradeability_path_lcb_min": 0.03,
                    "edge_tradeability_profit_min": 0.0005,
                    "edge_tradeability_pass": False,
                    "edge_tradeability_reasons": "signal_lcb_below_min",
                },
                "main_entry_ok": True,
                "main_entry_reasons": [],
            },
        }

        with patch.dict(
            "os.environ",
            {
                "PM_EDGE_TRADEABILITY_ENABLED": "1",
                "PM_LCB_GLOBAL_ENABLED": "1",
            },
            clear=False,
        ):
            ssot = _get_or_build_auto_trade_ssot(payload)

        self.assertIsInstance(ssot, dict)
        self.assertIs(ssot["main_entry_ok"], False)
        self.assertIn("signal_lcb=0.110<0.350", ssot["main_entry_reasons"])

    def test_get_or_build_rebuilds_stale_plain_peak_entry_ok_mismatch(self) -> None:
        from ui.server import _get_or_build_auto_trade_ssot

        payload = {
            "market_var": "temperature_2m_max",
            "run_date": "2026-04-15",
            "event_date": "2026-04-16",
            "market_closed": 0,
            "market_top_label": "25C+",
            "distribution": [
                {
                    "outcome": "25C+",
                    "best_ask_no": 0.50,
                    "best_bid_no": 0.50,
                    "spread_no": 0.00,
                    "q_round_no": 0.89,
                    "q_entry_no": 0.88,
                    "no_token": "no-token",
                    "edge_score_no": 15.0,
                    "exec_edge_entry_no": 0.30,
                    "exec_edge_exit_no": 0.30,
                    "exec_edge_score_no": 15.0,
                    "edge_realizable_no": 0.30,
                    "edge_realizable_score_no": 15.0,
                    "exec_fee_cost_no": 0.0,
                    "exec_fee_bps": 100.0,
                    "edge_signal_lcb_no": 0.60,
                    "edge_path_p_reach_lcb_no": 0.70,
                    "edge_tradeability_profit_value_no": 0.20,
                    "edge_tradeability_signal_lcb_min_no": 0.35,
                    "edge_tradeability_path_lcb_min_no": 0.03,
                    "edge_tradeability_profit_min_no": 0.0005,
                    "edge_tradeability_pass_no": True,
                }
            ],
            "auto_trades": [],
            "auto_trade_ssot": {
                "version": 2,
                "main_candidate": {
                    "side": "NO",
                    "outcome": "25C+",
                    "ask": 0.50,
                    "exec_edge_score": 15.0,
                    "exec_edge_entry": 0.30,
                    "exec_edge_exit": 0.30,
                    "edge_realizable": 0.30,
                    "edge_realizable_score": 15.0,
                    "exec_fee_cost": 0.0,
                    "exec_fee_bps": 100.0,
                    "edge_signal_lcb": 0.60,
                    "edge_path_p_reach_lcb": 0.70,
                    "edge_tradeability_profit_value": 0.20,
                    "edge_tradeability_signal_lcb_min": 0.35,
                    "edge_tradeability_path_lcb_min": 0.03,
                    "edge_tradeability_profit_min": 0.0005,
                    "edge_tradeability_pass": True,
                },
                "main_entry_ok": True,
                "main_entry_reasons": [],
            },
        }

        with patch.dict(
            "os.environ",
            {
                "PM_LCB_GLOBAL_ENABLED": "1",
                "PM_AUTO_TRADE_ENTRY_ONLY_REALIZABLE": "1",
                "PM_LCB_GLOBAL_PLAIN_NO_PEAK_EXCLUDE": "1",
            },
            clear=False,
        ):
            ssot = _get_or_build_auto_trade_ssot(payload)

        self.assertIsInstance(ssot, dict)
        self.assertIs(ssot["main_entry_ok"], False)
        self.assertIn("Plain LCB不做Q峰值桶NO", ssot["main_entry_reasons"])

    def test_builds_candidate_without_local_coalesce_helper(self) -> None:
        from ui.server import _ssot_build_auto_trade_ssot

        payload = {
            "market_var": "temperature_2m_max",
            "run_date": "2026-04-15",
            "event_date": "2026-04-15",
            "market_closed": 0,
            "distribution": [
                {
                    "outcome": "23C",
                    "best_ask": 0.30,
                    "best_bid": 0.25,
                    "spread": 0.05,
                    "q_round": 0.55,
                    "q_entry": 0.55,
                    "yes_token": "yes-token",
                    "edge_score_yes": 5.0,
                    "exec_edge_entry_yes": 0.20,
                    "exec_edge_exit_yes": 0.20,
                    "exec_edge_score_yes": 5.0,
                    "edge_realizable_yes": 0.20,
                    "edge_realizable_score_yes": 5.0,
                    "edge_tradeability_profit_value": 0.12,
                }
            ],
            "auto_trades": [],
        }

        ssot = _ssot_build_auto_trade_ssot(payload)

        self.assertIsInstance(ssot, dict)
        self.assertIsInstance(ssot.get("main_candidate"), dict)
        self.assertEqual(ssot["main_candidate"].get("outcome"), "23C")
        self.assertEqual(ssot["main_candidate"].get("side"), "YES")
        self.assertAlmostEqual(
            float(ssot["main_candidate"].get("edge_tradeability_profit_value")),
            0.12,
            places=9,
        )

    def test_get_or_build_rebuilds_legacy_exception_ssot(self) -> None:
        from ui.server import _get_or_build_auto_trade_ssot

        payload = {
            "market_var": "temperature_2m_max",
            "run_date": "2026-04-15",
            "event_date": "2026-04-15",
            "market_closed": 0,
            "distribution": [
                {
                    "outcome": "23C",
                    "best_ask": 0.30,
                    "best_bid": 0.25,
                    "spread": 0.05,
                    "q_round": 0.55,
                    "q_entry": 0.55,
                    "yes_token": "yes-token",
                    "edge_score_yes": 5.0,
                    "exec_edge_entry_yes": 0.20,
                    "exec_edge_exit_yes": 0.20,
                    "exec_edge_score_yes": 5.0,
                    "edge_realizable_yes": 0.20,
                    "edge_realizable_score_yes": 5.0,
                }
            ],
            "auto_trades": [],
            "auto_trade_ssot": {
                "version": 2,
                "main_candidate": None,
                "main_entry_ok": False,
                "main_entry_reasons": ["不适用：SSOT构建异常"],
            },
        }

        ssot = _get_or_build_auto_trade_ssot(payload)

        self.assertIsInstance(ssot, dict)
        self.assertIsInstance(ssot.get("main_candidate"), dict)
        self.assertEqual(ssot["main_candidate"].get("outcome"), "23C")


if __name__ == "__main__":
    unittest.main()

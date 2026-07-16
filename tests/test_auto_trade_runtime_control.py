import os
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ui.server as server
import scan_weather_market as swm

ROOT = Path(__file__).resolve().parents[1]


class AutoTradeRuntimeControlTests(unittest.TestCase):
    def test_build_auto_trade_criteria_pills_uses_config_for_signal_band_mode(self) -> None:
        payload = server.build_auto_trade_criteria_pills(
            {
                "entry_only_realizable": True,
                "path_probability_follow_enabled": True,
                "path_probability_follow_size_ratio": 0.35,
                "path_probability_follow_signal_lcb_min": 0.35,
                "path_probability_follow_path_lcb_min": 0.03,
                "path_probability_follow_edge_lcb_min": 0.0005,
                "path_probability_follow_plain_no_peak_exclude_min_price": 0.85,
                "path_probability_follow_signal_soft_band_mode": "small_live",
                "path_probability_follow_signal_soft_band_min": 0.25,
                "path_probability_follow_signal_soft_band_max": 0.35,
                "path_probability_follow_signal_soft_band_size_ratio": 0.35,
                "path_probability_follow_max_spread": 0.02,
                "path_probability_follow_min_depth_usdc": 10,
                "path_probability_follow_max_tick_age_sec": 90,
                "entry_usable_edge_size_scale_enabled": True,
                "entry_usable_edge_size_target": 0.05,
                "entry_usable_edge_min_size_scale": 0.10,
            }
        )
        sections = payload.get("sections") or []
        entry_pills = next(
            (
                section.get("pills")
                for section in sections
                if isinstance(section, dict) and str(section.get("id") or "") == "entry"
            ),
            [],
        )
        texts = [str(item.get("text") or "") for item in entry_pills if isinstance(item, dict)]
        self.assertTrue(any("signal_band=small_live" in text for text in texts))
        self.assertTrue(any("entry_usable_edge" in text and "排序+仓位" in text for text in texts))
        self.assertTrue(any("tradeability快照必需" in text for text in texts))

    def test_no_order_diagnostic_summarizes_repricing_probability_chain(self) -> None:
        rows = [
            {
                "will_order": 0,
                "decided_state": "blocked",
                "reason_codes_json": "[]",
                "created_at": "2026-05-06T01:00:00+00:00",
                "details_json": json.dumps(
                    {
                        "entry_strategy_family": "repricing_bucket",
                        "entry_usable_edge": -0.002,
                        "entry_usable_edge_size_scale": 0.0,
                        "entry_usable_edge_base_source": "edge_tradeability_profit_value",
                        "entry_edge_false_edge_type": "target_exit_execution_probability_too_low",
                        "entry_edge_root_cause": "repricing_target_surplus_discounted_by_execution_probability_below_friction",
                        "entry_edge_exit_path_required": True,
                        "entry_edge_repricing_execution_probability": 0.05,
                        "entry_edge_repricing_target_surplus_raw": 0.06,
                        "entry_edge_repricing_target_surplus_discounted": 0.003,
                        "entry_edge_repricing_friction_floor": 0.007,
                        "entry_edge_repricing_target_surplus_discounted_to_friction": -0.004,
                        "entry_edge_repricing_exit_evidence_limited": True,
                        "entry_edge_repricing_exit_evidence_status": "bucket_evidence",
                        "entry_edge_repricing_exit_evidence_probability_source": "p_converge=bucket_evidence:primary;p_fill_entry=bucket_evidence:primary;p_fill_exit=bucket_evidence:primary",
                        "edge_realizable_p_converge": 0.40,
                        "edge_realizable_p_converge_samples": 12,
                        "edge_realizable_p_converge_evidence_status": "bucket_evidence",
                        "edge_realizable_p_converge_source": "primary",
                        "edge_realizable_p_converge_bucket_key": "conv-low",
                        "edge_realizable_p_converge_bucket_used_key": "conv-low-any",
                        "edge_realizable_p_fill_entry": 0.50,
                        "edge_realizable_p_fill_entry_samples": 10,
                        "edge_realizable_p_fill_entry_evidence_status": "bucket_evidence",
                        "edge_realizable_p_fill_entry_source": "primary",
                        "edge_realizable_p_fill_entry_bucket_key": "entry-mid",
                        "edge_realizable_p_fill_entry_bucket_used_key": "entry-mid-any",
                        "edge_realizable_p_fill_exit": 0.25,
                        "edge_realizable_p_fill_exit_samples": 8,
                        "edge_realizable_p_fill_exit_evidence_status": "bucket_evidence",
                        "edge_realizable_p_fill_exit_source": "primary",
                        "edge_realizable_p_fill_exit_bucket_key": "exit-low",
                        "edge_realizable_p_fill_exit_bucket_used_key": "exit-low-any",
                    }
                ),
            },
            {
                "will_order": 0,
                "decided_state": "blocked",
                "reason_codes_json": "[]",
                "created_at": "2026-05-06T00:59:00+00:00",
                "details_json": json.dumps(
                    {
                        "entry_strategy_family": "lcb_global_main",
                        "entry_usable_edge": -0.01,
                        "entry_usable_edge_size_scale": 0.0,
                        "entry_edge_false_edge_type": "q_lcb_below_effective_entry_price",
                        "entry_edge_root_cause": "conservative_q_cannot_cover_effective_entry_price",
                    }
                ),
            },
            {
                "will_order": 1,
                "decided_state": "entry_ok",
                "reason_codes_json": "[]",
                "created_at": "2026-05-06T00:58:00+00:00",
                "details_json": "{}",
            },
        ]

        payload = server._auto_trade_no_order_diagnostic_from_decision_rows(rows)

        self.assertEqual(payload["window_rows"], 3)
        self.assertEqual(payload["blocked_rows"], 2)
        self.assertEqual(payload["will_order_rows"], 1)
        self.assertEqual(payload["hard_reason_rows"], 0)
        self.assertEqual(payload["nonpositive_entry_usable_edge_rows"], 2)
        self.assertEqual(payload["size_scale_zero_rows"], 2)
        self.assertEqual(
            payload["last_blocked"]["entry_edge_false_edge_type"],
            "target_exit_execution_probability_too_low",
        )
        self.assertEqual(payload["top_false_edge_types"][0]["type"], "q_lcb_below_effective_entry_price")
        repricing = payload["repricing"]
        self.assertEqual(repricing["candidate_rows"], 1)
        self.assertEqual(repricing["chain_rows"], 1)
        self.assertAlmostEqual(repricing["avg_execution_probability"], 0.05)
        self.assertAlmostEqual(repricing["avg_target_surplus_raw"], 0.06)
        self.assertAlmostEqual(repricing["avg_target_surplus_discounted"], 0.003)
        self.assertAlmostEqual(repricing["avg_discounted_surplus_to_friction"], -0.004)
        self.assertEqual(repricing["evidence_limited_rows"], 1)
        self.assertEqual(repricing["top_evidence_statuses"][0]["status"], "bucket_evidence")
        self.assertEqual(
            repricing["top_probability_sources"][0]["source"],
            "p_converge=bucket_evidence:primary",
        )
        self.assertEqual(repricing["limiting_component_top"][0]["component"], "p_fill_exit")
        components = repricing["component_breakdown"]
        self.assertAlmostEqual(components["p_converge"]["avg_probability"], 0.40)
        self.assertAlmostEqual(components["p_fill_entry"]["avg_probability"], 0.50)
        self.assertAlmostEqual(components["p_fill_exit"]["avg_probability"], 0.25)
        self.assertEqual(components["p_fill_exit"]["min_samples"], 8.0)
        self.assertEqual(components["p_fill_exit"]["top_statuses"][0]["status"], "bucket_evidence")
        self.assertEqual(
            components["p_fill_exit"]["low_probability_bucket_top"][0]["bucket_used_key"],
            "exit-low-any",
        )
        self.assertAlmostEqual(
            components["p_fill_exit"]["low_probability_bucket_top"][0]["avg_probability"],
            0.25,
        )
        self.assertEqual(
            components["p_converge"]["low_probability_bucket_top"][0]["bucket_used_key"],
            "conv-low-any",
        )

    def test_compact_reject_ledger_report_exposes_ui_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / "reject_ledger_latest.json"
            report_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "rows_scanned": 10,
                        "settled_rows": 4,
                        "pending_rows": 6,
                        "settlement_missing_rows": 0,
                        "next_mainline_task": "soft_haircut_shadow_probe_outcome_monitoring",
                        "quality": {"blockers": []},
                        "shadow_probe": {
                            "quality": {
                                "total_rows": 3,
                                "current_run_rows": 1,
                                "live_orders_enabled_rows": 0,
                            }
                        },
                        "quality_review": {
                            "current_run_id": 123,
                            "hard_soft_taxonomy": {
                                "active_soft_haircut_reason_count": 2,
                                "active_needs_mapping_reason_count": 0,
                                "active_diagnostic_or_legacy_reason_count": 0,
                                "class_counts": [{"gate_class": "soft_haircut", "active_count": 2}],
                            },
                            "rule_pruning_review": {
                                "next_mainline_task": "rule_pruning_shadow_probe_review",
                                "recommended_action": "shadow_probe_low_protection_high_false_reject_rules_before_pruning",
                                "active_quadrant_counts": [
                                    {"key": "low_protection_high_false_reject", "count": 1}
                                ],
                                "shadow_before_delete_or_release": [
                                    {
                                        "rejection_reason": "strategy_size_ratio_zero",
                                        "active_count": 1,
                                        "avg_counterfactual_pnl_per_share": 0.01,
                                    }
                                ],
                            },
                            "soft_haircut_shadow_probe_ledger": {
                                "status": "shadow_probe_ledger_active",
                                "next_mainline_task": "soft_haircut_shadow_probe_outcome_monitoring",
                                "outcome_monitoring": {
                                    "status": "waiting_for_post_instrument_settlements",
                                    "next_mainline_task": "soft_haircut_shadow_probe_outcome_monitoring",
                                    "rows": [
                                        {
                                            "new_shadow_decision_count": 9,
                                            "required_new_shadow_decisions": 50,
                                            "new_settled_counterfactual_count": 0,
                                            "required_new_settled_counterfactuals": 20,
                                        }
                                    ],
                                },
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            payload = server._compact_reject_ledger_report(report_path)

            self.assertTrue(payload["available"])
            self.assertEqual(payload["current_run_id"], 123)
            self.assertEqual(payload["hard_soft"]["active_soft_haircut_reason_count"], 2)
            self.assertEqual(
                payload["rule_pruning"]["active_quadrant_counts"][0]["key"],
                "low_protection_high_false_reject",
            )
            self.assertEqual(payload["shadow_probe"]["live_orders_enabled_rows"], 0)
            self.assertEqual(
                payload["shadow_probe"]["outcome_rows"][0]["required_new_settled_counterfactuals"],
                20,
            )

    def test_health_decision_rows_fall_back_to_latest_entry_run_date(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE auto_trade_decisions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT,
                action TEXT,
                will_order INTEGER,
                decided_state TEXT,
                reason_codes_json TEXT,
                details_json TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO auto_trade_decisions(
                run_date, action, will_order, decided_state,
                reason_codes_json, details_json, created_at
            ) VALUES ('2026-05-05', 'entry', 0, 'blocked', '[]', ?, '2026-05-06T01:00:00+00:00')
            """,
            (json.dumps({"entry_usable_edge": -0.01}),),
        )

        rows, decision_run_date, source = server._auto_trade_decision_rows_for_health(
            conn.cursor(),
            "2026-05-06",
        )

        self.assertEqual(decision_run_date, "2026-05-05")
        self.assertEqual(source, "latest_entry_decisions")
        self.assertEqual(len(rows), 1)
        conn.close()

    def test_legacy_realizable_flag_migrates_to_tradeability_snapshot(self) -> None:
        payload = {"entry_require_edge_realizable": False}

        migrated = server._migrate_legacy_edge_keys(payload)

        self.assertTrue(migrated)
        self.assertFalse(payload["entry_require_tradeability_snapshot"])
        self.assertFalse(payload["entry_require_edge_realizable"])

    def test_entry_only_realizable_disables_exec_edge_hard_gate_flags(self) -> None:
        payload = {
            "entry_only_realizable": True,
            "entry_yes_top_require_exec_edge": True,
            "entry_follow_require_exec_edge": True,
            "entry_hard_bound_no_require_exec_edge": True,
            "entry_adj_pair_require_exec_edge": True,
            "entry_require_signal_edge": True,
            "entry_require_edge_band": True,
            "entry_require_edge_confidence": True,
            "entry_apply_edge_bias": True,
            "entry_require_depth": True,
            "entry_require_q_range": True,
            "entry_require_q_band": True,
            "entry_require_ask_range": True,
            "entry_require_no_ask_range": True,
            "entry_require_yes_overprice": True,
            "entry_block_no_by_yes_q": True,
            "entry_skip_top_label": True,
            "entry_require_spread_max": True,
            "entry_require_auto_max_ask": True,
            "entry_lag_enabled": True,
            "entry_lag_limit_enabled": True,
            "entry_require_ask_le_q_min": True,
            "entry_min_outcome_volume": 25.0,
            "entry_outcome_volume_gate_mode": "final",
            "entry_outcome_volume_refresh_sec": 60.0,
            "entry_outcome_volume_refresh_missing_only": True,
        }

        server._normalize_entry_only_realizable_config(payload)

        self.assertTrue(payload["entry_only_realizable"])
        for key in (
            "entry_yes_top_require_exec_edge",
            "entry_follow_require_exec_edge",
            "entry_hard_bound_no_require_exec_edge",
            "entry_adj_pair_require_exec_edge",
            "entry_require_signal_edge",
            "entry_require_edge_band",
            "entry_require_edge_confidence",
            "entry_apply_edge_bias",
            "entry_require_depth",
            "entry_require_q_range",
            "entry_require_q_band",
            "entry_require_ask_range",
            "entry_require_no_ask_range",
            "entry_require_yes_overprice",
            "entry_block_no_by_yes_q",
            "entry_skip_top_label",
            "entry_require_spread_max",
            "entry_require_auto_max_ask",
            "entry_lag_enabled",
            "entry_lag_limit_enabled",
            "entry_require_ask_le_q_min",
        ):
            self.assertFalse(payload[key])
        self.assertEqual(payload["entry_min_outcome_volume"], 0.0)
        self.assertEqual(payload["entry_outcome_volume_gate_mode"], "off")
        self.assertEqual(payload["entry_outcome_volume_refresh_sec"], 0.0)
        self.assertFalse(payload["entry_outcome_volume_refresh_missing_only"])

    def test_criteria_pills_only_realizable_shows_soft_edge_profile(self) -> None:
        payload = server.build_auto_trade_criteria_pills(
            {
                "entry_only_realizable": True,
                "entry_require_signal_edge": True,
                "entry_require_edge_band": True,
                "entry_require_edge_confidence": True,
                "entry_apply_edge_bias": True,
                "entry_require_depth": True,
                "entry_require_spread_max": True,
                "entry_require_q_range": True,
                "entry_require_auto_max_ask": True,
                "entry_skip_top_label": True,
            }
        )

        texts = []
        for section in payload.get("sections") or []:
            if not isinstance(section, dict):
                continue
            if str(section.get("id") or "") != "entry":
                continue
            for pill in section.get("pills") or []:
                if isinstance(pill, dict):
                    texts.append(str(pill.get("text") or ""))

        self.assertIn("信号边际（仅展示，不阻断）", texts)
        self.assertIn("edge区间（仅展示）", texts)
        self.assertIn("可信度（仅展示）", texts)
        self.assertIn("偏差（仅展示）", texts)
        self.assertIn("偏差校正（仅展示）", texts)
        self.assertIn("趋势仅诊断（不阻断）", texts)
        self.assertNotIn("信号边际≥", "".join(texts))
        self.assertNotIn("edge区间[", "".join(texts))

    def test_criteria_pills_do_not_render_exec_edge_hard_gate_in_realizable_mode(self) -> None:
        payload = server.build_auto_trade_criteria_pills(
            {
                "entry_only_realizable": True,
                "entry_yes_top_enabled": True,
                "entry_follow_enabled": True,
                "entry_hard_bound_no_enabled": True,
                "entry_adj_pair_enabled": True,
                "entry_yes_top_require_exec_edge": True,
                "entry_follow_require_exec_edge": True,
                "entry_hard_bound_no_require_exec_edge": True,
                "entry_adj_pair_require_exec_edge": True,
            }
        )

        texts = []
        for section in payload.get("sections") or []:
            if not isinstance(section, dict):
                continue
            for pill in section.get("pills") or []:
                if isinstance(pill, dict):
                    texts.append(str(pill.get("text") or ""))

        self.assertFalse(any("硬门槛：exec_edge_entry" in text for text in texts))
        self.assertTrue(any("exec_edge_entry诊断项" in text for text in texts))

    def test_trade_config_page_does_not_label_exec_edge_as_hard_gate(self) -> None:
        html = (Path(__file__).resolve().parents[1] / "ui" / "index.html").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("硬门槛：exec_edge_entry", html)
        self.assertIn("exec_edge诊断", html)

    def test_apply_auto_trade_config_writes_tradeability_snapshot_and_compat_flag(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            server.apply_auto_trade_config(
                {
                    "entry_require_tradeability_snapshot": False,
                    "entry_only_realizable": True,
                }
            )

            self.assertEqual(os.environ.get("PM_AUTO_TRADE_ENTRY_REQUIRE_TRADEABILITY_SNAPSHOT"), "0")
            self.assertEqual(os.environ.get("PM_AUTO_TRADE_ENTRY_REQUIRE_EDGE_REALIZABLE"), "0")

    def test_trace_payload_carries_target_realization_risk_explanation(self) -> None:
        text = Path(server.__file__).read_text(encoding="utf-8")

        self.assertIn('"target_realization_ui_risk_explanation_text"', text)
        self.assertIn('target_realization_decision_details[risk_key] = details.get(risk_key)', text)
        self.assertIn('"details": target_realization_decision_details', text)
        self.assertIn('no_order_reasons.append(f"路径风险解释: {target_realization_risk_text}")', text)

    def test_scan_runtime_config_accepts_tradeability_snapshot_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "auto_trade_config.json"
            config_path.write_text(
                '{"entry_require_tradeability_snapshot": false}\n',
                encoding="utf-8",
            )
            with patch.object(swm, "AUTO_TRADE_CONFIG_PATH", config_path), patch.dict(
                os.environ,
                {},
                clear=False,
            ):
                original_mtime = swm._AUTO_TRADE_CONFIG_MTIME
                original_dry_run = swm.AUTO_TRADE_DRY_RUN
                try:
                    swm._AUTO_TRADE_CONFIG_MTIME = None
                    os.environ.pop("PM_AUTO_TRADE_ENTRY_REQUIRE_TRADEABILITY_SNAPSHOT", None)
                    os.environ.pop("PM_AUTO_TRADE_ENTRY_REQUIRE_EDGE_REALIZABLE", None)

                    swm._refresh_auto_trade_config()

                    self.assertEqual(
                        os.environ.get("PM_AUTO_TRADE_ENTRY_REQUIRE_TRADEABILITY_SNAPSHOT"),
                        "0",
                    )
                    self.assertEqual(
                        os.environ.get("PM_AUTO_TRADE_ENTRY_REQUIRE_EDGE_REALIZABLE"),
                        "0",
                    )
                finally:
                    swm._AUTO_TRADE_CONFIG_MTIME = original_mtime
                    swm.AUTO_TRADE_DRY_RUN = original_dry_run

    def test_entry_tradeability_gate_ignores_legacy_v2_entry_gate_env(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PM_EDGE_REALIZABLE_V2_ENABLED": "0",
                "PM_EDGE_REALIZABLE_V2_GATE_ENTRY": "0",
            },
            clear=True,
        ):
            self.assertTrue(swm._entry_tradeability_gate_enabled_from_env())
        with patch.dict(
            os.environ,
            {
                "PM_EDGE_TRADEABILITY_GATE_ENTRY": "0",
                "PM_EDGE_REALIZABLE_V2_GATE_ENTRY": "1",
            },
            clear=True,
        ):
            self.assertFalse(swm._entry_tradeability_gate_enabled_from_env())

    def test_hydrate_auto_trade_runtime_env_merges_repo_and_shared_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            repo_env = tmp_path / ".env"
            shared_env = tmp_path / "shared.env"
            repo_env.write_text(
                "\n".join(
                    [
                        "PM_AUTO_TRADE_ENABLED=1",
                        "PM_AUTO_TRADE_DRY_RUN=0",
                        "PM_AUTO_TRADE_SYNC_TRADES=1",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            shared_env.write_text(
                "\n".join(
                    [
                        "POLY_API_KEY=test-key",
                        "POLY_API_SECRET=test-secret",
                        "POLY_API_PASSPHRASE=test-passphrase",
                        "POLY_PRIVATE_KEY=0xabc123",
                        "PM_FUNDER_ADDRESS=0xfeedface",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with patch.object(server, "ENV_PATH", repo_env), patch.dict(
                os.environ,
                {"PM_SHARED_POLY_ENV_PATH": str(shared_env)},
                clear=False,
            ), patch.object(server, "_launchctl_getenv", return_value=""):
                for key in (
                    *server.AUTO_TRADE_CONTROL_ENV_KEYS,
                    *server.AUTO_TRADE_SHARED_ENV_KEYS,
                ):
                    os.environ.pop(key, None)
                server._hydrate_auto_trade_runtime_env()
                self.assertEqual(os.environ.get("PM_AUTO_TRADE_ENABLED"), "1")
                self.assertEqual(os.environ.get("PM_AUTO_TRADE_DRY_RUN"), "0")
                self.assertEqual(os.environ.get("POLY_API_KEY"), "test-key")
                self.assertEqual(os.environ.get("PRIVATE_KEY"), "0xabc123")
                self.assertEqual(os.environ.get("PM_FUNDER_ADDRESS"), "0xfeedface")

    def test_shell_env_loader_maps_bare_private_key_literal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            bare_key = "0x" + "1" * 64
            env_file.write_text(
                "\n".join(
                    [
                        "PM_AUTO_TRADE_ENABLED=0",
                        bare_key,
                        "POLY_API_KEY=test-key",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            proc = subprocess.run(
                [
                    "bash",
                    "-lc",
                    (
                        "source scripts/lib/load_launchctl_env.sh; "
                        "load_repo_env_file \"$ENV_FILE\"; "
                        "printf 'PRIVATE_KEY_SET=%s\\nINVALID_OK=%s\\n' "
                        "\"${PRIVATE_KEY:+1}\" \"${POLY_API_KEY}\""
                    ),
                ],
                cwd=ROOT,
                env={"ENV_FILE": str(env_file), "PATH": os.environ.get("PATH", "")},
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertIn("PRIVATE_KEY_SET=1", proc.stdout)
            self.assertIn("INVALID_OK=test-key", proc.stdout)
            self.assertNotIn("ignored", proc.stderr)

    def test_python_env_loaders_map_bare_private_key_literal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            bare_key = "0x" + "2" * 64
            env_file.write_text(
                "\n".join(
                    [
                        "PM_AUTO_TRADE_ENABLED=0",
                        bare_key,
                        "POLY_API_KEY=test-key",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            server_values = server._read_selected_env_file_vars(env_file, {"PRIVATE_KEY", "POLY_API_KEY"})
            scan_values = swm._read_runtime_env_file_vars(env_file, {"PRIVATE_KEY", "POLY_API_KEY"})
            self.assertEqual(server_values.get("PRIVATE_KEY"), bare_key)
            self.assertEqual(scan_values.get("PRIVATE_KEY"), bare_key)
            self.assertEqual(server_values.get("POLY_API_KEY"), "test-key")
            self.assertEqual(scan_values.get("POLY_API_KEY"), "test-key")

    def test_scan_node_env_hydrates_repo_bare_private_key_literal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            repo_env = tmp_path / ".env"
            shared_env = tmp_path / "shared.env"
            bare_key = "0x" + "3" * 64
            repo_env.write_text(
                "\n".join(
                    [
                        "POLY_API_KEY=test-key",
                        "POLY_API_SECRET=test-secret",
                        "POLY_API_PASSPHRASE=test-passphrase",
                        bare_key,
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            shared_env.write_text("", encoding="utf-8")
            with patch.object(swm, "_RUNTIME_ENV_PATH", repo_env), patch.object(
                swm,
                "_RUNTIME_ENV_HYDRATED",
                False,
            ), patch.object(swm, "_runtime_launchctl_getenv", return_value=""), patch.dict(
                os.environ,
                {
                    "PM_SHARED_POLY_ENV_PATH": str(shared_env),
                    "PM_CLOB_FORCE_DIRECT": "1",
                    "HOME": str(tmp_path),
                },
                clear=True,
            ):
                env = swm._build_node_env()
                self.assertEqual(env.get("POLY_API_KEY"), "test-key")
                self.assertEqual(env.get("POLY_API_SECRET"), "test-secret")
                self.assertEqual(env.get("POLY_API_PASSPHRASE"), "test-passphrase")
                self.assertEqual(env.get("PRIVATE_KEY"), bare_key)

    def test_hydrate_auto_trade_runtime_env_prefers_controlled_shared_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            repo_env = tmp_path / ".env"
            shared_env = tmp_path / "shared.env"
            repo_env.write_text(
                "\n".join(
                    [
                        "PM_AUTO_TRADE_ENABLED=1",
                        "POLY_API_KEY=repo-key",
                        "PRIVATE_KEY=0xrepo",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            shared_env.write_text(
                "\n".join(
                    [
                        "POLY_API_KEY=shared-key",
                        "POLY_API_SECRET=shared-secret",
                        "POLY_API_PASSPHRASE=shared-passphrase",
                        "PRIVATE_KEY=0xshared",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with patch.object(server, "ENV_PATH", repo_env), patch.dict(
                os.environ,
                {
                    "PM_SHARED_POLY_ENV_PATH": str(shared_env),
                    "POLY_API_KEY": "process-key",
                    "PRIVATE_KEY": "0xprocess",
                },
                clear=False,
            ), patch.object(server, "_launchctl_getenv", return_value=""):
                server._hydrate_auto_trade_runtime_env()
                self.assertEqual(os.environ.get("PM_AUTO_TRADE_ENABLED"), "1")
                self.assertEqual(os.environ.get("POLY_API_KEY"), "shared-key")
                self.assertEqual(os.environ.get("PRIVATE_KEY"), "0xshared")

    def test_hydrate_auto_trade_runtime_env_ignores_empty_shared_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            repo_env = tmp_path / ".env"
            shared_env = tmp_path / "shared.env"
            repo_env.write_text(
                "\n".join(
                    [
                        "POLY_API_KEY=repo-key",
                        "POLY_API_SECRET=repo-secret",
                        "POLY_API_PASSPHRASE=repo-passphrase",
                        "POLY_PRIVATE_KEY=0xrepo",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            shared_env.write_text(
                "\n".join(
                    [
                        "POLY_API_KEY=",
                        "POLY_API_SECRET=",
                        "POLY_API_PASSPHRASE=",
                        "PRIVATE_KEY=",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with patch.object(server, "ENV_PATH", repo_env), patch.dict(
                os.environ,
                {"PM_SHARED_POLY_ENV_PATH": str(shared_env)},
                clear=False,
            ), patch.object(server, "_launchctl_getenv", return_value=""):
                for key in (
                    *server.AUTO_TRADE_CONTROL_ENV_KEYS,
                    *server.AUTO_TRADE_SHARED_ENV_KEYS,
                ):
                    os.environ.pop(key, None)
                os.environ["PM_SHARED_POLY_ENV_PATH"] = str(shared_env)
                server._hydrate_auto_trade_runtime_env()
                self.assertEqual(os.environ.get("POLY_API_KEY"), "repo-key")
                self.assertEqual(os.environ.get("POLY_API_SECRET"), "repo-secret")
                self.assertEqual(os.environ.get("POLY_API_PASSPHRASE"), "repo-passphrase")
                self.assertEqual(os.environ.get("PRIVATE_KEY"), "0xrepo")

    def test_hydrate_auto_trade_runtime_env_does_not_use_launchctl_credentials_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_env = Path(tmpdir) / ".env"
            shared_env = Path(tmpdir) / "shared.env"
            repo_env.write_text("PM_AUTO_TRADE_ENABLED=1\n", encoding="utf-8")
            shared_env.write_text("", encoding="utf-8")
            with patch.object(server, "ENV_PATH", repo_env), patch.dict(
                os.environ,
                {"PM_SHARED_POLY_ENV_PATH": str(shared_env)},
                clear=False,
            ), patch.object(server, "_launchctl_getenv", return_value="launchctl-secret"):
                for key in (
                    *server.AUTO_TRADE_CONTROL_ENV_KEYS,
                    *server.AUTO_TRADE_SHARED_ENV_KEYS,
                    "PM_ALLOW_LAUNCHCTL_POLY_CREDENTIALS",
                ):
                    os.environ.pop(key, None)
                os.environ["PM_SHARED_POLY_ENV_PATH"] = str(shared_env)
                server._hydrate_auto_trade_runtime_env()
                self.assertEqual(os.environ.get("PM_AUTO_TRADE_ENABLED"), "1")
                self.assertIsNone(os.environ.get("POLY_API_KEY"))
                self.assertIsNone(os.environ.get("PRIVATE_KEY"))

    def test_credential_source_security_status_requires_user_only_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            shared_env = Path(tmpdir) / "shared.env"
            shared_env.write_text("POLY_API_KEY=x\n", encoding="utf-8")
            os.chmod(shared_env, 0o644)
            insecure = server._env_file_security_status(shared_env)
            self.assertFalse(insecure["secure"])
            self.assertIn("group_or_other_readable", insecure["issues"])

            os.chmod(shared_env, 0o600)
            secure = server._env_file_security_status(shared_env)
            self.assertTrue(secure["secure"])
            self.assertEqual(secure["mode"], "0o600")

    def test_apply_auto_trade_state_persists_dry_run_and_restarts_runtime(self) -> None:
        original_enabled = swm.AUTO_TRADE_ENABLED
        original_dry_run = swm.AUTO_TRADE_DRY_RUN
        original_sync_trades = swm.AUTO_TRADE_SYNC_TRADES
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_env = Path(tmpdir) / ".env"
            try:
                with patch.object(server, "ENV_PATH", repo_env), patch.object(
                    server,
                    "_restart_auto_trade_runtime_services",
                    return_value={"ok": True, "results": []},
                ) as restart_mock, patch.object(
                    server, "_launchctl_getenv", return_value=""
                ), patch.dict(os.environ, {}, clear=False):
                    payload = server.apply_auto_trade_state(True, dry_run=False)
                    self.assertTrue(payload["enabled"])
                    self.assertFalse(payload["dry_run"])
                    text = repo_env.read_text(encoding="utf-8")
                    self.assertIn("PM_AUTO_TRADE_ENABLED=1", text)
                    self.assertIn("PM_AUTO_TRADE_DRY_RUN=0", text)
                    restart_mock.assert_called_once()
            finally:
                swm.AUTO_TRADE_ENABLED = original_enabled
                swm.AUTO_TRADE_DRY_RUN = original_dry_run
                swm.AUTO_TRADE_SYNC_TRADES = original_sync_trades


if __name__ == "__main__":
    unittest.main()

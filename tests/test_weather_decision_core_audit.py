import json
import os
import sqlite3
import tempfile
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts import weather_decision_core_audit as audit


class WeatherDecisionCoreAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self._audit_env_before = {key: os.environ.get(key) for key in audit.AUDIT_ENV_KEYS}

    def tearDown(self) -> None:
        for key, value in self._audit_env_before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _create_empty_decision_db(self, path: Path) -> None:
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        conn.execute(
            """
            CREATE TABLE auto_trade_decisions (
                id INTEGER PRIMARY KEY,
                created_at TEXT,
                action TEXT,
                decided_state TEXT,
                will_order INTEGER,
                reason_codes_json TEXT,
                details_json TEXT
            )
            """
        )
        conn.commit()

    def _audit_args(self, db_path: Path):
        return type(
            "Args",
            (),
            {
                "since": "2026-05-04T00:00:00+00:00",
                "since_hours": 1.0,
                "limit": 100,
                "scan_terms": False,
                "db": str(db_path),
                "entry_only_realizable": None,
                "dry_run": None,
                "exit_policy_only": False,
            },
        )()

    def test_run_audit_hydrates_runtime_env_from_repo_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            root = Path(tmp)
            db_path = root / "archive.db"
            self._create_empty_decision_db(db_path)
            (root / ".env").write_text(
                "PM_AUTO_TRADE_ENTRY_ONLY_REALIZABLE=1\n"
                "PM_AUTO_TRADE_DRY_RUN=1\n"
                "PM_AUTO_TRADE_DRY_RUN=0\n"
            )
            with patch.object(audit, "ROOT", root), patch.object(
                audit.subprocess,
                "run",
                return_value=SimpleNamespace(stdout="", returncode=0),
            ):
                payload = audit.run_audit(self._audit_args(db_path))

        self.assertTrue(payload["config"]["entry_only_realizable"])
        self.assertFalse(payload["config"]["dry_run"])

    def test_run_audit_defaults_to_entry_only_dry_run_when_env_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            root = Path(tmp)
            db_path = root / "archive.db"
            self._create_empty_decision_db(db_path)
            with patch.object(audit, "ROOT", root), patch.object(
                audit.subprocess,
                "run",
                return_value=SimpleNamespace(stdout="", returncode=0),
            ):
                payload = audit.run_audit(self._audit_args(db_path))

        self.assertTrue(payload["config"]["entry_only_realizable"])
        self.assertTrue(payload["config"]["dry_run"])

    def test_run_audit_reports_db_read_error_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "archive.db"
            db_path.write_text("not-used", encoding="utf-8")
            with patch.object(
                audit.sqlite3,
                "connect",
                side_effect=audit.sqlite3.OperationalError("disk I/O error"),
            ):
                payload = audit.run_audit(self._audit_args(db_path))

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["decision_audit"]["reason"], "archive_db_read_error")
        self.assertIn("OperationalError:disk I/O error", payload["violations"]["archive_db_read_error"])
        self.assertEqual(
            payload["latest_exit_policy_coverage"]["reason"],
            "archive_db_read_error",
        )

    def test_term_scan_flags_forbidden_terms_outside_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir()
            (root / "docs" / "WEATHER_DECISION_CORE.md").write_text("trust_weight is mentioned as forbidden\n")
            (root / "bad.py").write_text("x = 'trust_weight'\n")

            hits = audit.scan_for_external_terms(root)

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["code"], "trust_weight")
        self.assertEqual(hits[0]["path"], "bad.py")

    def test_decision_audit_detects_legacy_fit_and_soft_hard_block(self) -> None:
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute(
            """
            CREATE TABLE auto_trade_decisions (
                id INTEGER PRIMARY KEY,
                created_at TEXT,
                action TEXT,
                decided_state TEXT,
                will_order INTEGER,
                reason_codes_json TEXT,
                details_json TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO auto_trade_decisions (
                id, created_at, action, decided_state, will_order,
                reason_codes_json, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    1,
                    "2026-05-05T00:00:00+00:00",
                    "entry",
                    "blocked",
                    0,
                    json.dumps(["spread_too_wide"]),
                    json.dumps(
                        {
                            "weather_strategy_source_trust_tier": "settlement_source",
                            "entry_usable_edge": 0.01,
                        }
                    ),
                ),
                (
                    2,
                    "2026-05-05T00:01:00+00:00",
                    "entry",
                    "entry",
                    1,
                    json.dumps(["order_submitted"]),
                    json.dumps(
                        {
                            "dry_run": True,
                            "source_fit_tier": "settlement_source",
                            "weather_strategy_source_fit_tier": "settlement_source",
                            "entry_usable_edge": 0.02,
                            "entry_usable_edge_size_scale": 0.4,
                        }
                    ),
                ),
                (
                    3,
                    "2026-05-05T00:02:00+00:00",
                    "entry",
                    "blocked",
                    0,
                    json.dumps(["yes_top_ask_above_max"]),
                    json.dumps(
                        {
                            "weather_strategy_source_fit_tier": "station_aligned",
                            "entry_usable_edge": 0.01,
                        }
                    ),
                ),
                (
                    4,
                    "2026-05-05T00:03:00+00:00",
                    "entry",
                    "blocked",
                    0,
                    json.dumps(["plain_lcb_pinned_tail_no_filtered"]),
                    json.dumps({"strategy": "lcb_global"}),
                ),
                (
                    5,
                    "2026-05-05T00:04:00+00:00",
                    "entry",
                    "blocked",
                    0,
                    json.dumps(["edge_realizable_missing"]),
                    json.dumps(
                        {
                            "source_fit_tier": "settlement_source",
                            "entry_usable_edge": 0.0,
                        }
                    ),
                ),
                (
                    6,
                    "2026-05-05T00:05:00+00:00",
                    "entry",
                    "blocked",
                    0,
                    json.dumps([]),
                    json.dumps(
                        {
                            "strategy": "lcb_global",
                            "q_entry": 0.42,
                        }
                    ),
                ),
                (
                    7,
                    "2026-05-05T00:06:00+00:00",
                    "entry",
                    "entry_dry_run",
                    1,
                    json.dumps(["order_submitted"]),
                    json.dumps(
                        {
                            "strategy": "lcb_global",
                            "q_entry": 0.72,
                            "dry_run": True,
                        }
                    ),
                ),
            ],
        )
        conn.commit()

        result = audit.audit_recent_decisions(
            conn,
            since_iso="2026-05-04T00:00:00+00:00",
            entry_only_realizable=True,
            dry_run=True,
        )

        self.assertEqual(result["rows_scanned"], 7)
        self.assertEqual(result["legacy_only_fit_rows"], 1)
        self.assertEqual(result["missing_top_fit_rows"], 1)
        self.assertEqual(result["missing_fit_na_reason_rows"], 1)
        self.assertEqual(result["missing_entry_usable_edge_rows"], 1)
        self.assertEqual(result["legacy_q_entry_observation_rows"], 1)
        self.assertEqual(result["dry_run_entry_unusable_edge_order_rows"], 1)
        self.assertEqual(result["source_station_mismatch_rows"], 0)
        self.assertEqual(
            result["soft_hard_block_counts"],
            {"spread_too_wide": 1, "yes_top_ask_above_max": 1},
        )
        self.assertEqual(result["legacy_edge_block_counts"], {"edge_realizable_missing": 1})
        self.assertEqual(len(result["dry_run_entry_state_violations"]), 1)
        self.assertEqual(result["entry_unusable_edge_order_violations"], [])

    def test_decision_audit_rejects_orders_with_unusable_entry_edge(self) -> None:
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute(
            """
            CREATE TABLE auto_trade_decisions (
                id INTEGER PRIMARY KEY,
                created_at TEXT,
                action TEXT,
                decided_state TEXT,
                will_order INTEGER,
                reason_codes_json TEXT,
                details_json TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO auto_trade_decisions (
                id, created_at, action, decided_state, will_order,
                reason_codes_json, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "2026-05-05T00:00:00+00:00",
                "entry",
                "entry_dry_run",
                1,
                json.dumps(["order_submitted"]),
                json.dumps(
                    {
                        "source_fit_tier": "settlement_source",
                        "entry_usable_edge": -0.001,
                        "entry_usable_edge_size_scale": 0.0,
                    }
                ),
            ),
        )
        conn.commit()

        result = audit.audit_recent_decisions(
            conn,
            since_iso="2026-05-04T00:00:00+00:00",
            entry_only_realizable=True,
            dry_run=True,
        )

        violations = result["entry_unusable_edge_order_violations"]
        self.assertEqual(len(violations), 1)
        self.assertAlmostEqual(violations[0]["entry_usable_edge"], -0.001)
        self.assertAlmostEqual(violations[0]["entry_usable_edge_size_scale"], 0.0)

    def test_decision_audit_allows_bayes_target_entry_without_edge_gate(self) -> None:
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute(
            """
            CREATE TABLE auto_trade_decisions (
                id INTEGER PRIMARY KEY,
                created_at TEXT,
                action TEXT,
                decided_state TEXT,
                will_order INTEGER,
                reason_codes_json TEXT,
                details_json TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO auto_trade_decisions (
                id, created_at, action, decided_state, will_order,
                reason_codes_json, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "2026-05-05T00:00:00+00:00",
                "entry",
                "entry",
                1,
                json.dumps([]),
                json.dumps(
                    {
                        "strategy": "target_realization_binary_settlement",
                        "decision_ssot": "posterior_temperature_distribution",
                        "posterior_target_direct": True,
                        "entry_alpha_gate_source": "posterior_settlement",
                        "entry_usable_edge": 0.0,
                        "entry_usable_edge_size_scale": 0.0,
                    }
                ),
            ),
        )
        conn.commit()

        result = audit.audit_recent_decisions(
            conn,
            since_iso="2026-05-04T00:00:00+00:00",
            entry_only_realizable=True,
            dry_run=False,
        )

        self.assertEqual(result["bayes_target_entry_rows"], 1)
        self.assertEqual(result["missing_fit_na_reason_rows"], 0)
        self.assertEqual(result["missing_entry_usable_edge_rows"], 0)
        self.assertEqual(result["entry_unusable_edge_order_violations"], [])

    def test_decision_audit_flags_resolution_station_mismatch(self) -> None:
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute(
            """
            CREATE TABLE auto_trade_decisions (
                id INTEGER PRIMARY KEY,
                created_at TEXT,
                action TEXT,
                decided_state TEXT,
                will_order INTEGER,
                reason_codes_json TEXT,
                details_json TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO auto_trade_decisions (
                id, created_at, action, decided_state, will_order,
                reason_codes_json, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "2026-05-05T00:00:00+00:00",
                "entry",
                "blocked",
                0,
                json.dumps([]),
                json.dumps(
                    {
                        "source_fit_tier": "station_mismatch",
                        "source_station_id": "ZSPD",
                        "source_station_expected": "ZBAA",
                        "source_station_match": False,
                        "source_na_reason": "resolution_station_mismatch",
                        "entry_usable_edge_na_reason": "resolution_station_mismatch",
                    }
                ),
            ),
        )
        conn.commit()

        result = audit.audit_recent_decisions(
            conn,
            since_iso="2026-05-04T00:00:00+00:00",
            entry_only_realizable=True,
            dry_run=True,
        )

        self.assertEqual(result["source_station_mismatch_rows"], 1)

    def test_decision_audit_requires_exit_policy_hold_edge_source(self) -> None:
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute(
            """
            CREATE TABLE auto_trade_decisions (
                id INTEGER PRIMARY KEY,
                created_at TEXT,
                action TEXT,
                decided_state TEXT,
                will_order INTEGER,
                reason_codes_json TEXT,
                details_json TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO auto_trade_decisions (
                id, created_at, action, decided_state, will_order,
                reason_codes_json, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    1,
                    "2026-05-05T00:00:00+00:00",
                    "exit",
                    "blocked",
                    0,
                    json.dumps(["exit_no_trigger"]),
                    json.dumps({"exec_edge_exit": -0.01}),
                ),
                (
                    2,
                    "2026-05-05T00:01:00+00:00",
                    "exit",
                    "blocked",
                    0,
                    json.dumps(["exit_no_trigger"]),
                    json.dumps(
                        {
                            "exit_policy_version": "exit_policy_v1_2026_05_05",
                            "exit_policy_primary_edge_source": "hold_edge_realizable",
                            "exit_edge_primary_source": "hold_edge_realizable",
                            "hold_edge_realizable": 0.04,
                        }
                    ),
                ),
            ],
        )
        conn.commit()

        result = audit.audit_recent_decisions(
            conn,
            since_iso="2026-05-04T00:00:00+00:00",
            entry_only_realizable=True,
            dry_run=True,
        )

        self.assertEqual(result["exit_policy_missing_rows"], 1)
        self.assertEqual(len(result["legacy_exit_policy_semantic_violations"]), 1)
        self.assertEqual(
            result["legacy_exit_policy_semantic_violations"][0]["exit_policy_primary_edge_source"],
            "hold_edge_realizable",
        )

    def test_decision_audit_infers_hold_edge_source_for_legacy_compacted_rows(self) -> None:
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute(
            """
            CREATE TABLE auto_trade_decisions (
                id INTEGER PRIMARY KEY,
                created_at TEXT,
                action TEXT,
                decided_state TEXT,
                will_order INTEGER,
                reason_codes_json TEXT,
                details_json TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO auto_trade_decisions (
                id, created_at, action, decided_state, will_order,
                reason_codes_json, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    1,
                    "2026-05-05T00:00:00+00:00",
                    "exit",
                    "exit",
                    1,
                    json.dumps(["order_submitted"]),
                    json.dumps(
                        {
                            "hold_edge_realizable": 0.044,
                            "q_bayes_source": "dirichlet_bma",
                            "source_fit_tier": "settlement_source",
                        }
                    ),
                ),
                (
                    2,
                    "2026-05-05T00:01:00+00:00",
                    "exit",
                    "exit",
                    1,
                    json.dumps(["order_submitted"]),
                    json.dumps(
                        {
                            "exit_policy_version": "exit_policy_v1_2026_05_05",
                            "exec_edge_exit": -0.01,
                            "q_bayes_source": "dirichlet_bma",
                            "source_fit_tier": "settlement_source",
                        }
                    ),
                ),
                (
                    3,
                    "2026-05-05T00:02:00+00:00",
                    "exit",
                    "blocked",
                    0,
                    json.dumps(["exit_no_trigger"]),
                    json.dumps({"exit_policy_version": "exit_policy_v1_2026_05_05"}),
                ),
            ],
        )
        conn.commit()

        result = audit.audit_recent_decisions(
            conn,
            since_iso="2026-05-04T00:00:00+00:00",
            entry_only_realizable=True,
            dry_run=True,
        )

        self.assertEqual(len(result["legacy_exit_policy_semantic_violations"]), 0)
        self.assertEqual(result["exit_policy_missing_rows"], 0)

    def test_decision_audit_tracks_unverifiable_legacy_exit_rows_without_blocking(self) -> None:
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute(
            """
            CREATE TABLE auto_trade_decisions (
                id INTEGER PRIMARY KEY,
                created_at TEXT,
                action TEXT,
                decided_state TEXT,
                will_order INTEGER,
                reason_codes_json TEXT,
                details_json TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO auto_trade_decisions (
                id, created_at, action, decided_state, will_order,
                reason_codes_json, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    1,
                    "2026-05-05T00:00:00+00:00",
                    "exit",
                    "exit",
                    1,
                    json.dumps(["order_submitted"]),
                    json.dumps(
                        {
                            "strategy": "exit",
                            "q_bayes_source": "dirichlet_bma",
                            "weather_likelihood_features_source": "scan_model_members",
                            "exit_policy_version": None,
                            "exit_policy_primary_edge_source": None,
                        }
                    ),
                ),
                (
                    2,
                    "2026-05-05T00:01:00+00:00",
                    "exit",
                    "blocked",
                    0,
                    json.dumps(["exit_no_trigger"]),
                    json.dumps({"strategy": "exit"}),
                ),
            ],
        )
        conn.commit()

        result = audit.audit_recent_decisions(
            conn,
            since_iso="2026-05-04T00:00:00+00:00",
            entry_only_realizable=True,
            dry_run=True,
        )

        self.assertEqual(result["legacy_compacted_unverifiable_exit_rows"], 1)
        self.assertEqual(result["exit_policy_missing_rows"], 1)
        self.assertEqual(result["legacy_exit_policy_semantic_violations"], [])

    def test_decision_audit_allows_legacy_compacted_operational_only_exit_rows(self) -> None:
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute(
            """
            CREATE TABLE auto_trade_decisions (
                id INTEGER PRIMARY KEY,
                created_at TEXT,
                action TEXT,
                decided_state TEXT,
                will_order INTEGER,
                reason_codes_json TEXT,
                details_json TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO auto_trade_decisions (
                id, created_at, action, decided_state, will_order,
                reason_codes_json, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    1,
                    "2026-05-05T00:00:00+00:00",
                    "exit",
                    "exit",
                    1,
                    json.dumps(["order_submitted"]),
                    json.dumps(
                        {
                            "strategy": "exit",
                            "q_bayes_source": "dirichlet_bma",
                            "weather_likelihood_features_source": "scan_model_members",
                            "exit_policy_version": None,
                            "exit_policy_primary_edge_source": None,
                            "exit_policy_edge_min": None,
                            "exit_policy_edge_value": None,
                            "hold_edge_realizable": None,
                        }
                    ),
                ),
                (
                    2,
                    "2026-05-05T00:01:00+00:00",
                    "exit",
                    "blocked",
                    0,
                    json.dumps(["exit_wait_merge_min_size"]),
                    json.dumps(
                        {
                            "strategy": "",
                            "q_bayes_source": "dirichlet_bma",
                            "weather_likelihood_features_source": "scan_model_members",
                            "exit_policy_version": None,
                            "exit_policy_primary_edge_source": None,
                        }
                    ),
                ),
                (
                    3,
                    "2026-05-05T00:02:00+00:00",
                    "exit",
                    "blocked",
                    0,
                    json.dumps(["exit_no_trigger"]),
                    json.dumps({"strategy": "exit"}),
                ),
            ],
        )
        conn.commit()

        result = audit.audit_recent_decisions(
            conn,
            since_iso="2026-05-04T00:00:00+00:00",
            entry_only_realizable=True,
            dry_run=True,
        )

        self.assertEqual(result["legacy_compacted_unverifiable_exit_rows"], 2)
        self.assertEqual(result["exit_policy_missing_rows"], 1)
        self.assertEqual(result["legacy_exit_policy_semantic_violations"], [])

    def test_exit_policy_snapshot_reports_family_and_hold_edge_coverage(self) -> None:
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute(
            """
            CREATE TABLE auto_trade_decisions (
                id INTEGER PRIMARY KEY,
                created_at TEXT,
                action TEXT,
                decided_state TEXT,
                will_order INTEGER,
                reason_codes_json TEXT,
                details_json TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO auto_trade_decisions (
                id, created_at, action, decided_state, will_order,
                reason_codes_json, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    1,
                    "2026-05-05T00:00:00+00:00",
                    "exit",
                    "blocked",
                    0,
                    json.dumps(["target_realization_switch_buy_exit_old"]),
                    json.dumps(
                        {
                            "exit_policy_version": "exit_policy_v2_2026_06_24",
                            "exit_policy_primary_source": "target_realization_binary_settlement",
                            "exit_policy_primary_edge_source": "",
                            "hold_edge_realizable": -0.02,
                            "exit_policy_legacy_edge_min": 0.0,
                            "exit_policy_family_codes": ["target_realization_exit"],
                            "exit_policy_primary_family": "target_realization_exit",
                            "exit_policy_trigger_reason_codes": ["target_realization_switch_buy_exit_old"],
                            "exit_policy_target_realization_triggered": True,
                        }
                    ),
                ),
                (
                    2,
                    "2026-05-05T00:01:00+00:00",
                    "exit",
                    "blocked",
                    0,
                    json.dumps(["target_realization_rebalance_over_target"]),
                    json.dumps(
                        {
                            "exit_policy_version": "exit_policy_v2_2026_06_24",
                            "exit_policy_primary_source": "target_realization_binary_settlement",
                            "exit_policy_primary_edge_source": "",
                            "hold_edge_realizable": 0.03,
                            "exit_policy_legacy_edge_min": 0.0,
                            "exit_policy_family_codes": ["target_realization_exit"],
                            "exit_policy_primary_family": "target_realization_exit",
                            "exit_policy_target_realization_triggered": True,
                            "exit_policy_order_submitted": True,
                        }
                    ),
                ),
                (
                    3,
                    "2026-05-05T00:02:00+00:00",
                    "exit",
                    "blocked",
                    0,
                    json.dumps(["exit_no_trigger"]),
                    json.dumps(
                        {
                            "exit_policy_version": "exit_policy_v2_2026_06_24",
                            "exit_policy_primary_source": "target_realization_binary_settlement",
                            "exit_policy_primary_edge_source": "",
                            "hold_edge_realizable": -0.10,
                            "exit_policy_legacy_edge_min": 0.0,
                            "exit_policy_no_trigger": True,
                        }
                    ),
                ),
            ],
        )
        conn.commit()

        decision = audit.audit_recent_decisions(
            conn,
            since_iso="2026-05-04T00:00:00+00:00",
            entry_only_realizable=True,
            dry_run=True,
        )
        payload = audit.build_exit_policy_snapshot(
            {
                "ok": True,
                "generated_at": "2026-05-05T00:03:00+00:00",
                "decision_audit": decision,
            }
        )

        self.assertEqual(payload["exit_rows"], 3)
        self.assertAlmostEqual(payload["exit_policy_version_present_rate"], 1.0)
        self.assertAlmostEqual(payload["target_realization_primary_source_rate"], 1.0)
        self.assertAlmostEqual(payload["hold_edge_primary_source_rate"], 0.0)
        self.assertEqual(payload["family_counts"], {"target_realization_exit": 2})
        self.assertEqual(payload["target_realization_triggered_count"], 2)
        self.assertEqual(payload["edge_triggered_count"], 0)
        self.assertEqual(payload["repricing_triggered_count"], 0)
        self.assertEqual(payload["risk_triggered_count"], 0)
        self.assertEqual(payload["order_submitted_count"], 1)
        self.assertEqual(payload["marketable_submitted_count"], 0)
        self.assertAlmostEqual(payload["avg_hold_edge_realizable"], (-0.02 + 0.03 - 0.10) / 3)

    def test_exit_policy_only_ignores_entry_violations_for_ok(self) -> None:
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute(
            """
            CREATE TABLE auto_trade_decisions (
                id INTEGER PRIMARY KEY,
                created_at TEXT,
                action TEXT,
                decided_state TEXT,
                will_order INTEGER,
                reason_codes_json TEXT,
                details_json TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO auto_trade_decisions (
                id, created_at, action, decided_state, will_order,
                reason_codes_json, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    1,
                    "2026-05-05T00:00:00+00:00",
                    "entry",
                    "entry_dry_run",
                    1,
                    json.dumps(["order_submitted"]),
                    json.dumps(
                        {
                            "source_fit_tier": "settlement_source",
                            "entry_usable_edge": -0.001,
                            "entry_usable_edge_size_scale": 0.0,
                        }
                    ),
                ),
                (
                    2,
                    "2026-05-05T00:01:00+00:00",
                    "exit",
                    "blocked",
                    0,
                    json.dumps(["exit_no_trigger"]),
                    json.dumps(
                        {
                            "exit_policy_version": "exit_policy_v2_2026_06_24",
                            "exit_policy_primary_source": "target_realization_binary_settlement",
                            "exit_policy_primary_edge_source": "",
                            "hold_edge_realizable": 0.03,
                            "exit_policy_no_trigger": True,
                        }
                    ),
                ),
            ],
        )
        conn.commit()

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "archive.db"
            disk = sqlite3.connect(db_path)
            conn.backup(disk)
            disk.close()
            args = type(
                "Args",
                (),
                {
                    "since": "2026-05-04T00:00:00+00:00",
                    "since_hours": 1.0,
                    "limit": 100,
                    "scan_terms": False,
                    "db": str(db_path),
                    "entry_only_realizable": True,
                    "dry_run": True,
                    "exit_policy_only": True,
                },
            )()
            payload = audit.run_audit(args)

        self.assertTrue(payload["ok"])
        self.assertEqual(
            len(payload["violations"]["entry_unusable_edge_order_violations"]),
            1,
        )
        self.assertEqual(payload["violations"]["exit_policy_missing_rows"], 0)
        self.assertEqual(payload["violations"]["legacy_exit_policy_semantic_violations"], [])

    def test_latest_exit_policy_coverage_is_diagnostic_only(self) -> None:
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute(
            """
            CREATE TABLE auto_trade_decisions (
                id INTEGER PRIMARY KEY,
                created_at TEXT,
                action TEXT,
                decided_state TEXT,
                will_order INTEGER,
                reason_codes_json TEXT,
                details_json TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO auto_trade_decisions (
                id, created_at, action, decided_state, will_order,
                reason_codes_json, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    1,
                    "2026-05-01T00:00:00+00:00",
                    "exit",
                    "exit_synced_fill",
                    1,
                    json.dumps(["synced_exit_fill_without_decision"]),
                    json.dumps({"source": "trade_sync"}),
                ),
                (
                    2,
                    "2026-05-05T00:00:00+00:00",
                    "entry",
                    "entry_dry_run",
                    1,
                    json.dumps(["order_submitted"]),
                    json.dumps(
                        {
                            "source_fit_tier": "settlement_source",
                            "entry_usable_edge": 0.02,
                            "entry_usable_edge_size_scale": 0.5,
                        }
                    ),
                ),
            ],
        )
        conn.commit()

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "archive.db"
            disk = sqlite3.connect(db_path)
            conn.backup(disk)
            disk.close()
            args = type(
                "Args",
                (),
                {
                    "since": "2026-05-04T00:00:00+00:00",
                    "since_hours": 1.0,
                    "limit": 100,
                    "scan_terms": False,
                    "db": str(db_path),
                    "entry_only_realizable": True,
                    "dry_run": True,
                    "exit_policy_only": False,
                },
            )()
            payload = audit.run_audit(args)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["violations"]["exit_policy_missing_rows"], 0)
        latest = payload["latest_exit_policy_coverage"]
        self.assertEqual(latest["rows_checked"], 1)
        self.assertEqual(latest["policy_missing_count"], 1)
        self.assertEqual(latest["current_since"], "2026-05-04T00:00:00+00:00")
        self.assertEqual(latest["current_rows_checked"], 0)
        self.assertEqual(latest["current_policy_missing_count"], 0)
        self.assertIsNone(latest["current_policy_version_present_rate"])
        self.assertEqual(
            latest["examples"]["policy_missing_rows"][0]["reason_codes"],
            ["synced_exit_fill_without_decision"],
        )

    def test_latest_exit_policy_coverage_tracks_current_window_separately(self) -> None:
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute(
            """
            CREATE TABLE auto_trade_decisions (
                id INTEGER PRIMARY KEY,
                created_at TEXT,
                action TEXT,
                decided_state TEXT,
                will_order INTEGER,
                reason_codes_json TEXT,
                details_json TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO auto_trade_decisions (
                id, created_at, action, decided_state, will_order,
                reason_codes_json, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    1,
                    "2026-05-01T00:00:00+00:00",
                    "exit",
                    "exit_synced_fill",
                    1,
                    json.dumps(["synced_exit_fill_without_decision"]),
                    json.dumps({"source": "trade_sync"}),
                ),
                (
                    2,
                    "2026-05-05T00:00:00+00:00",
                    "exit",
                    "blocked",
                    0,
                    json.dumps(["target_realization_switch_buy_exit_old"]),
                    json.dumps(
                        {
                            "exit_policy_version": "exit_policy_v2_2026_06_24",
                            "exit_policy_primary_source": "target_realization_binary_settlement",
                            "exit_policy_primary_edge_source": "",
                            "hold_edge_realizable": -0.02,
                            "exit_policy_family_codes": ["target_realization_exit"],
                        }
                    ),
                ),
            ],
        )
        conn.commit()

        latest = audit.audit_latest_exit_policy_coverage(
            conn,
            limit=100,
            since_iso="2026-05-04T00:00:00+00:00",
        )

        self.assertEqual(latest["rows_checked"], 2)
        self.assertEqual(latest["policy_missing_count"], 1)
        self.assertEqual(latest["current_rows_checked"], 1)
        self.assertEqual(latest["current_policy_missing_count"], 0)
        self.assertAlmostEqual(latest["current_policy_version_present_rate"], 1.0)
        self.assertAlmostEqual(latest["current_target_realization_primary_source_rate"], 1.0)
        self.assertAlmostEqual(latest["current_hold_edge_primary_source_rate"], 0.0)
        self.assertEqual(latest["current_family_required_rows"], 1)
        self.assertEqual(latest["current_family_missing_required_count"], 0)
        self.assertAlmostEqual(latest["current_family_required_present_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()

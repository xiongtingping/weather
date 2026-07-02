import json
import plistlib
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import rolling_validation


class RollingValidationTest(unittest.TestCase):
    def _create_db(self, path: Path) -> None:
        with sqlite3.connect(str(path)) as conn:
            conn.executescript(
                """
                CREATE TABLE auto_trade_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER,
                    run_date TEXT,
                    slug TEXT,
                    event_date TEXT,
                    title TEXT,
                    outcome TEXT,
                    side TEXT,
                    action TEXT,
                    decided_state TEXT,
                    will_order INTEGER,
                    reason_codes_json TEXT,
                    reasons_json TEXT,
                    details_json TEXT,
                    created_at TEXT
                );
                CREATE TABLE auto_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT UNIQUE,
                    slug TEXT,
                    event_date TEXT,
                    outcome TEXT,
                    side TEXT,
                    action TEXT,
                    price REAL,
                    size REAL,
                    order_type TEXT,
                    order_status TEXT,
                    order_checked_at TEXT,
                    created_at TEXT
                );
                CREATE TABLE edge_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slug TEXT,
                    event_date TEXT,
                    outcome TEXT,
                    side TEXT,
                    state TEXT,
                    position_size REAL,
                    exit_skip_reason TEXT,
                    exit_skip_detail TEXT,
                    exit_skip_at TEXT,
                    updated_at TEXT
                );
                CREATE TABLE edge_trade_fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slug TEXT,
                    event_date TEXT,
                    outcome TEXT,
                    outcome_side TEXT,
                    order_id TEXT,
                    trade_side TEXT,
                    created_at TEXT
                );
                """
            )

    def _insert_entry(self, path: Path, details: dict, *, reason_codes: list[str] | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with sqlite3.connect(str(path)) as conn:
            conn.execute(
                """
                INSERT INTO auto_trade_decisions(
                    run_id, run_date, slug, event_date, title, outcome, side,
                    action, decided_state, will_order, reason_codes_json,
                    reasons_json, details_json, created_at
                ) VALUES (?, '2026-05-05', 'weather-test', '2026-05-05',
                          'Highest temperature in Test?', '80°F or higher', 'YES',
                          'entry', 'blocked', 0, ?, '[]', ?, ?)
                """,
                (9, json.dumps(reason_codes or []), json.dumps(details), now),
            )

    def test_latest_run_snapshot_accepts_new_entry_core_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            self._create_db(db_path)
            self._insert_entry(
                db_path,
                {
                    "entry_strategy_family": "repricing_bucket",
                    "entry_usable_edge": 0.0,
                    "entry_usable_edge_base_source": "edge_tradeability_profit_value",
                },
            )
            with sqlite3.connect(str(db_path)) as conn:
                payload = rolling_validation._latest_run_snapshot(conn)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["run_id"], 9)
        self.assertEqual(payload["hard_reason_rows"], 0)
        self.assertEqual(payload["exec_edge_base_rows"], 0)
        self.assertEqual(payload["missing_entry_usable_edge_rows"], 0)

    def test_latest_run_snapshot_blocks_legacy_exec_edge_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            self._create_db(db_path)
            self._insert_entry(
                db_path,
                {
                    "entry_strategy_family": "repricing_bucket",
                    "entry_usable_edge": 0.0,
                    "entry_usable_edge_base_source": "exec_edge_entry",
                },
            )
            with sqlite3.connect(str(db_path)) as conn:
                payload = rolling_validation._latest_run_snapshot(conn)

        self.assertFalse(payload["ok"])
        self.assertIn("latest_run_exec_edge_base_rows", payload["blockers"])

    def test_launchd_snapshot_checks_system_monitor_daemon(self) -> None:
        def fake_run(args, **kwargs):
            self.assertEqual(args, ["launchctl", "print", "system/com.weather.monitor.daemon"])
            return SimpleNamespace(
                returncode=0,
                stdout="state = running\npid = 508\n",
                stderr="",
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            plist_path = Path(tmpdir) / "com.weather.monitor.daemon.plist"
            with plist_path.open("wb") as handle:
                plistlib.dump(
                    {
                        "Label": "com.weather.monitor.daemon",
                        "WorkingDirectory": str(rolling_validation.ROOT),
                        "ProgramArguments": [
                            "/bin/zsh",
                            "-lc",
                            f"exec {rolling_validation.ROOT}/scripts/polymarket_monitor_daemon.sh",
                        ],
                    },
                    handle,
                )
            with patch.object(rolling_validation.subprocess, "run", side_effect=fake_run), patch.object(
                rolling_validation,
                "MONITOR_DAEMON_PLIST",
                plist_path,
            ):
                payload = rolling_validation._launchd_snapshot()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["warnings"], [])
        self.assertEqual(payload["missing_labels"], [])
        self.assertEqual(
            payload["labels"]["system/com.weather.monitor.daemon"]["status"],
            "running",
        )

    def test_launchd_snapshot_reports_monitor_plist_drift(self) -> None:
        def fake_run(args, **kwargs):
            self.assertEqual(args, ["launchctl", "print", "system/com.weather.monitor.daemon"])
            return SimpleNamespace(returncode=113, stdout="", stderr="missing")

        with tempfile.TemporaryDirectory() as tmpdir:
            plist_path = Path(tmpdir) / "com.weather.monitor.daemon.plist"
            with plist_path.open("wb") as handle:
                plistlib.dump(
                    {
                        "Label": "com.weather.monitor.daemon",
                        "WorkingDirectory": "/Users/xiong/poly",
                        "ProgramArguments": ["/Users/xiong/poly/scripts/start_monitor_ui.sh"],
                    },
                    handle,
                )
            with patch.object(rolling_validation.subprocess, "run", side_effect=fake_run), patch.object(
                rolling_validation,
                "MONITOR_DAEMON_PLIST",
                plist_path,
            ):
                payload = rolling_validation._launchd_snapshot()

        self.assertFalse(payload["ok"])
        self.assertIn("system/com.weather.monitor.daemon", payload["missing_labels"])
        self.assertIn("monitor_plist_root_drift", payload["warnings"])
        self.assertIn("monitor_plist_program_drift", payload["warnings"])
        self.assertIn("install_monitor_launchdaemon.sh", payload["repair_command"])

    def test_build_rolling_validation_rolls_forward_after_clean_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            self._create_db(db_path)
            self._insert_entry(
                db_path,
                {
                    "entry_strategy_family": "repricing_bucket",
                    "entry_usable_edge": 0.0,
                    "entry_usable_edge_base_source": "edge_tradeability_profit_value",
                    "entry_edge_exit_path_required": True,
                    "entry_edge_repricing_exit_evidence_status": "bucket_evidence",
                    "entry_edge_repricing_exit_evidence_probability_source": (
                        "p_converge=bucket_evidence:primary;"
                        "p_fill_entry=bucket_evidence:primary;"
                        "p_fill_exit=bucket_evidence:primary"
                    ),
                    "edge_realizable_p_realize": 0.01,
                    "edge_realizable_p_converge": 0.04,
                    "edge_realizable_p_converge_evidence_status": "bucket_evidence",
                    "edge_realizable_p_converge_bucket_used_key": "conv-low",
                    "edge_realizable_p_converge_samples": 120,
                    "edge_realizable_p_fill_entry": 0.60,
                    "edge_realizable_p_fill_entry_evidence_status": "bucket_evidence",
                    "edge_realizable_p_fill_entry_bucket_used_key": "entry-ok",
                    "edge_realizable_p_fill_entry_samples": 80,
                    "edge_realizable_p_fill_exit": 0.50,
                    "edge_realizable_p_fill_exit_evidence_status": "bucket_evidence",
                    "edge_realizable_p_fill_exit_bucket_used_key": "exit-ok",
                    "edge_realizable_p_fill_exit_samples": 80,
                },
            )
            with patch.object(
                rolling_validation,
                "_launchd_snapshot",
                return_value={"ok": True, "missing_labels": []},
            ), patch.object(
                rolling_validation.entry_counterfactual_review,
                "build_report",
                return_value={
                    "ok": True,
                    "actionable_summary": {
                        "next_mainline_task": "rolling_validation",
                        "overall": {
                            "primary_diagnosis": "repricing_probability_low_ev_confirmed",
                            "next_mainline_task": "rolling_validation",
                        },
                    },
                    "current_runtime": {"run_id": 9},
                },
            ), patch.object(
                rolling_validation.bayesian_readiness_audit,
                "build_report",
                return_value={
                    "ok": True,
                    "city_filter": "chengdu",
                    "since_days": 1,
                    "readiness": {"event_run_bayes_status": "ok"},
                    "blockers": [],
                    "warnings": [],
                    "settlement_label_plan": {"planned_labels": 0},
                },
            ):
                payload = rolling_validation.build_rolling_validation(
                    db_path=db_path,
                    since_minutes=60,
                    skip_clob=True,
                )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["blockers"], [])
        self.assertEqual(payload["next_mainline_task"], "paper_trading_validation")
        self.assertEqual(payload["counterfactual"]["next_mainline_task"], "rolling_validation")
        self.assertTrue(payload["bayesian_readiness"]["ok"])
        self.assertNotIn("clob_preflight_skipped", payload["warnings"])
        self.assertNotIn("counterfactual_next_mainline:rolling_validation", payload["warnings"])

    def test_build_rolling_validation_reports_bayesian_readiness_as_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            self._create_db(db_path)
            self._insert_entry(
                db_path,
                {
                    "entry_strategy_family": "repricing_bucket",
                    "entry_usable_edge": 0.0,
                    "entry_usable_edge_base_source": "edge_tradeability_profit_value",
                    "entry_edge_exit_path_required": True,
                    "entry_edge_repricing_exit_evidence_status": "bucket_evidence",
                    "entry_edge_repricing_exit_evidence_probability_source": (
                        "p_converge=bucket_evidence:primary;"
                        "p_fill_entry=bucket_evidence:primary;"
                        "p_fill_exit=bucket_evidence:primary"
                    ),
                    "edge_realizable_p_realize": 0.01,
                    "edge_realizable_p_converge": 0.04,
                    "edge_realizable_p_converge_evidence_status": "bucket_evidence",
                    "edge_realizable_p_converge_bucket_used_key": "conv-low",
                    "edge_realizable_p_converge_samples": 120,
                    "edge_realizable_p_fill_entry": 0.60,
                    "edge_realizable_p_fill_entry_evidence_status": "bucket_evidence",
                    "edge_realizable_p_fill_entry_bucket_used_key": "entry-ok",
                    "edge_realizable_p_fill_entry_samples": 80,
                    "edge_realizable_p_fill_exit": 0.50,
                    "edge_realizable_p_fill_exit_evidence_status": "bucket_evidence",
                    "edge_realizable_p_fill_exit_bucket_used_key": "exit-ok",
                    "edge_realizable_p_fill_exit_samples": 80,
                },
            )
            with patch.object(
                rolling_validation,
                "_launchd_snapshot",
                return_value={"ok": True, "missing_labels": []},
            ), patch.object(
                rolling_validation.entry_counterfactual_review,
                "build_report",
                return_value={
                    "ok": True,
                    "actionable_summary": {"next_mainline_task": "rolling_validation"},
                    "current_runtime": {"run_id": 9},
                },
            ), patch.object(
                rolling_validation.bayesian_readiness_audit,
                "build_report",
                return_value={
                    "ok": False,
                    "city_filter": "chengdu",
                    "since_days": 1,
                    "readiness": {"event_run_bayes_status": "fail"},
                    "blockers": ["event_run_bayes_coverage_low"],
                    "warnings": ["calibration_labels_missing"],
                    "settlement_label_plan": {"planned_labels": 2},
                },
            ):
                payload = rolling_validation.build_rolling_validation(
                    db_path=db_path,
                    since_minutes=60,
                    skip_clob=True,
                )

        self.assertTrue(payload["ok"])
        self.assertFalse(payload["bayesian_readiness"]["ok"])
        self.assertEqual(payload["bayesian_readiness"]["settlement_label_plan"]["planned_labels"], 2)
        self.assertIn(
            "bayesian_readiness_blocker:event_run_bayes_coverage_low",
            payload["warnings"],
        )
        self.assertIn(
            "bayesian_readiness_warning:calibration_labels_missing",
            payload["warnings"],
        )

    def test_build_rolling_validation_reports_db_read_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            rolling_validation,
            "_launchd_snapshot",
            return_value={"ok": True, "missing_labels": []},
        ), patch.object(
            rolling_validation.sqlite3,
            "connect",
            side_effect=rolling_validation.sqlite3.OperationalError("disk I/O error"),
        ), patch.object(
            rolling_validation.weather_decision_core_audit,
            "run_audit",
            return_value={"ok": True, "violations": {}, "latest_exit_policy_coverage": {}},
        ), patch.object(
            rolling_validation.check_trade_readiness,
            "build_live_readiness",
            return_value={"ok": False, "blockers": ["archive_db_read_error"], "warnings": []},
        ), patch.object(
            rolling_validation.entry_counterfactual_review,
            "build_report",
            return_value={"ok": True, "actionable_summary": {"next_mainline_task": "rolling_validation"}},
        ):
            db_path = Path(tmpdir) / "archive.db"
            db_path.write_text("not-used", encoding="utf-8")

            payload = rolling_validation.build_rolling_validation(
                db_path=db_path,
                since_minutes=60,
                skip_clob=True,
            )

        self.assertFalse(payload["ok"])
        self.assertIn("archive_db_read_error", payload["blockers"])
        self.assertIn("OperationalError:disk I/O error", payload["latest_run"]["error"])

    def test_build_rolling_validation_promotes_counterfactual_db_error_to_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            rolling_validation,
            "_launchd_snapshot",
            return_value={"ok": True, "missing_labels": []},
        ), patch.object(
            rolling_validation.weather_decision_core_audit,
            "run_audit",
            return_value={"ok": True, "violations": {}, "latest_exit_policy_coverage": {}},
        ), patch.object(
            rolling_validation.check_trade_readiness,
            "build_live_readiness",
            return_value={"ok": True, "blockers": [], "warnings": []},
        ), patch.object(
            rolling_validation.entry_counterfactual_review,
            "build_report",
            return_value={
                "ok": False,
                "scope": "archive_db_read_error",
                "error": "OperationalError:disk I/O error",
                "actionable_summary": {"next_mainline_task": "database_health_check"},
            },
        ):
            db_path = Path(tmpdir) / "archive.db"
            self._create_db(db_path)
            self._insert_entry(
                db_path,
                {
                    "entry_strategy_family": "repricing_bucket",
                    "entry_usable_edge": 0.0,
                    "entry_usable_edge_base_source": "edge_tradeability_profit_value",
                },
            )

            payload = rolling_validation.build_rolling_validation(
                db_path=db_path,
                since_minutes=60,
                skip_clob=True,
            )

        self.assertFalse(payload["ok"])
        self.assertIn("archive_db_read_error", payload["blockers"])
        self.assertEqual(payload["counterfactual"]["next_mainline_task"], "database_health_check")


if __name__ == "__main__":
    unittest.main()

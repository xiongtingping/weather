import os
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import check_trade_readiness


class CheckTradeReadinessConfigTest(unittest.TestCase):
    def test_default_min_balance_matches_entry_min_notional(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(check_trade_readiness._readiness_min_balance_usdc(), 1.0)

    def test_min_balance_uses_order_usdc_when_larger(self):
        with patch.dict(os.environ, {"PM_ORDER_USDC": "2.5"}, clear=True):
            self.assertEqual(check_trade_readiness._readiness_min_balance_usdc(), 2.5)

    def test_explicit_min_balance_override_can_lower_threshold(self):
        with patch.dict(
            os.environ,
            {"PM_AUTO_TRADE_READINESS_MIN_BALANCE_USDC": "0.25", "PM_ORDER_USDC": "2.5"},
            clear=True,
        ):
            self.assertEqual(check_trade_readiness._readiness_min_balance_usdc(), 0.25)

    def test_live_trading_enabled_requires_enabled_and_not_dry_run(self):
        with patch.dict(
            os.environ,
            {"PM_AUTO_TRADE_ENABLED": "1", "PM_AUTO_TRADE_DRY_RUN": "0"},
            clear=True,
        ):
            self.assertTrue(check_trade_readiness._live_trading_enabled())
        with patch.dict(
            os.environ,
            {"PM_AUTO_TRADE_ENABLED": "1", "PM_AUTO_TRADE_DRY_RUN": "1"},
            clear=True,
        ):
            self.assertFalse(check_trade_readiness._live_trading_enabled())

    def test_identity_uses_node_eval_without_heredoc(self):
        calls: list[str] = []

        def fake_run_bash(command: str, timeout: int = 60):
            calls.append(command)
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "signer": "0x1111111111111111111111111111111111111111",
                        "funder": "0x1111111111111111111111111111111111111111",
                        "dataUser": "0x1111111111111111111111111111111111111111",
                        "sameSignerFunder": True,
                        "signatureType": "0",
                        "clobHost": "https://clob.polymarket.com",
                        "hasApiCreds": True,
                        "hasPrivateKey": True,
                    }
                ),
                stderr="",
            )

        with patch.object(check_trade_readiness, "_run_bash", side_effect=fake_run_bash):
            payload = check_trade_readiness._identity()

        self.assertTrue(payload["ok"])
        self.assertEqual(len(calls), 1)
        self.assertIn("node -e", calls[0])
        self.assertNotIn("<<", calls[0])

    def test_run_bash_preserves_explicit_clob_network_override_after_dotenv(self):
        with patch.dict(os.environ, {"PM_CLOB_FORCE_DIRECT": "1"}, clear=True), patch.object(
            check_trade_readiness.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
        ) as fake_run:
            check_trade_readiness._run_bash("true")

        command = fake_run.call_args.args[0][-1]
        self.assertIn("load_polymarket_launchctl_env", command)
        self.assertIn("load_repo_env_file .env", command)
        self.assertIn("export PM_CLOB_FORCE_DIRECT=1", command)
        self.assertNotIn(". ./.env", command)

    def _create_readiness_db(self, path: Path) -> None:
        with closing(sqlite3.connect(str(path))) as conn:
            conn.executescript(
                """
                CREATE TABLE auto_trade_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT,
                    slug TEXT,
                    event_date TEXT,
                    outcome TEXT,
                    side TEXT,
                    action TEXT,
                    decided_state TEXT,
                    will_order INTEGER,
                    reason_codes_json TEXT,
                    details_json TEXT
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
            conn.commit()

    def _insert_entry_decision(
        self,
        path: Path,
        *,
        details: dict,
        decided_state: str = "entry_dry_run",
        will_order: int = 1,
        reason_codes: list[str] | None = None,
    ) -> None:
        with closing(sqlite3.connect(str(path))) as conn:
            conn.execute(
                """
                INSERT INTO auto_trade_decisions(
                    created_at, slug, event_date, outcome, side, action, decided_state,
                    will_order, reason_codes_json, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "weather-test",
                    "2026-05-05",
                    "70°F or higher",
                    "YES",
                    "entry",
                    decided_state,
                    will_order,
                    json.dumps(reason_codes if reason_codes is not None else ["order_submitted"]),
                    json.dumps(details),
                ),
            )
            conn.commit()

    def _insert_auto_order(
        self,
        path: Path,
        *,
        created_at: str,
        order_checked_at: str | None = None,
        order_id: str = "0xabc",
    ) -> None:
        with closing(sqlite3.connect(str(path))) as conn:
            conn.execute(
                """
                INSERT INTO auto_orders(
                    order_id, slug, event_date, outcome, side, action, price, size,
                    order_type, order_status, order_checked_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    "weather-test",
                    "2026-05-05",
                    "70°F or higher",
                    "YES",
                    "entry",
                    0.25,
                    10.0,
                    "GTC",
                    "submitted" if order_checked_at else None,
                    order_checked_at,
                    created_at,
                ),
            )
            conn.commit()

    def test_db_live_readiness_accepts_complete_dry_run_intent(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {"PM_AUTO_TRADE_DRY_RUN": "1"},
            clear=True,
        ):
            db_path = Path(tmpdir) / "archive.db"
            self._create_readiness_db(db_path)
            self._insert_entry_decision(
                db_path,
                details={
                    "dry_run": True,
                    "status": "dry_run",
                    "token_id": "123",
                    "order_price": 0.25,
                    "order_size": 10.0,
                    "order_type": "GTC",
                    "entry_usable_edge": 0.03,
                    "edge_tradeability_pass": True,
                    "source_fit_tier": "settlement_source",
                    "market_active": True,
                    "market_closed": False,
                    "market_archived": False,
                    "market_accepting_orders": True,
                    "market_enable_orderbook": True,
                },
            )

            payload = check_trade_readiness.build_db_live_readiness(
                db_path=db_path,
                since_minutes=60,
                live_cutover=True,
                dry_run=True,
            )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["blockers"], [])
        self.assertEqual(payload["entry_intents"]["complete_intents"], 1)

    def test_db_live_readiness_uses_latest_intent_per_market_leg(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {"PM_AUTO_TRADE_DRY_RUN": "1"},
            clear=True,
        ):
            db_path = Path(tmpdir) / "archive.db"
            self._create_readiness_db(db_path)
            self._insert_entry_decision(
                db_path,
                details={
                    "order_price": 0.25,
                    "order_size": 10.0,
                    "order_type": "GTC",
                    "entry_usable_edge": 0.03,
                    "edge_tradeability_pass": True,
                },
            )
            self._insert_entry_decision(
                db_path,
                details={
                    "dry_run": True,
                    "status": "dry_run",
                    "token_id": "123",
                    "order_price": 0.25,
                    "order_size": 10.0,
                    "order_type": "GTC",
                    "entry_usable_edge": 0.03,
                    "edge_tradeability_pass": True,
                    "source_fit_tier": "settlement_source",
                },
            )

            payload = check_trade_readiness.build_db_live_readiness(
                db_path=db_path,
                since_minutes=60,
                live_cutover=True,
                dry_run=True,
            )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["entry_intents"]["will_order_rows"], 1)
        self.assertEqual(payload["entry_intents"]["unique_intent_keys"], 1)
        self.assertEqual(payload["entry_intents"]["complete_intents"], 1)

    def test_db_live_readiness_ignores_queued_posterior_target_intent_as_order(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {"PM_AUTO_TRADE_DRY_RUN": "0"},
            clear=True,
        ):
            db_path = Path(tmpdir) / "archive.db"
            self._create_readiness_db(db_path)
            self._insert_entry_decision(
                db_path,
                decided_state="entry",
                reason_codes=[
                    "posterior_target_direct",
                    "target_realization_order_intent",
                ],
                details={
                    "posterior_target_direct": True,
                    "posterior_target_update": True,
                    "posterior_settlement_entry_allowed": False,
                    "posterior_settlement_entry_block_reason": (
                        "temperature_path_trade_follow_not_allowed"
                    ),
                },
            )

            payload = check_trade_readiness.build_db_live_readiness(
                db_path=db_path,
                since_minutes=60,
                live_cutover=True,
                dry_run=False,
            )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["entry_intents"]["will_order_rows"], 0)
        self.assertEqual(payload["entry_intents"]["queued_order_intent_rows"], 1)
        self.assertEqual(payload["order_persistence"]["live_entry_rows"], 0)

    def test_db_live_readiness_treats_place_order_failed_as_pre_submit_warning(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {"PM_AUTO_TRADE_DRY_RUN": "0"},
            clear=True,
        ):
            db_path = Path(tmpdir) / "archive.db"
            self._create_readiness_db(db_path)
            self._insert_entry_decision(
                db_path,
                decided_state="entry_failed",
                reason_codes=["place_order_failed"],
                details={
                    "dry_run": False,
                    "status": "place_order_failed",
                    "token_id": "123",
                    "order_price": 0.25,
                    "order_size": 10.0,
                    "order_type": "GTC",
                    "entry_usable_edge": 0.03,
                    "edge_tradeability_pass": True,
                    "source_fit_tier": "settlement_source",
                },
            )
            self._insert_entry_decision(
                db_path,
                decided_state="entry",
                reason_codes=["order_submitted"],
                details={
                    "dry_run": False,
                    "status": "submitted",
                    "order_id": "0xabc",
                    "token_id": "123",
                    "order_price": 0.25,
                    "order_size": 10.0,
                    "order_type": "GTC",
                    "entry_usable_edge": 0.03,
                    "edge_tradeability_pass": True,
                    "source_fit_tier": "settlement_source",
                },
            )
            self._insert_auto_order(
                db_path,
                created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                order_checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                order_id="0xabc",
            )

            payload = check_trade_readiness.build_db_live_readiness(
                db_path=db_path,
                since_minutes=60,
                live_cutover=True,
                live_trading=True,
                dry_run=False,
            )

        self.assertTrue(payload["ok"])
        self.assertNotIn("intent_missing_order_submitted_code", payload["blockers"])
        self.assertNotIn("live_intent_missing_order_id", payload["blockers"])
        self.assertNotIn("live_entry_missing_order_id", payload["blockers"])
        self.assertIn("live_intent_pre_submit_failed", payload["warnings"])
        self.assertEqual(payload["entry_intents"]["failed_pre_submit_intent_rows"], 1)
        self.assertEqual(payload["order_persistence"]["live_entry_rows"], 1)

    def test_db_live_readiness_does_not_treat_entry_dry_run_as_live_order(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {"PM_AUTO_TRADE_DRY_RUN": "1"},
            clear=True,
        ):
            db_path = Path(tmpdir) / "archive.db"
            self._create_readiness_db(db_path)
            self._insert_entry_decision(
                db_path,
                decided_state="entry_dry_run",
                details={
                    "dry_run": True,
                    "status": "dry_run",
                    "token_id": "123",
                    "order_price": 0.25,
                    "order_size": 10.0,
                    "order_type": "GTC",
                    "entry_usable_edge": 0.03,
                    "edge_tradeability_pass": True,
                    "source_fit_tier": "settlement_source",
                },
            )

            payload = check_trade_readiness.build_db_live_readiness(
                db_path=db_path,
                since_minutes=60,
                live_cutover=True,
                dry_run=True,
            )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["order_persistence"]["live_entry_rows"], 0)

    def test_db_live_readiness_blocks_live_cutover_without_recent_intent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            self._create_readiness_db(db_path)

            payload = check_trade_readiness.build_db_live_readiness(
                db_path=db_path,
                since_minutes=60,
                live_cutover=True,
                dry_run=True,
            )

        self.assertFalse(payload["ok"])
        self.assertIn("no_recent_entry_order_intent", payload["blockers"])

    def test_db_live_readiness_accepts_pre_activation_d1_freeze_without_intent(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {
                "PM_AUTO_TRADE_DRY_RUN": "0",
                "PM_TARGET_REALIZATION_V3_ENABLED": "1",
                "PM_TARGET_REALIZATION_V3_ACTIVATION_DATE_ET": "2999-01-01",
            },
            clear=True,
        ):
            db_path = Path(tmpdir) / "archive.db"
            self._create_readiness_db(db_path)
            with closing(sqlite3.connect(str(db_path))) as conn:
                conn.execute(
                    """
                    INSERT INTO auto_trade_decisions(
                        created_at, slug, event_date, outcome, side, action,
                        decided_state, will_order, reason_codes_json, details_json
                    ) VALUES (?, ?, ?, ?, ?, 'hold', 'frozen', 0, ?, '{}')
                    """,
                    (
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "weather-test",
                        "2999-01-01",
                        "70°F or higher",
                        "YES",
                        json.dumps(["target_realization_d1_existing_positions_frozen"]),
                    ),
                )
                conn.commit()

            payload = check_trade_readiness.build_db_live_readiness(
                db_path=db_path,
                since_minutes=60,
                live_cutover=True,
                live_trading=True,
                dry_run=False,
            )

        self.assertNotIn("no_recent_entry_order_intent", payload["blockers"])
        self.assertTrue(payload["intentional_quiet_state"]["active"])
        self.assertEqual(
            payload["intentional_quiet_state"]["reason"],
            "target_realization_d1_existing_positions_frozen",
        )

    def test_live_readiness_reports_blocker_clearance(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {"PM_AUTO_TRADE_DRY_RUN": "1", "PM_AUTO_TRADE_ENABLED": "0"},
            clear=True,
        ), patch.object(
            check_trade_readiness,
            "build_clob_readiness",
            return_value={
                "ok": False,
                "blockers": ["collateral_balance_allowance_below_min_order"],
                "warnings": [],
                "balance": {
                    "balance_allowance": 0.25,
                    "min_required_usdc": 1.0,
                },
                "open_orders": {"ok": True, "count": 0},
            },
        ):
            db_path = Path(tmpdir) / "archive.db"
            self._create_readiness_db(db_path)

            payload = check_trade_readiness.build_live_readiness(
                db_path=db_path,
                since_minutes=60,
                live_cutover=True,
            )

        self.assertFalse(payload["ok"])
        clearance = payload["blocker_clearance"]
        self.assertFalse(clearance["ok"])
        self.assertEqual(
            clearance["next_mainline_task"],
            "live_cutover_readiness_blocker_clearance",
        )
        by_blocker = {item["blocker"]: item for item in clearance["items"]}
        self.assertAlmostEqual(
            by_blocker["collateral_balance_allowance_below_min_order"]["shortfall_usdc"],
            0.75,
        )
        self.assertEqual(
            by_blocker["no_recent_entry_order_intent"]["clearance"],
            "wait_for_or_trigger_complete_dry_run_entry_intent",
        )
        self.assertIn(
            "PM_AUTO_TRADE_DRY_RUN=1",
            by_blocker["no_recent_entry_order_intent"]["dry_run_probe_command"],
        )
        self.assertEqual(payload["cutover_steps"][0]["step"], "dry_run_probe")
        self.assertEqual(payload["cutover_steps"][2]["status"], "requires_confirmation")

    def test_default_readiness_conclusion_stays_in_paper_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {"PM_AUTO_TRADE_DRY_RUN": "1", "PM_AUTO_TRADE_ENABLED": "0"},
            clear=True,
        ), patch.object(
            check_trade_readiness,
            "build_clob_readiness",
            return_value={
                "ok": True,
                "blockers": [],
                "warnings": [],
                "balance": {"balance_allowance": 0.0, "min_required_usdc": 1.0},
                "open_orders": {"ok": True, "count": 0},
            },
        ):
            db_path = Path(tmpdir) / "archive.db"
            self._create_readiness_db(db_path)
            self._insert_entry_decision(
                db_path,
                details={
                    "dry_run": True,
                    "status": "dry_run",
                    "token_id": "123",
                    "order_price": 0.25,
                    "order_size": 10.0,
                    "order_type": "GTC",
                    "entry_usable_edge": 0.03,
                    "edge_tradeability_pass": True,
                    "source_fit_tier": "settlement_source",
                },
            )

            payload = check_trade_readiness.build_live_readiness(
                db_path=db_path,
                since_minutes=60,
                live_cutover=False,
                skip_clob=False,
            )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["conclusion"], "paper_trading_ready")
        self.assertEqual(payload["blocker_clearance"]["next_mainline_task"], "paper_trading_validation")

    def test_live_readiness_reports_clob_preflight_clearance_items(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {"PM_AUTO_TRADE_DRY_RUN": "1", "PM_AUTO_TRADE_ENABLED": "0"},
            clear=True,
        ), patch.object(
            check_trade_readiness,
            "build_clob_readiness",
            return_value={
                "ok": False,
                "blockers": ["clob_balance_unavailable", "open_orders_unavailable"],
                "warnings": [],
                "balance": {},
                "open_orders": {"ok": False, "count": None},
            },
        ):
            db_path = Path(tmpdir) / "archive.db"
            self._create_readiness_db(db_path)

            payload = check_trade_readiness.build_live_readiness(
                db_path=db_path,
                since_minutes=60,
                live_cutover=True,
            )

        by_blocker = {item["blocker"]: item for item in payload["blocker_clearance"]["items"]}
        self.assertEqual(
            by_blocker["clob_balance_unavailable"]["clearance"],
            "restore_read_only_clob_balance_preflight",
        )
        self.assertEqual(
            by_blocker["open_orders_unavailable"]["clearance"],
            "restore_read_only_clob_open_orders_preflight",
        )

    def test_db_live_readiness_reports_read_error_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            check_trade_readiness.sqlite3,
            "connect",
            side_effect=check_trade_readiness.sqlite3.OperationalError("disk I/O error"),
        ):
            db_path = Path(tmpdir) / "archive.db"
            db_path.write_text("not-used", encoding="utf-8")

            payload = check_trade_readiness.build_db_live_readiness(
                db_path=db_path,
                since_minutes=60,
                live_cutover=True,
                dry_run=True,
            )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["blockers"], ["archive_db_read_error"])
        self.assertIn("OperationalError:disk I/O error", payload["error"])

    def test_db_live_readiness_flags_incomplete_dry_run_intent(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {"PM_AUTO_TRADE_DRY_RUN": "1"},
            clear=True,
        ):
            db_path = Path(tmpdir) / "archive.db"
            self._create_readiness_db(db_path)
            self._insert_entry_decision(
                db_path,
                details={
                    "dry_run": True,
                    "status": "pending",
                    "order_price": 0.25,
                    "order_size": 10.0,
                    "order_type": "GTC",
                    "entry_usable_edge": 0.03,
                    "edge_tradeability_pass": True,
                },
            )

            payload = check_trade_readiness.build_db_live_readiness(
                db_path=db_path,
                since_minutes=60,
                live_cutover=True,
                dry_run=True,
            )

        self.assertFalse(payload["ok"])
        self.assertIn("intent_missing_token_id", payload["blockers"])
        self.assertIn("dry_run_intent_missing_dry_run_marker", payload["blockers"])

    def test_db_live_readiness_blocks_non_whitelisted_hard_reason_code(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            self._create_readiness_db(db_path)
            self._insert_entry_decision(
                db_path,
                decided_state="error_timeout",
                will_order=0,
                reason_codes=["event_worker_failed"],
                details={
                    "entry_original_reason_codes": ["event_worker_failed"],
                    "entry_soft_reason_codes": ["event_worker_failed"],
                },
            )

            payload = check_trade_readiness.build_db_live_readiness(
                db_path=db_path,
                since_minutes=60,
                live_cutover=False,
                dry_run=True,
            )

        self.assertFalse(payload["ok"])
        self.assertIn("entry_hard_reason_code_not_allowed", payload["blockers"])
        problems = payload["entry_hard_reason_policy"]["problems"]
        self.assertEqual(
            problems["entry_hard_reason_code_not_allowed"][0]["bad_reason_codes"],
            ["event_worker_failed"],
        )

    def test_db_live_readiness_accepts_target_realization_hard_reason_codes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            self._create_readiness_db(db_path)
            for code in (
                "target_switch_non_target_entry_suppressed",
                "temperature_path_trade_follow_not_allowed",
                "target_realization_plan_notional_nonpositive",
            ):
                self._insert_entry_decision(
                    db_path,
                    decided_state="blocked",
                    will_order=0,
                    reason_codes=[code],
                    details={"entry_original_reason_codes": [code]},
                )

            payload = check_trade_readiness.build_db_live_readiness(
                db_path=db_path,
                since_minutes=60,
                live_cutover=False,
                dry_run=True,
            )

        self.assertNotIn("entry_hard_reason_code_not_allowed", payload["blockers"])

    def test_db_live_readiness_accepts_legacy_posterior_cleanup_entry_codes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            self._create_readiness_db(db_path)
            self._insert_entry_decision(
                db_path,
                decided_state="entry",
                will_order=0,
                reason_codes=[
                    "posterior_target_direct",
                    "posterior_target_old_exit_cleanup",
                ],
                details={
                    "old_exit_cleanup_triggered": True,
                    "entry_order_update_triggered": False,
                },
            )

            payload = check_trade_readiness.build_db_live_readiness(
                db_path=db_path,
                since_minutes=60,
                live_cutover=False,
                dry_run=True,
            )

        self.assertNotIn("entry_hard_reason_code_not_allowed", payload["blockers"])

    def test_db_live_readiness_accepts_explicit_tradeability_override(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {"PM_AUTO_TRADE_DRY_RUN": "1"},
            clear=True,
        ):
            db_path = Path(tmpdir) / "archive.db"
            self._create_readiness_db(db_path)
            self._insert_entry_decision(
                db_path,
                details={
                    "dry_run": True,
                    "status": "dry_run",
                    "token_id": "123",
                    "order_price": 0.02,
                    "order_size": 250.0,
                    "order_type": "GTC",
                    "entry_usable_edge": 0.01,
                    "edge_tradeability_pass": False,
                    "edge_tradeability_blocked": False,
                    "edge_tradeability_lcb_canary_override": True,
                    "source_fit_tier": "settlement_source",
                },
            )

            payload = check_trade_readiness.build_db_live_readiness(
                db_path=db_path,
                since_minutes=60,
                live_cutover=True,
                dry_run=True,
            )

        self.assertTrue(payload["ok"])
        self.assertIn("intent_tradeability_override_used", payload["warnings"])

    def test_db_live_readiness_requires_live_order_persistence(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {"PM_AUTO_TRADE_DRY_RUN": "0"},
            clear=True,
        ):
            db_path = Path(tmpdir) / "archive.db"
            self._create_readiness_db(db_path)
            self._insert_entry_decision(
                db_path,
                decided_state="entry",
                details={
                    "dry_run": False,
                    "status": "submitted",
                    "order_id": "0xabc",
                    "token_id": "123",
                    "order_price": 0.25,
                    "order_size": 10.0,
                    "order_type": "GTC",
                    "entry_usable_edge": 0.03,
                    "edge_tradeability_pass": True,
                    "source_fit_tier": "settlement_source",
                },
            )

            payload = check_trade_readiness.build_db_live_readiness(
                db_path=db_path,
                since_minutes=60,
                live_cutover=True,
                live_trading=True,
                dry_run=False,
            )

        self.assertFalse(payload["ok"])
        self.assertIn("live_entry_order_not_recorded", payload["blockers"])

    def test_db_live_readiness_does_not_warn_for_legacy_orders_without_checked_at(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            self._create_readiness_db(db_path)
            self._insert_auto_order(
                db_path,
                created_at="2026-05-01T00:00:00+00:00",
                order_checked_at=None,
            )

            payload = check_trade_readiness.build_db_live_readiness(
                db_path=db_path,
                since="2026-05-06T00:00:00+00:00",
                live_cutover=False,
                dry_run=True,
            )

        self.assertNotIn("auto_order_status_sync_has_no_checked_at", payload["warnings"])
        self.assertEqual(payload["order_persistence"]["auto_orders_without_checked_at"], 1)
        self.assertEqual(payload["order_persistence"]["legacy_auto_orders_without_checked_at"], 1)
        self.assertEqual(payload["order_persistence"]["recent_auto_orders_without_checked_at"], 0)

    def test_db_live_readiness_warns_for_recent_order_without_checked_at(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            self._create_readiness_db(db_path)
            self._insert_auto_order(
                db_path,
                created_at="2026-05-06T00:05:00+00:00",
                order_checked_at=None,
            )

            payload = check_trade_readiness.build_db_live_readiness(
                db_path=db_path,
                since="2026-05-06T00:00:00+00:00",
                live_cutover=False,
                dry_run=True,
            )

        self.assertIn("auto_order_status_sync_has_no_checked_at", payload["warnings"])
        self.assertEqual(payload["order_persistence"]["recent_auto_orders"], 1)
        self.assertEqual(payload["order_persistence"]["recent_auto_orders_without_checked_at"], 1)

    def test_db_live_readiness_accepts_recent_order_with_checked_at(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            self._create_readiness_db(db_path)
            self._insert_auto_order(
                db_path,
                created_at="2026-05-06T00:05:00+00:00",
                order_checked_at="2026-05-06T00:06:00+00:00",
            )

            payload = check_trade_readiness.build_db_live_readiness(
                db_path=db_path,
                since="2026-05-06T00:00:00+00:00",
                live_cutover=False,
                dry_run=True,
            )

        self.assertNotIn("auto_order_status_sync_has_no_checked_at", payload["warnings"])
        self.assertEqual(payload["order_persistence"]["recent_auto_orders"], 1)
        self.assertEqual(payload["order_persistence"]["recent_auto_orders_without_checked_at"], 0)

    def test_db_live_readiness_requires_exit_skip_for_open_positions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            self._create_readiness_db(db_path)
            with closing(sqlite3.connect(str(db_path))) as conn:
                conn.execute(
                    """
                    INSERT INTO edge_trades(
                        slug, event_date, outcome, side, state, position_size, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "weather-test",
                        "2026-05-05",
                        "70°F or higher",
                        "YES",
                        "entry",
                        5.0,
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    ),
                )
                conn.commit()

            payload = check_trade_readiness.build_db_live_readiness(
                db_path=db_path,
                since_minutes=60,
                live_cutover=False,
                dry_run=True,
            )

        self.assertFalse(payload["ok"])
        self.assertIn("open_position_missing_exit_skip_audit", payload["blockers"])


if __name__ == "__main__":
    unittest.main()

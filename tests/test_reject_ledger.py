import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts import reject_ledger


class RejectLedgerTest(unittest.TestCase):
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
                CREATE TABLE event_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slug TEXT,
                    event_date TEXT,
                    settlement_actual REAL,
                    rounding_rule TEXT,
                    created_at TEXT
                );
                CREATE TABLE edge_trade_fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slug TEXT,
                    event_date TEXT,
                    outcome TEXT,
                    outcome_side TEXT,
                    trade_side TEXT,
                    price REAL,
                    size REAL,
                    order_id TEXT
                );
                """
            )

    def _insert_rejected_entry(
        self,
        path: Path,
        *,
        details: dict,
        reason_codes: list[str] | None = None,
        outcome: str = "80°F or higher",
        side: str = "YES",
        event_date: str | None = None,
        run_id: int = 11,
    ) -> int:
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        event_date = event_date or (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
        with sqlite3.connect(str(path)) as conn:
            cur = conn.execute(
                """
                INSERT INTO auto_trade_decisions(
                    run_id, run_date, slug, event_date, title, outcome, side,
                    action, decided_state, will_order, reason_codes_json,
                    reasons_json, details_json, created_at
                ) VALUES (?, ?, 'weather-test', ?, 'Highest temperature in Test?', ?, ?,
                          'entry', 'blocked', 0, ?, '[]', ?, ?)
                """,
                (
                    run_id,
                    event_date,
                    event_date,
                    outcome,
                    side,
                    json.dumps(reason_codes or []),
                    json.dumps(details),
                    created_at,
                ),
            )
            return int(cur.lastrowid)

    def test_materializes_settled_reject_with_counterfactual_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            self._create_db(db_path)
            event_date = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
            decision_id = self._insert_rejected_entry(
                db_path,
                event_date=event_date,
                reason_codes=["edge_tradeability_blocked"],
                details={
                    "entry_strategy_family": "repricing_bucket",
                    "entry_usable_edge": -0.02,
                    "entry_usable_edge_base_source": "edge_tradeability_profit_value",
                    "entry_usable_edge_size_scale_applied": 0.5,
                    "edge_tradeability_pass": False,
                    "edge_tradeability_blocked": True,
                    "edge_tradeability_profit_value": -0.01,
                    "q_entry": 0.62,
                    "ask": 0.39,
                    "bid": 0.38,
                    "spread": 0.01,
                    "order_price": 0.40,
                    "order_size": 5.0,
                    "entry_edge_false_edge_type": "target_exit_execution_probability_too_low",
                    "entry_edge_root_cause": "repricing_probability",
                    "edge_realizable_p_realize": 0.03,
                    "edge_realizable_p_converge": 0.10,
                    "edge_realizable_p_fill_entry": 0.70,
                    "edge_realizable_p_fill_exit": 0.50,
                    "edge_realizable_p_converge_evidence_status": "bucket_evidence",
                    "edge_realizable_p_converge_bucket_used_key": "conv-low",
                    "entry_usable_edge_version": "audit_v1_2026_05_05",
                },
            )
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute(
                    """
                    INSERT INTO event_runs(slug, event_date, settlement_actual, rounding_rule, created_at)
                    VALUES ('weather-test', ?, 82, '', ?)
                    """,
                    (event_date, datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )

            payload = reject_ledger.materialize_reject_ledger(db_path=db_path, days=7)

            self.assertTrue(payload["ok"])
            self.assertEqual(payload["rows_upserted"], 1)
            self.assertEqual(payload["settled_rows"], 1)
            with sqlite3.connect(str(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT * FROM reject_ledger_v1 WHERE source_decision_id = ?",
                    (decision_id,),
                ).fetchone()
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row["rejection_reason"], "edge_tradeability_blocked")
            self.assertEqual(row["strategy_family"], "repricing_bucket")
            self.assertEqual(row["side_hit"], 1)
            self.assertEqual(row["subsequent_outcome"], "hit")
            self.assertAlmostEqual(row["counterfactual_pnl_per_share"], 0.60)
            self.assertAlmostEqual(row["realized_counterfactual_pnl"], 3.0)
            self.assertEqual(row["hypothetical_size"], 5.0)
            self.assertIn("entry_usable_edge_version", json.loads(row["rule_version_json"]))
            components = json.loads(row["repricing_components_json"])
            self.assertEqual(components["p_converge"]["bucket_used_key"], "conv-low")

    def test_materialization_is_idempotent_by_source_decision_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            self._create_db(db_path)
            self._insert_rejected_entry(
                db_path,
                reason_codes=[],
                details={
                    "entry_strategy_family": "lcb_global_main",
                    "entry_usable_edge": -0.01,
                    "entry_edge_false_edge_type": "q_lcb_below_effective_entry_price",
                    "order_price": 0.25,
                    "kelly_target_notional": 2.5,
                },
            )

            first = reject_ledger.materialize_reject_ledger(db_path=db_path, days=7)
            second = reject_ledger.materialize_reject_ledger(db_path=db_path, days=7)

            self.assertEqual(first["rows_upserted"], 1)
            self.assertEqual(second["rows_upserted"], 1)
            with sqlite3.connect(str(db_path)) as conn:
                row_count = conn.execute("SELECT COUNT(*) FROM reject_ledger_v1").fetchone()[0]
                row = conn.execute(
                    "SELECT rejection_reason, hypothetical_size, subsequent_outcome FROM reject_ledger_v1"
                ).fetchone()
            self.assertEqual(row_count, 1)
            self.assertEqual(row[0], "q_lcb_below_effective_entry_price")
            self.assertAlmostEqual(row[1], 10.0)
            self.assertEqual(row[2], "settlement_missing")

    def test_dry_run_builds_rows_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            self._create_db(db_path)
            self._insert_rejected_entry(
                db_path,
                details={"entry_usable_edge": -0.01, "order_price": 0.20},
            )

            payload = reject_ledger.materialize_reject_ledger(
                db_path=db_path,
                days=7,
                dry_run=True,
            )

            self.assertTrue(payload["ok"])
            self.assertTrue(payload["dry_run"])
            self.assertEqual(payload["ledger_rows_built"], 1)
            with sqlite3.connect(str(db_path)) as conn:
                table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reject_ledger_v1'"
                ).fetchone()
            self.assertIsNone(table)

    def test_quality_review_promotes_active_false_reject_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            self._create_db(db_path)
            event_date = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
            for idx in range(3):
                self._insert_rejected_entry(
                    db_path,
                    event_date=event_date,
                    run_id=20,
                    reason_codes=[],
                    details={
                        "entry_strategy_family": "repricing_bucket",
                        "entry_edge_false_edge_type": "active_soft_rule",
                        "order_price": 0.30,
                        "order_size": 2.0 + idx,
                    },
                )
            self._insert_rejected_entry(
                db_path,
                event_date=event_date,
                run_id=21,
                reason_codes=[],
                details={
                    "entry_strategy_family": "repricing_bucket",
                    "entry_edge_false_edge_type": "active_soft_rule",
                    "order_price": 0.30,
                    "order_size": 1.0,
                },
            )
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute(
                    """
                    INSERT INTO event_runs(slug, event_date, settlement_actual, rounding_rule, created_at)
                    VALUES ('weather-test', ?, 85, '', ?)
                    """,
                    (event_date, datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )

            payload = reject_ledger.materialize_reject_ledger(db_path=db_path, days=7)

            review = payload["quality_review"]
            self.assertEqual(review["next_mainline_task"], "active_reject_rule_review")
            self.assertEqual(review["active_false_reject_review"][0]["rejection_reason"], "active_soft_rule")
            self.assertEqual(review["active_false_reject_review"][0]["active_count"], 1)
            self.assertEqual(payload["next_mainline_task"], "active_reject_rule_review")
            monitor = payload["rule_effectiveness_monitor"]
            self.assertFalse(monitor["ok"])
            self.assertEqual(monitor["status"], "active_false_reject_alert")

    def test_quality_review_keeps_active_protective_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            self._create_db(db_path)
            event_date = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
            for idx in range(3):
                self._insert_rejected_entry(
                    db_path,
                    event_date=event_date,
                    run_id=30,
                    reason_codes=["protective_rule"],
                    details={
                        "entry_strategy_family": "lcb_global_main",
                        "order_price": 0.70,
                        "order_size": 2.0 + idx,
                    },
                )
            self._insert_rejected_entry(
                db_path,
                event_date=event_date,
                run_id=31,
                reason_codes=["protective_rule"],
                details={
                    "entry_strategy_family": "lcb_global_main",
                    "order_price": 0.70,
                    "order_size": 1.0,
                },
            )
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute(
                    """
                    INSERT INTO event_runs(slug, event_date, settlement_actual, rounding_rule, created_at)
                    VALUES ('weather-test', ?, 70, '', ?)
                    """,
                    (event_date, datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )

            payload = reject_ledger.materialize_reject_ledger(db_path=db_path, days=7)

            review = payload["quality_review"]
            self.assertEqual(review["active_false_reject_review"], [])
            self.assertEqual(review["active_protective"][0]["rejection_reason"], "protective_rule")
            self.assertEqual(review["next_mainline_task"], "rule_effectiveness_monitoring")
            monitor = payload["rule_effectiveness_monitor"]
            self.assertTrue(monitor["ok"])
            self.assertEqual(monitor["status"], "healthy")
            self.assertEqual(monitor["active_protective_count"], 1)

    def test_quality_review_does_not_block_on_historical_size_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            self._create_db(db_path)
            event_date = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
            for _ in range(3):
                self._insert_rejected_entry(
                    db_path,
                    event_date=event_date,
                    run_id=40,
                    reason_codes=["historical_size_missing_rule"],
                    details={
                        "entry_strategy_family": "lcb_global_main",
                        "order_price": 0.20,
                    },
                )
            for idx in range(3):
                self._insert_rejected_entry(
                    db_path,
                    event_date=event_date,
                    run_id=41,
                    reason_codes=["current_protective_rule"],
                    details={
                        "entry_strategy_family": "lcb_global_main",
                        "order_price": 0.70,
                        "order_size": 1.0 + idx,
                    },
                )
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute(
                    """
                    INSERT INTO event_runs(slug, event_date, settlement_actual, rounding_rule, created_at)
                    VALUES ('weather-test', ?, 70, '', ?)
                    """,
                    (event_date, datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )

            payload = reject_ledger.materialize_reject_ledger(db_path=db_path, days=7)

            review = payload["quality_review"]
            self.assertEqual(review["active_pnl_size_coverage_gap"], [])
            self.assertEqual(
                review["historical_pnl_size_coverage_gap"][0]["rejection_reason"],
                "historical_size_missing_rule",
            )
            self.assertEqual(review["next_mainline_task"], "rule_effectiveness_monitoring")
            self.assertEqual(payload["rule_effectiveness_monitor"]["status"], "healthy")

    def test_hard_gate_size_gap_is_non_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            self._create_db(db_path)
            event_date = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
            for _ in range(3):
                self._insert_rejected_entry(
                    db_path,
                    event_date=event_date,
                    run_id=45,
                    reason_codes=["event_worker_timeout"],
                    details={
                        "entry_strategy_family": "lcb_global_main",
                        "order_price": 0.20,
                    },
                )
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute(
                    """
                    INSERT INTO event_runs(slug, event_date, settlement_actual, rounding_rule, created_at)
                    VALUES ('weather-test', ?, 70, '', ?)
                    """,
                    (event_date, datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )

            payload = reject_ledger.materialize_reject_ledger(db_path=db_path, days=7)

            review = payload["quality_review"]
            self.assertEqual(review["active_pnl_size_coverage_gap"], [])
            non_actionable = review["active_non_actionable_pnl_size_coverage_gap"]
            self.assertEqual(non_actionable[0]["rejection_reason"], "event_worker_timeout")
            self.assertEqual(non_actionable[0]["pnl_size_gap_scope"], "non_tradeable_hard_gate_na")
            self.assertNotEqual(review["next_mainline_task"], "reject_ledger_size_coverage_roll_forward")
            self.assertNotEqual(payload["next_mainline_task"], "reject_ledger_size_coverage_roll_forward")

    def test_rule_effectiveness_monitor_marks_active_sample_accrual(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            self._create_db(db_path)
            event_date = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
            self._insert_rejected_entry(
                db_path,
                event_date=event_date,
                run_id=50,
                reason_codes=["new_active_rule"],
                details={
                    "entry_strategy_family": "repricing_bucket",
                    "order_price": 0.50,
                    "order_size": 1.0,
                },
            )
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute(
                    """
                    INSERT INTO event_runs(slug, event_date, settlement_actual, rounding_rule, created_at)
                    VALUES ('weather-test', ?, 70, '', ?)
                    """,
                    (event_date, datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )

            payload = reject_ledger.materialize_reject_ledger(db_path=db_path, days=7)

            monitor = payload["rule_effectiveness_monitor"]
            self.assertTrue(monitor["ok"])
            self.assertEqual(monitor["status"], "active_sample_accrual")
            self.assertEqual(payload["next_mainline_task"], "reject_ledger_active_sample_accrual")

    def test_rule_taxonomy_splits_hard_gate_and_soft_haircut(self) -> None:
        cases = {
            "market_tick_stale": ("hard_gate", "data_invalid"),
            "market_closed": ("hard_gate", "exchange_or_execution_exception"),
            "already_in_entry": ("hard_gate", "exchange_or_execution_exception"),
            "bucket_loss_streak_blocked": ("hard_gate", "risk_redline"),
            "other_side_active": ("hard_gate", "risk_redline"),
            "exit_flag": ("hard_gate", "risk_redline"),
            "reentry_blocked": ("hard_gate", "risk_redline"),
            "contract_rule_missing": ("hard_gate", "causal_or_contract_safety"),
            "target_exit_execution_evidence_missing": ("soft_haircut", "sample_insufficient"),
            "target_exit_execution_evidence_low_sample": ("soft_haircut", "sample_insufficient"),
            "q_lcb_below_effective_entry_price": ("soft_haircut", "liquidity_or_price_weak"),
            "target_exit_execution_probability_too_low": (
                "soft_haircut",
                "calibration_or_probability_uncertain",
            ),
            "plain_lcb_pinned_tail_no_filtered": ("soft_haircut", "regime_uncertain"),
            "entry_gate_blocked": ("diagnostic_or_legacy", "wrapper_reason"),
        }
        for reason, expected in cases.items():
            with self.subTest(reason=reason):
                taxonomy = reject_ledger._rule_taxonomy(reason)
                self.assertEqual((taxonomy["gate_class"], taxonomy["gate_subtype"]), expected)

    def test_quality_review_includes_hard_soft_taxonomy_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            self._create_db(db_path)
            event_date = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
            self._insert_rejected_entry(
                db_path,
                event_date=event_date,
                run_id=60,
                reason_codes=["market_closed"],
                details={
                    "entry_strategy_family": "lcb_global_main",
                    "order_price": 0.70,
                    "order_size": 1.0,
                },
            )
            self._insert_rejected_entry(
                db_path,
                event_date=event_date,
                run_id=60,
                reason_codes=["target_exit_execution_evidence_missing"],
                details={
                    "entry_strategy_family": "repricing_bucket",
                    "order_price": 0.30,
                    "order_size": 1.0,
                },
            )
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute(
                    """
                    INSERT INTO event_runs(slug, event_date, settlement_actual, rounding_rule, created_at)
                    VALUES ('weather-test', ?, 70, '', ?)
                    """,
                    (event_date, datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )

            payload = reject_ledger.materialize_reject_ledger(db_path=db_path, days=7)

            taxonomy = payload["quality_review"]["hard_soft_taxonomy"]
            self.assertTrue(taxonomy["ok"])
            self.assertEqual(taxonomy["scope"], "reporting_only_no_policy_change")
            class_counts = {row["gate_class"]: row for row in taxonomy["class_counts"]}
            self.assertEqual(class_counts["hard_gate"]["total_count"], 1)
            self.assertEqual(class_counts["soft_haircut"]["total_count"], 1)
            rows_by_reason = {
                row["rejection_reason"]: row for row in payload["quality_review"]["rows"]
            }
            self.assertEqual(rows_by_reason["market_closed"]["gate_class"], "hard_gate")
            self.assertEqual(
                rows_by_reason["target_exit_execution_evidence_missing"]["gate_class"],
                "soft_haircut",
            )

    def test_primary_rejection_reason_prefers_soft_code_over_applied_state(self) -> None:
        reason = reject_ledger._primary_rejection_reason(
            [],
            {
                "kelly_reason": "applied",
                "entry_soft_reason_codes": ["strategy_size_ratio_zero"],
                "entry_original_reason_codes": ["strategy_size_ratio_zero"],
            },
        )

        self.assertEqual(reason, "strategy_size_ratio_zero")

    def test_primary_rejection_reason_skips_wrapper_code(self) -> None:
        reason = reject_ledger._primary_rejection_reason(
            ["entry_gate_blocked", "already_in_entry"],
            {},
        )

        self.assertEqual(reason, "already_in_entry")

    def test_soft_haircut_policy_review_keeps_negative_unit_pnl_at_zero_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            self._create_db(db_path)
            event_date = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
            for idx in range(3):
                self._insert_rejected_entry(
                    db_path,
                    event_date=event_date,
                    run_id=70,
                    reason_codes=["q_lcb_below_effective_entry_price"],
                    details={
                        "entry_strategy_family": "repricing_bucket",
                        "order_price": 0.30,
                        "order_size": 1.0 + idx,
                    },
                )
            self._insert_rejected_entry(
                db_path,
                event_date=event_date,
                run_id=71,
                reason_codes=["q_lcb_below_effective_entry_price"],
                details={
                    "entry_strategy_family": "repricing_bucket",
                    "order_price": 0.30,
                    "order_size": 1.0,
                },
            )
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute(
                    """
                    INSERT INTO event_runs(slug, event_date, settlement_actual, rounding_rule, created_at)
                    VALUES ('weather-test', ?, 70, '', ?)
                    """,
                    (event_date, datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )

            payload = reject_ledger.materialize_reject_ledger(db_path=db_path, days=7)

            review = payload["quality_review"]["soft_haircut_policy_review"]
            self.assertEqual(review["keep_zero_size_reason_count"], 1)
            self.assertEqual(review["release_ready_reason_count"], 0)
            item = review["keep_zero_size"][0]
            self.assertEqual(item["rejection_reason"], "q_lcb_below_effective_entry_price")
            self.assertEqual(item["disposition"], "keep_zero_size")
            self.assertEqual(item["recommended_policy_action"], "keep_zero_size_soft_haircut_until_unit_pnl_improves")

    def test_soft_haircut_policy_review_splits_high_hit_negative_unit_pnl_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            self._create_db(db_path)
            base_date = datetime.now(timezone.utc) - timedelta(days=5)
            rows = [
                (0, 80, 0.93, 80),
                (1, 81, 0.93, 85),
                (2, 82, 0.93, 70),
            ]
            for offset, run_id, price, actual in rows:
                event_date = (base_date + timedelta(days=offset)).date().isoformat()
                self._insert_rejected_entry(
                    db_path,
                    event_date=event_date,
                    run_id=run_id,
                    reason_codes=["edge_tradeability_blocked:edge_lcb_below_min"],
                    details={
                        "entry_strategy_family": "exploration_canary",
                        "order_price": price,
                        "order_size": 1.0,
                    },
                )
                with sqlite3.connect(str(db_path)) as conn:
                    conn.execute(
                        """
                        INSERT INTO event_runs(slug, event_date, settlement_actual, rounding_rule, created_at)
                        VALUES ('weather-test', ?, ?, '', ?)
                        """,
                        (
                            event_date,
                            actual,
                            datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        ),
                    )

            payload = reject_ledger.materialize_reject_ledger(db_path=db_path, days=7)

            review = payload["quality_review"]["soft_haircut_policy_review"]
            self.assertEqual(review["bucket_split_reason_count"], 1)
            self.assertEqual(review["release_ready_reason_count"], 0)
            item = review["bucket_split_before_haircut"][0]
            self.assertEqual(item["rejection_reason"], "edge_tradeability_blocked:edge_lcb_below_min")
            self.assertEqual(item["disposition"], "bucket_split_before_haircut")
            self.assertIn("high_hit_rate_but_negative_unit_pnl", item["evidence_label"])

    def test_rule_pruning_review_classifies_active_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            self._create_db(db_path)
            base_date = datetime.now(timezone.utc) - timedelta(days=5)
            cases = [
                ("q_lcb_below_effective_entry_price", [70, 70, 70], 0.30),
                ("edge_tradeability_blocked:edge_lcb_below_min", [85, 85, 70], 0.93),
                ("target_exit_execution_probability_too_low", [85, 85, 70], 0.20),
                ("market_closed", [85, 85, 85], 0.20),
            ]
            for case_idx, (reason, actuals, price) in enumerate(cases):
                for idx, actual in enumerate(actuals):
                    event_date = (base_date + timedelta(days=case_idx * 3 + idx)).date().isoformat()
                    self._insert_rejected_entry(
                        db_path,
                        event_date=event_date,
                        run_id=110,
                        reason_codes=[reason],
                        details={
                            "entry_strategy_family": "exploration_canary",
                            "order_price": price,
                            "order_size": 1.0,
                        },
                    )
                    with sqlite3.connect(str(db_path)) as conn:
                        conn.execute(
                            """
                            INSERT INTO event_runs(slug, event_date, settlement_actual, rounding_rule, created_at)
                            VALUES ('weather-test', ?, ?, '', ?)
                            """,
                            (
                                event_date,
                                actual,
                                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            ),
                        )

            payload = reject_ledger.materialize_reject_ledger(db_path=db_path, days=7)

            review = payload["quality_review"]["rule_pruning_review"]
            self.assertEqual(review["scope"], "reporting_only_no_policy_change")
            keep = {item["rejection_reason"]: item for item in review["keep_rules"]}
            self.assertEqual(
                keep["q_lcb_below_effective_entry_price"]["pruning_quadrant"],
                "high_protection_low_false_reject",
            )
            segment = {item["rejection_reason"]: item for item in review["segment_or_weight_before_pruning"]}
            self.assertEqual(
                segment["edge_tradeability_blocked:edge_lcb_below_min"]["pruning_quadrant"],
                "high_protection_high_false_reject",
            )
            shadow = {item["rejection_reason"]: item for item in review["shadow_before_delete_or_release"]}
            self.assertEqual(
                shadow["target_exit_execution_probability_too_low"]["recommended_pruning_action"],
                "shadow_probe_before_delete_or_weight_release",
            )
            hard = {item["rejection_reason"]: item for item in review["hard_invariant_not_pruned"]}
            self.assertEqual(
                hard["market_closed"]["recommended_pruning_action"],
                "keep_hard_gate_and_only_fix_noisy_source_or_specificity",
            )
            self.assertEqual(review["next_mainline_task"], "rule_pruning_shadow_probe_review")

    def test_bucket_split_review_marks_positive_exact_bucket_as_shadow_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            self._create_db(db_path)
            base_date = datetime.now(timezone.utc) - timedelta(days=5)
            rows = []
            for idx in range(20):
                actual = 85 if idx in {0, 7, 14} else 70
                run_id = 91 if idx == 19 else 90
                rows.append((idx, run_id, "YES", 0.01, actual))
            for offset, run_id, side, price, actual in rows:
                event_date = (base_date + timedelta(days=offset)).date().isoformat()
                self._insert_rejected_entry(
                    db_path,
                    event_date=event_date,
                    run_id=run_id,
                    side=side,
                    reason_codes=["strategy_size_ratio_zero"],
                    details={
                        "entry_strategy_family": "exploration_canary",
                        "order_price": price,
                        "order_size": 35.0,
                    },
                )
                with sqlite3.connect(str(db_path)) as conn:
                    conn.execute(
                        """
                        INSERT INTO event_runs(slug, event_date, settlement_actual, rounding_rule, created_at)
                        VALUES ('weather-test', ?, ?, '', ?)
                        """,
                        (
                            event_date,
                            actual,
                            datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        ),
                    )

            payload = reject_ledger.materialize_reject_ledger(db_path=db_path, days=7)

            review = payload["quality_review"]["soft_haircut_bucket_split_review"]
            self.assertEqual(review["next_mainline_task"], "soft_haircut_shadow_probe_design")
            self.assertEqual(review["shadow_probe_candidate_count"], 1)
            candidate = review["shadow_probe_candidates"][0]
            self.assertEqual(candidate["rejection_reason"], "strategy_size_ratio_zero")
            self.assertEqual(candidate["strategy_family"], "exploration_canary")
            self.assertEqual(candidate["side"], "YES")
            self.assertEqual(candidate["price_bucket"], "p00_002")
            self.assertEqual(candidate["notional_bucket"], "n000_050")
            self.assertEqual(candidate["disposition"], "shadow_probe_candidate")
            self.assertTrue(candidate["candidate_shadow_probe"])
            design = payload["quality_review"]["soft_haircut_shadow_probe_design"]
            self.assertEqual(
                design["next_mainline_task"],
                "soft_haircut_shadow_probe_ledger_instrumentation",
            )
            self.assertEqual(design["candidate_count"], 1)
            self.assertFalse(design["live_orders_enabled"])
            probe = design["designs"][0]
            self.assertEqual(probe["mode"], "shadow")
            self.assertFalse(probe["live_orders_enabled"])
            self.assertFalse(probe["tiny_live_candidate_enabled"])
            self.assertEqual(probe["exact_bucket"]["rejection_reason"], "strategy_size_ratio_zero")
            self.assertEqual(probe["exact_bucket"]["price_bucket"], "p00_002")
            self.assertTrue(probe["guards"]["require_shadow_or_dry_run_first"])
            self.assertTrue(probe["guards"]["isolate_from_main_strategy_pnl"])
            self.assertEqual(
                probe["recommended_implementation"]["next_step"],
                "instrument_shadow_probe_ledger_without_live_orders",
            )
            shadow = payload["shadow_probe"]
            self.assertTrue(shadow["ok"])
            self.assertEqual(shadow["rows_built"], 20)
            self.assertEqual(shadow["rows_upserted"], 20)
            with sqlite3.connect(str(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM soft_haircut_shadow_probe_ledger_v1"
                ).fetchall()
            self.assertEqual(len(rows), 20)
            row = rows[0]
            self.assertEqual(row["rejection_reason"], "strategy_size_ratio_zero")
            self.assertEqual(row["strategy_family"], "exploration_canary")
            self.assertEqual(row["side"], "YES")
            self.assertEqual(row["price_bucket"], "p00_002")
            self.assertEqual(row["notional_bucket"], "n000_050")
            self.assertEqual(row["shadow_mode"], "shadow")
            self.assertEqual(row["live_orders_enabled"], 0)
            self.assertEqual(row["live_block_reason"], "shadow_probe_report_only_no_live_order")
            self.assertAlmostEqual(row["shadow_notional"], 0.35)
            self.assertGreater(row["shadow_size"], 0)
            ledger_review = payload["quality_review"]["soft_haircut_shadow_probe_ledger"]
            self.assertEqual(ledger_review["status"], "shadow_probe_ledger_active")
            self.assertEqual(ledger_review["current_run_rows"], 1)
            self.assertEqual(ledger_review["total_rows"], 20)
            self.assertEqual(
                ledger_review["next_mainline_task"],
                "soft_haircut_shadow_probe_post_instrument_sample_accrual",
            )
            outcome = ledger_review["outcome_monitoring"]
            self.assertEqual(outcome["status"], "waiting_for_post_instrument_shadow_decisions")
            self.assertEqual(outcome["rows"][0]["new_shadow_decision_count"], 0)
            self.assertIn(
                "new_shadow_decisions_below_min",
                outcome["rows"][0]["promotion_blockers"],
            )

    def test_bucket_sample_accrual_reports_active_insufficient_exact_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            self._create_db(db_path)
            base_date = datetime.now(timezone.utc) - timedelta(days=5)
            for idx, actual in enumerate((85, 70, 70)):
                event_date = (base_date + timedelta(days=idx)).date().isoformat()
                self._insert_rejected_entry(
                    db_path,
                    event_date=event_date,
                    run_id=99,
                    reason_codes=["strategy_size_ratio_zero"],
                    details={
                        "entry_strategy_family": "exploration_canary",
                        "order_price": 0.01,
                        "order_size": 35.0,
                    },
                )
                with sqlite3.connect(str(db_path)) as conn:
                    conn.execute(
                        """
                        INSERT INTO event_runs(slug, event_date, settlement_actual, rounding_rule, created_at)
                        VALUES ('weather-test', ?, ?, '', ?)
                        """,
                        (
                            event_date,
                            actual,
                            datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        ),
                    )
            event_date = (base_date + timedelta(days=4)).date().isoformat()
            self._insert_rejected_entry(
                db_path,
                event_date=event_date,
                run_id=100,
                reason_codes=["strategy_size_ratio_zero"],
                details={
                    "entry_strategy_family": "lcb_global_main",
                    "order_price": 0.005,
                    "order_size": 70.0,
                },
            )

            payload = reject_ledger.materialize_reject_ledger(db_path=db_path, days=7)

            accrual = payload["quality_review"]["soft_haircut_bucket_sample_accrual"]
            self.assertEqual(accrual["next_mainline_task"], "soft_haircut_bucket_sample_accrual")
            self.assertEqual(accrual["active_insufficient_bucket_count"], 1)
            self.assertEqual(accrual["total_settled_gap_to_min"], 20)
            row = accrual["rows"][0]
            self.assertEqual(row["rejection_reason"], "strategy_size_ratio_zero")
            self.assertEqual(row["strategy_family"], "path_probability_follow")
            self.assertEqual(row["price_bucket"], "p00_002")
            self.assertEqual(row["notional_bucket"], "n000_050")
            self.assertEqual(row["settled_gap_to_min"], 20)
            self.assertEqual(payload["shadow_probe"]["rows_built"], 0)
            self.assertEqual(payload["quality_review"]["next_mainline_task"], "soft_haircut_bucket_sample_accrual")
            self.assertEqual(payload["rule_effectiveness_monitor"]["status"], "mainline_followup")
            self.assertEqual(payload["next_mainline_task"], "soft_haircut_bucket_sample_accrual")


if __name__ == "__main__":
    unittest.main()

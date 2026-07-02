import importlib.util
import json
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "entry_counterfactual_review",
    ROOT / "scripts" / "entry_counterfactual_review.py",
)
entry_counterfactual_review = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(entry_counterfactual_review)


class EntryCounterfactualReviewTest(unittest.TestCase):
    def _create_base_db(self, db_path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(db_path)
        conn.execute(
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
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE event_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT,
                event_date TEXT,
                settlement_actual REAL,
                rounding_rule TEXT,
                created_at TEXT,
                event_end_utc TEXT
            )
            """
        )
        conn.execute(
            """
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
            )
            """
        )
        return conn

    def test_build_report_reports_db_read_error_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            entry_counterfactual_review.sqlite3,
            "connect",
            side_effect=entry_counterfactual_review.sqlite3.OperationalError("disk I/O error"),
        ):
            db_path = Path(tmp) / "archive.db"
            db_path.write_text("not-used", encoding="utf-8")

            report = entry_counterfactual_review.build_report(db_path, days=1)

        self.assertFalse(report["ok"])
        self.assertEqual(report["scope"], "archive_db_read_error")
        self.assertIn("OperationalError:disk I/O error", report["error"])
        self.assertEqual(
            report["actionable_summary"]["next_mainline_task"],
            "database_health_check",
        )

    def test_load_settlements_batches_keys_and_keeps_latest_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "archive.db"
            conn = self._create_base_db(db_path)
            rows = [
                ("batched-market", "2026-05-05", 70, "old", "2026-05-06T00:00:00+00:00"),
                ("batched-market", "2026-05-05", 71, "new", "2026-05-06T01:00:00+00:00"),
                ("batched-market", "2026-05-05", 72, "newer-id", "2026-05-06T01:00:00+00:00"),
                ("blank-market", "2026-05-05", "", "blank", "2026-05-06T02:00:00+00:00"),
            ]
            conn.executemany(
                """
                INSERT INTO event_runs(slug, event_date, settlement_actual, rounding_rule, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
            conn.row_factory = sqlite3.Row

            settlements = entry_counterfactual_review._load_settlements(
                conn,
                [
                    ("batched-market", "2026-05-05"),
                    ("batched-market", "2026-05-05T00:00:00+00:00"),
                    ("blank-market", "2026-05-05"),
                ],
            )
            conn.close()

        self.assertEqual(set(settlements), {("batched-market", "2026-05-05")})
        self.assertEqual(settlements[("batched-market", "2026-05-05")]["settlement_actual"], 72.0)
        self.assertEqual(settlements[("batched-market", "2026-05-05")]["rounding_rule"], "newer-id")

    def test_load_event_settlement_presence_batches_without_market_closed_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "archive.db"
            conn = self._create_base_db(db_path)
            conn.executemany(
                """
                INSERT INTO event_runs(slug, event_date, settlement_actual, rounding_rule, created_at)
                VALUES (?, ?, ?, '', ?)
                """,
                [
                    ("presence-market", "2026-05-05", None, "2026-05-06T00:00:00+00:00"),
                    ("presence-market", "2026-05-05", 81, "2026-05-06T01:00:00+00:00"),
                ],
            )
            conn.commit()
            conn.row_factory = sqlite3.Row

            presence = entry_counterfactual_review._load_event_settlement_presence(
                conn,
                [("presence-market", "2026-05-05"), ("missing-market", "2026-05-05")],
            )
            conn.close()

        self.assertEqual(presence[("presence-market", "2026-05-05")]["event_run_count"], 2)
        self.assertEqual(presence[("presence-market", "2026-05-05")]["settled_row_count"], 1)
        self.assertFalse(presence[("presence-market", "2026-05-05")]["market_closed"])
        self.assertEqual(presence[("presence-market", "2026-05-05")]["latest_created_at"], "2026-05-06T01:00:00+00:00")
        self.assertEqual(presence[("missing-market", "2026-05-05")]["event_run_count"], 0)

    def test_load_fills_batches_events_and_aggregates_requested_contracts(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "archive.db"
            conn = self._create_base_db(db_path)
            conn.executemany(
                """
                INSERT INTO edge_trade_fills(
                    slug, event_date, outcome, outcome_side, trade_side, price, size, order_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ("fill-market", "2026-05-05", "80°F or higher", "YES", "BUY", 0.40, 10, "buy-1"),
                    ("fill-market", "2026-05-05", "80°F or higher", "YES", "BUY", 0.50, 2, "buy-2"),
                    ("fill-market", "2026-05-05", "80°F or higher", "YES", "SELL", 0.60, 3, "sell-1"),
                    ("fill-market", "2026-05-05", "81°F or higher", "YES", "BUY", 0.70, 5, "other"),
                    ("fill-market", "2026-05-05", "80°F or higher", "YES", "BUY", 0.45, 0, "zero"),
                ],
            )
            conn.commit()
            conn.row_factory = sqlite3.Row

            fills = entry_counterfactual_review._load_fills(
                conn,
                [("fill-market", "2026-05-05T12:00:00+00:00", "80°F or higher", "yes")],
            )
            conn.close()

        fill = fills[("fill-market", "2026-05-05", "80°F or higher", "YES")]
        self.assertEqual(fill["buy_qty"], 12.0)
        self.assertAlmostEqual(fill["buy_avg_price"], (0.40 * 10 + 0.50 * 2) / 12)
        self.assertEqual(fill["sell_qty"], 3.0)
        self.assertAlmostEqual(fill["sell_avg_price"], 0.60)
        self.assertEqual(fill["order_ids"], ["buy-1", "buy-2", "sell-1"])

    def test_counterfactual_answers_cover_four_main_questions(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "archive.db"
            conn = self._create_base_db(db_path)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")

            # Five bought candidates later lose, making the strategy/model quality answer yes.
            for idx in range(5):
                slug = f"bought-loser-{idx}"
                conn.execute(
                    """
                    INSERT INTO auto_trade_decisions(
                        run_id, run_date, slug, event_date, title, outcome, side,
                        action, decided_state, will_order, reason_codes_json,
                        reasons_json, details_json, created_at
                    )
                    VALUES (?, '2026-05-05', ?, '2026-05-05',
                            'Highest temperature in Test?', '80°F or higher', 'YES',
                            'entry', 'entry_live', 1, '[]', '[]', ?, ?)
                    """,
                    (idx + 1, slug, json.dumps({"entry_strategy_family": "repricing_bucket"}), now),
                )
                conn.execute(
                    """
                    INSERT INTO event_runs(slug, event_date, settlement_actual, rounding_rule, created_at)
                    VALUES (?, '2026-05-05', 70, '', ?)
                    """,
                    (slug, now),
                )
                conn.execute(
                    """
                    INSERT INTO edge_trade_fills(slug, event_date, outcome, outcome_side, trade_side, price, size, order_id)
                    VALUES (?, '2026-05-05', '80°F or higher', 'YES', 'BUY', 0.50, 10, ?)
                    """,
                    (slug, f"buy-{idx}"),
                )

            # Five rejected candidates later win with non-positive edge / zero size scale.
            for idx in range(5):
                slug = f"missed-winner-{idx}"
                details = {
                    "entry_strategy_family": "repricing_bucket",
                    "entry_usable_edge": -0.01,
                    "entry_usable_edge_size_scale": 0.0,
                }
                conn.execute(
                    """
                    INSERT INTO auto_trade_decisions(
                        run_id, run_date, slug, event_date, title, outcome, side,
                        action, decided_state, will_order, reason_codes_json,
                        reasons_json, details_json, created_at
                    )
                    VALUES (?, '2026-05-05', ?, '2026-05-05',
                            'Highest temperature in Test?', '80°F or higher', 'YES',
                            'entry', 'blocked', 0, '[]', '[]', ?, ?)
                    """,
                    (idx + 20, slug, json.dumps(details), now),
                )
                conn.execute(
                    """
                    INSERT INTO event_runs(slug, event_date, settlement_actual, rounding_rule, created_at)
                    VALUES (?, '2026-05-05', 85, '', ?)
                    """,
                    (slug, now),
                )

            # Mapping risk is reported from source/rules NA evidence.
            conn.execute(
                """
                INSERT INTO auto_trade_decisions(
                    run_id, run_date, slug, event_date, title, outcome, side,
                    action, decided_state, will_order, reason_codes_json,
                    reasons_json, details_json, created_at
                )
                VALUES (99, '2026-05-05', 'mapping-risk', '2026-05-05',
                        'Highest temperature in Test?', '80°F or higher', 'YES',
                        'entry', 'blocked', 0, '[]', '[]', ?, ?)
                """,
                (json.dumps({"source_fit_na_reason": "station_mapping_missing"}), now),
            )
            conn.commit()
            conn.close()

            report = entry_counterfactual_review.build_report(db_path, days=1)

        answers = report["counterfactual_answers"]["overall"]
        self.assertEqual(answers["is_strategy_wrong"]["answer"], "yes")
        self.assertEqual(answers["are_thresholds_too_strict"]["answer"], "yes")
        self.assertEqual(answers["is_exit_difficult"]["answer"], "yes")
        self.assertEqual(answers["is_model_or_station_mapping_wrong"]["answer"], "yes")
        by_strategy = report["counterfactual_answers"]["by_strategy"]
        self.assertEqual(
            by_strategy["repricing_bucket"]["is_strategy_wrong"]["answer"],
            "yes",
        )
        self.assertEqual(
            by_strategy["repricing_bucket"]["are_thresholds_too_strict"]["answer"],
            "yes",
        )
        summary = report["actionable_summary"]
        self.assertEqual(
            summary["overall"]["primary_diagnosis"],
            "model_or_station_mapping_gap",
        )
        self.assertEqual(
            summary["overall"]["recommended_action"],
            "repair_station_or_contract_mapping",
        )
        self.assertEqual(
            summary["by_strategy"]["repricing_bucket"]["primary_diagnosis"],
            "exit_execution_evidence_gap",
        )
        self.assertEqual(
            summary["by_strategy"]["repricing_bucket"]["next_mainline_task"],
            "repricing_bucket_execution_evidence_chain",
        )
        self.assertEqual(
            summary["by_strategy"]["repricing_bucket"]["primary_question"],
            "is_exit_difficult",
        )
        self.assertIn("primary_diagnosis", summary["field_contract"])

    def test_actionable_summary_prioritizes_diagnostics(self):
        answers = {
            "overall": {
                "is_strategy_wrong": {"answer": "yes", "confidence": "high"},
                "are_thresholds_too_strict": {"answer": "yes", "confidence": "high"},
                "is_exit_difficult": {"answer": "yes", "confidence": "medium"},
                "is_model_or_station_mapping_wrong": {"answer": "yes", "confidence": "medium"},
            },
            "by_strategy": {
                "exit_first": {
                    "is_strategy_wrong": {"answer": "yes", "confidence": "high"},
                    "are_thresholds_too_strict": {"answer": "yes", "confidence": "medium"},
                    "is_exit_difficult": {"answer": "yes", "confidence": "high"},
                    "is_model_or_station_mapping_wrong": {"answer": "no", "confidence": "high"},
                },
                "threshold_first": {
                    "is_strategy_wrong": {"answer": "yes", "confidence": "high"},
                    "are_thresholds_too_strict": {"answer": "yes", "confidence": "medium"},
                    "is_exit_difficult": {"answer": "no", "confidence": "high"},
                    "is_model_or_station_mapping_wrong": {"answer": "no", "confidence": "high"},
                },
                "quality_only": {
                    "is_strategy_wrong": {"answer": "yes", "confidence": "high"},
                    "are_thresholds_too_strict": {"answer": "no", "confidence": "high"},
                    "is_exit_difficult": {"answer": "no", "confidence": "high"},
                    "is_model_or_station_mapping_wrong": {"answer": "no", "confidence": "high"},
                },
                "clean": {
                    "is_strategy_wrong": {"answer": "no", "confidence": "high"},
                    "are_thresholds_too_strict": {"answer": "no", "confidence": "high"},
                    "is_exit_difficult": {"answer": "no", "confidence": "high"},
                    "is_model_or_station_mapping_wrong": {"answer": "no", "confidence": "high"},
                },
            },
        }
        kpis = {
            "overall": {
                "strategy": "overall",
                "candidate_count": 10,
                "station_missing_count": 1,
            },
            "by_strategy": {
                "exit_first": {
                    "strategy": "exit_first",
                    "candidate_count": 3,
                    "repricing_candidate_count": 3,
                    "repricing_evidence_missing_count": 2,
                    "repricing_evidence_missing_rate": 2 / 3,
                },
                "threshold_first": {
                    "strategy": "threshold_first",
                    "candidate_count": 4,
                    "missed_winner_count": 3,
                    "missed_winner_rate": 0.75,
                },
                "quality_only": {
                    "strategy": "quality_only",
                    "candidate_count": 5,
                    "false_positive_count": 4,
                    "false_positive_rate": 0.80,
                },
                "clean": {
                    "strategy": "clean",
                    "candidate_count": 6,
                },
            },
        }

        summary = entry_counterfactual_review._build_actionable_summary(answers, kpis)

        self.assertEqual(
            summary["overall"]["primary_diagnosis"],
            "model_or_station_mapping_gap",
        )
        self.assertEqual(
            summary["by_strategy"]["exit_first"]["primary_diagnosis"],
            "exit_execution_evidence_gap",
        )
        self.assertEqual(
            summary["by_strategy"]["threshold_first"]["primary_diagnosis"],
            "threshold_or_soft_penalty_too_strict",
        )
        self.assertEqual(
            summary["by_strategy"]["quality_only"]["primary_diagnosis"],
            "strategy_or_model_quality_risk",
        )
        self.assertEqual(
            summary["by_strategy"]["clean"]["primary_diagnosis"],
            "no_actionable_issue_detected",
        )
        self.assertEqual(
            [row["strategy"] for row in summary["rows"]],
            ["exit_first", "threshold_first", "quality_only", "clean"],
        )

    def test_operational_source_fit_timeout_is_not_station_mapping_risk(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "archive.db"
            conn = self._create_base_db(db_path)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")

            for idx in range(5):
                details = {
                    "entry_strategy_family": "repricing_bucket",
                    "source_fit_na_reason": "event_worker_timeout",
                    "entry_usable_edge_na_reason": "event_worker_timeout",
                }
                conn.execute(
                    """
                    INSERT INTO auto_trade_decisions(
                        run_id, run_date, slug, event_date, title, outcome, side,
                        action, decided_state, will_order, reason_codes_json,
                        reasons_json, details_json, created_at
                    )
                    VALUES (?, '2026-05-05', ?, '2026-05-05',
                            'Highest temperature in Test?', '80°F or higher', 'YES',
                            'entry', 'blocked', 0, '[]', '[]', ?, ?)
                    """,
                    (idx + 1, f"timeout-{idx}", json.dumps(details), now),
                )
            conn.commit()
            conn.close()

            report = entry_counterfactual_review.build_report(db_path, days=1)

        mapping_answer = report["counterfactual_answers"]["overall"][
            "is_model_or_station_mapping_wrong"
        ]
        self.assertEqual(mapping_answer["answer"], "no")
        self.assertEqual(mapping_answer["station_missing_count"], 0)
        self.assertNotEqual(
            report["actionable_summary"]["overall"]["primary_diagnosis"],
            "model_or_station_mapping_gap",
        )

    def test_rule_attribution_flags_false_reject_and_protective_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "archive.db"
            conn = self._create_base_db(db_path)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            rows = [
                (1, "spread-win", ["spread_too_wide"], 85, 0.02),
                (2, "spread-lose", ["spread_too_wide"], 75, 0.01),
                (3, "skip-win", ["plain_lcb_pinned_tail_no_filtered"], 86, 0.03),
                (4, "skip-win-2", ["plain_lcb_pinned_tail_no_filtered"], 87, 0.04),
                (5, "contract-win", ["contract_rule_missing"], 88, 0.05),
            ]
            for run_id, slug, codes, settlement_actual, usable_edge in rows:
                conn.execute(
                    """
                    INSERT INTO auto_trade_decisions(
                        run_id, run_date, slug, event_date, title, outcome, side,
                        action, decided_state, will_order, reason_codes_json,
                        reasons_json, details_json, created_at
                    )
                    VALUES (?, '2026-05-05', ?, '2026-05-05',
                            'Highest temperature in Test?', '80°F or higher', 'YES',
                            'entry', 'blocked', 0, ?, '[]', ?, ?)
                    """,
                    (
                        run_id,
                        slug,
                        json.dumps(codes),
                        json.dumps(
                            {
                                "entry_strategy_family": "repricing_bucket",
                                "entry_usable_edge": usable_edge,
                                "q_entry": 0.55,
                                "ask": 0.50,
                            }
                        ),
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO event_runs(slug, event_date, settlement_actual, rounding_rule, created_at)
                    VALUES (?, '2026-05-05', ?, '', ?)
                    """,
                    (slug, settlement_actual, now),
                )
            conn.commit()
            conn.close()

            report = entry_counterfactual_review.build_report(db_path, days=1)

        attribution = report["rule_attribution"]["overall"]
        rows_by_code = {row["rule_code"]: row for row in attribution["rows"]}
        self.assertEqual(rows_by_code["spread_too_wide"]["rule_type"], "soft_haircut")
        self.assertAlmostEqual(rows_by_code["spread_too_wide"]["false_reject_rate"], 0.5)
        self.assertAlmostEqual(rows_by_code["spread_too_wide"]["true_block_rate"], 0.5)
        self.assertEqual(
            rows_by_code["plain_lcb_pinned_tail_no_filtered"]["recommended_action"],
            "soften_or_prune_candidate",
        )
        self.assertIn(
            "plain_lcb_pinned_tail_no_filtered",
            [row["rule_code"] for row in attribution["pruning_candidates"]],
        )
        self.assertEqual(rows_by_code["contract_rule_missing"]["rule_type"], "hard_invariant")
        self.assertEqual(rows_by_code["contract_rule_missing"]["recommended_action"], "keep_hard_block")
        self.assertNotIn(
            "contract_rule_missing",
            [row["rule_code"] for row in attribution["pruning_candidates"]],
        )
        by_strategy = report["rule_attribution"]["by_strategy"]["repricing_bucket"]
        self.assertIn("field_contract", by_strategy)
        self.assertFalse(attribution["sample_progress"]["ready_for_rule_pruning_review"])
        self.assertEqual(
            report["rule_attribution"]["next_mainline_task"],
            "settled_counterfactual_sample_accrual",
        )

    def test_rule_attribution_sample_progress_rolls_to_pruning_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "archive.db"
            conn = self._create_base_db(db_path)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for idx in range(20):
                slug = f"spread-settled-{idx}"
                settlement_actual = 85 if idx < 8 else 75
                conn.execute(
                    """
                    INSERT INTO auto_trade_decisions(
                        run_id, run_date, slug, event_date, title, outcome, side,
                        action, decided_state, will_order, reason_codes_json,
                        reasons_json, details_json, created_at
                    )
                    VALUES (?, '2026-05-05', ?, '2026-05-05',
                            'Highest temperature in Test?', '80°F or higher', 'YES',
                            'entry', 'blocked', 0, ?, '[]', ?, ?)
                    """,
                    (
                        idx + 1,
                        slug,
                        json.dumps(["spread_too_wide"]),
                        json.dumps(
                            {
                                "entry_strategy_family": "repricing_bucket",
                                "entry_usable_edge": 0.01,
                            }
                        ),
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO event_runs(slug, event_date, settlement_actual, rounding_rule, created_at)
                    VALUES (?, '2026-05-05', ?, '', ?)
                    """,
                    (slug, settlement_actual, now),
                )
            conn.commit()
            conn.close()

            report = entry_counterfactual_review.build_report(db_path, days=1)

        progress = report["rule_attribution"]["overall"]["sample_progress"]
        self.assertTrue(progress["ready_for_rule_pruning_review"])
        self.assertEqual(progress["settled_blocked_count"], 20)
        self.assertEqual(progress["evaluable_rule_count"], 1)
        self.assertEqual(progress["next_mainline_task"], "rule_pruning_review_v1")
        self.assertEqual(report["rule_attribution"]["next_mainline_task"], "rule_pruning_review_v1")

    def test_rule_pruning_review_separates_active_and_historical_false_rejects(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "archive.db"
            conn = self._create_base_db(db_path)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for idx in range(20):
                slug = f"tail-filter-{idx}"
                conn.execute(
                    """
                    INSERT INTO auto_trade_decisions(
                        run_id, run_date, slug, event_date, title, outcome, side,
                        action, decided_state, will_order, reason_codes_json,
                        reasons_json, details_json, created_at
                    )
                    VALUES (?, '2026-05-05', ?, '2026-05-05',
                            'Highest temperature in Test?', '80°F or higher', 'YES',
                            'entry', 'blocked', 0, ?, '[]', ?, ?)
                    """,
                    (
                        idx + 1,
                        slug,
                        json.dumps(["plain_lcb_pinned_tail_no_filtered"]),
                        json.dumps({"entry_strategy_family": "repricing_bucket"}),
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO event_runs(slug, event_date, settlement_actual, rounding_rule, created_at)
                    VALUES (?, '2026-05-05', 85, '', ?)
                    """,
                    (slug, now),
                )
            conn.execute(
                """
                INSERT INTO auto_trade_decisions(
                    run_id, run_date, slug, event_date, title, outcome, side,
                    action, decided_state, will_order, reason_codes_json,
                    reasons_json, details_json, created_at
                )
                VALUES (999, '2026-05-05', 'latest-clean', '2026-05-05',
                        'Highest temperature in Test?', '80°F or higher', 'YES',
                        'entry', 'blocked', 0, '[]', '[]', ?, ?)
                """,
                (json.dumps({"entry_strategy_family": "repricing_bucket"}), now),
            )
            conn.commit()
            conn.close()

            report = entry_counterfactual_review.build_report(db_path, days=1)

        review = report["rule_pruning_review"]
        self.assertEqual(review["next_mainline_task"], "rolling_validation")
        self.assertEqual(review["active_prune_or_soften"], [])
        residual_codes = [row["rule_code"] for row in review["historical_false_reject_residual"]]
        self.assertIn("plain_lcb_pinned_tail_no_filtered", residual_codes)
        residual = {
            row["rule_code"]: row
            for row in review["historical_false_reject_residual"]
        }["plain_lcb_pinned_tail_no_filtered"]
        self.assertFalse(residual["current_run_active"])
        self.assertEqual(residual["current_run_reason_count"], 0)

    def test_rule_pruning_review_does_not_promote_inactive_threshold_residuals(self):
        rows = [
            {
                "rule_code": "edge_tradeability_blocked",
                "rule_type": "alpha_policy_gate",
                "recommended_action": "review_threshold_or_haircut_strength",
                "settled_blocked_count": 20,
                "later_winner_count": 9,
                "false_reject_rate": 0.45,
                "true_block_rate": 0.55,
            }
        ]
        review = entry_counterfactual_review._build_rule_pruning_review(
            {
                "overall": {
                    "rows": rows,
                    "sample_progress": {
                        "ready_for_rule_pruning_review": True,
                        "settled_blocked_count": 20,
                    },
                }
            },
            current_runtime={
                "run_id": 7,
                "overall": {"reason_code_counts": {"order_submitted": 1}},
            },
        )

        self.assertEqual(review["threshold_or_haircut_review"], [])
        self.assertEqual(review["next_mainline_task"], "rolling_validation")
        self.assertEqual(
            review["historical_threshold_or_haircut_residual"][0]["rule_code"],
            "edge_tradeability_blocked",
        )

    def test_rule_type_classifies_sort_missing_as_diagnostic(self):
        self.assertEqual(
            entry_counterfactual_review._rule_type_for_code("edge_realizable_sort_missing"),
            "diagnostic_or_sort_missing",
        )

    def test_rule_pruning_review_does_not_promote_inactive_wrapper_residuals(self):
        rows = [
            {
                "rule_code": "entry_gate_blocked",
                "rule_type": "wrapper_or_missing_reason",
                "recommended_action": "keep_rule_currently_protective",
                "settled_blocked_count": 20,
                "later_winner_count": 4,
                "false_reject_rate": 0.2,
                "true_block_rate": 0.8,
            }
        ]
        review = entry_counterfactual_review._build_rule_pruning_review(
            {
                "overall": {
                    "rows": rows,
                    "sample_progress": {
                        "ready_for_rule_pruning_review": True,
                        "settled_blocked_count": 20,
                    },
                }
            },
            current_runtime={
                "run_id": 7,
                "overall": {"reason_code_counts": {"order_submitted": 1}},
            },
        )

        self.assertEqual(review["wrapper_reason_cleanup"], [])
        self.assertEqual(review["next_mainline_task"], "rolling_validation")
        self.assertEqual(
            review["historical_wrapper_residual"][0]["rule_code"],
            "entry_gate_blocked",
        )

    def test_rule_pruning_review_flags_active_false_reject_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "archive.db"
            conn = self._create_base_db(db_path)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for idx in range(20):
                slug = f"active-spread-{idx}"
                conn.execute(
                    """
                    INSERT INTO auto_trade_decisions(
                        run_id, run_date, slug, event_date, title, outcome, side,
                        action, decided_state, will_order, reason_codes_json,
                        reasons_json, details_json, created_at
                    )
                    VALUES (?, '2026-05-05', ?, '2026-05-05',
                            'Highest temperature in Test?', '80°F or higher', 'YES',
                            'entry', 'blocked', 0, ?, '[]', ?, ?)
                    """,
                    (
                        idx + 1,
                        slug,
                        json.dumps(["spread_too_wide"]),
                        json.dumps({"entry_strategy_family": "repricing_bucket"}),
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO event_runs(slug, event_date, settlement_actual, rounding_rule, created_at)
                    VALUES (?, '2026-05-05', 85, '', ?)
                    """,
                    (slug, now),
                )
            conn.execute(
                """
                INSERT INTO auto_trade_decisions(
                    run_id, run_date, slug, event_date, title, outcome, side,
                    action, decided_state, will_order, reason_codes_json,
                    reasons_json, details_json, created_at
                )
                VALUES (999, '2026-05-05', 'latest-active', '2026-05-05',
                        'Highest temperature in Test?', '80°F or higher', 'YES',
                        'entry', 'blocked', 0, ?, '[]', ?, ?)
                """,
                (
                    json.dumps(["spread_too_wide"]),
                    json.dumps({"entry_strategy_family": "repricing_bucket"}),
                    now,
                ),
            )
            conn.commit()
            conn.close()

            report = entry_counterfactual_review.build_report(db_path, days=1)

        review = report["rule_pruning_review"]
        self.assertEqual(review["next_mainline_task"], "active_rule_prune_or_soften")
        active_codes = [row["rule_code"] for row in review["active_prune_or_soften"]]
        self.assertIn("spread_too_wide", active_codes)
        active = {row["rule_code"]: row for row in review["active_prune_or_soften"]}[
            "spread_too_wide"
        ]
        self.assertTrue(active["current_run_active"])
        self.assertEqual(active["current_run_reason_count"], 1)

    def test_settlement_coverage_reports_missing_settlement_for_eligible_rejects(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "archive.db"
            conn = self._create_base_db(db_path)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            old_event_date = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
            conn.execute(
                """
                INSERT INTO auto_trade_decisions(
                    run_id, run_date, slug, event_date, title, outcome, side,
                    action, decided_state, will_order, reason_codes_json,
                    reasons_json, details_json, created_at
                )
                VALUES (1, ?, 'missing-settlement', ?,
                        'Highest temperature in Test?', '80°F or higher', 'YES',
                        'entry', 'blocked', 0, ?, '[]', ?, ?)
                """,
                (
                    old_event_date,
                    old_event_date,
                    json.dumps(["spread_too_wide"]),
                    json.dumps({"entry_strategy_family": "repricing_bucket"}),
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO event_runs(slug, event_date, settlement_actual, rounding_rule, created_at, event_end_utc)
                VALUES ('missing-settlement', ?, NULL, '', ?, ?)
                """,
                (
                    old_event_date,
                    now,
                    (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds"),
                ),
            )
            conn.commit()
            conn.close()

            report = entry_counterfactual_review.build_report(db_path, days=3)

        coverage = report["settlement_coverage"]
        self.assertFalse(coverage["ok"])
        self.assertEqual(coverage["settlement_eligible_rejected_count"], 1)
        self.assertEqual(coverage["event_run_present_count"], 1)
        self.assertEqual(coverage["settlement_missing_count"], 1)
        self.assertEqual(coverage["next_mainline_task"], "settlement_fact_capture_gap_review")
        self.assertEqual(coverage["primary_missing_bucket"], "latest_event_run_before_event_end")
        self.assertEqual(
            coverage["settlement_missing_bucket_top"],
            [{"key": "latest_event_run_before_event_end", "count": 1}],
        )
        self.assertEqual(
            coverage["settlement_missing_examples"][0]["missing_bucket"],
            "latest_event_run_before_event_end",
        )
        self.assertEqual(
            coverage["settlement_missing_examples"][0]["slug"],
            "missing-settlement",
        )

    def test_settlement_coverage_passes_when_eligible_rejects_have_settlement(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "archive.db"
            conn = self._create_base_db(db_path)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            old_event_date = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
            conn.execute(
                """
                INSERT INTO auto_trade_decisions(
                    run_id, run_date, slug, event_date, title, outcome, side,
                    action, decided_state, will_order, reason_codes_json,
                    reasons_json, details_json, created_at
                )
                VALUES (1, ?, 'covered-settlement', ?,
                        'Highest temperature in Test?', '80°F or higher', 'YES',
                        'entry', 'blocked', 0, ?, '[]', ?, ?)
                """,
                (
                    old_event_date,
                    old_event_date,
                    json.dumps(["spread_too_wide"]),
                    json.dumps({"entry_strategy_family": "repricing_bucket"}),
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO event_runs(slug, event_date, settlement_actual, rounding_rule, created_at)
                VALUES ('covered-settlement', ?, 81, '', ?)
                """,
                (old_event_date, now),
            )
            conn.commit()
            conn.close()

            report = entry_counterfactual_review.build_report(db_path, days=3)

        coverage = report["settlement_coverage"]
        self.assertTrue(coverage["ok"])
        self.assertEqual(coverage["settlement_eligible_rejected_count"], 1)
        self.assertEqual(coverage["settlement_present_count"], 1)
        self.assertEqual(coverage["settlement_missing_count"], 0)

    def test_false_edge_friction_missed_winner_is_attributed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "archive.db"
            conn = sqlite3.connect(db_path)
            conn.execute(
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
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE event_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slug TEXT,
                    event_date TEXT,
                    settlement_actual REAL,
                    rounding_rule TEXT,
                    created_at TEXT
                )
                """
            )
            conn.execute(
                """
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
                )
                """
            )
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            details = {
                "strategy": "lcb_global",
                "entry_usable_edge": -0.0075,
                "entry_usable_edge_size_scale": 0.0,
                "entry_edge_false_edge": True,
                "entry_edge_false_edge_type": "target_exit_does_not_cover_friction",
                "entry_edge_root_cause": "repricing_target_surplus_below_spread_cost_floor",
                "entry_edge_entry_surplus": 0.18,
                "entry_edge_target_exit_surplus": 0.0075,
                "entry_edge_repricing_friction_floor": 0.015,
                "entry_edge_target_surplus_to_friction": -0.0075,
            }
            conn.execute(
                """
                INSERT INTO auto_trade_decisions(
                    run_id, run_date, slug, event_date, title, outcome, side,
                    action, decided_state, will_order, reason_codes_json,
                    reasons_json, details_json, created_at
                )
                VALUES (1, '2026-05-05', 'test-market', '2026-05-05',
                        'Highest temperature in Test?', '80°F or higher', 'YES',
                        'entry', 'blocked', 0, '[]', '[]', ?, ?)
                """,
                (json.dumps(details), now),
            )
            conn.execute(
                """
                INSERT INTO event_runs(slug, event_date, settlement_actual, rounding_rule, created_at)
                VALUES ('test-market', '2026-05-05', 81, '', ?)
                """,
                (now,),
            )
            conn.commit()
            conn.close()

            report = entry_counterfactual_review.build_report(db_path, days=1)

        overall = report["overall"]
        self.assertEqual(overall["entry_false_edge_count"], 1)
        self.assertEqual(overall["entry_false_edge_missed_winner_count"], 1)
        self.assertEqual(overall["friction_false_edge_count"], 1)
        self.assertEqual(overall["friction_false_edge_missed_winner_count"], 1)
        self.assertEqual(
            overall["entry_false_edge_type_top"][0]["key"],
            "target_exit_does_not_cover_friction",
        )
        self.assertEqual(
            report["examples"]["friction_false_edge_missed_winners"][0][
                "entry_edge_target_surplus_to_friction"
            ],
            -0.0075,
        )

    def test_missing_entry_usable_edge_is_split_between_na_and_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "archive.db"
            conn = sqlite3.connect(db_path)
            conn.execute(
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
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE event_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slug TEXT,
                    event_date TEXT,
                    settlement_actual REAL,
                    rounding_rule TEXT,
                    created_at TEXT
                )
                """
            )
            conn.execute(
                """
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
                )
                """
            )
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            rows = [
                (
                    "na-market",
                    "80°F or higher",
                    {
                        "entry_usable_edge_na_reason": "edge_tradeability_blocked:edge_lcb_below_min",
                        "exec_edge_entry": -0.02,
                        "q_entry": 0.97,
                        "q_quote": 0.98,
                        "ask": 0.99,
                        "order_price": 0.985,
                        "edge_tradeability_profit_value": -0.015,
                        "edge_tradeability_edge_lcb": -0.02,
                        "edge_realizable": -0.01,
                        "edge_realizable_score": -0.005,
                    },
                ),
                ("legacy-market", "81°F or higher", {}),
            ]
            for slug, outcome, details in rows:
                conn.execute(
                    """
                    INSERT INTO auto_trade_decisions(
                        run_id, run_date, slug, event_date, title, outcome, side,
                        action, decided_state, will_order, reason_codes_json,
                        reasons_json, details_json, created_at
                    )
                    VALUES (1, '2026-05-05', ?, '2026-05-05',
                            'Highest temperature in Test?', ?, 'YES',
                            'entry', 'blocked', 0, '[]', '[]', ?, ?)
                    """,
                    (slug, outcome, json.dumps(details), now),
                )
            conn.commit()
            conn.close()

            report = entry_counterfactual_review.build_report(db_path, days=1)

        overall = report["overall"]
        self.assertEqual(overall["entry_usable_edge_missing_count"], 2)
        self.assertEqual(overall["entry_usable_edge_na_reason_present_count"], 1)
        self.assertEqual(overall["entry_usable_edge_silent_missing_count"], 1)
        self.assertEqual(
            overall["entry_usable_edge_na_reason_top"][0]["key"],
            "edge_tradeability_blocked:edge_lcb_below_min",
        )
        self.assertEqual(
            overall["entry_usable_edge_na_reason_breakdown"][0]["key"],
            "edge_tradeability_blocked:edge_lcb_below_min",
        )
        self.assertEqual(
            overall["entry_usable_edge_na_reason_breakdown"][0]["candidate_count"],
            1,
        )
        self.assertAlmostEqual(
            overall["entry_usable_edge_na_reason_breakdown"][0]["avg_exec_edge_entry"],
            -0.02,
        )
        self.assertAlmostEqual(
            overall["entry_usable_edge_na_reason_breakdown"][0]["avg_ask"],
            0.99,
        )
        self.assertAlmostEqual(
            overall["entry_usable_edge_na_reason_breakdown"][0]["avg_q_minus_ask"],
            -0.02,
        )
        self.assertAlmostEqual(
            overall["entry_usable_edge_na_reason_breakdown"][0][
                "avg_q_minus_order_price"
            ],
            -0.015,
        )
        self.assertAlmostEqual(
            overall["entry_usable_edge_na_reason_breakdown"][0][
                "avg_q_quote_minus_ask"
            ],
            -0.01,
        )
        self.assertAlmostEqual(
            overall["entry_usable_edge_na_reason_breakdown"][0][
                "avg_edge_tradeability_profit_value"
            ],
            -0.015,
        )

    def test_latest_reason_code_breakdown_uses_latest_candidate_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "archive.db"
            conn = sqlite3.connect(db_path)
            conn.execute(
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
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE event_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slug TEXT,
                    event_date TEXT,
                    settlement_actual REAL,
                    rounding_rule TEXT,
                    created_at TEXT
                )
                """
            )
            conn.execute(
                """
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
                )
                """
            )
            details = {"exec_edge_entry": 0.10, "q_entry": 0.20, "ask": 0.10}
            first_at = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(
                timespec="seconds"
            )
            second_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(
                timespec="seconds"
            )
            conn.execute(
                """
                INSERT INTO auto_trade_decisions(
                    run_id, run_date, slug, event_date, title, outcome, side,
                    action, decided_state, will_order, reason_codes_json,
                    reasons_json, details_json, created_at
                )
                VALUES (1, '2026-05-05', 'test-market', '2026-05-05',
                        'Highest temperature in Test?', '80°F or higher', 'YES',
                        'entry', 'blocked', 0, '["entry_gate_blocked"]', '[]', ?, ?)
                """,
                (json.dumps(details), first_at),
            )
            conn.execute(
                """
                INSERT INTO auto_trade_decisions(
                    run_id, run_date, slug, event_date, title, outcome, side,
                    action, decided_state, will_order, reason_codes_json,
                    reasons_json, details_json, created_at
                )
                VALUES (2, '2026-05-05', 'test-market', '2026-05-05',
                        'Highest temperature in Test?', '80°F or higher', 'YES',
                        'entry', 'entry_dry_run', 1, '["order_submitted"]', '[]', ?, ?)
                """,
                (json.dumps(details), second_at),
            )
            conn.commit()
            conn.close()

            report = entry_counterfactual_review.build_report(db_path, days=1)

        latest = {
            row["key"]: row
            for row in report["overall"]["latest_reason_code_breakdown"]
        }
        historical = {
            row["key"]: row
            for row in report["overall"]["reason_code_breakdown"]
        }
        self.assertIn("entry_gate_blocked", historical)
        self.assertNotIn("entry_gate_blocked", latest)
        self.assertEqual(latest["order_submitted"]["will_order_count"], 1)

    def test_strategy_kpis_expose_daily_tuning_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "archive.db"
            conn = self._create_base_db(db_path)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")

            conn.execute(
                """
                INSERT INTO auto_trade_decisions(
                    run_id, run_date, slug, event_date, title, outcome, side,
                    action, decided_state, will_order, reason_codes_json,
                    reasons_json, details_json, created_at
                )
                VALUES (1, '2026-05-05', 'filled-low-price', '2026-05-05',
                        'Highest temperature in Test?', '80°F or higher', 'YES',
                        'entry', 'entry_live', 1, '[]', '[]', ?, ?)
                """,
                (
                    json.dumps(
                        {
                            "entry_strategy_family": "repricing_bucket",
                            "entry_usable_edge_low_price_bucket": True,
                            "entry_usable_edge_expected_exit_bid": 0.45,
                        }
                    ),
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO edge_trade_fills(slug, event_date, outcome, outcome_side, trade_side, price, size, order_id)
                VALUES ('filled-low-price', '2026-05-05', '80°F or higher', 'YES', 'BUY', 0.40, 10, 'buy-1')
                """
            )
            conn.execute(
                """
                INSERT INTO edge_trade_fills(slug, event_date, outcome, outcome_side, trade_side, price, size, order_id)
                VALUES ('filled-low-price', '2026-05-05', '80°F or higher', 'YES', 'SELL', 0.43, 10, 'sell-1')
                """
            )
            conn.execute(
                """
                INSERT INTO event_runs(slug, event_date, settlement_actual, rounding_rule, created_at)
                VALUES ('filled-low-price', '2026-05-05', 70, '', ?)
                """,
                (now,),
            )

            conn.execute(
                """
                INSERT INTO auto_trade_decisions(
                    run_id, run_date, slug, event_date, title, outcome, side,
                    action, decided_state, will_order, reason_codes_json,
                    reasons_json, details_json, created_at
                )
                VALUES (2, '2026-05-05', 'soft-missed', '2026-05-05',
                        'Highest temperature in Test?', '81°F or higher', 'YES',
                        'entry', 'blocked', 0, '[]', '[]', ?, ?)
                """,
                (
                    json.dumps(
                        {
                            "entry_strategy_family": "repricing_bucket",
                            "entry_usable_edge_size_scale": 0.5,
                        }
                    ),
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO event_runs(slug, event_date, settlement_actual, rounding_rule, created_at)
                VALUES ('soft-missed', '2026-05-05', 82, '', ?)
                """,
                (now,),
            )
            conn.commit()
            conn.close()

            report = entry_counterfactual_review.build_report(db_path, days=1)

        kpis = report["strategy_kpis"]
        selfEqual = self.assertEqual
        selfEqual(
            kpis["field_contract"],
            [
                "missed_winner_rate",
                "false_positive_rate",
                "exit_fill_rate",
                "avg_exit_slippage",
                "low_price_hit_rate",
                "low_price_exit_success_rate",
                "soft_penalty_hit_rate",
                "soft_penalty_missed_winner_rate",
                "soft_penalty_exit_fill_rate",
                "repricing_evidence_missing_rate",
                "repricing_evidence_component_missing_rate",
                "repricing_evidence_default_only_rate",
                "repricing_evidence_status_missing_rate",
                "repricing_evidence_status_default_only_rate",
                "repricing_evidence_missing_table_candidate_rate",
                "repricing_evidence_default_table_candidate_rate",
                "repricing_evidence_entry_missing_flag_rate",
                "repricing_evidence_realize_missing_flag_rate",
                "repricing_evidence_realize_default_flag_rate",
                "repricing_evidence_missing_residual_rate",
                "repricing_evidence_low_sample_rate",
                "repricing_evidence_unreported_component_rate",
                "repricing_evidence_all_bucket_evidence_rate",
                "repricing_probability_too_low_rate",
                "avg_repricing_p_realize",
                "avg_repricing_p_converge",
                "avg_repricing_p_fill_entry",
                "avg_repricing_p_fill_exit",
                "avg_repricing_evidence_scale",
                "avg_repricing_target_surplus_raw",
                "avg_repricing_target_surplus_discounted",
                "avg_repricing_target_surplus_discounted_to_friction",
                "avg_repricing_target_surplus_to_friction",
                "avg_repricing_friction_floor",
            ],
        )
        row = kpis["by_strategy"]["repricing_bucket"]
        selfEqual(row["missed_winner_count"], 1)
        self.assertAlmostEqual(row["missed_winner_rate"], 1.0)
        selfEqual(row["false_positive_count"], 1)
        self.assertAlmostEqual(row["false_positive_rate"], 1.0)
        self.assertAlmostEqual(row["exit_fill_rate"], 1.0)
        self.assertAlmostEqual(row["avg_exit_slippage"], 0.02)
        self.assertEqual(row["low_price_settled_count"], 1)
        self.assertEqual(row["low_price_hit_count"], 0)
        self.assertAlmostEqual(row["low_price_hit_rate"], 0.0)
        self.assertAlmostEqual(row["low_price_exit_success_rate"], 1.0)
        self.assertEqual(row["soft_penalty_rejected_settled_count"], 1)
        self.assertEqual(row["soft_penalty_rejected_hit_count"], 1)
        self.assertAlmostEqual(row["soft_penalty_hit_rate"], 1.0)
        self.assertAlmostEqual(row["soft_penalty_missed_winner_rate"], 1.0)
        self.assertIsNone(row["soft_penalty_exit_fill_rate"])

    def test_repricing_execution_evidence_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "archive.db"
            conn = self._create_base_db(db_path)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            rows = [
                (
                    1,
                    "missing-evidence",
                    "target_exit_execution_evidence_missing",
                    {
                        "entry_edge_repricing_exit_evidence_missing": True,
                        "entry_edge_repricing_exit_evidence_status": "default_only",
                        "entry_edge_repricing_exit_evidence_na_reason": "bucket_evidence_missing",
                        "entry_edge_repricing_exit_evidence_missing_reason": "calibration_bucket_missing",
                        "entry_edge_repricing_exit_evidence_probability_source": "p_converge=missing:regional_calibration;p_fill_entry=bucket_evidence;p_fill_exit=default_only:global_default",
                        "entry_edge_repricing_exit_evidence_missing_tables": "p_converge_buckets,p_fill_exit_buckets",
                        "entry_edge_repricing_exit_evidence_default_tables": "p_fill_exit_buckets",
                        "entry_edge_repricing_exit_evidence_bucket_samples_min": 18,
                        "entry_edge_repricing_exit_evidence_scale": 0.0,
                        "entry_edge_repricing_target_surplus_raw": 0.08,
                        "entry_edge_repricing_target_surplus_discounted": 0.0,
                        "entry_edge_repricing_friction_floor": 0.01,
                        "entry_edge_repricing_target_surplus_discounted_to_friction": -0.01,
                        "entry_edge_target_surplus_to_friction": 0.07,
                        "edge_realizable_p_realize": 0.50,
                        "edge_realizable_p_converge": 0.80,
                        "edge_realizable_p_converge_evidence_status": "missing",
                        "edge_realizable_p_converge_source": "regional_calibration",
                        "edge_realizable_p_converge_bucket_key": "conv-missing",
                        "edge_realizable_p_converge_bucket_used_key": "conv-missing-any",
                        "edge_realizable_p_fill_entry": 0.90,
                        "edge_realizable_p_fill_entry_evidence_status": "bucket_evidence",
                        "edge_realizable_p_fill_entry_source": "primary",
                        "edge_realizable_p_fill_entry_samples": 33,
                        "edge_realizable_p_fill_entry_bucket_key": "entry-ok",
                        "edge_realizable_p_fill_entry_bucket_used_key": "entry-ok",
                        "edge_realizable_p_fill_exit": 0.70,
                        "edge_realizable_p_fill_exit_evidence_status": "default_only",
                        "edge_realizable_p_fill_exit_source": "global_default",
                        "edge_realizable_p_fill_exit_bucket_key": "exit-default",
                    },
                ),
                (
                    2,
                    "low-sample-evidence",
                    "target_exit_execution_evidence_low_sample",
                    {
                        "entry_edge_repricing_exit_evidence_low_sample": True,
                        "entry_edge_repricing_exit_evidence_status": "low_sample",
                        "entry_edge_repricing_exit_evidence_low_sample_tables": "p_fill_entry_buckets,p_fill_exit_buckets",
                        "entry_edge_repricing_exit_evidence_probability_source": "p_converge=bucket_evidence:primary;p_fill_entry=low_sample:primary;p_fill_exit=low_sample:primary",
                        "entry_edge_repricing_exit_evidence_bucket_samples_min": 3,
                        "entry_edge_repricing_exit_evidence_scale": 0.05,
                        "entry_edge_repricing_target_surplus_raw": 0.06,
                        "entry_edge_repricing_target_surplus_discounted": 0.003,
                        "entry_edge_repricing_friction_floor": 0.007,
                        "entry_edge_repricing_target_surplus_discounted_to_friction": -0.004,
                        "entry_edge_target_surplus_to_friction": 0.053,
                        "edge_realizable_p_realize": 0.05,
                        "edge_realizable_p_converge": 0.25,
                        "edge_realizable_p_converge_evidence_status": "bucket_evidence",
                        "edge_realizable_p_converge_source": "primary",
                        "edge_realizable_p_converge_samples": 40,
                        "edge_realizable_p_converge_bucket_key": "conv-ok",
                        "edge_realizable_p_converge_bucket_used_key": "conv-ok",
                        "edge_realizable_p_fill_entry": 0.45,
                        "edge_realizable_p_fill_entry_evidence_status": "low_sample",
                        "edge_realizable_p_fill_entry_source": "primary",
                        "edge_realizable_p_fill_entry_samples": 3,
                        "edge_realizable_p_fill_entry_bucket_key": "entry-low",
                        "edge_realizable_p_fill_entry_bucket_used_key": "entry-low",
                        "edge_realizable_p_fill_exit": 0.20,
                        "edge_realizable_p_fill_exit_evidence_status": "low_sample",
                        "edge_realizable_p_fill_exit_source": "primary",
                        "edge_realizable_p_fill_exit_samples": 3,
                        "edge_realizable_p_fill_exit_bucket_key": "exit-low",
                        "edge_realizable_p_fill_exit_bucket_used_key": "exit-low",
                    },
                ),
                (
                    3,
                    "probability-low",
                    "target_exit_execution_probability_too_low",
                    {
                        "entry_edge_repricing_exit_evidence_status": "bucket_evidence",
                        "entry_edge_repricing_exit_evidence_probability_source": "p_converge=bucket_evidence:primary;p_fill_entry=bucket_evidence:primary;p_fill_exit=bucket_evidence:primary",
                        "entry_edge_repricing_exit_evidence_bucket_samples_min": 25,
                        "entry_edge_repricing_exit_evidence_scale": 0.04,
                        "entry_edge_repricing_target_surplus_raw": 0.05,
                        "entry_edge_repricing_target_surplus_discounted": 0.002,
                        "entry_edge_repricing_friction_floor": 0.006,
                        "entry_edge_repricing_target_surplus_discounted_to_friction": -0.004,
                        "entry_edge_target_surplus_to_friction": 0.044,
                        "edge_realizable_p_realize": 0.04,
                        "edge_realizable_p_converge": 0.40,
                        "edge_realizable_p_converge_evidence_status": "bucket_evidence",
                        "edge_realizable_p_converge_source": "primary",
                        "edge_realizable_p_converge_samples": 25,
                        "edge_realizable_p_converge_bucket_key": "conv-ok",
                        "edge_realizable_p_converge_bucket_used_key": "conv-ok",
                        "edge_realizable_p_fill_entry": 0.85,
                        "edge_realizable_p_fill_entry_evidence_status": "bucket_evidence",
                        "edge_realizable_p_fill_entry_source": "primary",
                        "edge_realizable_p_fill_entry_samples": 26,
                        "edge_realizable_p_fill_entry_bucket_key": "entry-ok",
                        "edge_realizable_p_fill_entry_bucket_used_key": "entry-ok",
                        "edge_realizable_p_fill_exit": 0.10,
                        "edge_realizable_p_fill_exit_evidence_status": "bucket_evidence",
                        "edge_realizable_p_fill_exit_source": "primary",
                        "edge_realizable_p_fill_exit_samples": 25,
                        "edge_realizable_p_fill_exit_bucket_key": "exit-ok",
                        "edge_realizable_p_fill_exit_bucket_used_key": "exit-ok",
                    },
                ),
                (
                    4,
                    "probability-low-hit",
                    "target_exit_execution_probability_too_low",
                    {
                        "entry_edge_repricing_exit_evidence_status": "bucket_evidence",
                        "entry_edge_repricing_exit_evidence_probability_source": "p_converge=bucket_evidence:primary;p_fill_entry=bucket_evidence:primary;p_fill_exit=bucket_evidence:primary",
                        "entry_edge_repricing_exit_evidence_bucket_samples_min": 25,
                        "entry_edge_repricing_exit_evidence_scale": 0.04,
                        "entry_edge_repricing_target_surplus_raw": 0.05,
                        "entry_edge_repricing_target_surplus_discounted": 0.002,
                        "entry_edge_repricing_friction_floor": 0.006,
                        "entry_edge_repricing_target_surplus_discounted_to_friction": -0.004,
                        "entry_edge_target_surplus_to_friction": 0.044,
                        "edge_realizable_p_realize": 0.04,
                        "edge_realizable_p_converge": 0.40,
                        "edge_realizable_p_converge_evidence_status": "bucket_evidence",
                        "edge_realizable_p_converge_source": "primary",
                        "edge_realizable_p_converge_samples": 25,
                        "edge_realizable_p_converge_bucket_key": "conv-ok",
                        "edge_realizable_p_converge_bucket_used_key": "conv-ok",
                        "edge_realizable_p_fill_entry": 0.85,
                        "edge_realizable_p_fill_entry_evidence_status": "bucket_evidence",
                        "edge_realizable_p_fill_entry_source": "primary",
                        "edge_realizable_p_fill_entry_samples": 26,
                        "edge_realizable_p_fill_entry_bucket_key": "entry-ok",
                        "edge_realizable_p_fill_entry_bucket_used_key": "entry-ok",
                        "edge_realizable_p_fill_exit": 0.10,
                        "edge_realizable_p_fill_exit_evidence_status": "bucket_evidence",
                        "edge_realizable_p_fill_exit_source": "primary",
                        "edge_realizable_p_fill_exit_samples": 25,
                        "edge_realizable_p_fill_exit_bucket_key": "exit-ok",
                        "edge_realizable_p_fill_exit_bucket_used_key": "exit-ok",
                    },
                ),
            ]
            for run_id, slug, false_edge_type, extra in rows:
                details = {
                    "entry_strategy_family": "repricing_bucket",
                    "entry_edge_exit_path_required": True,
                    "entry_edge_false_edge": True,
                    "entry_edge_false_edge_type": false_edge_type,
                    "entry_edge_root_cause": "repricing_execution_evidence_test",
                    "entry_usable_edge": -0.01,
                    **extra,
                }
                conn.execute(
                    """
                    INSERT INTO auto_trade_decisions(
                        run_id, run_date, slug, event_date, title, outcome, side,
                        action, decided_state, will_order, reason_codes_json,
                        reasons_json, details_json, created_at
                    )
                    VALUES (?, '2026-05-05', ?, '2026-05-05',
                            'Highest temperature in Test?', '80°F or higher', 'YES',
                            'entry', 'blocked', 0, '[]', '[]', ?, ?)
                    """,
                    (run_id, slug, json.dumps(details), now),
                )
            conn.commit()
            conn.execute(
                """
                INSERT INTO event_runs(slug, event_date, settlement_actual, rounding_rule, created_at)
                VALUES ('probability-low-hit', '2026-05-05', 80, '', ?)
                """,
                (now,),
            )
            conn.commit()
            conn.close()

            report = entry_counterfactual_review.build_report(db_path, days=1)

        overall = report["overall"]
        evidence = overall["repricing_execution_evidence"]
        self.assertEqual(evidence["candidate_count"], 4)
        self.assertEqual(evidence["missing_count"], 1)
        self.assertAlmostEqual(evidence["missing_rate"], 1 / 4)
        self.assertEqual(evidence["component_missing_count"], 1)
        self.assertAlmostEqual(evidence["component_missing_rate"], 1 / 4)
        self.assertEqual(evidence["default_only_count"], 1)
        self.assertAlmostEqual(evidence["default_only_rate"], 1 / 4)
        self.assertEqual(evidence["status_missing_count"], 0)
        self.assertAlmostEqual(evidence["status_missing_rate"], 0.0)
        self.assertEqual(evidence["status_default_only_count"], 1)
        self.assertAlmostEqual(evidence["status_default_only_rate"], 1 / 4)
        self.assertEqual(evidence["missing_table_candidate_count"], 1)
        self.assertAlmostEqual(evidence["missing_table_candidate_rate"], 1 / 4)
        self.assertEqual(evidence["default_table_candidate_count"], 1)
        self.assertAlmostEqual(evidence["default_table_candidate_rate"], 1 / 4)
        self.assertEqual(evidence["entry_missing_flag_count"], 1)
        self.assertAlmostEqual(evidence["entry_missing_flag_rate"], 1 / 4)
        self.assertEqual(evidence["realize_missing_flag_count"], 0)
        self.assertAlmostEqual(evidence["realize_missing_flag_rate"], 0.0)
        self.assertEqual(evidence["realize_default_flag_count"], 0)
        self.assertAlmostEqual(evidence["realize_default_flag_rate"], 0.0)
        self.assertEqual(evidence["missing_residual_count"], 0)
        self.assertAlmostEqual(evidence["missing_residual_rate"], 0.0)
        self.assertEqual(evidence["low_sample_count"], 1)
        self.assertAlmostEqual(evidence["low_sample_rate"], 1 / 4)
        self.assertEqual(evidence["unreported_component_count"], 0)
        self.assertAlmostEqual(evidence["unreported_component_rate"], 0.0)
        self.assertEqual(evidence["all_bucket_evidence_count"], 2)
        self.assertAlmostEqual(evidence["all_bucket_evidence_rate"], 2 / 4)
        self.assertEqual(evidence["probability_too_low_count"], 2)
        self.assertAlmostEqual(evidence["probability_too_low_rate"], 2 / 4)
        self.assertAlmostEqual(evidence["avg_p_realize"], (0.50 + 0.05 + 0.04 + 0.04) / 4)
        self.assertAlmostEqual(evidence["avg_p_converge"], (0.80 + 0.25 + 0.40 + 0.40) / 4)
        self.assertAlmostEqual(evidence["avg_p_fill_entry"], (0.90 + 0.45 + 0.85 + 0.85) / 4)
        self.assertAlmostEqual(evidence["avg_p_fill_exit"], (0.70 + 0.20 + 0.10 + 0.10) / 4)
        self.assertAlmostEqual(evidence["avg_evidence_scale"], (0.0 + 0.05 + 0.04 + 0.04) / 4)
        self.assertAlmostEqual(
            evidence["avg_execution_probability"], (0.0 + 0.05 + 0.04 + 0.04) / 4
        )
        self.assertAlmostEqual(evidence["avg_target_surplus_raw"], (0.08 + 0.06 + 0.05 + 0.05) / 4)
        self.assertAlmostEqual(
            evidence["avg_target_surplus_discounted"], (0.0 + 0.003 + 0.002 + 0.002) / 4
        )
        self.assertAlmostEqual(
            evidence["avg_discounted_surplus_to_friction"],
            (-0.01 - 0.004 - 0.004 - 0.004) / 4,
        )
        self.assertAlmostEqual(evidence["avg_friction_floor"], (0.01 + 0.007 + 0.006 + 0.006) / 4)
        components = evidence["component_breakdown"]
        self.assertEqual(components["p_converge"]["candidate_count"], 4)
        self.assertEqual(components["p_converge"]["missing_count"], 1)
        p_converge_buckets = {
            row["key"]: row
            for row in components["p_converge"]["bucket_used_breakdown"]
        }
        self.assertEqual(p_converge_buckets["conv-ok"]["candidate_count"], 3)
        self.assertEqual(p_converge_buckets["conv-ok"]["rejected_settled_count"], 1)
        self.assertEqual(p_converge_buckets["conv-ok"]["rejected_hit_count"], 1)
        self.assertEqual(p_converge_buckets["conv-ok"]["missed_winner_count"], 1)
        self.assertAlmostEqual(p_converge_buckets["conv-ok"]["rejected_hit_rate"], 1.0)
        self.assertEqual(components["p_fill_entry"]["candidate_count"], 4)
        self.assertEqual(components["p_fill_entry"]["low_sample_count"], 1)
        self.assertEqual(components["p_fill_entry"]["bucket_evidence_count"], 3)
        self.assertAlmostEqual(components["p_fill_entry"]["avg_prob"], (0.90 + 0.45 + 0.85 + 0.85) / 4)
        self.assertAlmostEqual(components["p_fill_entry"]["avg_samples"], (33 + 3 + 26 + 26) / 4)
        self.assertEqual(components["p_fill_entry"]["min_samples"], 3)
        self.assertEqual(components["p_fill_entry"]["source_top"][0]["key"], "primary")
        self.assertEqual(components["p_fill_entry"]["bucket_key_top"][0]["key"], "entry-ok")
        self.assertEqual(components["p_fill_exit"]["default_only_count"], 1)
        self.assertEqual(components["p_fill_exit"]["low_sample_count"], 1)
        self.assertEqual(components["p_fill_exit"]["bucket_evidence_count"], 2)
        self.assertEqual(components["p_fill_exit"]["probability_too_low_count"], 2)
        self.assertEqual(components["p_fill_exit"]["limiting_probability_count"], 2)
        self.assertEqual(
            components["p_fill_exit"]["limiting_bucket_used_key_top"][0],
            {"key": "exit-ok", "count": 2},
        )
        exit_quality = {
            row["key"]: row
            for row in components["p_fill_exit"]["probability_bucket_quality"]
        }
        self.assertEqual(exit_quality["exit-ok"]["candidate_count"], 2)
        self.assertEqual(exit_quality["exit-ok"]["probability_too_low_count"], 2)
        self.assertEqual(exit_quality["exit-ok"]["limiting_probability_count"], 2)
        self.assertAlmostEqual(exit_quality["exit-ok"]["avg_prob"], 0.10)
        self.assertAlmostEqual(exit_quality["exit-ok"]["avg_p_realize"], 0.04)
        self.assertEqual(evidence["status_top"][0]["key"], "bucket_evidence")
        missing_tables = {row["key"]: row["count"] for row in evidence["missing_table_top"]}
        self.assertEqual(missing_tables["p_converge_buckets"], 1)
        self.assertEqual(missing_tables["p_fill_exit_buckets"], 1)
        default_tables = {row["key"]: row["count"] for row in evidence["default_table_top"]}
        self.assertEqual(default_tables["p_fill_exit_buckets"], 1)
        low_sample_tables = {row["key"]: row["count"] for row in evidence["low_sample_table_top"]}
        self.assertEqual(low_sample_tables["p_fill_entry_buckets"], 1)
        self.assertEqual(low_sample_tables["p_fill_exit_buckets"], 1)
        missing_reasons = {row["key"]: row["count"] for row in evidence["missing_reason_top"]}
        self.assertEqual(missing_reasons["calibration_bucket_missing"], 1)
        probability_sources = {row["key"]: row["count"] for row in evidence["probability_source_top"]}
        self.assertEqual(probability_sources["p_converge=missing:regional_calibration"], 1)
        self.assertEqual(probability_sources["p_fill_entry=low_sample:primary"], 1)
        self.assertEqual(probability_sources["p_fill_exit=low_sample:primary"], 1)
        self.assertEqual(probability_sources["p_fill_exit=bucket_evidence:primary"], 2)
        self.assertEqual(evidence["min_bucket_samples"], 3)
        self.assertAlmostEqual(evidence["avg_bucket_samples_min"], (18 + 3 + 25 + 25) / 4)

        answers = report["counterfactual_answers"]["overall"]
        self.assertEqual(answers["is_exit_difficult"]["answer"], "yes")
        self.assertIn(
            "repricing_exit_evidence_missing",
            answers["is_exit_difficult"]["notes"],
        )
        self.assertIn(
            "repricing_exit_component_missing",
            answers["is_exit_difficult"]["notes"],
        )
        self.assertIn(
            "repricing_exit_default_only",
            answers["is_exit_difficult"]["notes"],
        )
        self.assertIn(
            "repricing_exit_status_default_only",
            answers["is_exit_difficult"]["notes"],
        )
        self.assertIn(
            "repricing_exit_missing_table",
            answers["is_exit_difficult"]["notes"],
        )
        self.assertIn(
            "repricing_exit_default_table",
            answers["is_exit_difficult"]["notes"],
        )
        self.assertIn(
            "repricing_exit_entry_missing_flag",
            answers["is_exit_difficult"]["notes"],
        )
        self.assertIn(
            "repricing_exit_evidence_low_sample",
            answers["is_exit_difficult"]["notes"],
        )
        self.assertIn(
            "repricing_exit_probability_too_low",
            answers["is_exit_difficult"]["notes"],
        )

        row = report["strategy_kpis"]["by_strategy"]["repricing_bucket"]
        self.assertEqual(row["repricing_candidate_count"], 4)
        self.assertAlmostEqual(row["repricing_evidence_missing_rate"], 1 / 4)
        self.assertEqual(row["repricing_evidence_component_missing_count"], 1)
        self.assertAlmostEqual(row["repricing_evidence_component_missing_rate"], 1 / 4)
        self.assertEqual(row["repricing_evidence_default_only_count"], 1)
        self.assertAlmostEqual(row["repricing_evidence_default_only_rate"], 1 / 4)
        self.assertEqual(row["repricing_evidence_status_missing_count"], 0)
        self.assertAlmostEqual(row["repricing_evidence_status_missing_rate"], 0.0)
        self.assertEqual(row["repricing_evidence_status_default_only_count"], 1)
        self.assertAlmostEqual(row["repricing_evidence_status_default_only_rate"], 1 / 4)
        self.assertEqual(row["repricing_evidence_missing_table_candidate_count"], 1)
        self.assertAlmostEqual(row["repricing_evidence_missing_table_candidate_rate"], 1 / 4)
        self.assertEqual(row["repricing_evidence_default_table_candidate_count"], 1)
        self.assertAlmostEqual(row["repricing_evidence_default_table_candidate_rate"], 1 / 4)
        self.assertEqual(row["repricing_evidence_entry_missing_flag_count"], 1)
        self.assertAlmostEqual(row["repricing_evidence_entry_missing_flag_rate"], 1 / 4)
        self.assertEqual(row["repricing_evidence_realize_missing_flag_count"], 0)
        self.assertAlmostEqual(row["repricing_evidence_realize_missing_flag_rate"], 0.0)
        self.assertEqual(row["repricing_evidence_realize_default_flag_count"], 0)
        self.assertAlmostEqual(row["repricing_evidence_realize_default_flag_rate"], 0.0)
        self.assertEqual(row["repricing_evidence_missing_residual_count"], 0)
        self.assertAlmostEqual(row["repricing_evidence_missing_residual_rate"], 0.0)
        self.assertAlmostEqual(row["repricing_evidence_low_sample_rate"], 1 / 4)
        self.assertEqual(row["repricing_evidence_unreported_component_count"], 0)
        self.assertAlmostEqual(row["repricing_evidence_unreported_component_rate"], 0.0)
        self.assertEqual(row["repricing_evidence_all_bucket_evidence_count"], 2)
        self.assertAlmostEqual(row["repricing_evidence_all_bucket_evidence_rate"], 2 / 4)
        self.assertAlmostEqual(row["repricing_probability_too_low_rate"], 2 / 4)
        self.assertAlmostEqual(row["avg_repricing_p_realize"], (0.50 + 0.05 + 0.04 + 0.04) / 4)
        self.assertAlmostEqual(row["avg_repricing_p_fill_entry"], (0.90 + 0.45 + 0.85 + 0.85) / 4)
        self.assertAlmostEqual(row["avg_repricing_target_surplus_raw"], (0.08 + 0.06 + 0.05 + 0.05) / 4)
        self.assertAlmostEqual(
            row["avg_repricing_target_surplus_discounted_to_friction"],
            (-0.01 - 0.004 - 0.004 - 0.004) / 4,
        )
        self.assertIn("avg_repricing_p_fill_entry", report["strategy_kpis"]["field_contract"])
        self.assertIn(
            "avg_repricing_target_surplus_discounted_to_friction",
            report["strategy_kpis"]["field_contract"],
        )

    def test_repricing_flag_only_missing_is_attributed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "archive.db"
            conn = self._create_base_db(db_path)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            details = {
                "entry_strategy_family": "repricing_bucket",
                "entry_edge_exit_path_required": True,
                "edge_realizable_p_realize_missing": True,
                "edge_realizable_p_realize_uses_default": True,
                "edge_realizable_p_realize": 0.20,
            }
            conn.execute(
                """
                INSERT INTO auto_trade_decisions(
                    run_id, run_date, slug, event_date, title, outcome, side,
                    action, decided_state, will_order, reason_codes_json,
                    reasons_json, details_json, created_at
                )
                VALUES (1, '2026-05-05', 'flag-only-missing', '2026-05-05',
                        'Highest temperature in Test?', '80°F or higher', 'YES',
                        'entry', 'blocked', 0, '[]', '[]', ?, ?)
                """,
                (json.dumps(details), now),
            )
            conn.commit()
            conn.close()

            report = entry_counterfactual_review.build_report(db_path, days=1)

        evidence = report["overall"]["repricing_execution_evidence"]
        self.assertEqual(evidence["candidate_count"], 1)
        self.assertEqual(evidence["missing_count"], 1)
        self.assertEqual(evidence["entry_missing_flag_count"], 0)
        self.assertEqual(evidence["realize_missing_flag_count"], 1)
        self.assertEqual(evidence["realize_default_flag_count"], 1)
        self.assertEqual(evidence["missing_residual_count"], 0)

        row = report["strategy_kpis"]["by_strategy"]["repricing_bucket"]
        self.assertEqual(row["repricing_evidence_realize_missing_flag_count"], 1)
        self.assertEqual(row["repricing_evidence_realize_default_flag_count"], 1)
        self.assertEqual(row["repricing_evidence_missing_residual_count"], 0)

    def test_actionable_summary_downgrades_historical_repricing_gap_when_latest_run_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "archive.db"
            conn = self._create_base_db(db_path)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            old_details = {
                "entry_strategy_family": "repricing_bucket",
                "entry_edge_exit_path_required": True,
                "entry_edge_repricing_exit_evidence_missing": True,
                "entry_edge_repricing_exit_evidence_status": "missing",
                "edge_realizable_p_realize_missing": True,
            }
            conn.execute(
                """
                INSERT INTO auto_trade_decisions(
                    run_id, run_date, slug, event_date, title, outcome, side,
                    action, decided_state, will_order, reason_codes_json,
                    reasons_json, details_json, created_at
                )
                VALUES (1, '2026-05-05', 'old-missing', '2026-05-05',
                        'Highest temperature in Test?', '80°F or higher', 'YES',
                        'entry', 'blocked', 0, '[]', '[]', ?, ?)
                """,
                (json.dumps(old_details), now),
            )
            clean_details = {
                "entry_strategy_family": "repricing_bucket",
                "entry_edge_exit_path_required": True,
                "entry_edge_repricing_exit_evidence_status": "bucket_evidence",
                "entry_edge_repricing_exit_evidence_probability_source": (
                    "p_converge=bucket_evidence:primary;"
                    "p_fill_entry=bucket_evidence:primary;"
                    "p_fill_exit=bucket_evidence:primary"
                ),
                "edge_realizable_p_realize": 0.42,
                "edge_realizable_p_converge": 0.70,
                "edge_realizable_p_fill_entry": 0.80,
                "edge_realizable_p_fill_exit": 0.75,
            }
            conn.execute(
                """
                INSERT INTO auto_trade_decisions(
                    run_id, run_date, slug, event_date, title, outcome, side,
                    action, decided_state, will_order, reason_codes_json,
                    reasons_json, details_json, created_at
                )
                VALUES (2, '2026-05-05', 'latest-clean', '2026-05-05',
                        'Highest temperature in Test?', '80°F or higher', 'YES',
                        'entry', 'blocked', 0, '[]', '[]', ?, ?)
                """,
                (json.dumps(clean_details), now),
            )
            conn.commit()
            conn.close()

            report = entry_counterfactual_review.build_report(db_path, days=1)

        summary = report["actionable_summary"]
        self.assertEqual(report["current_runtime"]["run_id"], 2)
        self.assertEqual(
            report["current_runtime"]["overall"]["repricing_candidate_count"],
            1,
        )
        self.assertEqual(
            report["current_runtime"]["overall"]["repricing_evidence_missing_count"],
            0,
        )
        self.assertEqual(
            summary["overall"]["primary_diagnosis"],
            "historical_repricing_evidence_residual",
        )
        self.assertEqual(
            summary["overall"]["recommended_action"],
            "continue_repricing_evidence_rolling_validation",
        )
        self.assertEqual(summary["overall"]["next_mainline_task"], "rolling_validation")
        self.assertEqual(summary["next_mainline_task"], "rolling_validation")

    def test_actionable_summary_separates_repricing_probability_quality_from_missing_chain(self):
        answers = {
            "overall": {
                "is_strategy_wrong": {"answer": "no", "confidence": "high"},
                "are_thresholds_too_strict": {"answer": "no", "confidence": "high"},
                "is_exit_difficult": {"answer": "yes", "confidence": "high"},
                "is_model_or_station_mapping_wrong": {"answer": "no", "confidence": "high"},
            },
            "by_strategy": {},
        }
        kpis = {
            "overall": {
                "strategy": "overall",
                "candidate_count": 8,
                "repricing_candidate_count": 8,
                "repricing_probability_too_low_count": 6,
                "repricing_probability_too_low_rate": 0.75,
            },
            "by_strategy": {},
        }
        runtime = {
            "overall": {
                "run_id": 10,
                "entry_rows": 8,
                "repricing_candidate_count": 8,
                "repricing_evidence_missing_count": 0,
                "repricing_evidence_defaultish_count": 0,
                "repricing_evidence_missing_tables_count": 0,
                "repricing_evidence_component_missing_count": 0,
                "repricing_evidence_component_default_only_count": 0,
                "repricing_evidence_component_unreported_count": 0,
                "repricing_evidence_low_sample_count": 0,
                "repricing_probability_too_low_count": 6,
                "repricing_all_bucket_evidence_count": 8,
            },
            "by_strategy": {},
        }

        summary = entry_counterfactual_review._build_actionable_summary(
            answers,
            kpis,
            current_runtime=runtime,
        )

        self.assertEqual(
            summary["overall"]["primary_diagnosis"],
            "repricing_probability_quality_review",
        )
        self.assertEqual(
            summary["overall"]["recommended_action"],
            "review_repricing_probability_quality",
        )
        self.assertEqual(
            summary["overall"]["next_mainline_task"],
            "repricing_probability_quality_review",
        )
        self.assertEqual(summary["next_mainline_task"], "repricing_probability_quality_review")

    def test_actionable_summary_marks_explained_low_probability_as_low_ev_confirmed(self):
        answers = {
            "overall": {
                "is_strategy_wrong": {"answer": "no", "confidence": "high"},
                "are_thresholds_too_strict": {"answer": "no", "confidence": "high"},
                "is_exit_difficult": {"answer": "yes", "confidence": "high"},
                "is_model_or_station_mapping_wrong": {"answer": "no", "confidence": "high"},
            },
            "by_strategy": {},
        }
        kpis = {
            "overall": {
                "strategy": "overall",
                "candidate_count": 8,
                "repricing_candidate_count": 8,
                "repricing_probability_too_low_count": 6,
                "repricing_probability_too_low_rate": 0.75,
            },
            "by_strategy": {},
        }
        runtime = {
            "overall": {
                "run_id": 10,
                "entry_rows": 8,
                "repricing_candidate_count": 8,
                "repricing_evidence_missing_count": 0,
                "repricing_evidence_defaultish_count": 0,
                "repricing_evidence_missing_tables_count": 0,
                "repricing_evidence_component_missing_count": 0,
                "repricing_evidence_component_default_only_count": 0,
                "repricing_evidence_component_unreported_count": 0,
                "repricing_evidence_low_sample_count": 1,
                "repricing_probability_too_low_count": 6,
                "repricing_all_bucket_evidence_count": 7,
                "repricing_probability_quality": {
                    "limiting_component_top": [{"key": "p_converge", "count": 6}],
                    "component_breakdown": {
                        "p_converge": {
                            "limiting_probability_count": 6,
                            "limiting_bucket_used_key_top": [
                                {"key": "d0|s1|ru|eu|x2|ot:ANY", "count": 6}
                            ],
                        },
                        "p_fill_entry": {},
                        "p_fill_exit": {},
                    },
                },
            },
            "by_strategy": {},
        }

        summary = entry_counterfactual_review._build_actionable_summary(
            answers,
            kpis,
            current_runtime=runtime,
        )

        self.assertEqual(
            summary["overall"]["primary_diagnosis"],
            "repricing_probability_low_ev_confirmed",
        )
        self.assertEqual(
            summary["overall"]["recommended_action"],
            "continue_repricing_probability_rolling_validation",
        )
        self.assertEqual(summary["overall"]["next_mainline_task"], "rolling_validation")
        self.assertEqual(summary["next_mainline_task"], "rolling_validation")

    def test_current_runtime_reports_repricing_probability_bucket_quality(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "archive.db"
            conn = self._create_base_db(db_path)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            rows = [
                (
                    "exit-limited",
                    {
                        "entry_strategy_family": "repricing_bucket",
                        "entry_edge_exit_path_required": True,
                        "entry_edge_repricing_exit_evidence_status": "bucket_evidence",
                        "entry_edge_repricing_exit_evidence_probability_source": (
                            "p_converge=bucket_evidence:primary;"
                            "p_fill_entry=bucket_evidence:primary;"
                            "p_fill_exit=bucket_evidence:primary"
                        ),
                        "edge_realizable_p_realize": 0.004,
                        "edge_realizable_p_converge": 0.40,
                        "edge_realizable_p_converge_evidence_status": "bucket_evidence",
                        "edge_realizable_p_converge_source": "primary",
                        "edge_realizable_p_converge_samples": 40,
                        "edge_realizable_p_converge_bucket_used_key": "conv-ok",
                        "edge_realizable_p_fill_entry": 0.50,
                        "edge_realizable_p_fill_entry_evidence_status": "bucket_evidence",
                        "edge_realizable_p_fill_entry_source": "primary",
                        "edge_realizable_p_fill_entry_samples": 50,
                        "edge_realizable_p_fill_entry_bucket_used_key": "entry-ok",
                        "edge_realizable_p_fill_exit": 0.02,
                        "edge_realizable_p_fill_exit_evidence_status": "bucket_evidence",
                        "edge_realizable_p_fill_exit_source": "primary",
                        "edge_realizable_p_fill_exit_samples": 60,
                        "edge_realizable_p_fill_exit_bucket_used_key": "exit-thin",
                    },
                ),
                (
                    "converge-limited",
                    {
                        "entry_strategy_family": "repricing_bucket",
                        "entry_edge_exit_path_required": True,
                        "entry_edge_repricing_exit_evidence_status": "bucket_evidence",
                        "entry_edge_repricing_exit_evidence_probability_source": (
                            "p_converge=bucket_evidence:primary;"
                            "p_fill_entry=bucket_evidence:primary;"
                            "p_fill_exit=bucket_evidence:primary"
                        ),
                        "edge_realizable_p_realize": 0.006,
                        "edge_realizable_p_converge": 0.03,
                        "edge_realizable_p_converge_evidence_status": "bucket_evidence",
                        "edge_realizable_p_converge_source": "primary",
                        "edge_realizable_p_converge_samples": 80,
                        "edge_realizable_p_converge_bucket_used_key": "conv-thin",
                        "edge_realizable_p_fill_entry": 0.50,
                        "edge_realizable_p_fill_entry_evidence_status": "bucket_evidence",
                        "edge_realizable_p_fill_entry_source": "primary",
                        "edge_realizable_p_fill_entry_samples": 50,
                        "edge_realizable_p_fill_entry_bucket_used_key": "entry-ok",
                        "edge_realizable_p_fill_exit": 0.40,
                        "edge_realizable_p_fill_exit_evidence_status": "bucket_evidence",
                        "edge_realizable_p_fill_exit_source": "primary",
                        "edge_realizable_p_fill_exit_samples": 60,
                        "edge_realizable_p_fill_exit_bucket_used_key": "exit-ok",
                    },
                ),
            ]
            for slug, details in rows:
                conn.execute(
                    """
                    INSERT INTO auto_trade_decisions(
                        run_id, run_date, slug, event_date, title, outcome, side,
                        action, decided_state, will_order, reason_codes_json,
                        reasons_json, details_json, created_at
                    )
                    VALUES (7, '2026-05-05', ?, '2026-05-05',
                            'Highest temperature in Test?', '80°F or higher', 'YES',
                            'entry', 'blocked', 0, '[]', '[]', ?, ?)
                    """,
                    (slug, json.dumps(details), now),
                )
            conn.commit()
            conn.close()

            report = entry_counterfactual_review.build_report(db_path, days=1)

        runtime = report["current_runtime"]["by_strategy"]["repricing_bucket"]
        self.assertEqual(runtime["repricing_probability_too_low_count"], 2)
        self.assertIn("repricing_probability_quality", runtime)
        self.assertNotIn("repricing_probability_component_stats", runtime)
        quality = runtime["repricing_probability_quality"]
        limiter_top = {row["key"]: row["count"] for row in quality["limiting_component_top"]}
        self.assertEqual(limiter_top["p_fill_exit"], 1)
        self.assertEqual(limiter_top["p_converge"], 1)
        components = quality["component_breakdown"]
        self.assertEqual(components["p_fill_exit"]["limiting_probability_count"], 1)
        self.assertEqual(
            components["p_fill_exit"]["limiting_bucket_used_key_top"][0],
            {"key": "exit-thin", "count": 1},
        )
        exit_quality = {
            row["key"]: row
            for row in components["p_fill_exit"]["probability_bucket_quality"]
        }
        self.assertAlmostEqual(exit_quality["exit-thin"]["avg_prob"], 0.02)
        self.assertAlmostEqual(exit_quality["exit-thin"]["avg_samples"], 60.0)
        self.assertEqual(exit_quality["exit-thin"]["limiting_probability_count"], 1)

    def test_feedback_snapshot_compacts_and_persists_review_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "archive.db"
            conn = self._create_base_db(db_path)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            details = {
                "entry_strategy_family": "repricing_bucket",
                "entry_edge_exit_path_required": True,
                "entry_edge_repricing_exit_evidence_missing": True,
                "entry_edge_repricing_exit_evidence_status": "default_only",
                "entry_edge_repricing_exit_evidence_probability_source": "p_converge=missing:primary;p_fill_entry=bucket_evidence:primary;p_fill_exit=default_only:global_default",
                "edge_realizable_p_converge": 0.70,
                "edge_realizable_p_converge_evidence_status": "missing",
                "edge_realizable_p_fill_entry": 0.90,
                "edge_realizable_p_fill_entry_evidence_status": "bucket_evidence",
                "edge_realizable_p_fill_exit": 0.60,
                "edge_realizable_p_fill_exit_evidence_status": "default_only",
            }
            conn.execute(
                """
                INSERT INTO auto_trade_decisions(
                    run_id, run_date, slug, event_date, title, outcome, side,
                    action, decided_state, will_order, reason_codes_json,
                    reasons_json, details_json, created_at
                )
                VALUES (1, '2026-05-05', 'snapshot-market', '2026-05-05',
                        'Highest temperature in Test?', '80°F or higher', 'YES',
                        'entry', 'blocked', 0, '[]', '[]', ?, ?)
                """,
                (json.dumps(details), now),
            )
            conn.commit()
            conn.close()

            report = entry_counterfactual_review.build_report(db_path, days=1)
            snapshot = entry_counterfactual_review.build_feedback_snapshot(report)
            write_result = entry_counterfactual_review.save_feedback_snapshot(
                db_path,
                snapshot,
                days=1,
            )

            conn = sqlite3.connect(db_path)
            row = conn.execute(
                """
                SELECT kind, days, payload_json, ok
                FROM feedback_snapshots
                WHERE kind='entry_counterfactual_review'
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            conn.close()

        self.assertTrue(write_result["ok"])
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "entry_counterfactual_review")
        self.assertEqual(row[1], 1)
        self.assertEqual(row[3], 1)
        payload = json.loads(row[2])
        self.assertIn("counterfactual_answers", payload)
        self.assertIn("actionable_summary", payload)
        self.assertIn("strategy_kpis", payload)
        self.assertIn("repricing_execution_evidence", payload)
        self.assertIn("actionable_summary", payload["field_contract"])
        self.assertIn(
            "primary_diagnosis",
            payload["actionable_summary"]["field_contract"],
        )
        self.assertEqual(
            payload["actionable_summary"]["overall"]["primary_diagnosis"],
            "exit_execution_evidence_gap",
        )
        self.assertEqual(
            payload["repricing_execution_evidence"]["candidate_count"],
            1,
        )
        self.assertIn(
            "avg_repricing_p_fill_entry",
            payload["strategy_kpis"]["field_contract"],
        )
        self.assertIn(
            "soft_penalty_missed_winner_rate",
            payload["strategy_kpis"]["field_contract"],
        )
        self.assertIn(
            "low_price_hit_rate",
            payload["strategy_kpis"]["field_contract"],
        )
        self.assertIn("rule_attribution", payload)
        self.assertIn("rule_code", payload["rule_attribution"]["overall"]["field_contract"])
        self.assertIn("rule_pruning_review", payload)
        self.assertIn("review_bucket", payload["rule_pruning_review"]["field_contract"])
        self.assertIn("settlement_coverage", payload)


if __name__ == "__main__":
    unittest.main()

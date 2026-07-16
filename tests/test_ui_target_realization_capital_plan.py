import unittest
import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

from ui.server import (
    _attach_target_realization_capital_plan,
    _build_target_realization_capital_plan,
    _latest_target_realization_v3_plan_for_ui,
)


def _attach_posterior_ssot(item, target_bucket, target_probability, ranked=None):
    item["posterior_ssot_fact"] = {
        "schema": "bayes_posterior_ssot_fact_v1",
        "state": "available",
        "target_bucket": target_bucket,
        "target_probability": target_probability,
        "target_probability_field": "posterior_ssot_fact.target_probability",
        "ranked_bucket_probabilities": ranked or [
            {"outcome": target_bucket, "probability": target_probability},
        ],
    }
    item["posterior_target_bucket_selection"] = {
        "ssot_source": "posterior_ssot_fact",
        "target_bucket_selection_source": "posterior_ssot_fact.top_bucket",
        "target_probability_field": "posterior_ssot_fact.target_probability",
        "target_bucket": target_bucket,
        "target_probability": target_probability,
    }


class UiTargetRealizationCapitalPlanTest(unittest.TestCase):
    def test_v3_ui_reads_persisted_decision_plan_and_locks_without_recomputing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "archive.db"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            conn.executescript(
                """
                CREATE TABLE auto_trade_decisions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slug TEXT, event_date TEXT, details_json TEXT, created_at TEXT
                );
                CREATE TABLE target_realization_execution_locks(
                    slug TEXT, event_date TEXT, outcome TEXT, side TEXT,
                    lock_type TEXT, state TEXT, source_order_id TEXT,
                    plan_id TEXT, locked_at TEXT, expires_at TEXT, updated_at TEXT
                );
                """
            )
            plan = {
                "ok": True,
                "version": "strong_posterior_three_leg_strategy_v2",
                "plan_id": "trv3-ui-ssot",
                "target_bucket": "33°C",
                "target_posterior": 0.42,
                "probability_recomputed": False,
            }
            conn.execute(
                "INSERT INTO auto_trade_decisions(slug,event_date,details_json,created_at) VALUES(?,?,?,?)",
                (
                    "chengdu-july-11",
                    "2026-07-11",
                    json.dumps({"target_realization_capital_plan": plan}),
                    "2026-07-11T06:00:00+00:00",
                ),
            )
            conn.execute(
                """
                INSERT INTO target_realization_execution_locks(
                    slug,event_date,outcome,side,lock_type,state,
                    source_order_id,plan_id,locked_at,expires_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "chengdu-july-11",
                    "2026-07-11",
                    "33°C",
                    "YES",
                    "sell_reconcile",
                    "active",
                    "sell-1",
                    "trv3-ui-ssot",
                    "2026-07-11T06:00:00+00:00",
                    "2026-07-11T23:59:59",
                    "2026-07-11T06:00:00+00:00",
                ),
            )
            conn.commit()

            persisted = _latest_target_realization_v3_plan_for_ui(
                conn,
                slug="chengdu-july-11",
                event_date="2026-07-11",
            )
            conn.close()
        item = {"target_realization_capital_plan": persisted}
        with patch.dict("os.environ", {"PM_TARGET_REALIZATION_V3_ENABLED": "1"}, clear=False):
            _attach_target_realization_capital_plan(item)

        self.assertEqual(item["target_realization_capital_plan"]["plan_id"], "trv3-ui-ssot")
        self.assertEqual(item["target_realization_capital_plan"]["source"], "auto_trade_decisions")
        self.assertFalse(item["target_realization_capital_plan"]["ui_probability_recomputed"])
        self.assertEqual(
            item["target_realization_capital_plan"]["execution_locks"][0]["lock_type"],
            "sell_reconcile",
        )

    def test_ui_does_not_freeze_or_recompute_legacy_target_plan(self):
        item = {
            "slug": "highest-temperature-in-chengdu-on-july-11-2026",
            "run_date": "2026-07-10",
            "event_date": "2026-07-11",
            "distribution": [
                {"bucket": "32°C", "best_ask": 0.10, "best_ask_no": 0.89},
                {"bucket": "33°C", "best_ask": 0.34, "best_ask_no": 0.65},
                {"bucket": "34°C", "best_ask": 0.33, "best_ask_no": 0.66},
                {"bucket": "35°C", "best_ask": 0.15, "best_ask_no": 0.84},
                {"bucket": "36°C", "best_ask": 0.08, "best_ask_no": 0.91},
            ],
            "posterior_ssot_fact": {
                "schema": "bayes_posterior_ssot_fact_v1",
                "state": "available",
                "target_bucket": "33°C",
                "target_probability": 0.35,
                "bucket_probabilities": [
                    {"bucket": "32°C", "probability": 0.10},
                    {"bucket": "33°C", "probability": 0.35},
                    {"bucket": "34°C", "probability": 0.32},
                    {"bucket": "35°C", "probability": 0.15},
                    {"bucket": "36°C", "probability": 0.08},
                ],
            },
        }
        with patch.dict(
            "os.environ",
            {
                "PM_TARGET_REALIZATION_V3_ENABLED": "1",
                "PM_TARGET_REALIZATION_V3_ACTIVATION_DATE_ET": "2026-07-11",
            },
            clear=False,
        ):
            plan = _build_target_realization_capital_plan(item)

        self.assertFalse(plan["ok"])
        self.assertEqual(plan["reason"], "committed_dynamic_three_leg_plan_missing")

    def test_ui_does_not_activate_legacy_capital_contract(self):
        item = {
            "slug": "highest-temperature-in-chengdu-on-july-11-2026",
            "run_date": "2026-07-11",
            "event_date": "2026-07-11",
            "distribution": [
                {"bucket": "31°C", "best_ask": 0.01, "best_ask_no": 0.97},
                {"bucket": "32°C", "best_ask": 0.08, "best_ask_no": 0.91},
                {"bucket": "33°C", "best_ask": 0.34, "best_ask_no": 0.65},
                {"bucket": "34°C", "best_ask": 0.33, "best_ask_no": 0.66},
                {"bucket": "35°C", "best_ask": 0.16, "best_ask_no": 0.83},
                {"bucket": "36°C", "best_ask": 0.06, "best_ask_no": 0.93},
                {"bucket": "37°C", "best_ask": 0.02, "best_ask_no": 0.97},
            ],
            "posterior_ssot_fact": {
                "schema": "bayes_posterior_ssot_fact_v1",
                "state": "available",
                "target_bucket": "33°C",
                "target_probability": 0.35,
                "bucket_probabilities": [
                    {"bucket": "31°C", "probability": 0.01},
                    {"bucket": "32°C", "probability": 0.08},
                    {"bucket": "33°C", "probability": 0.35},
                    {"bucket": "34°C", "probability": 0.32},
                    {"bucket": "35°C", "probability": 0.16},
                    {"bucket": "36°C", "probability": 0.06},
                    {"bucket": "37°C", "probability": 0.02},
                ],
            },
        }
        with patch.dict(
            "os.environ",
            {
                "PM_TARGET_REALIZATION_V3_ENABLED": "1",
                "PM_TARGET_REALIZATION_V3_ACTIVATION_DATE_ET": "2026-07-11",
                "PM_TARGET_REALIZATION_BANKROLL_USDC": "1000",
                "PM_TARGET_REALIZATION_AVAILABLE_CASH_USDC": "1000",
            },
            clear=False,
        ):
            plan = _build_target_realization_capital_plan(item)

        self.assertFalse(plan["ok"])
        self.assertEqual(plan["reason"], "committed_dynamic_three_leg_plan_missing")

    def test_capital_plan_blocks_when_posterior_ssot_fact_missing(self):
        item = {
            "run_date": "2026-06-18",
            "event_date": "2026-06-18",
            "target_bucket_selection": {
                "target_bucket": "30°C",
                "watch_buckets": [
                    {"outcome": "30°C"},
                    {"outcome": "31°C"},
                    {"outcome": "29°C"},
                ],
            },
            "distribution": [
                {"outcome": "28°C", "q_temperature_bucket_posterior": 0.90, "best_ask": 0.05},
                {"outcome": "30°C", "q_temperature_bucket_posterior": 0.68, "best_ask": 0.55},
                {"outcome": "31°C", "q_temperature_bucket_posterior": 0.30, "best_ask": 0.35},
                {"outcome": "29°C", "q_temperature_bucket_posterior": 0.02, "best_ask": 0.15},
            ],
        }
        with patch.dict(
            "os.environ",
            {
                "PM_TARGET_REALIZATION_BANKROLL_USDC": "900",
                "PM_TARGET_REALIZATION_TODAY_RATIO": "1",
                "PM_TARGET_REALIZATION_TOMORROW_RATIO": "0",
                "PM_TARGET_REALIZATION_DAY_AFTER_RATIO": "0",
            },
            clear=False,
        ):
            plan = _build_target_realization_capital_plan(item)

        self.assertFalse(plan["ok"])
        self.assertEqual(plan["reason"], "committed_dynamic_three_leg_plan_missing")

    def test_ui_rejects_remote_no_reconstruction(self):
        item = {
            "run_date": "2026-07-01",
            "event_date": "2026-07-01",
            "target_bucket_selection": {
                "target_bucket": "31°C",
                "watch_buckets": [
                    {"outcome": "31°C"},
                    {"outcome": "32°C"},
                ],
            },
            "distribution": [
                {
                    "outcome": "31°C",
                    "q_temperature_bucket_posterior": 0.40,
                    "best_ask": 0.42,
                    "best_ask_no": 0.60,
                },
                {
                    "outcome": "32°C",
                    "q_temperature_bucket_posterior": 0.22,
                    "best_ask": 0.24,
                    "best_ask_no": 0.80,
                },
                {
                    "outcome": "33°C",
                    "q_temperature_bucket_posterior": 0.14,
                    "best_ask": 0.16,
                    "best_ask_no": 0.86,
                },
                {
                    "outcome": "34°C",
                    "q_temperature_bucket_posterior": 0.12,
                    "best_ask": 0.14,
                    "best_ask_no": 0.88,
                },
                {
                    "outcome": "35°C or higher",
                    "q_temperature_bucket_posterior": 0.10,
                    "best_ask": 0.12,
                    "best_ask_no": 0.90,
                },
            ],
        }
        _attach_posterior_ssot(
            item,
            "31°C",
            0.40,
            ranked=[
                {"outcome": "31°C", "probability": 0.40},
                {"outcome": "32°C", "probability": 0.22},
            ],
        )
        with patch.dict(
            "os.environ",
            {
                "PM_TARGET_REALIZATION_BANKROLL_USDC": "900",
                "PM_TARGET_REALIZATION_TODAY_RATIO": "1",
                "PM_TARGET_REALIZATION_TOMORROW_RATIO": "0",
                "PM_TARGET_REALIZATION_DAY_AFTER_RATIO": "0",
                "PM_TARGET_REALIZATION_REMOTE_NO_MIN_BUCKET_DISTANCE": "2",
                "PM_TARGET_REALIZATION_REMOTE_NO_MAX_BUCKET_DISTANCE": "3",
            },
            clear=False,
        ):
            plan = _build_target_realization_capital_plan(item)

        self.assertFalse(plan["ok"])
        self.assertEqual(plan["reason"], "committed_dynamic_three_leg_plan_missing")

    def test_ui_rejects_lower_ring_remote_no_reconstruction(self):
        item = {
            "run_date": "2026-07-01",
            "event_date": "2026-07-01",
            "target_bucket_selection": {
                "target_bucket": "35°C or higher",
                "watch_buckets": [
                    {"outcome": "35°C or higher"},
                    {"outcome": "34°C"},
                ],
            },
            "distribution": [
                {"outcome": "35°C or higher", "q_temperature_bucket_posterior": 0.40, "best_ask": 0.42, "best_ask_no": 0.60},
                {"outcome": "34°C", "q_temperature_bucket_posterior": 0.22, "best_ask": 0.24, "best_ask_no": 0.80},
                {"outcome": "33°C", "q_temperature_bucket_posterior": 0.14, "best_ask": 0.16, "best_ask_no": 0.86},
                {"outcome": "32°C", "q_temperature_bucket_posterior": 0.12, "best_ask": 0.14, "best_ask_no": 0.88},
                {"outcome": "31°C", "q_temperature_bucket_posterior": 0.10, "best_ask": 0.12, "best_ask_no": 0.90},
            ],
        }
        _attach_posterior_ssot(
            item,
            "35°C or higher",
            0.40,
            ranked=[
                {"outcome": "35°C or higher", "probability": 0.40},
                {"outcome": "34°C", "probability": 0.22},
            ],
        )
        with patch.dict(
            "os.environ",
            {
                "PM_TARGET_REALIZATION_BANKROLL_USDC": "900",
                "PM_TARGET_REALIZATION_TODAY_RATIO": "1",
                "PM_TARGET_REALIZATION_TOMORROW_RATIO": "0",
                "PM_TARGET_REALIZATION_DAY_AFTER_RATIO": "0",
                "PM_TARGET_REALIZATION_REMOTE_NO_MIN_BUCKET_DISTANCE": "2",
                "PM_TARGET_REALIZATION_REMOTE_NO_MAX_BUCKET_DISTANCE": "3",
            },
            clear=False,
        ):
            plan = _build_target_realization_capital_plan(item)

        self.assertFalse(plan["ok"])
        self.assertEqual(plan["reason"], "committed_dynamic_three_leg_plan_missing")


if __name__ == "__main__":
    unittest.main()

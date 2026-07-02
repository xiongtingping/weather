import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import bayes_tradeability_runtime as runtime


class BayesTradeabilityRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_env = os.environ.copy()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._old_env)

    def _make_db(self) -> Path:
        temp_dir = tempfile.mkdtemp(prefix="bayes_rt_")
        db_path = Path(temp_dir) / "archive.db"
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS auto_trade_decisions (
                    id INTEGER PRIMARY KEY,
                    created_at TEXT,
                    action TEXT,
                    will_order INTEGER,
                    outcome TEXT,
                    side TEXT,
                    slug TEXT,
                    event_date TEXT,
                    details_json TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS calibration_labels (
                    decision_id INTEGER,
                    label INTEGER
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS edge_trades (
                    id INTEGER PRIMARY KEY,
                    realized_pnl REAL,
                    state TEXT,
                    slug TEXT,
                    outcome TEXT,
                    side TEXT,
                    event_date TEXT
                )
                """
            )
            conn.commit()
        finally:
            conn.close()
        return db_path

    def test_no_history_does_not_block(self) -> None:
        db_path = self._make_db()
        os.environ["PM_BAYES_TRADEABILITY_ENABLED"] = "1"
        os.environ["PM_BAYES_TRADEABILITY_REQUIRE_DATA"] = "1"

        edge_after, meta = runtime.apply_bayes_tradeability_adjustment(
            edge_lcb=0.20,
            db_path=str(db_path),
            strategy="main",
            outcome="A",
            side="YES",
            slug="slug-1",
            event_date="2026-05-30",
        )

        self.assertIsNotNone(edge_after)
        self.assertAlmostEqual(edge_after, 0.20, places=9)
        self.assertEqual(meta.get("edge_tradeability_bayes_source"), "edge_trades")
        self.assertEqual(meta.get("edge_tradeability_bayes_scope"), "edge_trades")
        self.assertEqual(meta.get("edge_tradeability_bayes_evidence"), 0)
        self.assertEqual(meta.get("edge_tradeability_bayes_na_reason"), "history_missing")

    def test_low_sample_keeps_soft_and_reports_na_reason(self) -> None:
        db_path = self._make_db()
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.cursor()
            # one profitable / four losing realized examples -> evidence=4
            rows = [
                (-0.10, "closed", "s-1", "A", "YES", "2026-05-30"),
                (0.20, "closed", "s-1", "A", "YES", "2026-05-30"),
                (-0.05, "closed", "s-1", "A", "YES", "2026-05-30"),
                (-0.02, "closed", "s-1", "A", "YES", "2026-05-30"),
                (-0.01, "closed", "s-1", "A", "YES", "2026-05-30"),
            ]
            for pnl, state, slug, outcome, side, event_date in rows:
                cur.execute(
                    "INSERT INTO edge_trades (realized_pnl, state, slug, outcome, side, event_date) VALUES (?, ?, ?, ?, ?, ?)",
                    (pnl, state, slug, outcome, side, event_date),
                )
            conn.commit()
        finally:
            conn.close()

        os.environ["PM_BAYES_TRADEABILITY_ENABLED"] = "1"
        os.environ["PM_BAYES_TRADEABILITY_PRIOR_ALPHA"] = "8"
        os.environ["PM_BAYES_TRADEABILITY_PRIOR_BETA"] = "8"
        os.environ["PM_BAYES_TRADEABILITY_MIN_SOFT_EVIDENCE"] = "10"
        os.environ["PM_BAYES_TRADEABILITY_SCALE_FLOOR"] = "0.40"

        edge_after, meta = runtime.apply_bayes_tradeability_adjustment(
            edge_lcb=0.25,
            db_path=str(db_path),
            strategy="main",
            outcome="A",
            side="YES",
            slug="s-1",
            event_date="2026-05-30",
        )

        self.assertIsNotNone(edge_after)
        self.assertLess(edge_after, 0.25)
        self.assertEqual(meta.get("edge_tradeability_bayes_source"), "edge_trades")
        self.assertEqual(meta.get("edge_tradeability_bayes_scope"), "edge_trades")
        self.assertEqual(meta.get("edge_tradeability_bayes_evidence"), 5)
        self.assertEqual(meta.get("edge_tradeability_bayes_na_reason"), "low_sample")

    def test_decision_layer_has_higher_scope_priority(self) -> None:
        db_path = self._make_db()
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO auto_trade_decisions (id, created_at, action, will_order, outcome, side, slug, event_date, details_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (1, "2026-05-30T00:00:00", "entry", 1, "A", "YES", "s-1", "2026-05-30", '{"strategy":"main"}'),
            )
            cur.execute(
                "INSERT INTO calibration_labels (decision_id, label) VALUES (?, ?)",
                (1, 0),
            )
            conn.commit()
        finally:
            conn.close()

        os.environ["PM_BAYES_TRADEABILITY_ENABLED"] = "1"
        os.environ["PM_BAYES_TRADEABILITY_PRIOR_ALPHA"] = "1"
        os.environ["PM_BAYES_TRADEABILITY_PRIOR_BETA"] = "1"

        edge_after, meta = runtime.apply_bayes_tradeability_adjustment(
            edge_lcb=0.20,
            db_path=str(db_path),
            strategy="main",
            outcome="A",
            side="YES",
            slug="s-1",
            event_date="2026-05-30",
        )

        self.assertAlmostEqual(edge_after, 0.13333333333333333, places=12)
        self.assertEqual(meta.get("edge_tradeability_bayes_source"), "exact_slug_event")
        self.assertEqual(meta.get("edge_tradeability_bayes_scope"), "exact_slug_event")


if __name__ == "__main__":
    unittest.main()

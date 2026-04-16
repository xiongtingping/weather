import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import scan_weather_market as swm


class TestAutoTradeRotation(unittest.TestCase):
    def test_fact_rows_rotate_when_events_per_job_max_is_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "archive.db"
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)

            with mock.patch.object(swm, "DB_PATH", db_path), mock.patch.object(swm, "DATA_DIR", data_dir):
                swm.init_db()
                run_date = "2026-04-14"
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS event_fact_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_date TEXT NOT NULL,
                        slug TEXT NOT NULL,
                        event_date TEXT NOT NULL,
                        fact_as_of TEXT NOT NULL,
                        best_edge_score REAL,
                        composer_version TEXT NOT NULL,
                        fact_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                for idx, slug in enumerate(["event-a", "event-b", "event-c"], start=1):
                    fact_json = json.dumps(
                        {
                            "slug": slug,
                            "rows": [{"outcome": f"{idx}C", "q_entry": 0.5, "q_calibrated": 0.5}],
                        },
                        ensure_ascii=True,
                    )
                    cur.execute(
                        """
                        INSERT INTO event_fact_history(
                            run_date, slug, event_date, fact_as_of, best_edge_score, composer_version, fact_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_date,
                            slug,
                            run_date,
                            f"{run_date}T0{idx}:00:00+00:00",
                            float(10 - idx),
                            "test",
                            fact_json,
                            f"{run_date}T0{idx}:00:00+00:00",
                        ),
                    )
                conn.commit()
                conn.close()

                seen_calls = []

                def _capture_execute(run_date_key, event_date_key, payload, **kwargs):
                    seen_calls.append((str(payload.get("slug") or ""), event_date_key))

                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_AUTO_TRADE_EVENTS_PER_JOB_MAX": "2",
                        "PM_AUTO_TRADE_EVENT_TIMEOUT_SEC": "0",
                        "PM_AUTO_TRADE_EVENT_BACKOFF_SEC": "0",
                    },
                    clear=False,
                ), mock.patch.object(swm, "execute_auto_trades", side_effect=_capture_execute):
                    swm.run_auto_trades_for_date(run_date)
                    first_run = list(seen_calls)
                    seen_calls.clear()
                    swm.run_auto_trades_for_date(run_date)
                    second_run = list(seen_calls)

            self.assertEqual(len(first_run), 2)
            self.assertEqual(len(second_run), 2)
            self.assertNotEqual(first_run, second_run)
            self.assertEqual({slug for slug, _ in first_run + second_run}, {"event-a", "event-b", "event-c"})


if __name__ == "__main__":
    unittest.main()

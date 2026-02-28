import importlib.util
import sqlite3
import sys
import unittest
from pathlib import Path


def _load_server_module():
    root = Path(__file__).resolve().parents[1]
    path = root / "ui" / "server.py"
    spec = importlib.util.spec_from_file_location("weather_ui_server", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # dataclasses and other reflection logic expects the module to be registered.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestFactsMaterializeKeys(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _load_server_module()
        self.conn = sqlite3.connect(":memory:")
        cur = self.conn.cursor()
        cur.execute(
            """
            CREATE TABLE model_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT,
                event_date TEXT,
                slug TEXT,
                payload_json TEXT,
                meta_json TEXT,
                created_at TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT,
                event_date TEXT,
                slug TEXT,
                payload_json TEXT,
                meta_json TEXT,
                created_at TEXT
            )
            """
        )
        self.conn.commit()

    def tearDown(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def test_union_model_and_market_keys(self) -> None:
        cur = self.conn.cursor()
        # Model refreshed today, market refreshed yesterday.
        cur.execute(
            "INSERT INTO model_snapshots(run_date,event_date,slug,payload_json,meta_json,created_at) VALUES(?,?,?,?,?,?)",
            ("2026-02-10", "2026-02-11", "slug_a", "{}", "{}", "2026-02-10T01:00:00+00:00"),
        )
        cur.execute(
            "INSERT INTO market_snapshots(run_date,event_date,slug,payload_json,meta_json,created_at) VALUES(?,?,?,?,?,?)",
            ("2026-02-09", "2026-02-11", "slug_a", "{}", "{}", "2026-02-09T01:00:00+00:00"),
        )
        # Market refreshed today, model refreshed yesterday.
        cur.execute(
            "INSERT INTO market_snapshots(run_date,event_date,slug,payload_json,meta_json,created_at) VALUES(?,?,?,?,?,?)",
            ("2026-02-10", "2026-02-11", "slug_b", "{}", "{}", "2026-02-10T02:00:00+00:00"),
        )
        cur.execute(
            "INSERT INTO model_snapshots(run_date,event_date,slug,payload_json,meta_json,created_at) VALUES(?,?,?,?,?,?)",
            ("2026-02-09", "2026-02-11", "slug_b", "{}", "{}", "2026-02-09T02:00:00+00:00"),
        )
        self.conn.commit()

        keys = self.server._snapshot_key_rows_for_run_date(self.conn, "2026-02-10")
        self.assertIn(("slug_a", "2026-02-11", "2026-02-10"), keys)
        self.assertIn(("slug_b", "2026-02-11", "2026-02-10"), keys)

    def test_range_picks_latest_run_date_per_key(self) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO market_snapshots(run_date,event_date,slug,payload_json,meta_json,created_at) VALUES(?,?,?,?,?,?)",
            ("2026-02-09", "2026-02-11", "slug_c", "{}", "{}", "2026-02-09T01:00:00+00:00"),
        )
        cur.execute(
            "INSERT INTO model_snapshots(run_date,event_date,slug,payload_json,meta_json,created_at) VALUES(?,?,?,?,?,?)",
            ("2026-02-10", "2026-02-11", "slug_c", "{}", "{}", "2026-02-10T01:00:00+00:00"),
        )
        self.conn.commit()

        keys = self.server._snapshot_key_rows_for_run_date_range(self.conn, "2026-02-09", "2026-02-10")
        self.assertIn(("slug_c", "2026-02-11", "2026-02-10"), keys)


if __name__ == "__main__":
    unittest.main()

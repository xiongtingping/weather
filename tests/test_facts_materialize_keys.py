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
        cur.execute(
            """
            CREATE TABLE event_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT,
                event_date TEXT,
                slug TEXT,
                payload_json TEXT,
                created_at TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE station_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT,
                event_date TEXT,
                station_id TEXT,
                resolution_source TEXT,
                source_kind TEXT,
                observed_at TEXT,
                published_at TEXT,
                value REAL,
                unit TEXT,
                market_var TEXT,
                trust_tier TEXT,
                source_confidence REAL,
                raw_json TEXT,
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

    def test_event_run_keys_are_included_for_live_ssot_compose(self) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO event_runs(run_date,event_date,slug,payload_json,created_at) VALUES(?,?,?,?,?)",
            (
                "2026-02-10",
                "2026-02-11",
                "slug_event_run_only",
                '{"rows":[{"outcome":"29°C","q_round":0.5}]}',
                "2026-02-10T03:00:00+00:00",
            ),
        )
        self.conn.commit()

        keys = self.server._snapshot_key_rows_for_run_date(self.conn, "2026-02-10")

        self.assertIn(("slug_event_run_only", "2026-02-11", "2026-02-10"), keys)

    def test_future_event_date_triggers_single_day_materialization(self) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO model_snapshots(run_date,event_date,slug,payload_json,meta_json,created_at) VALUES(?,?,?,?,?,?)",
            (
                "2026-02-09",
                "2026-02-10",
                "slug_future_event",
                "{}",
                "{}",
                "2026-02-09T23:00:00+00:00",
            ),
        )
        self.conn.commit()

        keys = self.server._snapshot_key_rows_for_run_date(self.conn, "2026-02-10")

        self.assertIn(("slug_future_event", "2026-02-10", "2026-02-09"), keys)

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

    def test_station_observations_trigger_chengdu_d0_materialization_key(self) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            CREATE TABLE weather_market_discovery (
                slug TEXT,
                event_date TEXT,
                title TEXT,
                market_open_at TEXT,
                first_seen_at TEXT,
                first_tokenized_at TEXT,
                token_count INTEGER,
                status TEXT,
                details_json TEXT,
                updated_at TEXT,
                PRIMARY KEY(slug, event_date)
            )
            """
        )
        cur.execute(
            """
            INSERT INTO weather_market_discovery(
                slug, event_date, title, first_seen_at, token_count, status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "highest-temperature-in-chengdu-on-february-10-2026",
                "2026-02-10",
                "Highest temperature in Chengdu on February 10?",
                "2026-02-09T18:00:00+00:00",
                11,
                "tokenized",
                "2026-02-10T02:00:00+00:00",
            ),
        )
        cur.execute(
            """
            INSERT INTO station_observations(
                run_date, event_date, station_id, resolution_source, source_kind,
                observed_at, published_at, value, unit, market_var, trust_tier,
                source_confidence, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-02-09",
                "2026-02-10",
                "ZUUU",
                "https://example.test/stations/ZUUU",
                "wunderground_history",
                "2026-02-10T08:00:00+00:00",
                "2026-02-10T08:00:00+00:00",
                33.0,
                "C",
                "temperature_2m_max",
                "settlement_source_nowcast",
                0.85,
                "2026-02-10T08:08:01+00:00",
            ),
        )
        self.conn.commit()

        keys = self.server._snapshot_key_rows_for_run_date(self.conn, "2026-02-10")

        self.assertIn(
            (
                "highest-temperature-in-chengdu-on-february-10-2026",
                "2026-02-10",
                "2026-02-10",
            ),
            keys,
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
import sqlite3

from reported_trajectory_market_registry import (
    commit_registry_rows,
    load_config,
    materialize_event_registry,
)


def event(city: str, station: str = "ZBAA") -> dict:
    slug = f"highest-temperature-in-{city}-on-july-18-2026"
    return {
        "id": "e1",
        "slug": slug,
        "description": (
            "This market resolves at an airport. The resolution source is Wunderground "
            f"https://www.wunderground.com/history/daily/cn/{city}/{station}. "
            "The resolution source measures temperatures to whole degrees Celsius."
        ),
        "markets": [
            {"id": "1", "groupItemTitle": "29°C or below", "outcomes": '["Yes","No"]', "clobTokenIds": '["y1","n1"]', "active": True, "closed": False, "acceptingOrders": True},
            {"id": "2", "groupItemTitle": "30°C", "outcomes": '["Yes","No"]', "clobTokenIds": '["y2","n2"]', "active": True, "closed": False, "acceptingOrders": True},
            {"id": "3", "groupItemTitle": "31°C or higher", "outcomes": '["Yes","No"]', "clobTokenIds": '["y3","n3"]', "active": True, "closed": False, "acceptingOrders": True},
        ],
    }


class ReportedTrajectoryRegistryTests(unittest.TestCase):
    def test_registry_replay_is_semantically_idempotent(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE reported_trajectory_market_registry(
              slug TEXT,event_date TEXT,outcome TEXT,city_slug TEXT,
              station_id TEXT,settlement_timezone TEXT,settlement_unit TEXT,
              source_lane TEXT,source_eligible INTEGER,source_block_reason TEXT,
              resolution_url TEXT,rules_sha256 TEXT,yes_token TEXT,no_token TEXT,
              market_active INTEGER,market_accepting_orders INTEGER,
              registry_json TEXT,refreshed_at TEXT,
              PRIMARY KEY(slug,event_date,outcome)
            );
            CREATE TABLE market_tokens(
              slug TEXT,event_date TEXT,outcome TEXT,yes_token TEXT,no_token TEXT,
              market_active INTEGER,market_closed INTEGER,market_archived INTEGER,
              market_accepting_orders INTEGER,market_enable_orderbook INTEGER,
              updated_at TEXT,
              PRIMARY KEY(slug,event_date,outcome)
            );
            """
        )
        rows = materialize_event_registry(
            event("london", "EGLL"),
            event_date="2026-07-18",
            config=load_config(),
        )
        self.assertEqual(
            commit_registry_rows(
                conn,
                rows,
                refreshed_at="2026-07-17T00:00:00Z",
            ),
            3,
        )
        self.assertEqual(
            commit_registry_rows(
                conn,
                rows,
                refreshed_at="2026-07-17T00:05:00Z",
            ),
            0,
        )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(DISTINCT refreshed_at) "
                "FROM reported_trajectory_market_registry"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(DISTINCT updated_at) FROM market_tokens"
            ).fetchone()[0],
            1,
        )
        conn.close()

    def test_china_market_routes_to_amsc(self) -> None:
        rows = materialize_event_registry(event("beijing"), event_date="2026-07-18", config=load_config())
        self.assertTrue(all(row["source_lane"] == "amsc" for row in rows))
        self.assertTrue(all(row["source_eligible"] for row in rows))
        self.assertEqual(rows[0]["station_id"], "ZBAA")
        self.assertEqual(rows[0]["preferred_origin_source_id"], "cn_amsc")
        self.assertEqual(rows[0]["origin_country_code"], "CN")

    def test_non_china_market_routes_to_awc_when_enabled(self) -> None:
        rows = materialize_event_registry(event("london", "EGLL"), event_date="2026-07-18", config=load_config())
        self.assertTrue(all(row["source_lane"] == "awc" for row in rows))
        self.assertTrue(all(row["source_eligible"] for row in rows))

    def test_non_airport_resolution_is_blocked(self) -> None:
        value = event("hong-kong", "VHHH")
        value["description"] = "Resolution source is the Hong Kong Observatory. It measures to one decimal place."
        rows = materialize_event_registry(value, event_date="2026-07-18", config=load_config())
        self.assertTrue(all(row["source_lane"] == "blocked" for row in rows))
        self.assertTrue(all(not row["source_eligible"] for row in rows))


if __name__ == "__main__":
    unittest.main()

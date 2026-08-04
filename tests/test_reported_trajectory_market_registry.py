from __future__ import annotations

import json
import unittest
import sqlite3
from pathlib import Path
from unittest.mock import patch

from reported_trajectory_country_sources import station_source_route
from reported_trajectory_market_registry import (
    commit_registry_rows,
    load_config,
    materialize_event_registry,
)


ROOT = Path(__file__).resolve().parents[1]
AWC_GLOBAL_STATIONS = {
    "CYYZ", "EDDM", "EFHK", "EGLC", "EHAM", "EPWA", "FACT", "KATL", "KAUS",
    "KBKF", "KDAL", "KHOU", "KLAX", "KLGA", "KMIA", "KORD", "KSEA", "KSFO",
    "LEMD", "LFPB", "LIMC", "LTAC", "MMMX", "MPMG", "NZWN", "OEJN", "OPKC",
    "RCSS", "RJTT", "RKPK", "RKSI", "RPLL", "SAEZ", "SBGR", "VILK", "WMKK",
    "WSSS", "ZBAA", "ZGGG", "ZGSZ", "ZHHH", "ZSPD", "ZSQD", "ZUCK", "ZUUU",
}


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

    def test_beijing_market_routes_to_global_awc(self) -> None:
        rows = materialize_event_registry(event("beijing"), event_date="2026-07-18", config=load_config())
        self.assertTrue(all(row["source_lane"] == "awc" for row in rows))
        self.assertTrue(all(row["source_eligible"] for row in rows))
        self.assertEqual(rows[0]["station_id"], "ZBAA")
        self.assertEqual(rows[0]["preferred_origin_source_id"], "global_noaa_awc_relay")
        self.assertEqual(rows[0]["preferred_origin_adapter"], "awc")
        self.assertEqual(rows[0]["origin_country_code"], "ZZ")

    def test_rcss_uses_global_awc_relay(self) -> None:
        rows = materialize_event_registry(
            event("taipei", "RCSS"), event_date="2026-07-18", config=load_config()
        )
        self.assertTrue(all(row["source_lane"] == "awc" for row in rows))
        self.assertTrue(all(row["source_eligible"] for row in rows))
        self.assertEqual(rows[0]["preferred_origin_source_id"], "global_noaa_awc_relay")
        self.assertEqual(rows[0]["preferred_origin_adapter"], "awc")
        self.assertEqual(rows[0]["origin_country_code"], "ZZ")
        self.assertEqual(station_source_route("RCSS")["provider_id"], "global_noaa_awc_relay")
        self.assertEqual(station_source_route("RCSS")["adapter"], "awc")

    def test_rcss_rejects_cn_amsc_impersonation(self) -> None:
        with patch(
            "reported_trajectory_market_registry.station_source_route",
            return_value={"provider_id": "cn_amsc", "adapter": "amsc"},
        ):
            rows = materialize_event_registry(
                event("taipei", "RCSS"), event_date="2026-07-18", config=load_config()
            )
        self.assertTrue(all(row["source_lane"] == "awc" for row in rows))
        self.assertTrue(all(not row["source_eligible"] for row in rows))
        self.assertTrue(
            all("amsc_production_route_forbidden" in row["source_block_reasons"] for row in rows)
        )

    def test_resolved_station_not_city_slug_selects_the_formal_lane(self) -> None:
        rows = materialize_event_registry(
            event("beijing", "KATL"), event_date="2026-07-18", config=load_config()
        )
        self.assertTrue(all(row["source_lane"] == "awc" for row in rows))
        self.assertTrue(all(row["source_eligible"] for row in rows))

    def test_station_lane_config_has_nine_amsc_and_thirty_six_awc_stations(self) -> None:
        lane_map = json.loads(
            (ROOT / "config/trading/bayes_basket_target_station_lane_map_v1.json").read_text(
                encoding="utf-8"
            )
        )
        stations = lane_map["stations"]
        amsc = {station for station, lane in stations.items() if lane == "amsc"}
        awc = {station for station, lane in stations.items() if lane == "awc"}
        self.assertEqual(amsc, set())
        self.assertEqual(awc, AWC_GLOBAL_STATIONS)
        self.assertEqual(len(awc), 45)
        self.assertEqual(awc, set(stations))

    def test_non_china_market_routes_to_awc_when_enabled(self) -> None:
        rows = materialize_event_registry(event("london", "EGLC"), event_date="2026-07-18", config=load_config())
        self.assertTrue(all(row["source_lane"] == "awc" for row in rows))
        self.assertTrue(all(row["source_eligible"] for row in rows))

    def test_non_airport_resolution_is_blocked(self) -> None:
        value = event("hong-kong", "VHHH")
        value["description"] = "Resolution source is the Hong Kong Observatory. It measures to one decimal place."
        rows = materialize_event_registry(value, event_date="2026-07-18", config=load_config())
        self.assertTrue(all(row["source_lane"] == "awc" for row in rows))
        self.assertTrue(all(not row["source_eligible"] for row in rows))


if __name__ == "__main__":
    unittest.main()

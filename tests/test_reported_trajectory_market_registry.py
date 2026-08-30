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


# Every live Gamma market carries the exchange's own admission rule for an
# order.  Observed on the real API 2026-08-30: ``orderMinSize`` 5 across the
# board, ``orderPriceMinTickSize`` 0.001 or 0.01 depending on the bucket.
SIZING = {"orderMinSize": 5, "orderPriceMinTickSize": 0.01}


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
            {"id": "1", "groupItemTitle": "29°C or below", "outcomes": '["Yes","No"]', "clobTokenIds": '["y1","n1"]', "active": True, "closed": False, "acceptingOrders": True, **SIZING},
            {"id": "2", "groupItemTitle": "30°C", "outcomes": '["Yes","No"]', "clobTokenIds": '["y2","n2"]', "active": True, "closed": False, "acceptingOrders": True, **SIZING},
            {"id": "3", "groupItemTitle": "31°C or higher", "outcomes": '["Yes","No"]', "clobTokenIds": '["y3","n3"]', "active": True, "closed": False, "acceptingOrders": True, **SIZING},
        ],
    }


def noaa_event(city: str, station: str = "EHAM", unit: str = "Celsius") -> dict:
    """The wording Polymarket moved to: NOAA's timeseries page, ICAO in the query string."""
    slug = f"highest-temperature-in-{city}-on-july-18-2026"
    body = dict(event(city, station))
    body["slug"] = slug
    body["description"] = (
        "This market will resolve to the temperature range that contains the highest "
        f"temperature recorded by NOAA at the {city.title()} Airport Station in degrees "
        f"{unit} on 18 Jul '26.\n\nThe resolution source for this market will be "
        "information from NOAA, specifically the highest reading under the \"Temp\" column "
        f"for all times on this day, available here: https://www.weather.gov/wrh/timeseries?site={station.lower()}"
        f"\n\nThe resolution source for this market measures temperatures to whole degrees {unit} (eg, 9°{unit[0]})."
    )
    return body


class ReportedTrajectoryRegistryTests(unittest.TestCase):
    def test_noaa_resolution_source_maps_to_the_same_station_as_wunderground(self) -> None:
        """Polymarket reworded these markets one event date at a time; both must resolve."""
        for city, station in (("amsterdam", "EHAM"), ("miami", "KMIA"), ("toronto", "CYYZ")):
            with self.subTest(city=city):
                rows = materialize_event_registry(
                    noaa_event(city, station), event_date="2026-07-18", config=load_config()
                )
                self.assertTrue(rows)
                for row in rows:
                    self.assertEqual(row["station_id"], station)
                    self.assertEqual(
                        row["resolution_url"],
                        f"https://www.weather.gov/wrh/timeseries?site={station.lower()}",
                    )
                    self.assertNotIn(
                        "official_resolution_is_not_airport_station",
                        row["source_block_reasons"],
                    )
                    self.assertNotIn("official_source_route_unmapped", row["source_block_reasons"])

    def test_unrecognised_resolution_source_still_blocks(self) -> None:
        """The gate is the point: a market we cannot tie to a station must not go live."""
        body = noaa_event("amsterdam", "EHAM")
        body["description"] = body["description"].replace(
            "https://www.weather.gov/wrh/timeseries?site=eham", "https://example.com/whatever"
        )
        rows = materialize_event_registry(body, event_date="2026-07-18", config=load_config())
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["station_id"], "")
            self.assertFalse(row["source_eligible"])
            self.assertIn("official_resolution_is_not_airport_station", row["source_block_reasons"])

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

    def test_exchange_order_sizing_rule_is_captured_and_missing_rule_fails_closed(self) -> None:
        """The minimum lot has to reach the registry, or the gateway cannot enforce it.

        Polymarket refuses an order below the market's ``orderMinSize``.  Before
        this field was collected the only thing that knew the rule was the CLOB,
        so an undersized order was found out by signing it and posting it.  A
        market that does not state its rule is one whose orders cannot be judged,
        so it is blocked rather than defaulted.
        """
        rows = materialize_event_registry(event("beijing"), event_date="2026-07-18", config=load_config())
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["order_min_size"], 5)
            self.assertEqual(row["order_price_min_tick_size"], 0.01)
            self.assertTrue(row["source_eligible"])

        for label, broken in (
            ("absent", {}),
            ("null", {"orderMinSize": None, "orderPriceMinTickSize": None}),
            ("zero", {"orderMinSize": 0, "orderPriceMinTickSize": 0}),
            ("negative", {"orderMinSize": -5, "orderPriceMinTickSize": -0.01}),
            ("unparseable", {"orderMinSize": "five", "orderPriceMinTickSize": "fine"}),
            ("tick_only", {"orderPriceMinTickSize": 0.01}),
        ):
            with self.subTest(shape=label):
                value = event("beijing")
                for market in value["markets"]:
                    market.pop("orderMinSize", None)
                    market.pop("orderPriceMinTickSize", None)
                    market.update(broken)
                rows = materialize_event_registry(value, event_date="2026-07-18", config=load_config())
                self.assertTrue(rows)
                for row in rows:
                    self.assertIsNone(row["order_min_size"])
                    self.assertFalse(row["source_eligible"])
                    self.assertIn("official_market_order_sizing_rule_missing", row["source_block_reasons"])

    def test_decimal_string_sizing_rule_is_parsed_not_rejected(self) -> None:
        """Gamma sends these as bare numbers today and as strings on some events."""
        value = event("beijing")
        for market in value["markets"]:
            market.update({"orderMinSize": "5", "orderPriceMinTickSize": "0.001"})
        rows = materialize_event_registry(value, event_date="2026-07-18", config=load_config())
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["order_min_size"], 5.0)
            self.assertEqual(row["order_price_min_tick_size"], 0.001)
            self.assertTrue(row["source_eligible"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import unittest
import sqlite3
from pathlib import Path
from unittest.mock import patch

from reported_trajectory_country_sources import station_source_route
from reported_trajectory_market_registry import (
    NOAA_FAMILY,
    WU_FAMILY,
    commit_registry_rows,
    load_config,
    materialize_event_registry,
    settlement_contract,
    settlement_source_check,
)
from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.station_registry import coordinate_registry_manifest


ROOT = Path(__file__).resolve().parents[1]
# The fleet, not a third copy of it: this file, the season map and the
# no-live runner each kept their own roster, and each went stale on its own
# schedule.
AWC_GLOBAL_STATIONS = set(coordinate_registry_manifest()["station_ids"])


# Every live Gamma market carries the exchange's own admission rule for an
# order.  Observed on the real API 2026-08-30: ``orderMinSize`` 5 across the
# board, ``orderPriceMinTickSize`` 0.001 or 0.01 depending on the bucket.
SIZING = {"orderMinSize": 5, "orderPriceMinTickSize": 0.01}


def event(city: str, station: str = "ZBAA") -> dict:
    slug = f"highest-temperature-in-{city}-on-july-18-2026"
    return {
        "id": "e1",
        "slug": slug,
        # The Wunderground wording as Taipei still carries it (2026-09-23).
        "description": (
            "This market will resolve based on the highest temperature recorded in the "
            "'Daily Observations' table on Weather Underground, not the figure displayed in "
            "the 'Day High & Low' summary section.\n\nThis market will resolve to the "
            f"temperature range that contains the highest temperature recorded at the {city.title()} "
            "Airport Station in degrees Celsius on 18 Jul '26.\n\nThe resolution source for this "
            "market will be information from Wunderground, specifically the highest temperature "
            f"recorded for all times on this day, available here: https://www.wunderground.com/history/daily/cn/{city}/{station}."
            "\n\nThis market will resolve once the first data point for the following date has "
            "been published on the resolution source, or by 11:59 PM ET on the day following the "
            "observation date, whichever comes first.\n\nThe resolution source for this market "
            "measures temperatures to whole degrees Celsius (eg, 9°C)."
        ),
        "markets": [
            {"id": "1", "groupItemTitle": "29°C or below", "outcomes": '["Yes","No"]', "clobTokenIds": '["y1","n1"]', "active": True, "closed": False, "acceptingOrders": True, **SIZING},
            {"id": "2", "groupItemTitle": "30°C", "outcomes": '["Yes","No"]', "clobTokenIds": '["y2","n2"]', "active": True, "closed": False, "acceptingOrders": True, **SIZING},
            {"id": "3", "groupItemTitle": "31°C or higher", "outcomes": '["Yes","No"]', "clobTokenIds": '["y3","n3"]', "active": True, "closed": False, "acceptingOrders": True, **SIZING},
        ],
    }


def noaa_event(city: str, station: str = "EHAM", unit: str = "Celsius", *, hourly: bool = False) -> dict:
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
        + (
            "\n\nThis market will resolve off of the Hourly Data provided using the "
            "\"Show Hourly Data\" button."
            if hourly else ""
        )
        + "\n\nIf NOAA data for the observation date is unavailable by 11:59 PM ET on the day "
        "following the observation date, the Weather Underground Daily Observations table will "
        "be used as the resolution source.\n\nThis market will resolve once the first data "
        "point for the following date has been published on the resolution source, or by "
        "11:59 PM ET on the day following the observation date, whichever comes first."
        f"\n\nThe resolution source for this market measures temperatures to whole degrees {unit} (eg, 9°{unit[0]})."
    )
    return body


def fahrenheit(body: dict) -> dict:
    """The same event with a US station's 2°F ladder."""
    for market, title in zip(body["markets"], ("69°F or below", "70-71°F", "72°F or higher")):
        market["groupItemTitle"] = title
    return body


def contract_blocks(rows: list[dict]) -> set[str]:
    return {
        reason
        for row in rows
        for reason in row["source_block_reasons"]
        if reason.startswith("official_resolution_")
    }


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

    def test_station_lane_config_is_the_whole_fleet_on_awc(self) -> None:
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
        self.assertEqual(len(awc), expected_station_count())
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


class SettlementSourceContractTests(unittest.TestCase):
    """Charter 205: every refresh reads what each market settles on."""

    def test_baseline_is_the_whole_fleet_and_hourly_only_is_the_us_ladder(self) -> None:
        cities = load_config()["settlement_source_contract"]["cities"]
        self.assertEqual({entry["station_id"] for entry in cities.values()}, AWC_GLOBAL_STATIONS)
        self.assertEqual(len(cities), expected_station_count())
        for city, entry in cities.items():
            with self.subTest(city=city):
                # NOAA lists 5-minute rows only at US ASOS, and every F market
                # is a US ASOS; the clause and the ladder travel together.
                self.assertEqual(entry["hourly_data_only"], entry["unit"] == "F")
        self.assertEqual(
            sorted(city for city, entry in cities.items() if entry["family"] == WU_FAMILY),
            ["taipei"],
        )

    def test_live_wordings_pass_without_a_contract_block(self) -> None:
        config = load_config()
        for label, body in (
            ("noaa_c", noaa_event("amsterdam", "EHAM")),
            ("noaa_f_hourly", fahrenheit(noaa_event("nyc", "KLGA", "Fahrenheit", hourly=True))),
            ("wu", event("taipei", "RCSS")),
        ):
            with self.subTest(wording=label):
                rows = materialize_event_registry(body, event_date="2026-07-18", config=config)
                self.assertTrue(rows)
                self.assertEqual(contract_blocks(rows), set())
                self.assertTrue(all(row["source_eligible"] for row in rows))
                check = settlement_source_check([("2026-07-18", body)], config=config)
                self.assertEqual(check["deviations"], [])

    def test_us_market_that_admits_five_minute_rows_is_blocked(self) -> None:
        """Our truth is METAR+SPECI; NOAA's all-data view adds 5-minute ASOS rows."""
        body = fahrenheit(noaa_event("nyc", "KLGA", "Fahrenheit", hourly=False))
        rows = materialize_event_registry(body, event_date="2026-07-18", config=load_config())
        self.assertIn("official_resolution_admits_subhourly_data", contract_blocks(rows))
        self.assertTrue(all(not row["source_eligible"] for row in rows))

    def test_a_reading_our_truth_does_not_reproduce_is_blocked(self) -> None:
        noaa = noaa_event("amsterdam", "EHAM")
        noaa["description"] = noaa["description"].replace(
            'highest reading under the "Temp" column', 'value under the "24 Hr Max" column'
        )
        wu = event("taipei", "RCSS")
        wu["description"] = wu["description"].replace(
            "'Daily Observations' table on Weather Underground, not the figure displayed in the ",
            "",
        ).replace("summary section", "summary section on Weather Underground")
        for label, body in (("noaa_24h_max", noaa), ("wu_day_high_low", wu)):
            with self.subTest(wording=label):
                rows = materialize_event_registry(body, event_date="2026-07-18", config=load_config())
                self.assertIn("official_resolution_reading_unrecognized", contract_blocks(rows))
                self.assertTrue(all(not row["source_eligible"] for row in rows))

    def test_stated_unit_that_disagrees_with_the_ladder_is_blocked(self) -> None:
        body = noaa_event("amsterdam", "EHAM", "Fahrenheit")
        rows = materialize_event_registry(body, event_date="2026-07-18", config=load_config())
        self.assertIn("official_resolution_unit_mismatch", contract_blocks(rows))

    def test_moves_between_pages_we_can_settle_are_reported_not_blocked(self) -> None:
        """Taipei onto NOAA or London onto Heathrow change nothing we compute wrongly."""
        config = load_config()
        for label, body, field in (
            ("taipei_to_noaa", noaa_event("taipei", "RCSS"), "family"),
            ("london_to_egll", noaa_event("london", "EGLL"), "station_id"),
        ):
            with self.subTest(move=label):
                rows = materialize_event_registry(body, event_date="2026-07-18", config=config)
                self.assertEqual(contract_blocks(rows), set())
                check = settlement_source_check([("2026-07-18", body)], config=config)
                self.assertFalse(check["ok"])
                self.assertEqual(check["blocked_market_count"], 0)
                line = next(item for item in check["deviations"] if item["field"] == field)
                self.assertFalse(line["blocking"])

    def test_check_folds_a_rewrite_to_the_dates_it_reached(self) -> None:
        """Polymarket rewrites one event date at a time; the line names which."""
        config = load_config()
        old = event("taipei", "RCSS")
        new = noaa_event("taipei", "RCSS")
        newer = noaa_event("taipei", "RCSS")
        check = settlement_source_check(
            [("2026-09-24", old), ("2026-09-25", new), ("2026-09-26", newer)], config=config
        )
        family = [item for item in check["deviations"] if item["field"] == "family"]
        self.assertEqual(len(family), 1)
        self.assertEqual(family[0]["expected"], WU_FAMILY)
        self.assertEqual(family[0]["observed"], NOAA_FAMILY)
        self.assertEqual(family[0]["event_dates"], ["2026-09-25", "2026-09-26"])

    def test_a_baseline_city_with_no_market_is_reported(self) -> None:
        check = settlement_source_check(
            [("2026-07-18", noaa_event("amsterdam", "EHAM"))], config=load_config()
        )
        self.assertFalse(check["ok"])
        self.assertIn("london", check["baseline_cities_without_market"])
        self.assertNotIn("amsterdam", check["baseline_cities_without_market"])

    def test_first_named_url_is_the_resolution_page(self) -> None:
        text = noaa_event("amsterdam", "EHAM")["description"].replace(
            "Daily Observations table will be used",
            "Daily Observations table at https://www.wunderground.com/history/daily/nl/amsterdam/EHAM. will be used",
        )
        contract = settlement_contract(text)
        self.assertEqual(contract["family"], NOAA_FAMILY)
        self.assertEqual(contract["station_id"], "EHAM")

    def test_config_refuses_an_incomplete_baseline(self) -> None:
        import tempfile

        for label, mutate in (
            ("missing", lambda config: config.pop("settlement_source_contract")),
            ("empty", lambda config: config["settlement_source_contract"].update(cities={})),
            ("bad_family", lambda config: config["settlement_source_contract"]["cities"]["london"].update(family="HKO")),
            ("no_finality", lambda config: config["settlement_source_contract"]["cities"]["london"].pop("finality")),
        ):
            with self.subTest(shape=label), tempfile.TemporaryDirectory() as tmp:
                config = load_config()
                mutate(config)
                path = Path(tmp) / "config.json"
                path.write_text(json.dumps(config), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "settlement_source_contract_invalid"):
                    load_config(path)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
import time
from unittest.mock import patch

import reported_trajectory_country_sources as sources


class ReportedTrajectoryCountrySourceTests(unittest.TestCase):
    def setUp(self) -> None:
        sources._FETCH_CACHE.clear()
        sources._NOAA_STATION_CACHE.clear()

    def test_current_market_station_routes_are_complete_and_valid(self) -> None:
        expected = {
            "CYYZ", "EDDM", "EFHK", "EGLC", "EHAM", "EPWA", "FACT",
            "KATL", "KAUS", "KBKF", "KDAL", "KHOU", "KLAX", "KLGA",
            "KMIA", "KORD", "KSEA", "KSFO", "LEMD", "LFPB", "LIMC",
            "LTAC", "MMMX", "MPMG", "NZWN", "OEJN", "OPKC", "RCSS",
            "RJTT", "RKPK", "RKSI", "RPLL", "SAEZ", "SBGR", "VILK",
            "WMKK", "WSSS", "ZBAA", "ZGGG", "ZGSZ", "ZHHH", "ZSPD",
            "ZSQD", "ZUCK", "ZUUU",
        }
        config = sources.load_official_source_config()
        self.assertEqual(expected - set(config["station_routes"]), set())
        self.assertTrue(
            all(provider_id in config["providers"] for provider_id in config["station_routes"].values())
        )

    def test_pagasa_blocks_never_swallow_the_next_report(self) -> None:
        body = """
          <div>RPLL 171400Z 24006KT 9999 FEW025 29/26 Q1011 NOSIG</div>
          <div>RPLL 171300Z 26008KT 9999 FEW025 30/26 Q1010 NOSIG</div>
          <div>RPLL 171200Z 26009KT 9999 FEW025 31/26 Q1010 NOSIG</div>
          <div>RPLL 171100Z 26010KT 9999 FEW025 32/25 Q1009 NOSIG</div>
        """
        rows = sources._extract_pagasa_metars(body, "RPLL")
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["raw_text"], "METAR RPLL 171400Z 24006KT 9999 FEW025 29/26 Q1011 NOSIG=")
        self.assertNotIn("171300Z", rows[0]["raw_text"])

    def test_fmi_and_imgw_json_adapters_preserve_raw_tac(self) -> None:
        config = sources.load_official_source_config()
        with patch.object(
            sources,
            "_http_text",
            return_value=(
                '{"METAREFHK":{"p1":"METAR EFHK 171420Z 15008KT 9999 24/15 Q1014="}}',
                200,
                "2026-07-17T14:20:01.000Z",
            ),
        ):
            fmi = sources._fetch_fmi("fi_fmi_aviation", config["providers"]["fi_fmi_aviation"], ["EFHK"], timeout=1)
        self.assertEqual(fmi["reports"][0]["raw_text"], "METAR EFHK 171420Z 15008KT 9999 24/15 Q1014=")
        with patch.object(
            sources,
            "_http_text",
            return_value=(
                '{"EPWA":{"metars":{"sa":{"pl":[{"message":"METAR EPWA 171400Z 15007KT CAVOK 31/16 Q1012="}]}}}}',
                200,
                "2026-07-17T14:20:01.000Z",
            ),
        ):
            imgw = sources._fetch_imgw("pl_imgw_aviation", config["providers"]["pl_imgw_aviation"], ["EPWA"], timeout=1)
        self.assertEqual(imgw["reports"][0]["raw_text"], "METAR EPWA 171400Z 15007KT CAVOK 31/16 Q1012=")

    def test_noaa_station_adapter_restores_only_metar_kind_and_preserves_file_time(self) -> None:
        config = sources.load_official_source_config()
        provider = config["providers"]["us_noaa_awc"]
        self.assertEqual(provider["adapter"], "noaa_station_metar")
        with patch.object(
            sources,
            "_http_text",
            return_value=(
                "2026/07/18 00:55\nKAUS 180053Z 15011KT 10SM FEW050 31/21 A3003 RMK AO2=\n",
                200,
                "2026-07-18T00:55:31.000Z",
            ),
        ):
            value = sources._fetch_noaa_station_metars(
                "us_noaa_awc", provider, ["KAUS"], timeout=1,
            )
        self.assertEqual(value["transport"], "country_official_noaa_nws_station_metar")
        self.assertEqual(value["reports"][0]["raw_text"], "METAR KAUS 180053Z 15011KT 10SM FEW050 31/21 A3003 RMK AO2")
        self.assertEqual(value["reports"][0]["source_report_time"], "")
        self.assertEqual(value["reports"][0]["origin_file_header_time"], "2026-07-18T00:55:00Z")
        self.assertTrue(value["reports"][0]["origin_direct_country_official"])

    def test_noaa_station_cache_is_per_station_and_retries_only_failures(self) -> None:
        provider = sources.load_official_source_config()["providers"]["us_noaa_awc"]
        calls: list[str] = []

        def response(url, **_kwargs):
            station = url.rsplit("/", 1)[-1].split(".", 1)[0]
            calls.append(station)
            if station == "KDAL" and calls.count("KDAL") == 1:
                raise TimeoutError("transient")
            return (
                f"2026/07/18 00:55\n{station} 180053Z 15011KT 10SM FEW050 31/21 A3003=\n",
                200,
                "2026-07-18T00:55:31.000Z",
            )

        with patch.object(sources, "_http_text", side_effect=response):
            first = sources._fetch_noaa_station_metars("us_noaa_awc", provider, ["KAUS", "KDAL"], timeout=1)
            second = sources._fetch_noaa_station_metars("us_noaa_awc", provider, ["KAUS", "KDAL"], timeout=1)
        self.assertEqual({row["station_id"] for row in first["reports"]}, {"KAUS"})
        self.assertEqual({row["station_id"] for row in second["reports"]}, {"KAUS", "KDAL"})
        self.assertEqual(calls.count("KAUS"), 1)
        self.assertEqual(calls.count("KDAL"), 2)

    def test_router_uses_fallback_only_for_unserved_station(self) -> None:
        def fake_fetch(provider_id, _provider, stations, *, timeout):
            del timeout
            if provider_id == "fi_fmi_aviation":
                return {
                    "reports": [{
                        "station_id": "EFHK", "raw_text": "METAR EFHK 171420Z CAVOK 24/15 Q1014=",
                        "origin_source_id": provider_id, "origin_country_code": "FI",
                        "origin_authority": "FMI", "origin_url": "fmi", "origin_direct_country_official": True,
                        "origin_fallback_used": False, "origin_fallback_reason": "",
                    }],
                    "response_started_at": "2026-07-17T14:20:01.000Z",
                    "transport": "fmi",
                }
            self.assertEqual(provider_id, "global_noaa_awc_relay")
            self.assertEqual(list(stations), ["EDDM", "EFHK"])
            return {
                "reports": [{
                    "station_id": "EDDM", "raw_text": "METAR EDDM 171420Z CAVOK 25/15 Q1014=",
                    "origin_source_id": provider_id, "origin_country_code": "ZZ",
                    "origin_authority": "AWC", "origin_url": "awc", "origin_direct_country_official": True,
                    "origin_fallback_used": False, "origin_fallback_reason": "",
                }],
                "response_started_at": "2026-07-17T14:20:01.000Z",
                "transport": "awc",
            }

        with patch.object(sources, "_provider_fetch", side_effect=fake_fetch):
            result = sources.fetch_country_routed_temperature_reports(
                ["EFHK", "EDDM"], timeout=1, awc_hours=2,
            )
        by_station = {row["station_id"]: row for row in result["reports"]}
        self.assertTrue(by_station["EFHK"]["origin_direct_country_official"])
        self.assertFalse(by_station["EDDM"]["origin_direct_country_official"])
        self.assertTrue(by_station["EDDM"]["origin_fallback_used"])
        self.assertEqual(by_station["EDDM"]["preferred_origin_source_id"], "de_dwd_pcmet")

    def test_fast_country_provider_is_emitted_before_slow_relay(self) -> None:
        batches = []

        def fake_fetch(provider_id, _provider, stations, *, timeout):
            del timeout
            if provider_id == "global_noaa_awc_relay":
                time.sleep(0.05)
                return {
                    "reports": [{
                        "station_id": "EDDM", "raw_text": "METAR EDDM 171430Z CAVOK 25/15 Q1014=",
                        "origin_source_id": provider_id, "origin_country_code": "ZZ",
                        "origin_authority": "AWC", "origin_url": "awc",
                        "origin_direct_country_official": True, "origin_fallback_used": False,
                        "origin_fallback_reason": "",
                    }],
                    "response_started_at": "2026-07-17T14:30:01.000Z", "transport": "awc",
                }
            self.assertEqual(provider_id, "fi_fmi_aviation")
            return {
                "reports": [{
                    "station_id": "EFHK", "raw_text": "METAR EFHK 171430Z CAVOK 24/15 Q1014=",
                    "origin_source_id": provider_id, "origin_country_code": "FI",
                    "origin_authority": "FMI", "origin_url": "fmi",
                    "origin_direct_country_official": True, "origin_fallback_used": False,
                    "origin_fallback_reason": "",
                }],
                "response_started_at": "2026-07-17T14:30:00.100Z", "transport": "fmi",
            }

        with patch.object(sources, "_provider_fetch", side_effect=fake_fetch):
            sources.fetch_country_routed_temperature_reports(
                ["EFHK", "EDDM"], timeout=1, awc_hours=2,
                on_provider_batch=batches.append,
            )
        self.assertEqual(batches[0]["station_ids"], ["EFHK"])
        self.assertEqual(batches[1]["station_ids"], ["EDDM"])
        self.assertLess(
            batches[0]["provider_completed_after_router_start_ms"],
            batches[1]["provider_completed_after_router_start_ms"],
        )

    def test_newer_awc_shadow_replaces_stale_country_page(self) -> None:
        def fake_fetch(provider_id, _provider, _stations, *, timeout):
            del timeout
            if provider_id == "global_noaa_awc_relay":
                time.sleep(0.02)
                return {
                    "reports": [{
                        "station_id": "EFHK", "raw_text": "METAR EFHK 171430Z CAVOK 25/15 Q1014=",
                        "origin_source_id": provider_id, "origin_authority": "AWC",
                    }],
                    "response_started_at": "2026-07-17T14:30:30.100Z", "transport": "awc",
                }
            return {
                "reports": [{
                    "station_id": "EFHK", "raw_text": "METAR EFHK 171400Z CAVOK 24/15 Q1014=",
                    "origin_source_id": provider_id, "origin_country_code": "FI",
                    "origin_authority": "FMI", "origin_url": "fmi",
                    "origin_direct_country_official": True, "origin_fallback_used": False,
                    "origin_fallback_reason": "",
                }],
                "response_started_at": "2026-07-17T14:20:00.100Z", "transport": "fmi",
            }

        with patch.object(sources, "_provider_fetch", side_effect=fake_fetch):
            result = sources.fetch_country_routed_temperature_reports(
                ["EFHK"], timeout=1, awc_hours=2,
            )
        self.assertEqual({row["raw_text"] for row in result["reports"]}, {
            "METAR EFHK 171400Z CAVOK 24/15 Q1014=",
            "METAR EFHK 171430Z CAVOK 25/15 Q1014=",
        })
        relay = next(row for row in result["reports"] if "171430Z" in row["raw_text"])
        self.assertTrue(relay["origin_fallback_used"])
        self.assertEqual(relay["origin_fallback_reason"], "preferred_provider_stale_vs_awc_relay")

    def test_country_page_suppresses_equal_or_older_awc_shadow(self) -> None:
        def fake_fetch(provider_id, _provider, _stations, *, timeout):
            del timeout
            if provider_id == "global_noaa_awc_relay":
                time.sleep(0.02)
                return {
                    "reports": [{
                        "station_id": "EFHK", "raw_text": "METAR EFHK 171400Z CAVOK 24/15 Q1014=",
                        "origin_source_id": provider_id, "origin_authority": "AWC",
                    }],
                    "response_started_at": "2026-07-17T14:30:30.100Z", "transport": "awc",
                }
            return {
                "reports": [{
                    "station_id": "EFHK", "raw_text": "METAR EFHK 171430Z CAVOK 25/15 Q1014=",
                    "origin_source_id": provider_id, "origin_country_code": "FI",
                    "origin_authority": "FMI", "origin_url": "fmi",
                    "origin_direct_country_official": True, "origin_fallback_used": False,
                    "origin_fallback_reason": "",
                }],
                "response_started_at": "2026-07-17T14:30:00.100Z", "transport": "fmi",
            }

        with patch.object(sources, "_provider_fetch", side_effect=fake_fetch):
            result = sources.fetch_country_routed_temperature_reports(
                ["EFHK"], timeout=1, awc_hours=2,
            )
        self.assertEqual([row["raw_text"] for row in result["reports"]], [
            "METAR EFHK 171430Z CAVOK 25/15 Q1014=",
        ])
        self.assertNotIn("EFHK", result["fallback_reasons"])

    def test_awc_network_request_is_aligned_to_plus_30_seconds(self) -> None:
        provider = {
            **sources.load_official_source_config()["providers"]["global_noaa_awc_relay"],
            "_request_hours": 2.0,
        }
        with patch.object(sources, "_release_clock", return_value=("202607171430", 10.0, "202607171430")), patch.object(
            sources, "_fetch_awc_origin"
        ) as fetch:
            waiting = sources._provider_fetch("global_noaa_awc_relay", provider, ["EDDM"], timeout=1)
        self.assertTrue(waiting["source_deferred"])
        fetch.assert_not_called()

        response = {
            "status": 200, "response_started_at": "2026-07-17T14:30:30.100Z",
            "reports": [], "transport": "awc",
        }
        with patch.object(sources, "_release_clock", return_value=("202607171430", 30.0, "202607171430")), patch.object(
            sources, "_fetch_awc_origin", return_value=response
        ) as fetch:
            sources._provider_fetch("global_noaa_awc_relay", provider, ["EDDM"], timeout=1)
            sources._provider_fetch("global_noaa_awc_relay", provider, ["EDDM"], timeout=1)
        fetch.assert_called_once()

    def test_awc_origin_uses_bounded_requested_settlement_lookback(self) -> None:
        provider = {
            **sources.load_official_source_config()["providers"]["global_noaa_awc_relay"],
            "_request_hours": 6.0,
        }
        with patch.object(
            sources,
            "fetch_awc_temperature_reports",
            return_value={"reports": [], "transport": "awc"},
        ) as fetch:
            value = sources._fetch_awc_origin(
                "global_noaa_awc_relay", provider, ["RKPK"], timeout=1,
            )
        self.assertEqual(fetch.call_args.kwargs["hours"], 6.0)
        self.assertEqual(value["requested_hours"], 6.0)

        provider["_request_hours"] = 999.0
        with patch.object(
            sources,
            "fetch_awc_temperature_reports",
            return_value={"reports": [], "transport": "awc"},
        ) as fetch:
            value = sources._fetch_awc_origin(
                "global_noaa_awc_relay", provider, ["RKPK"], timeout=1,
            )
        self.assertEqual(fetch.call_args.kwargs["hours"], 30.0)
        self.assertEqual(value["requested_hours"], 30.0)


if __name__ == "__main__":
    unittest.main()

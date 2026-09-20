"""Charter 162: TGFTP is the second door on NOAA's own METAR lane.

AWC's TLS certificate expired at 2026-09-19T23:59:59Z.  Every AWC transport
verifies that certificate, so the raw API, the global cache and the per-station
batches failed in the same second and the fleet stopped reading reports at all:
running highs froze, P1 kept recomputing on an eight-hour-old high, and the
truth lane had nothing to capture.  TGFTP publishes the same NOAA reports per
clock hour on a host with its own certificate.
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import reported_trajectory_sources as sources  # noqa: E402
from bayes_basket_target.awc_observation_contract import (  # noqa: E402
    AWC_ORIGIN_URL,
    NOAA_TGFTP_ORIGIN_AUTHORITY,
    NOAA_TGFTP_ORIGIN_SOURCE_ID,
    NOAA_TGFTP_ORIGIN_URL,
    official_origin_lineage_ok,
)

CYCLE = """
2026/09/20 01:00
ZUUU 200100Z 18002MPS 130V210 CAVOK 21/20 Q1020 NOSIG

2026/09/20 00:51
KLGA 200051Z 14009KT 10SM SCT045 BKN120 19/12 A3016 RMK AO2

2026/09/20 00:55
EHAM 200055Z 25016KT 9999 FEW019 SCT027 19/16 Q1017 NOSIG

2026/09/20 00:58
SPECI EDDM 200058Z 20008KT 9999 -RA BKN012 14/13 Q1013

2026/09/20 00:40
KJFK 200040Z 15008KT 10SM CLR 20/13 A3016 RMK AO2
""".strip()


class CycleParsingTest(unittest.TestCase):
    def _reports(self, stations, text=CYCLE):
        sources_get = sources._curl_official_get

        def fake_curl(url, **kwargs):
            self.assertIn("tgftp.nws.noaa.gov", url)
            return text.encode()

        sources._curl_official_get = fake_curl
        try:
            # The direct opener fails in the test environment, so the curl
            # transport is what answers -- the same order production uses.
            return sources._tgftp_cycle_reports(set(stations), cycle_hour=1, timeout=1.0)
        finally:
            sources._curl_official_get = sources_get

    def test_only_the_stations_asked_for_come_back(self) -> None:
        reports = self._reports({"ZUUU", "EHAM"})
        self.assertEqual(
            sorted(row["station_id"] for row in reports), ["EHAM", "ZUUU"]
        )

    def test_a_bare_body_is_typed_as_the_routine_report_it_is(self) -> None:
        """TGFTP omits the METAR keyword; every reader downstream types the
        report by that keyword, so it is spelled back in."""

        [report] = self._reports({"ZUUU"})
        self.assertTrue(report["raw_text"].startswith("METAR ZUUU 200100Z"))
        self.assertEqual(report["source_report_time"], "2026-09-20T01:00:00Z")

    def test_a_speci_keeps_its_own_type(self) -> None:
        [report] = self._reports({"EDDM"})
        self.assertTrue(report["raw_text"].startswith("SPECI EDDM"))

    def test_every_report_carries_its_true_origin(self) -> None:
        [report] = self._reports({"KLGA"})
        self.assertEqual(report["origin_source_id"], NOAA_TGFTP_ORIGIN_SOURCE_ID)
        self.assertEqual(report["origin_url"], NOAA_TGFTP_ORIGIN_URL)
        self.assertEqual(report["origin_transport"], sources.NOAA_TGFTP_TRANSPORT)
        self.assertIs(report["origin_fallback_used"], True)

    def test_the_cycle_url_is_the_hour_asked_for(self) -> None:
        seen = []
        original = sources._curl_official_get

        def fake_curl(url, **kwargs):
            seen.append(url)
            return CYCLE.encode()

        sources._curl_official_get = fake_curl
        try:
            sources._tgftp_cycle_reports({"ZUUU"}, cycle_hour=25, timeout=1.0)
        finally:
            sources._curl_official_get = original
        self.assertTrue(seen[0].endswith("/cycles/01Z.TXT"), seen)


class FallbackTest(unittest.TestCase):
    def test_two_cycles_are_read_and_the_later_one_wins(self) -> None:
        calls = []
        original = sources._curl_official_get

        def fake_curl(url, **kwargs):
            calls.append(url)
            return CYCLE.encode()

        sources._curl_official_get = fake_curl
        try:
            poll = sources.fetch_noaa_tgftp_temperature_reports(
                ["ZUUU"], timeout=1.0,
                reference=datetime(2026, 9, 20, 1, 5, tzinfo=timezone.utc),
            )
        finally:
            sources._curl_official_get = original
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0].endswith("00Z.TXT") and calls[1].endswith("01Z.TXT"))
        self.assertEqual(poll["transport"], sources.NOAA_TGFTP_TRANSPORT)
        self.assertEqual(poll["source_lane"], "awc")
        self.assertEqual(poll["taf_requested"], False)
        self.assertEqual(len(poll["reports"]), 1)

    def test_the_switch_shuts_the_second_door(self) -> None:
        """The suite runs with it shut, so a test that makes AWC fail
        measures that failure and reaches no network."""

        original_open = sources.urllib.request.build_opener
        original_curl = sources._curl_official_get

        class DeadOpener:
            def open(self, *args, **kwargs):
                raise RuntimeError("certificate has expired")

        def refuse(url, **kwargs):
            if "tgftp" in url:
                raise AssertionError("tgftp must not be asked while the switch is off")
            raise RuntimeError("official_curl_exit_60")

        sources.urllib.request.build_opener = lambda *a, **k: DeadOpener()
        sources._curl_official_get = refuse
        os.environ[sources.TGFTP_FALLBACK_ENV] = "off"
        try:
            with self.assertRaises(RuntimeError):
                sources.fetch_awc_temperature_reports(["ZUUU"], hours=1.0, timeout=1.0)
        finally:
            os.environ.pop(sources.TGFTP_FALLBACK_ENV, None)
            sources.urllib.request.build_opener = original_open
            sources._curl_official_get = original_curl

    def test_the_awc_fetch_falls_back_and_says_why(self) -> None:
        """The reason the doors shut rides on every report, so an audit can
        tell a TGFTP report from an AWC one and know what happened."""

        original_curl = sources._curl_official_get
        original_open = sources.urllib.request.build_opener

        def fake_curl(url, **kwargs):
            if "tgftp" in url:
                return CYCLE.encode()
            raise RuntimeError("official_curl_exit_60")

        class DeadOpener:
            def open(self, *args, **kwargs):
                raise RuntimeError("certificate has expired")

        sources._curl_official_get = fake_curl
        sources.urllib.request.build_opener = lambda *a, **k: DeadOpener()
        os.environ[sources.TGFTP_FALLBACK_ENV] = "on"
        try:
            poll = sources.fetch_awc_temperature_reports(["ZUUU"], hours=1.0, timeout=1.0)
        finally:
            os.environ.pop(sources.TGFTP_FALLBACK_ENV, None)
            sources._curl_official_get = original_curl
            sources.urllib.request.build_opener = original_open
        self.assertEqual(poll["transport"], sources.NOAA_TGFTP_TRANSPORT)
        [report] = poll["reports"]
        self.assertEqual(report["station_id"], "ZUUU")
        self.assertIn("awc_all_transports_failed", report["origin_fallback_reason"])

    def test_awc_is_still_preferred_when_it_answers(self) -> None:
        original_curl = sources._curl_official_get
        original_open = sources.urllib.request.build_opener

        def fake_curl(url, **kwargs):
            self.assertNotIn("tgftp", url)
            return b"METAR ZUUU 200100Z 18002MPS CAVOK 21/20 Q1020 NOSIG\n"

        class DeadOpener:
            def open(self, *args, **kwargs):
                raise RuntimeError("proxy")

        sources._curl_official_get = fake_curl
        sources.urllib.request.build_opener = lambda *a, **k: DeadOpener()
        try:
            poll = sources.fetch_awc_temperature_reports(["ZUUU"], hours=1.0, timeout=1.0)
        finally:
            sources._curl_official_get = original_curl
            sources.urllib.request.build_opener = original_open
        self.assertTrue(poll["transport"].startswith("official_awc_"))
        self.assertEqual(poll["reports"][0]["station_id"], "ZUUU")


class OriginContractTest(unittest.TestCase):
    def test_both_noaa_doors_are_admitted(self) -> None:
        self.assertTrue(official_origin_lineage_ok(
            origin_source_id="global_noaa_awc_relay",
            origin_url=AWC_ORIGIN_URL,
            origin_transport="official_awc_raw_metar_batch_direct",
            origin_authority="NOAA/NWS Aviation Weather Center worldwide relay",
        ))
        self.assertTrue(official_origin_lineage_ok(
            origin_source_id=NOAA_TGFTP_ORIGIN_SOURCE_ID,
            origin_url=NOAA_TGFTP_ORIGIN_URL,
            origin_transport=sources.NOAA_TGFTP_TRANSPORT,
            origin_authority=NOAA_TGFTP_ORIGIN_AUTHORITY,
        ))

    def test_a_mismatched_or_unknown_origin_is_still_refused(self) -> None:
        for kwargs in (
            {"origin_source_id": "someone_else"},
            {"origin_url": "https://example.invalid/"},
            {"origin_transport": "scraped_from_a_forum"},
            {"origin_authority": "a person on the internet"},
        ):
            base = {
                "origin_source_id": NOAA_TGFTP_ORIGIN_SOURCE_ID,
                "origin_url": NOAA_TGFTP_ORIGIN_URL,
                "origin_transport": sources.NOAA_TGFTP_TRANSPORT,
                "origin_authority": NOAA_TGFTP_ORIGIN_AUTHORITY,
            }
            base.update(kwargs)
            self.assertFalse(official_origin_lineage_ok(**base), kwargs)

    def test_the_builder_reads_the_same_rule(self) -> None:
        import inspect

        from bayes_basket_target import awc_observation_builder as builder

        source = inspect.getsource(builder)
        self.assertIn("official_origin_lineage_ok(", source)


if __name__ == "__main__":
    unittest.main()

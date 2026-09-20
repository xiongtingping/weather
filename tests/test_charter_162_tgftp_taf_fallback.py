"""Charter 162, second half: the TAF capture's own second door.

The forward TAF capture reads AWC's JSON, which carries the decoded segments
the cloud/precipitation/wind fields come from.  TGFTP carries the bulletin and
nothing else, so those three are absent there -- charter 158 already reads a
TAF with no weather beside it as the SMALL group at full strength -- while the
TX group, which is what P0-M actually multiplies by, is parsed from the text
on both doors.
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target import noaa_tgftp_taf as tgftp  # noqa: E402
from bayes_basket_target.contracts import ContractError  # noqa: E402
from scripts import capture_bayes_basket_target_awc_taf_revision as capture  # noqa: E402

CYCLE_00Z = """
2026/09/20 00:00
TAF ZUUU 192103Z 2000/2024 22003MPS 6000 SCT030 TX25/2007Z TN19/2022Z

2026/09/20 00:20
TAF KJFK 192320Z 2000/2106 12008KT P6SM SCT040 BKN120
     FM200900 12009KT P6SM SCT025 BKN080
     FM202000 17012G19KT 3SM -RA BR SCT007 OVC010

2026/09/20 00:31
TAF AMD EDDM 200030Z 2001/2024 20008KT 9999 BKN012 TX18/2013Z TN11/2004Z
""".strip()


class ParseTest(unittest.TestCase):
    def test_a_single_line_bulletin_reads_whole(self) -> None:
        [row] = tgftp.parse_cycle(CYCLE_00Z, stations=["ZUUU"])
        self.assertEqual(row["icaoId"], "ZUUU")
        self.assertEqual(row["issueTime"], "2026-09-19T21:03:00Z")
        self.assertEqual(row["validTimeFrom"], "2026-09-20T00:00:00Z")
        self.assertEqual(row["validTimeTo"], "2026-09-21T00:00:00Z")
        self.assertIn("TX25/2007Z", row["rawTAF"])
        self.assertTrue(tgftp.is_tgftp_row(row))
        self.assertEqual(row["fcsts"], [])

    def test_the_indented_continuation_lines_belong_to_the_bulletin(self) -> None:
        [row] = tgftp.parse_cycle(CYCLE_00Z, stations=["KJFK"])
        self.assertIn("FM200900", row["rawTAF"])
        self.assertIn("FM202000", row["rawTAF"])
        self.assertNotIn("\n", row["rawTAF"])
        # 2000/2106 crosses midnight and lands on the next day at 06Z.
        self.assertEqual(row["validTimeFrom"], "2026-09-20T00:00:00Z")
        self.assertEqual(row["validTimeTo"], "2026-09-21T06:00:00Z")

    def test_an_amended_bulletin_is_read_at_its_station(self) -> None:
        [row] = tgftp.parse_cycle(CYCLE_00Z, stations=["EDDM"])
        self.assertEqual(row["issueTime"], "2026-09-20T00:30:00Z")

    def test_availability_is_never_before_the_issue_instant(self) -> None:
        """The file names the hour, the bulletin names the minute; a stamp
        behind the issue would claim it was readable before it existed."""

        [row] = tgftp.parse_cycle(CYCLE_00Z, stations=["ZUUU"])
        self.assertGreaterEqual(row["dbPopTime"], row["issueTime"])
        [amended] = tgftp.parse_cycle(CYCLE_00Z, stations=["EDDM"])
        self.assertEqual(amended["dbPopTime"], "2026-09-20T00:31:00Z")

    def test_a_bulletin_from_the_month_before_keeps_its_month(self) -> None:
        text = "2026/10/01 00:05\nTAF ZUUU 302103Z 0100/0124 22003MPS 6000 SCT030 TX25/0107Z"
        [row] = tgftp.parse_cycle(text, stations=["ZUUU"])
        self.assertEqual(row["issueTime"], "2026-09-30T21:03:00Z")
        self.assertEqual(row["validTimeFrom"], "2026-10-01T00:00:00Z")

    def test_stations_not_asked_for_are_dropped(self) -> None:
        self.assertEqual(tgftp.parse_cycle(CYCLE_00Z, stations=["ZBAA"]), [])


class CycleSelectionTest(unittest.TestCase):
    def test_only_the_four_issuance_cycles_are_read(self) -> None:
        """TGFTP files TAFs by issuance cycle; the hours between redirect."""

        seen = []

        def getter(url):
            seen.append(url)
            return CYCLE_00Z.encode()

        rows, url, sha, size = tgftp.fetch_taf_rows(
            "ZUUU", cycles=3, getter=getter,
            reference=datetime(2026, 9, 20, 2, 40, tzinfo=timezone.utc),
        )
        self.assertEqual(
            [entry.rsplit("/", 1)[-1] for entry in seen],
            ["12Z.TXT", "18Z.TXT", "00Z.TXT"],
        )
        self.assertEqual(len(rows), 1)
        self.assertTrue(url.endswith("00Z.TXT"))
        self.assertEqual(size, 3 * len(CYCLE_00Z.encode()))
        self.assertEqual(len(sha), 64)

    def test_one_missing_cycle_does_not_fail_the_fetch(self) -> None:
        def getter(url):
            if url.endswith("18Z.TXT"):
                raise RuntimeError("tgftp_taf_curl_exit_22")
            return CYCLE_00Z.encode()

        rows, _url, _sha, _size = tgftp.fetch_taf_rows(
            "ZUUU", cycles=3, getter=getter,
            reference=datetime(2026, 9, 20, 2, 40, tzinfo=timezone.utc),
        )
        self.assertEqual(len(rows), 1)

    def test_every_cycle_failing_is_a_failed_fetch(self) -> None:
        def getter(url):
            raise RuntimeError("tgftp_taf_curl_exit_60")

        with self.assertRaises(RuntimeError) as caught:
            tgftp.fetch_taf_rows("ZUUU", cycles=2, getter=getter)
        self.assertIn("tgftp_taf_no_rows", str(caught.exception))


class CaptureFallbackTest(unittest.TestCase):
    def setUp(self) -> None:
        self._fetch_json = capture._fetch_json
        self._fetch_rows = tgftp.fetch_taf_rows
        self._env = os.environ.get(tgftp.FALLBACK_ENV)
        os.environ[tgftp.FALLBACK_ENV] = "on"

    def tearDown(self) -> None:
        capture._fetch_json = self._fetch_json
        tgftp.fetch_taf_rows = self._fetch_rows
        if self._env is None:
            os.environ.pop(tgftp.FALLBACK_ENV, None)
        else:
            os.environ[tgftp.FALLBACK_ENV] = self._env

    def _capture(self):
        return capture.capture_forward_awc_taf(
            station_id="ZUUU",
            event_date="2026-09-20",
            settlement_timezone="Asia/Shanghai",
            timeout_sec=1,
        )

    def test_awc_down_is_served_from_tgftp_and_says_so(self) -> None:
        def dead_awc(url, *, timeout_sec):
            raise RuntimeError("curl exit 60: certificate has expired")

        def rows(station_id, *, timeout_sec=12, cycles=3, reference=None, getter=None):
            return (
                tgftp.parse_cycle(CYCLE_00Z, stations=[station_id]),
                "https://tgftp.nws.noaa.gov/data/forecasts/taf/cycles/00Z.TXT",
                "a" * 64,
                len(CYCLE_00Z),
            )

        capture._fetch_json = dead_awc
        tgftp.fetch_taf_rows = rows
        identity = self._capture()
        self.assertEqual(identity["provider"], tgftp.PROVIDER)
        self.assertIn("tgftp.nws.noaa.gov", identity["source_url"])
        self.assertEqual(identity["taf_fields"]["tx_c"], 25.0)
        # No decoded segments on this door: charter 158 reads the missing
        # weather as the SMALL group rather than refusing the capture.
        self.assertIsNone(identity["taf_fields"]["cloud_cover_fraction"])
        self.assertIsNone(identity["taf_fields"]["precipitation_probability"])
        self.assertIsNone(identity["taf_fields"]["wind_speed_mps"])
        self.assertEqual(
            identity["forecast_revision"]["valid_from_utc"], "2026-09-20T00:00:00Z"
        )

    def test_the_switch_shuts_the_second_door(self) -> None:
        """The test suite runs with it shut (tests/conftest.py), so a test
        that makes AWC fail measures that failure and reaches no network."""

        def dead_awc(url, *, timeout_sec):
            raise RuntimeError("curl exit 60: certificate has expired")

        def refuse(station_id, **kwargs):
            raise AssertionError("tgftp must not be asked while the switch is off")

        capture._fetch_json = dead_awc
        tgftp.fetch_taf_rows = refuse
        os.environ[tgftp.FALLBACK_ENV] = "off"
        with self.assertRaises(RuntimeError):
            self._capture()

    def test_a_station_asks_the_second_door_once_for_all_its_days(self) -> None:
        """The round's cache is keyed on the request it would repeat."""

        asked = []

        def dead_awc(url, *, timeout_sec):
            raise RuntimeError("curl exit 60: certificate has expired")

        def rows(station_id, **kwargs):
            asked.append(station_id)
            return (
                tgftp.parse_cycle(CYCLE_00Z, stations=[station_id]),
                "https://tgftp.nws.noaa.gov/data/forecasts/taf/cycles/00Z.TXT",
                "a" * 64,
                len(CYCLE_00Z),
            )

        capture._fetch_json = dead_awc
        tgftp.fetch_taf_rows = rows
        cache: dict[str, object] = {}
        for day in ("2026-09-20", "2026-09-21"):
            try:
                capture.capture_forward_awc_taf(
                    station_id="ZUUU", event_date=day,
                    settlement_timezone="Asia/Shanghai", timeout_sec=1,
                    response_cache=cache,
                )
            except Exception:
                pass
        self.assertEqual(asked, ["ZUUU"])

    def test_both_doors_shut_raises_the_awc_error(self) -> None:
        def dead_awc(url, *, timeout_sec):
            raise RuntimeError("curl exit 60: certificate has expired")

        def dead_tgftp(station_id, **kwargs):
            raise RuntimeError("tgftp_taf_no_rows:empty")

        capture._fetch_json = dead_awc
        tgftp.fetch_taf_rows = dead_tgftp
        with self.assertRaises(RuntimeError) as caught:
            self._capture()
        self.assertIn("certificate has expired", str(caught.exception))

    def test_awc_is_still_the_door_when_it_answers(self) -> None:
        awc_rows = [{
            "icaoId": "ZUUU",
            "rawTAF": "TAF ZUUU 192103Z 2000/2024 22003MPS 6000 SCT030 TX25/2007Z TN19/2022Z",
            "issueTime": "2026-09-19T21:03:00Z",
            "dbPopTime": "2026-09-19T22:01:00Z",
            "validTimeFrom": "2026-09-20T00:00:00Z",
            "validTimeTo": "2026-09-21T00:00:00Z",
            "fcsts": [{
                "timeFrom": int(datetime(2026, 9, 20, 0, tzinfo=timezone.utc).timestamp()),
                "timeTo": int(datetime(2026, 9, 21, 0, tzinfo=timezone.utc).timestamp()),
                "clouds": [{"cover": "SCT", "base": 3000}],
                "wdir": 220, "wspd": 6, "wxString": None, "probability": None,
            }],
        }]

        def live_awc(url, *, timeout_sec):
            return awc_rows, "b" * 64, 512

        def refuse(station_id, **kwargs):
            raise AssertionError("tgftp must not be asked while AWC answers")

        capture._fetch_json = live_awc
        tgftp.fetch_taf_rows = refuse
        identity = self._capture()
        self.assertEqual(identity["provider"], "NOAA Aviation Weather Center")
        self.assertIn("aviationweather.gov", identity["source_url"])
        self.assertEqual(identity["taf_fields"]["tx_c"], 25.0)
        self.assertIsNotNone(identity["taf_fields"]["cloud_cover_fraction"])


class TemperingTest(unittest.TestCase):
    def test_a_tgftp_taf_lands_in_the_small_group(self) -> None:
        """Charter 158's own rule, restated here because charter 162 now
        relies on it: no weather beside the TX means SMALL, pull unchanged."""

        from bayes_basket_target import taf_likelihood as TL

        self.assertEqual(
            TL.disagreement_group(x_served_c=2.5, cloud_cover=None, precip_prob=None),
            TL.GROUP_SMALL,
        )
        self.assertEqual(TL.tempering_lambda(None, TL.GROUP_SMALL), 1.0)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
from unittest.mock import patch

from copy_follow_official_taf import fetch_amsc_taf_peak, fetch_awc_taf_peak


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_awc_taf_extracts_tx_for_non_china_station() -> None:
    raw = "TAF RKSI 201100Z 2012/2118 15006KT 6000 SCT015 BKN035 OVC080 TN23/2020Z TX26/2106Z"

    def opener(_request, *, timeout):
        assert timeout == 4.0
        return _Response([{"icaoId": "RKSI", "issueTime": "2026-07-20T11:00:00Z", "rawTAF": raw}])

    result = fetch_awc_taf_peak(station_id="RKSI", event_date="2026-07-21", opener=opener)
    assert result["state"] == "available"
    assert result["peak_c"] == 26.0
    assert result["display_only"] is True


def test_awc_taf_missing_tx_is_not_a_weather_value() -> None:
    raw = "TAF EGLC 201101Z 2012/2021 02008KT CAVOK BECMG 2015/2018 09011KT"

    def opener(_request, *, timeout):
        return _Response([{"icaoId": "EGLC", "issueTime": "2026-07-20T11:01:00Z", "rawTAF": raw}])

    result = fetch_awc_taf_peak(station_id="EGLC", event_date="2026-07-21", opener=opener)
    assert result["state"] == "unavailable"
    assert result["reason"] == "tx_peak_missing"
    assert result["peak_c"] is None


def test_awc_taf_maps_tx_to_station_local_date() -> None:
    raw = "TAF SAEZ 202300Z 2100/2124 14005KT 3000 -DZ BR OVC006 TX15/2118Z TN10/2109Z"

    def opener(_request, *, timeout):
        return _Response([{"icaoId": "SAEZ", "issueTime": "2026-07-20T23:00:00Z", "rawTAF": raw}])

    result = fetch_awc_taf_peak(station_id="SAEZ", event_date="2026-07-21", opener=opener)
    assert result["state"] == "available"
    assert result["peak_c"] == 15.0
    assert result["rows"][0]["event_date"] == "2026-07-21"


def test_amsc_taf_supplements_awc_when_it_has_tx() -> None:
    raw = "TAF SAEZ 202300Z 2100/2124 14005KT 3000 -DZ BR OVC006 TX15/2118Z TN10/2109Z="
    with patch(
        "copy_follow_official_taf.fetch_amsc_taf_reports",
        return_value={
            "transport": "official_amsc_taf_batch_cccc",
            "reports": [{
                "station_id": "SAEZ",
                "raw_text": raw,
                "source_report_time": "2026-07-20T23:00:00Z",
            }],
        },
    ):
        result = fetch_amsc_taf_peak(station_id="SAEZ", event_date="2026-07-21")
    assert result["state"] == "available"
    assert result["peak_c"] == 15.0
    assert result["source_lane"] == "amsc_taf"

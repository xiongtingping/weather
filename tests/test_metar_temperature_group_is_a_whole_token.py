"""The METAR temperature group is a whole TT/TdTd token.

EGLC 2026-10-21: an automatic report's missing cloud group "//////CB" read as
temperature missing and came before the real group, so ten METARs -- the 13C
high at 09:50Z among them -- never reached the archive, and the day settled a
bucket low (12C against the market's 13C).
"""

from __future__ import annotations

import pytest

from weather_basket_yes_runtime import parse_metar_temperature


@pytest.mark.parametrize("raw, expected", [
    ("METAR EGLC 210950Z AUTO 07004KT 9999 BKN005/// BKN043/// //////CB 13/12 Q0997", 13.0),
    ("METAR EGLC 211350Z AUTO 25017KT 9999 -RA BKN014/// BKN026/// OVC044/// //////TCU 10/08 Q1002 RERA", 10.0),
    ("METAR KLGA 211251Z 05011KT 10SM FEW250 M02/M08 A3034 RMK AO2 SLP275 T10221083", -2.2),
    ("METAR EGLC 211050Z 25017G30KT 9999 -SHRA BKN010CB OVC036 11/09 Q0998 RERA", 11.0),
])
def test_the_temperature_group_is_read_past_a_missing_cloud_group(raw, expected) -> None:
    assert parse_metar_temperature(raw).reported_temperature_c == pytest.approx(expected)


def test_a_report_whose_temperature_is_missing_still_says_so() -> None:
    with pytest.raises(ValueError, match="reported_temperature_missing"):
        parse_metar_temperature("METAR EGLC 211200Z AUTO 00000KT 9999 NCD ///// Q1010")
    with pytest.raises(ValueError, match="metar_temperature_group_missing"):
        parse_metar_temperature("METAR EGLC 211200Z AUTO 00000KT 9999 //////CB Q1010")

"""TAF and METAR taken apart for the board, and read for temperature."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import report_explain as rex  # noqa: E402

ZUUU_TAF = "TAF ZUUU 180901Z 1812/1912 02003MPS 6000 SCT050 TX26/1907Z TN20/1822Z"


def test_a_taf_is_taken_apart_token_by_token() -> None:
    parsed = rex.explain_taf(ZUUU_TAF)
    meanings = {t["token"]: t["meaning"] for t in parsed["tokens"]}
    assert parsed["kind"] == "TAF"
    assert parsed["tx_c"] == 26 and parsed["tx_at"] == "19 日 07Z"
    assert parsed["tn_c"] == 20
    assert meanings["1812/1912"].startswith("有效期 18 日 12Z")
    assert "疏云" in meanings["SCT050"]
    assert "3 米/秒" in meanings["02003MPS"]


def test_an_amended_taf_is_special_and_its_weather_is_read() -> None:
    parsed = rex.explain_taf(
        "TAF AMD RPLL 121700Z 1218/1324 01008KT 9999 FEW025 OVC100 TX31/1306Z "
        "TN26/1221Z TEMPO 1218/1224 VRB03KT -TSRA BKN100"
    )
    assert parsed["kind"] == "TAF AMD"
    assert parsed["base_cover"] == 1.0          # OVC in the base forecast
    assert parsed["precip"] is True
    assert parsed["change_groups"] == ["TEMPO"]
    reading = rex.taf_temperature_reading(parsed, 29.0)
    assert any("偏暖" in line for line in reading)   # TX 31 vs model 29
    assert any("多云到阴" in line for line in reading)
    assert any("雷雨" in line for line in reading)


def test_a_taf_in_line_with_the_model_says_so() -> None:
    reading = rex.taf_temperature_reading(rex.explain_taf(ZUUU_TAF), 25.8)
    assert reading[0].endswith("基本一致")


def test_a_metar_is_read_against_the_running_high_and_the_peak() -> None:
    parsed = rex.explain_metar("METAR ZUUU 181400Z VRB01MPS 9999 SCT050 22/20 Q1018 NOSIG")
    assert parsed["temp_c"] == 22 and parsed["dew_c"] == 20
    assert parsed["trend"] == "NOSIG"
    reading = rex.metar_temperature_reading(
        parsed, running_high_c=24, forecast_peak_c=24.3,
        previous_temps=[23], local_hour=22,
    )
    assert reading[0] == "现在 22°C，今天已到最高 24°C"
    assert any("回落" in line for line in reading)
    assert any("已过午后峰值" in line for line in reading)
    assert any("接近露点" in line for line in reading)


def test_a_speci_with_rain_is_flagged() -> None:
    parsed = rex.explain_metar("SPECI RJTT 180512Z 18012KT 3000 +RA BKN008 OVC020 21/20 Q1009")
    assert parsed["kind"] == "SPECI"
    assert parsed["precip"] is True
    reading = rex.metar_temperature_reading(
        parsed, running_high_c=23, forecast_peak_c=26.0,
        previous_temps=[22], local_hour=14,
    )
    assert any("特殊报" in line for line in reading)
    assert any("偏冷" in line for line in reading)
    assert any("还差" in line for line in reading)


def test_a_taf_is_read_against_the_mean_it_is_given_by_name() -> None:
    """The board passes each TAF its own day's served mean; the line says which."""

    reading = rex.taf_temperature_reading(
        rex.explain_taf(ZUUU_TAF), 23.9, model_label="09-19 的 P0-M 均值")
    assert reading[0].startswith("预报最高温 26°C，比09-19 的 P0-M 均值 23.9°C 高 2.1°C")


def test_repeated_change_groups_fold_into_a_count() -> None:
    parsed = rex.explain_taf(
        "TAF SBGR 180800Z 1812/1918 11005KT 9999 BKN008 TX25/1917Z TN15/1909Z "
        "BECMG 1812/1814 BKN016 BECMG 1815/1817 BKN025 TEMPO 1818/1821 BKN011"
    )
    line = next(x for x in rex.taf_temperature_reading(parsed, None) if x.startswith("含变化组"))
    assert "逐渐转为 ×2" in line and "逐渐转为、逐渐转为" not in line


def test_a_metar_is_cut_into_its_groups_for_the_table() -> None:
    """User 2026-10-10「metar改为表格形式，拆分报文及对应简要说明和趋势」."""

    g = rex.metar_groups(
        "METAR ZUUU 092300Z 33002MPS 300V020 8000 -SHRA FEW040TCU SCT040 18/17 Q1022 NOSIG")
    assert g["wind"]["raw"] == "33002MPS 300V020"
    assert g["wind"]["text"] == "西北风 2 m/s，摆动 300°–020°" and g["wind"]["mps"] == 2.0
    assert g["vis"] == {"raw": "8000", "text": "8 km", "m": 8000.0}
    assert [w["text"] for w in g["wx"]] == ["小阵雨"] and g["wx"][0]["precip"] is True
    assert [c["text"] for c in g["cloud"]] == ["少云 1220 m · 浓积云", "疏云 1220 m"]
    assert (g["temp"]["temp_c"], g["temp"]["dew_c"]) == (18, 17)
    assert g["qnh"]["hpa"] == 1022.0
    assert g["trend"] == {"raw": "NOSIG", "text": "两小时内无变化"}
    assert g["other"] == []


def test_a_us_metar_reads_knots_miles_inches_and_stops_at_the_remarks() -> None:
    g = rex.metar_groups(
        "METAR KLGA 101551Z 05012G20KT 10SM -RA BKN015 OVC030 14/12 A2992 RMK AO2 SLP132 P0003")
    assert g["wind"]["text"] == "东北风 6 m/s，阵风 10 m/s"
    assert g["vis"]["text"] == "16 km"
    assert g["qnh"]["hpa"] == 1013.2
    assert [c["raw"] for c in g["cloud"]] == ["BKN015", "OVC030"]
    assert g["trend"] is None and g["other"] == []


def test_a_taf_is_cut_into_its_base_and_change_periods_on_the_clock() -> None:
    """User 2026-10-10「TAF 同理」: periods as UTC instants, each with its groups."""

    g = rex.taf_groups(
        "TAF ZUUU 092103Z 1000/1024 30003MPS 6000 SCT040 TX23/1007Z TN18/1022Z TEMPO 1000/1003 -SHRA",
        "2026-10-09T21:03:00Z")
    assert (g["valid_from_utc"], g["valid_to_utc"]) == ("2026-10-10T00:00:00Z", "2026-10-11T00:00:00Z")
    assert g["base"]["wind"]["text"] == "西北风 3 m/s" and g["base"]["vis"]["text"] == "6 km"
    assert g["tx"] == {"raw": "TX23/1007Z", "c": 23, "at_utc": "2026-10-10T07:00:00Z"}
    assert g["tn"]["c"] == 18
    [tempo] = g["changes"]
    assert tempo["raw"] == "TEMPO 1000/1003 -SHRA"
    assert (tempo["from_utc"], tempo["to_utc"]) == ("2026-10-10T00:00:00Z", "2026-10-10T03:00:00Z")
    assert (tempo["word"], tempo["text"]) == ("短时", "短时 小阵雨")


def test_fm_periods_run_to_the_next_fm_and_prob_joins_its_tempo() -> None:
    g = rex.taf_groups(
        "TAF AMD KLGA 101736Z 1018/1124 05012KT P6SM BKN030 FM102200 06010KT 5SM -RA OVC015 "
        "PROB30 TEMPO 1102/1106 2SM RA BR OVC008 FM111200 36008KT P6SM SCT040",
        "2026-10-10T17:36:00Z")
    assert g["amend"] == "AMD"
    assert [c["kind"] for c in g["changes"]] == ["FM", "PROB30 TEMPO", "FM"]
    fm1, prob, fm2 = g["changes"]
    assert (fm1["from_utc"], fm1["to_utc"]) == ("2026-10-10T22:00:00Z", "2026-10-11T12:00:00Z")
    assert fm2["to_utc"] == g["valid_to_utc"] == "2026-10-12T00:00:00Z"
    assert prob["text"].startswith("30% 可能 短时")


def test_a_taf_day_past_the_month_end_rolls_into_the_next_month() -> None:
    g = rex.taf_groups("TAF ZUUU 301700Z 3018/0118 VRB01MPS 9999 NSC TX25/0107Z", "2026-09-30T17:00:00Z")
    assert g["valid_to_utc"] == "2026-10-01T18:00:00Z"
    assert g["tx"]["at_utc"] == "2026-10-01T07:00:00Z"

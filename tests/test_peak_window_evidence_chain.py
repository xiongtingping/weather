import importlib.util
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "peak_window_evidence_chain.py"
spec = importlib.util.spec_from_file_location("peak_window_evidence_chain", SCRIPT_PATH)
pec = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(pec)


def test_compact_url_redacts_weather_api_key():
    url = "https://api.weather.com/v3/wx/observations/current?apiKey=secret&icaoCode=ZUUU&units=m"

    assert pec.compact_url(url) == (
        "https://api.weather.com/v3/wx/observations/current"
        "?apiKey=REDACTED&icaoCode=ZUUU&units=m"
    )


def test_parse_wu_display_html_extracts_current_temperature_and_update_text():
    html = """
    <p><i>access_time</i><strong> 5:28 PM CST on July 3, 2026</strong> (GMT +8)
    <span class="separator">| </span><span class="last-updated">Updated just now</span></p>
    <div>Chengdu Shuangliu Intl Airport Station | Report</div>
    <div class="conditions-circle">85 ° F like 90°</div>
    <script src="/bundle.js?apiKey=abc123"></script>
    """

    parsed = pec.parse_wu_display_html(html, "Asia/Shanghai")

    assert parsed["display_updated_text"] == "Updated just now"
    assert parsed["display_access_time_text"] == "5:28 PM CST on July 3, 2026 (GMT +8)"
    assert parsed["station_label"] == "Chengdu Shuangliu Intl Airport Station | Report"
    assert parsed["temperature_f"] == 85.0
    assert parsed["temperature_c"] == 29.444
    assert parsed["api_key_found"] is True


def test_parse_awc_cache_text_extracts_station_row():
    text = "\n".join(
        [
            "raw_text,station_id,observation_time,temp_c,dewpoint_c,wind_dir_degrees,wind_speed_kt,altim_in_hg,flight_category,metar_type",
            '"METAR AAAA 030900Z 01/00 Q1000",AAAA,2026-07-03T09:00:00.000Z,1,0,0,0,29.50,VFR,METAR',
            '"METAR ZUUU 030900Z 34002MPS 300V020 9999 FEW033 SCT050 30/20 Q1008 NOSIG",ZUUU,2026-07-03T09:00:00.000Z,30,20,340,4,29.76,VFR,METAR',
        ]
    )

    parsed = pec.parse_awc_cache_text(text, "ZUUU", "Asia/Shanghai")

    assert parsed["ok"] is True
    assert parsed["station_id"] == "ZUUU"
    assert parsed["temperature_c"] == 30.0
    assert parsed["valid_time_utc"] == "2026-07-03T09:00:00.000Z"
    assert parsed["valid_time_local"].startswith("2026-07-03T17:00:00")
    assert parsed["raw_text"].startswith("METAR ZUUU 030900Z")

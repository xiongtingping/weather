import json

from amsc_raw_history_archive import DAILY_COVERAGE_DIRECTORY, raw_text_sha256
from scripts import fetch_zuuu_metar_taf as fetcher
from scripts.fetch_zuuu_metar_taf import (
    build_amsc_observation_bundle,
    filtered_report_texts,
    query_params,
    source_payloads_from_amsc_payload,
)
from metar_strong_likelihood_runtime import build_metar_strong_likelihood


SAMPLE_PAYLOAD = {
    "code": 200,
    "msg": "操作成功",
    "data": [
        {
            "cccc": "ZUUU",
            "airportName": "成都双流机场",
            "airportEnName": "CHENGDU",
            "order": 0,
            "content": "METAR ZUUU 051430Z 18003MPS 140V210 CAVOK 32/20 Q1001 NOSIG=",
        },
        {
            "cccc": "ZUUU",
            "airportName": "成都双流机场",
            "airportEnName": "CHENGDU",
            "order": 0,
            "content": (
                "TAF ZUUU 050904Z 0512/0612 20003MPS 6000 SCT040 "
                "TX36/0607Z TN27/0522Z TEMPO 0513/0516 FEW030CB SCT030="
            ),
        },
        {
            "cccc": "ZUUU",
            "content": "SPECI ZUUU 051431Z 18003MPS CAVOK 32/20 Q1001=",
        },
    ],
}


def test_explicit_chrome_profile_root_survives_daemon_home_boundary(tmp_path, monkeypatch):
    chrome_root = tmp_path / "owner-chrome"
    leveldb = chrome_root / "Default" / "Local Storage" / "leveldb"
    leveldb.mkdir(parents=True)
    token = "1234$$12345678-1234-1234-1234-123456789abc"
    (leveldb / "000001.log").write_bytes(b"https://www.amsc.net.cn sessionId " + token.encode("ascii"))
    monkeypatch.setenv("AMSC_CHROME_PROFILE_ROOT", str(chrome_root))
    monkeypatch.delenv("AMSC_SESSION_ID", raising=False)
    monkeypatch.delenv("AMSC_TOKEN", raising=False)

    candidates = fetcher.token_candidates()
    assert candidates[0] == token
    assert token in candidates


def test_filtered_report_texts_keeps_metar_speci_and_taf_prefixes():
    reports = filtered_report_texts(SAMPLE_PAYLOAD)

    assert len(reports) == 3
    assert reports[0].startswith("METAR ZUUU 051430Z")
    assert reports[1].startswith("TAF ZUUU 050904Z")
    assert reports[2].startswith("SPECI ZUUU 051431Z")


def test_amsc_query_requests_special_report_type_in_addition_to_regular_metar():
    assert "SP" in {value for value in query_params("token", 6)["tt"].split(",") if value}


def test_amsc_payloads_parse_metar_speci_and_taf_fact_layer_fields():
    sources = source_payloads_from_amsc_payload(
        SAMPLE_PAYLOAD,
        fetched_at="2026-07-05T14:31:00Z",
        duration_ms=321.0,
    )

    assert len(sources) == 3
    metar = sources[0]
    assert metar["source"] == "amsc_metar"
    assert metar["event_date"] == "2026-07-05"
    assert metar["valid_time_utc"] == "2026-07-05T14:30:00Z"
    assert metar["valid_time_local"] == "2026-07-05T22:30:00+08:00"
    assert metar["temperature_c"] == 32.0
    assert metar["dewpoint_c"] == 20.0
    assert metar["visibility_m"] == 9999.0
    assert metar["cloud_text"] == "CAVOK"
    assert metar["cloud_cover_pct"] == 5.0
    assert metar["low_cloud_cover_pct"] == 5.0
    assert metar["wind_dir_deg"] == 180.0
    assert metar["wind_speed_mps"] == 3.0
    assert metar["pressure_hpa"] == 1001.0
    assert metar["raw_ref"].startswith("METAR ZUUU")
    assert metar["report_type"] == "METAR"
    assert metar["first_seen_at"] == "2026-07-05T14:31:00Z"
    assert metar["as_of_eligible_at"] == "2026-07-05T14:31:00Z"
    assert metar["arrival_time_quality"] == "observed_first_seen"

    taf = sources[1]
    assert taf["source"] == "amsc_taf"
    assert taf["issue_time_utc"] == "2026-07-05T09:04:00Z"
    assert taf["valid_time_utc"] == "2026-07-06T07:00:00Z"
    assert taf["valid_time_local"] == "2026-07-06T15:00:00+08:00"
    assert taf["forecast_peak_c"] == 36.0
    assert taf["source_modes_schema"] == "forecast_source_modes_v1"
    assert taf["source_modes"][0]["mode_id"] == "taf_tx_primary"
    assert taf["source_modes"][0]["mode_probability"] == 1.0
    assert taf["raw_ref"].startswith("TAF ZUUU")

    speci = sources[2]
    assert speci["source"] == "amsc_metar"
    assert speci["report_type"] == "SPECI"
    assert speci["valid_time_utc"] == "2026-07-05T14:31:00Z"
    assert speci["temperature_c"] == 32.0
    assert speci["raw_ref"].startswith("SPECI ZUUU")


def test_amsc_payloads_capture_taf_amd_as_weak_forecast_revision():
    sources = source_payloads_from_amsc_payload(
        {
            "data": [
                {
                    "cccc": "ZUUU",
                    "content": (
                        "TAF AMD ZUUU 150530Z 1506/1606 02004MPS 6000 SCT040 "
                        "TX37/1508Z TN26/1522Z="
                    ),
                }
            ]
        },
        fetched_at="2026-07-15T05:31:00Z",
    )
    assert len(sources) == 1
    taf = sources[0]
    assert taf["source_id"] == "zuuu_taf_forecast_amsc"
    assert taf["forecast_revision_kind"] == "amendment"
    assert taf["taf_modifier"] == "AMD"
    assert taf["forecast_peak_c"] == 37.0
    assert taf["forecast_weather_regime"] == "clear_dry"
    assert taf["source_revision_id"].startswith("tafrev_")
    assert taf["first_seen_at"] == "2026-07-15T05:31:00Z"


def test_metar_current_observation_is_separated_from_trend_and_keeps_all_fields():
    payload = {
        "code": 200,
        "data": [
            {
                "cccc": "ZUUU",
                "content": (
                    "METAR ZUUU 100600Z 01005G08MPS 330V040 7000 FEW040TCU "
                    "32/24 Q0998 BECMG TL0730 -TSRA FEW030CB="
                ),
            }
        ],
    }

    metar = source_payloads_from_amsc_payload(
        payload,
        fetched_at="2026-07-10T06:01:00Z",
        duration_ms=123.0,
    )[0]

    assert metar["cloud_text"] == "FEW040TCU"
    assert metar["weather_text"] == ""
    assert metar["metar_current_no_significant_weather"] is True
    assert metar["metar_trend_text"].startswith("BECMG TL0730")
    assert "TSRA" in metar["metar_trend_weather_text"]
    assert metar["metar_trend_cloud_text"] == "FEW030CB"
    assert metar["wind_dir_deg"] == 10.0
    assert metar["wind_speed_mps"] == 5.0
    assert metar["wind_gust_mps"] == 8.0
    assert metar["wind_variable"] is True
    assert metar["wind_variable_from_deg"] == 330.0
    assert metar["wind_variable_to_deg"] == 40.0
    assert metar["visibility_m"] == 7000.0
    assert metar["pressure_hpa"] == 998.0


def test_observation_bundle_uses_source_observation_contract_and_stays_read_only():
    bundle = build_amsc_observation_bundle(
        SAMPLE_PAYLOAD,
        fetched_at="2026-07-05T14:31:00Z",
        duration_ms=321.0,
        include_payload=True,
    )

    assert bundle["schema"] == "amsc_zuuu_metar_taf_observation_bundle_v1"
    assert bundle["read_only"] is True
    assert bundle["guards"]["writes_archive_db"] is False
    assert bundle["guards"]["writes_facts_sqlite"] is False
    assert bundle["source_payload_count"] == 3
    trace = bundle["source_acquisition_trace"]
    assert trace["schema"] == "weather_source_acquisition_trace_v1"
    assert trace["source_class_counts"]["hard_observation_candidate"] == 2
    assert trace["source_class_counts"]["forecast_weak_source"] == 1

    by_source = {row["source_id"]: row for row in bundle["source_observations"]}
    assert by_source["zuuu_metar_raw_amsc"]["source_observation_id"].startswith("srcobs_")
    assert by_source["zuuu_metar_raw_amsc"]["hard_eligible"] is True
    assert by_source["zuuu_metar_raw_amsc"]["temperature_c"] == 32.0
    assert by_source["zuuu_taf_forecast_amsc"]["hard_eligible"] is False
    assert by_source["zuuu_taf_forecast_amsc"]["forecast_peak_c"] == 36.0
    hard_report_types = {
        row["payload"]["report_type"]
        for row in bundle["source_observations"]
        if row["source_id"] == "zuuu_metar_raw_amsc"
    }
    assert hard_report_types == {"METAR", "SPECI"}


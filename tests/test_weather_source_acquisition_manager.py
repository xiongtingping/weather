from amsc_raw_history_archive import raw_text_sha256
from source_observation_runtime import SOURCE_CLASSES
from weather_source_acquisition_manager import (
    build_source_acquisition_trace,
    build_trace_from_peak_window_sample,
    build_trace_from_refresh_result,
    normalize_source_id,
    observation_from_source_payload,
    source_acquisition_catalog,
    source_payloads_from_fact_payload,
)


def test_source_id_aliases_preserve_role_boundaries():
    assert normalize_source_id("wu_display_html") == "wu_display_current"
    assert normalize_source_id("wu_page_api_current_icao_metric") == "wu_page_api_current"
    assert normalize_source_id("weather_com_current") == "twc_current_icao_metric"
    assert normalize_source_id("taf_peak") == "zuuu_taf_forecast_peak"
    assert normalize_source_id("amsc_metar") == "zuuu_metar_raw_amsc"
    assert normalize_source_id("amsc_taf") == "zuuu_taf_forecast_amsc"
    assert normalize_source_id("orderbook") == "market_realtime_orderbook"


def test_source_acquisition_catalog_lists_roles_lanes_and_write_policy():
    catalog = source_acquisition_catalog()

    assert catalog["schema"] == "weather_source_acquisition_catalog_v1"
    assert catalog["read_only"] is True
    assert catalog["guards"]["writes_archive_db"] is False
    assert catalog["guards"]["places_orders"] is False
    by_id = {row["source_id"]: row for row in catalog["sources"]}

    awc = by_id["zuuu_metar_raw_awc"]
    assert awc["owner"] == "weather_source_acquisition_manager"
    assert awc["source_class"] == SOURCE_CLASSES["hard_observation"]
    assert awc["hard_eligible"] is True
    assert awc["lane"] == "hard_source_reference_awc_raw_metar"
    assert awc["cache_ttl_sec"] == 15
    assert awc["source_role_tier"] == "reference"
    assert awc["primary_source_id"] == "zuuu_metar_raw_amsc"
    assert awc["write_policy"] == "normalized_trace_only_no_db_write"
    assert "source_observation_id" in awc["required_latency_fields"]
    assert "source_event_id" in awc["required_latency_fields"]
    assert "valid_time" in awc["required_latency_fields"]

    amsc_metar = by_id["zuuu_metar_raw_amsc"]
    assert amsc_metar["source_class"] == SOURCE_CLASSES["hard_observation"]
    assert amsc_metar["hard_eligible"] is True
    assert amsc_metar["lane"] == "hard_source_primary_amsc_raw_metar"
    assert amsc_metar["cache_ttl_sec"] == 1800
    assert amsc_metar["source_role_tier"] == "primary"
    assert amsc_metar["source_priority"] > awc["source_priority"]
    assert "amsc_metar" in amsc_metar["aliases"]
    assert amsc_metar["write_policy"] == "normalized_trace_only_no_db_write"

    amsc_taf = by_id["zuuu_taf_forecast_amsc"]
    assert amsc_taf["source_class"] == SOURCE_CLASSES["forecast_weak"]
    assert amsc_taf["hard_eligible"] is False
    assert amsc_taf["lane"] == "forecast_weak_primary_amsc_taf"
    assert amsc_taf["cache_ttl_sec"] == 1800
    assert amsc_taf["source_role_tier"] == "primary"
    assert "amsc_taf" in amsc_taf["aliases"]

    twc_current = by_id["twc_current_icao_metric"]
    assert twc_current["source_class"] == SOURCE_CLASSES["diagnostic_current"]
    assert twc_current["hard_eligible"] is False
    assert twc_current["lane"] == "external_current_diagnostic"
    assert "twc_current" in twc_current["aliases"]

    gfs = by_id["gfs_model_peak"]
    assert gfs["source_class"] == SOURCE_CLASSES["forecast_weak"]
    assert gfs["lane"] == "forecast_weak_likelihood"
    assert gfs["timeout_ms"] == 5000


def test_awc_payload_becomes_hard_observation_with_latency_fields():
    obs = observation_from_source_payload(
        {
            "source": "awc_cache_metars_csv_gz",
            "provider": "NOAA AviationWeather cache",
            "station_id": "ZUUU",
            "valid_time_utc": "2026-07-03T09:00:00Z",
            "valid_time_local": "2026-07-03T17:00:00+08:00",
            "temperature_c": 30.0,
            "dewpoint_c": 22.0,
            "raw_text": "ZUUU 030900Z 00000MPS 9999 FEW020 30/22 Q1004",
            "transport": {
                "url": "https://aviationweather.gov/data/cache/metars.cache.csv.gz",
                "request_completed_at": "2026-07-03T09:05:36Z",
                "duration_ms": 431,
            },
        },
        event_date="2026-07-03",
    )

    assert obs.source_class == SOURCE_CLASSES["hard_observation"]
    assert obs.hard_eligible is True
    assert obs.response_duration_ms == 431
    assert obs.valid_time == "2026-07-03T09:00:00Z"
    assert obs.valid_time_local == "2026-07-03T17:00:00+08:00"
    assert obs.temperature_c == 30.0
    assert obs.raw_ref.endswith("metars.cache.csv.gz")
    assert obs.source_event_id.startswith("srcevt_")
    assert obs.source_observation_id.startswith("srcobs_")


def test_raw_archive_lineage_overrides_poll_payload_hash_without_losing_metadata():
    raw_text = "METAR ZUUU 051430Z 18003MPS CAVOK 32/20 Q1001 NOSIG="
    obs = observation_from_source_payload(
        {
            "source": "amsc_metar",
            "station_id": "ZUUU",
            "valid_time_utc": "2026-07-05T14:30:00Z",
            "valid_time_local": "2026-07-05T22:30:00+08:00",
            "temperature_c": 32.0,
            "raw_ref": raw_text,
            "raw_text": raw_text,
            "raw_sha256": raw_text_sha256(raw_text),
            "raw_object_ref": "objects/aa/example.json.gz",
            "first_seen_at": "2026-07-05T14:31:00Z",
            "arrival_time_quality": "observed_first_seen",
            "verified_metar_valid_time": "2026-07-05T14:30:00Z",
            "transport": {"request_completed_at": "2026-07-05T14:31:00Z"},
        },
        event_date="2026-07-05",
    )

    assert obs.raw_sha256 == raw_text_sha256(raw_text)
    payload = obs.as_dict()["payload"]
    assert payload["raw_object_ref"] == "objects/aa/example.json.gz"
    assert payload["first_seen_at"] == "2026-07-05T14:31:00Z"
    assert payload["arrival_time_quality"] == "observed_first_seen"


def test_hard_source_final_flags_and_raw_text_survive_source_observation_payload():
    trace = build_source_acquisition_trace(
        station_id="ZUUU",
        event_date="2026-07-05",
        fetched_at="2026-07-05T13:05:02Z",
        include_payload=True,
        sources=[
            {
                "source": "awc_cache_metars_csv_gz",
                "provider": "NOAA AviationWeather cache",
                "station_id": "ZUUU",
                "valid_time_utc": "2026-07-05T13:00:00Z",
                "valid_time_local": "2026-07-05T21:00:00+08:00",
                "temperature_c": 36.0,
                "observed_today_max_c": 37.0,
                "observed_today_max_final": True,
                "hard_source_final_daily_max": True,
                "temperature_observation_final_daily_max": True,
                "hard_source_final_lock_reason": "awc_latest_covers_event_end",
                "raw_text": "ZUUU 051300Z 00000MPS 9999 FEW020 36/22 Q1004",
                "transport": {
                    "url": "https://aviationweather.gov/data/cache/metars.cache.csv.gz",
                    "request_completed_at": "2026-07-05T13:05:02Z",
                    "duration_ms": 431,
                },
            }
        ],
    )

    row = trace["source_observations"][0]
    payload = row["payload"]
    assert row["source_class"] == SOURCE_CLASSES["hard_observation"]
    assert row["observed_today_max_c"] == 37.0
    assert payload["observed_today_max_final"] is True
    assert payload["hard_source_final_daily_max"] is True
    assert payload["temperature_observation_final_daily_max"] is True
    assert payload["hard_source_final_lock_reason"] == "awc_latest_covers_event_end"
    assert "FEW020" in payload["raw_text"]


def test_wu_twc_current_stays_diagnostic_not_hard_source():
    trace = build_source_acquisition_trace(
        station_id="ZUUU",
        event_date="2026-07-03",
        sources=[
            {
                "source": "twc_current_icao_metric",
                "station_id": "ZUUU",
                "valid_time_utc": "2026-07-03T09:15:37Z",
                "valid_time_local": "2026-07-03T17:15:37+0800",
                "temperature_c": 30.0,
                "transport": {"url": "https://api.weather.com/v3/wx/observations/current?icaoCode=ZUUU", "duration_ms": 120},
            },
            {
                "source": "wu_display_html",
                "valid_time_local_text": "5:10 PM CST on July 3, 2026",
                "temperature_c": 29.4,
                "transport": {"url": "https://www.wunderground.com/weather/cn/chengdu", "duration_ms": 230},
            },
        ],
        fetched_at="2026-07-03T09:15:40Z",
    )

    assert trace["read_only"] is True
    assert trace["guards"]["writes_archive_db"] is False
    assert trace["guards"]["places_orders"] is False
    assert trace["source_class_counts"][SOURCE_CLASSES["diagnostic_current"]] == 2
    assert trace["hard_observation_candidate_ids"] == []
    assert len(trace["source_observation_ids"]) == 2
    for row in trace["source_latency_trace"]:
        assert row["source_observation_id"].startswith("srcobs_")
        assert row["hard_eligible"] is False
        assert "hard_source" in row["demotion_reason"] or "not_hard_source" in row["demotion_reason"]
        assert row["raw_ref"]


def test_forecast_market_and_settlement_sources_keep_separate_roles():
    trace = build_source_acquisition_trace(
        station_id="ZUUU",
        event_date="2026-07-03",
        sources=[
            {
                "source": "zuuu_taf_forecast_peak",
                "valid_time_utc": "2026-07-03T07:00:00Z",
                "forecast_peak_c": 33.0,
                "raw_ref": "TAF ZUUU 0305/0405 TX33/0307Z",
            },
            {
                "source": "current_model_ensemble",
                "valid_time_utc": "2026-07-03T00:00:00Z",
                "forecast_peak_c": 32.5,
                "raw_ref": "model_snapshot:123",
            },
            {
                "source": "market_realtime_orderbook",
                "valid_time_utc": "2026-07-03T09:00:02Z",
                "raw_ref": "clob:market:slug",
            },
            {
                "source": "settlement_actual",
                "valid_time_utc": "2026-07-03T16:00:00Z",
                "observed_today_max_c": 32.0,
                "raw_ref": "settlement:official",
            },
        ],
    )

    assert trace["source_class_counts"][SOURCE_CLASSES["forecast_weak"]] == 2
    assert trace["source_class_counts"][SOURCE_CLASSES["market"]] == 1
    assert trace["source_class_counts"][SOURCE_CLASSES["settlement"]] == 1
    assert len(trace["forecast_weak_ids"]) == 2
    settlement = [row for row in trace["source_latency_trace"] if row["source_id"] == "settlement_actual"][0]
    assert settlement["hard_eligible"] is True
    assert settlement["observed_today_max_c"] == 32.0


def test_peak_window_sample_builds_trace_without_payload_by_default():
    trace = build_trace_from_peak_window_sample(
        {
            "station_id": "ZUUU",
            "event_date": "2026-07-03",
            "sample_completed_at": "2026-07-03T09:05:40Z",
            "sources": [
                {
                    "source": "awc_cache_metars_csv_gz",
                    "valid_time_utc": "2026-07-03T09:00:00Z",
                    "temperature_c": 30.0,
                    "transport": {"url": "https://aviationweather.gov/data/cache/metars.cache.csv.gz", "duration_ms": 400},
                }
            ],
        }
    )

    assert trace["schema"] == "weather_source_acquisition_trace_v1"
    assert trace["station_id"] == "ZUUU"
    assert trace["event_date"] == "2026-07-03"
    assert trace["quality_summary"]["source_count"] == 1
    assert "payload" not in trace["source_observations"][0]
    assert trace["source_observations"][0]["source_observation_id"].startswith("srcobs_")
    assert trace["source_latency_trace"][0]["source_observation_id"].startswith("srcobs_")
    assert trace["next_boundary"] == "fact_snapshot_writer_dry_run_or_no_apply"


def test_awc_refresh_result_builds_hard_source_latency_trace():
    trace = build_trace_from_refresh_result(
        {
            "ok": True,
            "status": "updated",
            "event_date": "2026-07-03",
            "station_id": "ZUUU",
            "fact_as_of": "2026-07-03T09:05:36Z",
            "awc_latest_at": "2026-07-03T09:00:00Z",
            "awc_today_max_at": "2026-07-03T09:00:00Z",
            "awc_today_max_c": 30.0,
            "awc_latest_temp_c": 30.0,
            "awc_latest_dewpoint_c": 22.0,
            "fetch_duration_ms": 431,
            "awc_source_url": "https://aviationweather.gov/data/cache/metars.cache.csv.gz",
        },
        source_family="awc",
    )

    assert trace["refresh_source_family"] == "awc"
    assert trace["refresh_status"] == "updated"
    assert trace["source_class_counts"][SOURCE_CLASSES["hard_observation"]] == 1
    assert len(trace["hard_observation_candidate_ids"]) == 1
    row = trace["source_latency_trace"][0]
    assert row["source_id"] == "zuuu_metar_raw_awc"
    assert row["source_observation_id"].startswith("srcobs_")
    assert row["hard_eligible"] is True
    assert row["valid_time"] == "2026-07-03T09:00:00Z"
    assert row["fetched_at"] == "2026-07-03T09:05:36Z"
    assert row["response_duration_ms"] == 431
    assert row["observed_today_max_c"] == 30.0


def test_wu_twc_refresh_result_keeps_current_diagnostic_and_forecast_weak():
    trace = build_trace_from_refresh_result(
        {
            "ok": True,
            "status": "updated",
            "event_date": "2026-07-03",
            "station_id": "ZUUU",
            "fact_as_of": "2026-07-03T09:10:02Z",
            "source_features": {
                "wu_twc_observed_intraday_today_max_c": 30.0,
                "wu_twc_observed_intraday_current_temp_c": 29.5,
                "wu_twc_observed_intraday_today_max_at": "2026-07-03T09:10:00Z",
                "wu_twc_observed_intraday_fetched_at": "2026-07-03T09:10:02Z",
                "wu_twc_observed_intraday_fetch_duration_ms": 210,
                "wu_twc_observed_intraday_source_url": "https://www.wunderground.com/weather/cn/chengdu",
                "wu_forecast_peak_c": 32.0,
                "wu_forecast_peak_at": "2026-07-03T07:00:00Z",
                "wu_forecast_peak_source": "wu_forecast:test",
                "weather_com_forecast_peak_c": 33.0,
                "weather_com_forecast_peak_valid_local": "2026-07-03T15:00:00+08:00",
                "weather_com_forecast_peak_source_url": "https://weather.com",
            },
        },
        source_family="wu_twc",
    )

    assert trace["refresh_source_family"] == "wu_twc"
    assert trace["hard_observation_candidate_ids"] == []
    assert trace["source_class_counts"][SOURCE_CLASSES["diagnostic_current"]] == 1
    assert trace["source_class_counts"][SOURCE_CLASSES["forecast_weak"]] == 2
    by_id = {row["source_id"]: row for row in trace["source_latency_trace"]}
    assert by_id["wu_twc_observed_intraday"]["hard_eligible"] is False
    assert by_id["wu_forecast_high"]["source_class"] == SOURCE_CLASSES["forecast_weak"]
    assert by_id["weather_com_forecast_peak"]["source_class"] == SOURCE_CLASSES["forecast_weak"]


def test_fact_payload_source_builder_keeps_hard_diagnostic_and_weak_boundaries():
    payload = {
        "weather_likelihood_features": {
            "wu_twc_observed_intraday_today_max_c": 30.0,
            "wu_twc_observed_intraday_current_temp_c": 29.5,
            "wu_twc_observed_intraday_today_max_at": "2026-07-03T09:10:00Z",
            "wu_twc_observed_intraday_fetched_at": "2026-07-03T09:10:02Z",
            "wu_twc_observed_intraday_fetch_duration_ms": 210,
            "wu_twc_observed_intraday_source_url": "https://www.wunderground.com/weather/cn/chengdu",
            "forecast_peak_temp_c": 32.4,
            "forecast_valid_time": "2026-07-03T08:00:00Z",
            "wu_forecast_peak_c": 32.0,
            "wu_forecast_peak_at": "2026-07-03T07:00:00Z",
            "wu_forecast_peak_source": "wu_forecast:test",
            "weather_com_forecast_peak_c": 33.0,
            "weather_com_forecast_peak_valid_local": "2026-07-03T15:00:00+08:00",
            "weather_com_forecast_peak_source_url": "https://weather.com",
            "taf_forecast_peak_c": 31.0,
            "taf_forecast_peak_raw": "TAF ZUUU 0300/0324 TX31/0307Z",
            "taf_forecast_peak_at": "2026-07-03T07:00:00Z",
        },
        "model_members_decimal_raw": {
            "gfs_seamless": [31.8, 32.0, 32.2],
            "icon_seamless": [30.8, 31.0, 31.2],
            "ecmwf_ifs025": [32.6, 32.8, 33.0],
        },
        "model_member_cache_snapshot": {
            "models": {
                "gfs_seamless": {
                    "member_count": 3,
                    "mean_c": 32.0,
                    "created_at": "2026-07-03T00:10:00Z",
                    "official_updated_source": "gfs-cache",
                }
            }
        },
    }
    sources = source_payloads_from_fact_payload(
        payload,
        station_id="ZUUU",
        event_date="2026-07-03",
        fact_as_of="2026-07-03T09:05:36Z",
        hard_observation={
            "ok": True,
            "source_id": "awc_cache_metars_csv_gz",
            "observed_at": "2026-07-03T09:00:00Z",
            "fetched_at": "2026-07-03T09:05:36Z",
            "fetch_duration_ms": 431,
            "temperature_c": 29.5,
            "observed_today_max_c": 30.0,
            "dewpoint_c": 22.0,
            "raw": "ZUUU 030900Z 9999 FEW020 30/22 Q1004",
        },
    )
    trace = build_source_acquisition_trace(
        sources=sources,
        station_id="ZUUU",
        event_date="2026-07-03",
        fetched_at="2026-07-03T09:05:36Z",
    )

    by_id = {row["source_id"]: row for row in trace["source_latency_trace"]}
    assert by_id["awc_cache_metars_csv_gz"]["source_class"] == SOURCE_CLASSES["hard_observation"]
    assert by_id["awc_cache_metars_csv_gz"]["hard_eligible"] is True
    assert by_id["awc_cache_metars_csv_gz"]["temperature_c"] == 29.5
    assert by_id["awc_cache_metars_csv_gz"]["observed_today_max_c"] == 30.0
    assert by_id["wu_twc_observed_intraday"]["source_class"] == SOURCE_CLASSES["diagnostic_current"]
    assert by_id["wu_twc_observed_intraday"]["hard_eligible"] is False
    assert by_id["current_model_ensemble"]["source_class"] == SOURCE_CLASSES["forecast_weak"]
    assert by_id["current_model_ensemble"]["forecast_peak_c"] == 32.4
    assert by_id["wu_forecast_high"]["source_class"] == SOURCE_CLASSES["forecast_weak"]
    assert by_id["weather_com_forecast_peak"]["source_class"] == SOURCE_CLASSES["forecast_weak"]
    assert by_id["zuuu_taf_forecast_peak"]["source_class"] == SOURCE_CLASSES["forecast_weak"]
    assert by_id["gfs_model_peak"]["source_class"] == SOURCE_CLASSES["forecast_weak"]
    assert by_id["icon_model_peak"]["source_class"] == SOURCE_CLASSES["forecast_weak"]
    assert by_id["ecmwf_model_peak"]["source_class"] == SOURCE_CLASSES["forecast_weak"]
    assert by_id["gfs_model_peak"]["forecast_peak_c"] == 32.0

from source_observation_runtime import (
    SOURCE_CLASSES,
    build_source_observation,
    raw_payload_sha256,
    source_defaults,
)


def test_awc_observation_is_hard_eligible_and_has_source_event_id():
    obs = build_source_observation(
        source_id="zuuu_metar_raw_awc",
        station_id="ZUUU",
        event_date="2026-07-03",
        valid_time="2026-07-03T09:00:00Z",
        fetched_at="2026-07-03T09:05:30Z",
        temperature_c=30.0,
        raw_payload="ZUUU 030900Z ... 30/22",
    )
    data = obs.as_dict()

    assert data["source_class"] == SOURCE_CLASSES["hard_observation"]
    assert data["hard_eligible"] is True
    assert data["demotion_reason"] == ""
    assert data["source_event_id"].startswith("srcevt_")
    assert data["source_observation_id"].startswith("srcobs_")
    assert data["raw_sha256"] == raw_payload_sha256("ZUUU 030900Z ... 30/22")


def test_wu_twc_current_defaults_to_diagnostic_not_hard_source():
    for source_id in ("wu_display_current", "twc_current_icao_metric"):
        obs = build_source_observation(
            source_id=source_id,
            station_id="ZUUU",
            event_date="2026-07-03",
            valid_time="2026-07-03T09:35:39Z",
            temperature_c=29.0,
        )

        assert obs.source_class == SOURCE_CLASSES["diagnostic_current"]
        assert obs.hard_eligible is False
        assert "hard_source" in obs.demotion_reason or "not_hard_source" in obs.demotion_reason


def test_unknown_source_is_not_hard_eligible():
    defaults = source_defaults("new_vendor_current")

    assert defaults["source_class"] == SOURCE_CLASSES["unknown"]
    assert defaults["hard_eligible"] is False
    assert defaults["demotion_reason"] == "source_not_registered"


def test_forecast_source_stays_weak_likelihood():
    obs = build_source_observation(
        source_id="zuuu_taf_forecast_peak",
        station_id="ZUUU",
        event_date="2026-07-03",
        valid_time="2026-07-03T07:00:00Z",
        forecast_peak_c=33.0,
    )

    assert obs.source_class == SOURCE_CLASSES["forecast_weak"]
    assert obs.hard_eligible is False
    assert obs.forecast_peak_c == 33.0


def test_canonical_logical_forecast_sources_are_registered_weak_sources():
    for source_id in ("ecmwf", "icon", "gfs", "jma", "taf", "wu", "twc"):
        defaults = source_defaults(source_id)
        assert defaults["source_class"] == SOURCE_CLASSES["forecast_weak"]
        assert defaults["hard_eligible"] is False
        assert defaults["demotion_reason"] == "forecast_source_not_hard_observation"


def test_current_and_forecast_aliases_keep_layer_boundaries():
    current = build_source_observation(
        source_id="wu_page_api_current_icao_metric",
        station_id="ZUUU",
        event_date="2026-07-03",
        valid_time="2026-07-03T09:15:00Z",
        temperature_c=30.0,
    )
    twc_forecast = build_source_observation(
        source_id="weather_com_forecast_peak",
        station_id="ZUUU",
        event_date="2026-07-03",
        valid_time="2026-07-03T00:00:00Z",
        forecast_peak_c=33.0,
    )
    model = build_source_observation(
        source_id="ecmwf_model_peak",
        station_id="ZUUU",
        event_date="2026-07-03",
        valid_time="2026-07-03T00:00:00Z",
        forecast_peak_c=32.5,
    )

    assert current.source_class == SOURCE_CLASSES["diagnostic_current"]
    assert current.hard_eligible is False
    assert twc_forecast.source_class == SOURCE_CLASSES["forecast_weak"]
    assert twc_forecast.hard_eligible is False
    assert model.source_class == SOURCE_CLASSES["forecast_weak"]
    assert model.hard_eligible is False

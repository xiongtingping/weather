import json
from io import BytesIO
from urllib.error import HTTPError

from scripts import capture_current_forecast_source_forward as capture


def test_capture_archives_wu_and_twc_without_production_writes(monkeypatch):
    monkeypatch.setattr(
        capture,
        "_fetch_open_meteo_model_curve",
        lambda **kwargs: {
            "ok": True,
            "value_c": 34.0,
            "forecast_peak_c": 34.0,
            "hourly_curve": [{"valid_time_local": "2026-07-12T14:00", "temperature_c": 34.0}],
            "model": kwargs["model"],
            "requested_at": "2026-07-11T12:00:00.000Z",
            "response_started_at": "2026-07-11T12:00:00.010Z",
            "response_completed_at": "2026-07-11T12:00:00.200Z",
            "capture_time_provenance": "observed_transport",
            "response_headers_observed": True,
            "raw_provider_response_utf8": '{"hourly":{}}',
        },
    )
    monkeypatch.setattr(
        capture,
        "_fetch_wunderground_inline_forward_curve",
        lambda **_kwargs: {
            "ok": True,
            "value_c": 35.0,
            "hourly_curve": [],
            "requested_at": "2026-07-11T12:00:00.000Z",
            "response_started_at": "2026-07-11T12:00:00.010Z",
            "response_completed_at": "2026-07-11T12:00:00.200Z",
            "capture_time_provenance": "observed_transport",
            "response_headers_observed": True,
            "raw_provider_response_utf8": "<html>safe wu page</html>",
        },
    )
    monkeypatch.setattr(
        capture.swm,
        "fetch_weather_com_hourly_peak_metrics",
        lambda *_args, **_kwargs: {"ok": True, "value_c": 36.0, "hourly_curve": [{"valid_time_local": "2026-07-12T14:00:00+08:00", "temperature_c": 36.0}]},
    )
    monkeypatch.setattr(
        capture.swm,
        "fetch_weather_com_forecast_peak_metrics",
        lambda *_args, **_kwargs: {"ok": True, "value_c": 36.0},
    )
    writes = []
    monkeypatch.setattr(capture, "write_forward_metrics_snapshot", lambda **kwargs: writes.append(kwargs) or {"ok": True, "state": "written"})

    report = capture.capture_current_forward_sources(event_date="2026-07-12", allow_research_write=True)

    assert [row["source_id"] for row in writes] == [
        "ecmwf_model_peak",
        "gfs_model_peak",
        "icon_model_peak",
        "wu_forecast_peak",
        "weather_com_forecast_peak",
    ]
    assert writes[4]["metrics"]["auxiliary_hourly_curve"][0]["temperature_c"] == 36.0
    assert writes[4]["metrics"]["forecast_value_kind"] == "daily_max_event_window"
    assert writes[4]["metrics"]["trajectory_eligible"] is False
    assert writes[0]["capture_time_provenance"] == "observed_transport"
    assert writes[0]["response_completed_at"] == "2026-07-11T12:00:00.200Z"
    assert writes[0]["raw_provider_response_utf8"] == '{"hourly":{}}'
    assert writes[3]["capture_time_provenance"] == "observed_transport"
    assert writes[3]["response_headers_observed"] is True
    assert report["guards"]["writes_archive_db"] is False
    assert report["guards"]["places_orders"] is False


def test_capture_isolates_vendor_fetch_failure_from_other_forward_sources(monkeypatch):
    monkeypatch.setattr(
        capture,
        "_fetch_open_meteo_model_curve",
        lambda **kwargs: {"ok": False, "error": "model_unavailable"},
    )
    monkeypatch.setattr(
        capture,
        "_fetch_wunderground_inline_forward_curve",
        lambda **_kwargs: (_ for _ in ()).throw(TimeoutError()),
    )
    monkeypatch.setattr(
        capture.swm,
        "fetch_wunderground_forecast_peak_metrics",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError()),
    )
    monkeypatch.setattr(
        capture.swm,
        "fetch_weather_com_hourly_peak_metrics",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError()),
    )
    monkeypatch.setattr(
        capture.swm,
        "fetch_weather_com_forecast_peak_metrics",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError()),
    )

    report = capture.capture_current_forward_sources(event_date="2026-07-12")

    assert report["results"]["wu_forecast_peak"]["state_reason"] == "wu_fetch_failed:TimeoutError"
    assert report["results"]["weather_com_forecast_peak"]["state_reason"] == "twc_daily_fetch_failed:TimeoutError"
    assert report["results"]["gfs_model_peak"]["state_reason"] == "model_unavailable"


def test_capture_records_twc_as_observed_transport_only_with_header_evidence(monkeypatch):
    monkeypatch.setattr(
        capture,
        "_fetch_open_meteo_model_curve",
        lambda **kwargs: {"ok": False, "error": "model_unavailable"},
    )
    monkeypatch.setattr(
        capture,
        "_fetch_wunderground_inline_forward_curve",
        lambda **_kwargs: {"ok": False, "error": "wu_unavailable"},
    )
    monkeypatch.setattr(capture.swm, "fetch_wunderground_forecast_peak_metrics", lambda *_args, **_kwargs: {"ok": False, "error": "wu_unavailable"})
    monkeypatch.setattr(
        capture.swm,
        "fetch_weather_com_hourly_peak_metrics",
        lambda *_args, **_kwargs: {"ok": True, "hourly_curve": []},
    )
    monkeypatch.setattr(
        capture.swm,
        "fetch_weather_com_forecast_peak_metrics",
        lambda *_args, **_kwargs: {
            "ok": True,
            "value_c": 36.0,
            "source_url": "https://api.weather.com/forecast",
            "requested_at": "2026-07-12T01:00:00.000Z",
            "response_started_at": "2026-07-12T01:00:00.010Z",
                "response_completed_at": "2026-07-12T01:00:00.200Z",
                "capture_time_provenance": "observed_transport",
                "response_headers_observed": True,
                "raw_provider_response_utf8": '{"temperatureMax":[36]}',
        },
    )
    writes = []
    monkeypatch.setattr(capture, "write_forward_metrics_snapshot", lambda **kwargs: writes.append(kwargs) or {"ok": True, "state": "written"})

    capture.capture_current_forward_sources(event_date="2026-07-12", allow_research_write=True)

    twc = next(row for row in writes if row["source_id"] == "weather_com_forecast_peak")
    assert twc["capture_time_provenance"] == "observed_transport"
    assert twc["response_headers_observed"] is True
    assert twc["requested_at"] == "2026-07-12T01:00:00.000Z"
    assert twc["response_completed_at"] == "2026-07-12T01:00:00.200Z"
    assert twc["raw_provider_response_utf8"] == '{"temperatureMax":[36]}'
    assert "raw_provider_response_utf8" not in twc["metrics"]


def test_capture_refuses_to_persist_sensitive_provider_body_as_tier_a_evidence(monkeypatch):
    transport = capture._transport_capture_kwargs(
        {
            "requested_at": "2026-07-12T01:00:00.000Z",
            "response_started_at": "2026-07-12T01:00:00.010Z",
            "response_completed_at": "2026-07-12T01:00:00.200Z",
            "capture_time_provenance": "observed_transport",
            "raw_provider_response_utf8": '{"apiKey":"secret","temperatureMax":[36]}',
        },
        fallback_observed_at="2026-07-12T01:01:00.000Z",
    )

    assert transport["capture_time_provenance"] == "batch_clock_proxy"
    assert transport["raw_provider_response_utf8"] == ""


def test_wu_inline_capture_records_response_header_boundary_before_body(monkeypatch):
    class _Response:
        status_code = 200
        text = "<html>embedded hourly forecast</html>"

    monkeypatch.setattr(capture.swm, "_wunderground_forecast_hourly_source", lambda _source: "https://wu.example/hourly")
    calls = []
    monkeypatch.setattr(capture.swm, "requests_get", lambda *_args, **kwargs: calls.append(kwargs) or _Response())
    monkeypatch.setattr(capture.swm, "_extract_wunderground_hourly_api_url", lambda _body: "https://api.example/hourly?units=e")
    monkeypatch.setattr(
        capture.swm,
        "_extract_wunderground_inline_hourly_rows",
        lambda *_args, **_kwargs: [
            {"valid_time_local": "2026-07-12T13:00:00+08:00", "temperature": 90.0, "unit": "F"},
            {"valid_time_local": "2026-07-12T14:00:00+08:00", "temperature": 91.0, "unit": "F"},
        ],
    )

    result = capture._fetch_wunderground_inline_forward_curve(
        resolution_source="https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
        event_date="2026-07-12",
        station_id="ZUUU",
    )

    assert result["ok"] is True
    assert result["forecast_peak_c"] == 32.77777777777778
    assert result["capture_time_provenance"] == "observed_transport"
    assert result["response_headers_observed"] is True
    assert result["raw_provider_response_utf8"] == "<html>embedded hourly forecast</html>"
    assert calls[0]["stream"] is True


def test_wu_inline_capture_rejects_credential_shaped_page_before_parsing_or_persisting(monkeypatch):
    class _Response:
        status_code = 200
        text = '<html>apiKey="secret"</html>'

    monkeypatch.setattr(capture.swm, "_wunderground_forecast_hourly_source", lambda _source: "https://wu.example/hourly")
    monkeypatch.setattr(capture.swm, "requests_get", lambda *_args, **_kwargs: _Response())

    result = capture._fetch_wunderground_inline_forward_curve(
        resolution_source="https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
        event_date="2026-07-12",
        station_id="ZUUU",
    )

    assert result == {"ok": False, "error": "wu_hourly_page_raw_body_not_safe_for_immutable_capture"}


def test_capture_latest_report_is_published_by_atomic_replace(monkeypatch, tmp_path):
    output = tmp_path / "hourly_capture_latest.json"
    original_replace = capture.os.replace
    seen = {}

    def assert_atomic_replace(source, destination):
        seen["source_exists"] = source.exists()
        seen["destination_existed_before_replace"] = destination.exists()
        original_replace(source, destination)

    monkeypatch.setattr(capture.os, "replace", assert_atomic_replace)

    capture._write_json_atomically(output, {"schema": "test", "value": 1})

    assert seen == {"source_exists": True, "destination_existed_before_replace": False}
    assert json.loads(output.read_text(encoding="utf-8")) == {"schema": "test", "value": 1}
    assert not list(tmp_path.glob(".*.tmp.*"))


def test_open_meteo_429_records_shared_cooldown_without_creating_forecast_evidence(monkeypatch, tmp_path):
    headers = {"Retry-After": "120"}
    error = HTTPError(
        "https://api.open-meteo.com/v1/forecast",
        429,
        "rate limited",
        headers,
        BytesIO(b'{"reason":"rate limited"}'),
    )
    monkeypatch.setattr(capture, "OPEN_METEO_RATE_LIMIT_STATE", tmp_path / "rate-limit.json")
    monkeypatch.setattr(capture, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))

    result = capture._fetch_open_meteo_model_curve(
        source_id="gfs_model_peak",
        model="gfs_seamless",
        event_date="2026-07-12",
        latitude=30.5728,
        longitude=104.0668,
    )

    assert result["ok"] is False
    assert result["error"] == "open_meteo_http_429"
    assert result["availability"]["provider_state"] == "rate_limited"
    assert result["availability"]["retry_not_before"]
    assert result["provider_error_body_present"] is True
    persisted = json.loads((tmp_path / "rate-limit.json").read_text(encoding="utf-8"))
    assert persisted["boundary"]["stores_prediction"] is False


def test_open_meteo_shared_cooldown_skips_network_attempt(monkeypatch, tmp_path):
    from open_meteo_rate_limit_runtime import record_rate_limit

    state = tmp_path / "rate-limit.json"
    record_rate_limit(
        state_path=state,
        observed_at="2026-07-12T08:00:00Z",
        source="test",
        retry_after_seconds=900,
    )
    monkeypatch.setattr(capture, "OPEN_METEO_RATE_LIMIT_STATE", state)
    monkeypatch.setattr(capture, "_utc_now", lambda: "2026-07-12T08:05:00Z")
    monkeypatch.setattr(capture, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not fetch")))

    result = capture._fetch_open_meteo_model_curve(
        source_id="gfs_model_peak",
        model="gfs_seamless",
        event_date="2026-07-12",
        latitude=30.5728,
        longitude=104.0668,
    )

    assert result["ok"] is False
    assert result["error"] == "open_meteo_rate_limit_cooldown_active"

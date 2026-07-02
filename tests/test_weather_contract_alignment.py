import datetime as dt

import pytest

import scan_weather_market as swm
from scripts.market_ws_listener import (
    TickCoalescer,
    TokenBinding,
    _write_ticks,
    parse_args,
    rows_from_ws_message,
)


def test_parse_station_from_wunderground_history_url() -> None:
    url = "https://www.wunderground.com/history/daily/cn/shanghai/ZSPD"
    assert swm.parse_station_from_resolution_source(url) == "ZSPD"


def test_parse_station_from_weather_gov_timeseries_url() -> None:
    url = "https://www.weather.gov/wrh/timeseries?site=RCTP&hours=72"
    assert swm.parse_station_from_resolution_source(url) == "RCTP"


def test_extract_resolution_source_prefers_weather_rule_url() -> None:
    text = "Rules use https://www.weather.gov/wrh/timeseries?site=UUWW after finalization."
    assert swm.extract_resolution_source_from_text(text) == "https://www.weather.gov/wrh/timeseries?site=UUWW"


def test_contract_profile_requires_station_for_temperature_market() -> None:
    profile = swm.build_weather_contract_profile(
        title="Highest temperature in Taipei on April 4?",
        slug="highest-temperature-in-taipei-on-april-4-2026",
        description="Data will come from NOAA.",
        resolution_source="https://www.weather.gov/wrh/timeseries",
        outcome_labels=["20°C", "21°C"],
        event_date="2026-04-04",
        market_var="temperature_2m_max",
        market_unit="C",
    )
    assert profile["ok"] is False
    assert "station_id" in profile["missing_reasons"]


def test_wunderground_weather_page_is_auxiliary_not_tradeable() -> None:
    profile = swm.build_weather_contract_profile(
        title="Highest temperature in Shanghai on April 30?",
        slug="highest-temperature-in-shanghai-on-april-30-2026",
        description="Rules reference a Wunderground weather page.",
        resolution_source="https://www.wunderground.com/weather/cn/shanghai/ZSPD",
        outcome_labels=["20°C", "21°C"],
        event_date="2026-04-30",
        market_var="temperature_2m_max",
        market_unit="C",
    )

    assert profile["ok"] is False
    assert profile["station_id"] == "ZSPD"
    assert profile["source_kind"] == "wunderground_aux"
    assert profile["source_aux_only"] is True
    assert profile["source_usable_for_settlement"] is False
    assert profile["rules_tradeable"] is False
    assert "resolution_source_untrusted" in profile["missing_reasons"]


def test_wunderground_history_is_settlement_source() -> None:
    profile = swm.build_weather_contract_profile(
        title="Highest temperature in Shanghai on April 30?",
        slug="highest-temperature-in-shanghai-on-april-30-2026",
        description="Rules use finalized data from Wunderground.",
        resolution_source="https://www.wunderground.com/history/daily/cn/shanghai/ZSPD",
        outcome_labels=["20°C", "21°C"],
        event_date="2026-04-30",
        market_var="temperature_2m_max",
        market_unit="C",
    )

    assert profile["ok"] is True
    assert profile["source_kind"] == "wunderground_history"
    assert profile["source_confidence"] >= 0.9
    assert profile["source_fit_tier"] == "settlement_source"
    assert profile["source_trust_tier"] == "settlement_source"
    assert profile["source_usable_for_settlement"] is True
    assert profile["rules_tradeable"] is True


def test_resolution_source_rejects_station_mismatch() -> None:
    profile = swm.weather_resolution_source_profile(
        "https://www.wunderground.com/history/daily/cn/shanghai/ZSPD",
        station_id="ZBAA",
        description="Rules use finalized data from Wunderground.",
    )

    assert profile["source_fit_tier"] == "station_mismatch"
    assert profile["source_usable_for_settlement"] is False
    assert profile["source_station_id"] == "ZSPD"
    assert profile["source_station_expected"] == "ZBAA"
    assert profile["source_station_match"] is False
    assert profile["source_na_reason"] == "resolution_station_mismatch"


def test_expected_temperature_peak_hour_prefers_forecast_peak_timestamp(monkeypatch) -> None:
    monkeypatch.delenv("PM_TEMPERATURE_PEAK_HOUR_LOCAL", raising=False)

    hour, meta = swm._expected_temperature_peak_hour_local(
        "2026-06-25",
        "Asia/Shanghai",
        station_id="ZUUU",
        forecast_features={
            "weather_com_forecast_peak_at": "2026-06-25T13:30:00+08:00",
        },
    )

    assert hour == pytest.approx(13.5)
    assert meta["expected_peak_hour_source"] == "weather_com_forecast_peak_at"
    assert meta["expected_peak_hour_basis"] == "forecast_peak_timestamp"


def test_expected_temperature_peak_hour_ignores_forecast_peak_timestamp_for_other_date(monkeypatch) -> None:
    monkeypatch.delenv("PM_TEMPERATURE_PEAK_HOUR_LOCAL", raising=False)

    hour, meta = swm._expected_temperature_peak_hour_local(
        "2026-06-25",
        "Asia/Shanghai",
        station_id="ZUUU",
        forecast_features={
            "weather_com_forecast_peak_at": "2026-06-24T13:30:00+08:00",
        },
    )

    assert hour == pytest.approx(16.0)
    assert meta["expected_peak_hour_source"] == "chengdu_monthly_seasonal_peak_hour_prior_v1"


def test_non_history_wunderground_source_marks_rules_incomplete() -> None:
    event = {
        "title": "Highest temperature in Shanghai on April 30?",
        "slug": "highest-temperature-in-shanghai-on-april-30-2026",
        "description": "Rules reference Wunderground.",
        "resolutionSource": "https://www.wunderground.com/weather/cn/shanghai/ZSPD",
    }

    assert swm.weather_rules_incomplete(event) is True


def test_observed_nowcast_rejects_auxiliary_wunderground_source() -> None:
    value, source = swm.fetch_observed_nowcast_value(
        "https://www.wunderground.com/weather/cn/shanghai/ZSPD",
        "ZSPD",
        "2026-04-30",
        "temperature_2m_max",
        "C",
    )

    assert value is None
    assert source == "source_not_tradeable:wu_non_history_source"


def test_wunderground_forecast_peak_is_soft_not_settlement_or_observed(monkeypatch) -> None:
    monkeypatch.setattr(
        swm,
        "fetch_wunderground_daily_temp",
        lambda resolution_source, event_date, kind, market_unit, **kwargs: 30.0,
    )

    metrics = swm.fetch_wunderground_forecast_peak_metrics(
        "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
        "2999-06-12",
        "temperature_2m_max",
        "C",
    )

    assert metrics["ok"] is True
    assert metrics["value_c"] == 30.0
    assert metrics["source"] == "wunderground_forecast_daily_peak"
    assert metrics["source_trust_tier"] == "soft_forecast"
    assert metrics["source_usable_for_settlement"] is False
    assert metrics["request_timeout"] == [2.0, 4.0]
    assert metrics["request_retries"] == 1


def test_weather_com_forecast_peak_matches_event_date_and_stays_display_only(monkeypatch) -> None:
    class FakeResponse:
        status_code = 200
        url = "https://api.weather.com/v3/wx/forecast/daily/10day?apiKey=secret"

        def json(self):
            return {
                "validTimeLocal": [
                    "2999-06-11T07:00:00+0800",
                    "2999-06-12T07:00:00+0800",
                ],
                "calendarDayTemperatureMax": [29, 31],
            }

    calls = []

    def fake_requests_get(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return FakeResponse()

    monkeypatch.setenv("PM_WEATHER_COM_API_KEY", "secret")
    monkeypatch.setattr(swm, "requests_get", fake_requests_get)

    metrics = swm.fetch_weather_com_forecast_peak_metrics(
        30.558257,
        103.945966,
        "2999-06-12",
        "temperature_2m_max",
        "C",
        station_id="ZUUU",
        resolution_source="https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
    )

    assert metrics["ok"] is True
    assert metrics["value_c"] == 31.0
    assert metrics["source"] == "weather_com_forecast_daily_peak"
    assert metrics["source_trust_tier"] == "soft_forecast_display_only"
    assert metrics["source_usable_for_settlement"] is False
    assert metrics["posterior_included"] is False
    assert metrics["not_observed_nowcast"] is True
    assert "secret" not in metrics["source_url"]
    assert calls[0]["params"]["geocode"] == "30.558257,103.945966"


def test_iem_noaaport_metar_cross_check_reports_same_day_max(monkeypatch) -> None:
    class FakeResponse:
        status_code = 200
        url = "https://mesonet.agron.iastate.edu/request/download.phtml?station=ZUUU"
        text = "\n".join(
            [
                "station,valid,tmpc,dwpc,metar",
                "ZUUU,2026-06-22 07:00,32,17,METAR ZUUU 220700Z 00000KT CAVOK 32/17 Q1000",
                "ZUUU,2026-06-22 08:00,33,17,METAR ZUUU 220800Z 00000KT CAVOK 33/17 Q1000",
            ]
        )

        def raise_for_status(self):
            return None

    monkeypatch.setattr(swm, "requests_get", lambda *args, **kwargs: FakeResponse())

    features = swm._fetch_iem_noaaport_metar_cross_check_features(
        "ZUUU",
        target_event_date="2026-06-22",
        tz="Asia/Shanghai",
        hours=48,
    )
    features.update(
        {
            "zuuu_metar_raw_awc_latest_temp_c": 33.0,
            "zuuu_metar_raw_awc_today_max_c": 33.0,
        }
    )
    summary = swm._build_metar_cross_check_summary(features)

    assert features["zuuu_metar_iem_noaaport_available"] is True
    assert features["zuuu_metar_iem_noaaport_today_max_c"] == 33.0
    assert summary["available_count"] == 1
    assert summary["aligned_count"] == 1
    assert summary["sources"][0]["status"] == "aligned"


def test_twc_pws_rapid_history_is_auxiliary_not_final_settlement(monkeypatch) -> None:
    class FakeResponse:
        status_code = 200
        url = "https://api.weather.com/v2/pws/observations/all/1day?stationId=PWS1"

        @staticmethod
        def json():
            return {
                "observations": [
                    {"obsTimeUtc": "2026-06-22T07:00:00Z", "metric": {"temp": 31.0}},
                    {"obsTimeUtc": "2026-06-22T08:00:00Z", "metric": {"temp": 33.0}},
                ]
            }

        def raise_for_status(self):
            return None

    monkeypatch.setenv("PM_TWC_PWS_RAPID_HISTORY_STATION_ID", "PWS1")
    monkeypatch.setenv("PM_TWC_PWS_API_KEY", "secret")
    monkeypatch.setattr(swm, "requests_get", lambda *args, **kwargs: FakeResponse())

    features = swm.fetch_twc_pws_rapid_history_aux_features(
        "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
        "ZUUU",
        "2026-06-22",
        tz="Asia/Shanghai",
    )

    assert features["twc_pws_rapid_history_aux_available"] is True
    assert features["twc_pws_rapid_history_aux_today_max_c"] == 33.0
    assert features["twc_pws_rapid_history_aux_usable_for_final_settlement"] is False
    assert "observed_today_max_c" not in features
    assert "settlement_actual" not in features


def test_scan_orderbook_uses_split_timeout_for_fast_failure(monkeypatch) -> None:
    calls = []

    def fake_requests_get(url, **kwargs):
        calls.append({"url": url, **kwargs})
        raise swm.requests.RequestException("slow clob")

    monkeypatch.setenv("PM_SCAN_CLOB_BOOK_TIMEOUT_SEC", "4")
    monkeypatch.delenv("PM_SCAN_CLOB_BOOK_CONNECT_TIMEOUT_SEC", raising=False)
    monkeypatch.setattr(swm, "requests_get", fake_requests_get)

    with pytest.raises(ValueError, match="orderbook_unavailable"):
        swm.fetch_orderbook(
            "token-1",
            retries=1,
            timeout_sec=None,
            price_augment_enabled=False,
            apply_global_rate_limit=False,
            use_cache=False,
        )

    assert calls
    for call in calls:
        connect_timeout, read_timeout = call["timeout"]
        assert abs(connect_timeout - 1.4) < 1e-9
        assert read_timeout == 4.0


def test_scan_orderbook_budget_clamps_global_rate_limit_wait(monkeypatch) -> None:
    calls = []

    def fake_wait(*args, **kwargs):
        calls.append(kwargs)
        return 0.0

    def fake_requests_get(url, **kwargs):
        raise swm.requests.RequestException("slow clob")

    monkeypatch.setattr(swm, "_clob_book_wait_global_slot", fake_wait)
    monkeypatch.setattr(swm, "requests_get", fake_requests_get)

    with pytest.raises(ValueError, match="orderbook_unavailable"):
        swm.fetch_orderbook(
            "token-1",
            retries=1,
            timeout_sec=1.0,
            price_augment_enabled=False,
            apply_global_rate_limit=True,
            max_wall_time_sec=0.5,
            use_cache=False,
        )

    assert calls
    assert calls[0]["max_wait_sec"] <= 0.36


def test_clob_price_quotes_use_shared_wall_budget(monkeypatch) -> None:
    monotonic_values = iter([100.0, 100.0, 100.43])
    calls = []

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"price": 0.42}

    def fake_monotonic():
        return next(monotonic_values)

    def fake_requests_get(url, **kwargs):
        calls.append(kwargs)
        return FakeResponse()

    monkeypatch.setattr(swm.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(swm, "requests_get", fake_requests_get)

    bid, ask = swm.fetch_clob_price_quotes(
        "token-1",
        timeout_sec=1.0,
        retries=1,
        force_direct=True,
        max_wall_time_sec=0.45,
    )

    assert bid == 0.42
    assert ask is None
    assert len(calls) == 1
    assert calls[0]["timeout"] <= 0.45


def test_nowcast_share_rises_intraday() -> None:
    morning = dt.datetime(2026, 4, 29, 9, 0, tzinfo=dt.timezone.utc)
    afternoon = dt.datetime(2026, 4, 29, 20, 0, tzinfo=dt.timezone.utc)
    early_share = swm._nowcast_observed_share(
        "2026-04-29",
        "2026-04-29",
        "UTC",
        "temperature_2m_max",
        now_dt=morning,
    )
    late_share = swm._nowcast_observed_share(
        "2026-04-29",
        "2026-04-29",
        "UTC",
        "temperature_2m_max",
        now_dt=afternoon,
    )
    assert early_share == 0.0
    assert late_share > early_share


def test_nowcast_share_uses_event_local_day_not_run_date_only() -> None:
    share = swm._nowcast_observed_share(
        "2026-06-11",
        "2026-06-12",
        "Asia/Shanghai",
        "temperature_2m_max",
        now_dt=dt.datetime(2026, 6, 12, 3, 0, tzinfo=dt.timezone.utc),
    )

    assert share > 0.0


def test_wunderground_current_observation_parser_prefers_station_max_since7am() -> None:
    body = {
        "temperature": 82,
        "temperatureMaxSince7Am": 86,
        "validTimeLocal": "2026-06-12T14:15:00+0800",
        "validTimeUtc": 1781244900,
    }
    state = {
        "station_obs": {
            "u": (
                "https://api.weather.com/v3/wx/observations/current"
                "?apiKey=x&language=en-US&units=e&format=json&icaoCode=ZUUU"
            ),
            "b": body,
        }
    }
    html = (
        '<script id="app-root-state" type="application/json">'
        + swm.html.escape(swm.json.dumps(state))
        + "</script>"
    )

    metrics = swm._extract_wunderground_current_observation_metrics_from_html(
        html,
        station_id="ZUUU",
        event_date="2026-06-12",
        kind="max",
        market_unit="C",
    )

    assert metrics["ok"] is True
    assert metrics["source"] == "wunderground_current_station_max_since7am"
    assert abs(metrics["value"] - 30.0) < 1e-9


def test_wu_twc_intraday_ignores_current_endpoint_max_since7am_before_7am(monkeypatch) -> None:
    current_at = dt.datetime(2026, 6, 24, 22, 50, 15, tzinfo=dt.timezone.utc)
    history_peak_at = dt.datetime(2026, 6, 24, 16, 0, tzinfo=dt.timezone.utc)

    monkeypatch.setattr(
        swm,
        "fetch_wunderground_current_and_max_observation_metrics",
        lambda *args, **kwargs: (
            {
                "ok": True,
                "value": 22.0,
                "source": "wunderground_current_station_current_temperature",
                "source_url": "https://api.weather.com/v3/wx/observations/current?icaoCode=ZUUU",
                "time_gmt": current_at.timestamp(),
                "valid_time_local": "2026-06-25T06:50:15+0800",
                "raw_value": 22.0,
                "raw_unit": "C",
            },
            {"ok": False, "error": "request_error:ReadTimeout"},
        ),
    )
    monkeypatch.setattr(
        swm,
        "_wunderground_history_temp_pairs_c",
        lambda *args, **kwargs: (
            [
                (history_peak_at, 25.0),
                (dt.datetime(2026, 6, 24, 22, 0, tzinfo=dt.timezone.utc), 22.0),
            ],
            {
                "method": "weather_com_history_api",
                "units": "m",
                "peak_raw_value": 25.0,
                "peak_raw_unit": "C",
                "peak_raw_at": history_peak_at.isoformat(),
            },
        ),
    )
    monkeypatch.setattr(
        swm,
        "_wunderground_history_peak_raw_observation",
        lambda *args, **kwargs: {
            "ok": True,
            "raw_value": 77.0,
            "raw_unit": "F",
            "observed_at": history_peak_at.isoformat(),
        },
    )

    def fake_current_endpoint_raw(source_url, *, units):
        if units == "m":
            return {
                "ok": True,
                "units": "m",
                "raw_unit": "C",
                "current_raw_value": 22.0,
                "max_since7am_raw_value": 28.0,
                "valid_time_local": "2026-06-25T06:50:15+0800",
                "time_gmt": current_at.timestamp(),
                "observed_at": current_at.isoformat(),
            }
        return {
            "ok": True,
            "units": "e",
            "raw_unit": "F",
            "current_raw_value": 71.0,
            "max_since7am_raw_value": 71.0,
            "valid_time_local": "2026-06-25T06:50:15+0800",
            "time_gmt": current_at.timestamp(),
            "observed_at": current_at.isoformat(),
        }

    monkeypatch.setattr(swm, "_weather_com_current_endpoint_raw_observation", fake_current_endpoint_raw)

    features = swm.fetch_wu_twc_observed_intraday_features(
        "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
        "ZUUU",
        "2026-06-25",
        "temperature_2m_max",
        "C",
        tz="Asia/Shanghai",
    )

    assert features["wu_twc_observed_intraday_current_metric_max_since7am_raw_value"] == 28.0
    assert features["wu_twc_observed_intraday_today_max_c"] == 25.0
    assert features["wu_twc_observed_intraday_today_max_source"] == "wu_twc_observed_intraday_history"
    assert features["observed_today_max_c"] == 25.0
    assert features["observed_today_max_source"] == "wu_twc_observed_intraday_history"


def test_max_nowcast_is_lower_bound_not_exact_final_bucket() -> None:
    below = swm.parse_outcome_label("28°C")
    current = swm.parse_outcome_label("29°C")
    above = swm.parse_outcome_label("30°C")

    assert (
        swm.observed_nowcast_bucket_weight(
            29.7,
            below,
            "whole_degrees",
            "temperature_2m_max",
            base_prob=0.25,
        )
        == 0.0
    )
    assert (
        swm.observed_nowcast_bucket_weight(
            29.7,
            current,
            "whole_degrees",
            "temperature_2m_max",
            base_prob=0.25,
        )
        == 1.0
    )
    assert (
        swm.observed_nowcast_bucket_weight(
            29.7,
            above,
            "whole_degrees",
            "temperature_2m_max",
            base_prob=0.10,
        )
        == 1.0
    )


def test_min_nowcast_is_upper_bound_not_exact_final_bucket() -> None:
    lower = swm.parse_outcome_label("18°C")
    current = swm.parse_outcome_label("19°C")
    above = swm.parse_outcome_label("20°C")

    assert (
        swm.observed_nowcast_bucket_weight(
            19.4,
            lower,
            "whole_degrees",
            "temperature_2m_min",
            base_prob=0.20,
        )
        == 1.0
    )
    assert (
        swm.observed_nowcast_bucket_weight(
            19.4,
            current,
            "whole_degrees",
            "temperature_2m_min",
            base_prob=0.30,
        )
        == 1.0
    )
    assert (
        swm.observed_nowcast_bucket_weight(
            19.4,
            above,
            "whole_degrees",
            "temperature_2m_min",
            base_prob=0.40,
        )
        == 0.0
    )


def test_market_ws_book_message_maps_to_tick_row() -> None:
    bindings = {
        "token-1": TokenBinding(
            token_id="token-1",
            slug="slug-a",
            event_date="2026-04-29",
            outcome="20°C",
            side="YES",
        )
    }
    rows = rows_from_ws_message(
        {
            "event_type": "book",
            "asset_id": "token-1",
            "bids": [{"price": "0.31", "size": "4"}],
            "asks": [{"price": "0.34", "size": "5"}],
        },
        bindings,
        depth_levels=5,
        created_at="2026-04-29T12:00:00+00:00",
        include_source_details={"book"},
    )
    assert len(rows) == 1
    assert rows[0]["slug"] == "slug-a"
    assert rows[0]["side"] == "YES"
    assert rows[0]["best_bid"] == 0.31
    assert rows[0]["best_ask"] == 0.34
    assert rows[0]["source"] == "clob_ws"


def test_tick_coalescer_skips_same_quote_until_refresh_interval() -> None:
    row = {
        "slug": "slug-a",
        "event_date": "2026-04-29",
        "outcome": "20°C",
        "side": "YES",
        "best_bid": 0.31,
        "best_ask": 0.34,
        "spread": 0.03,
        "bid_depth_topN": 4.0,
        "ask_depth_topN": 5.0,
    }
    changed = dict(row, best_ask=0.35, spread=0.04)
    coalescer = TickCoalescer(10.0)

    assert coalescer.filter_rows([row], now=100.0) == [row]
    assert coalescer.filter_rows([dict(row)], now=105.0) == []
    assert coalescer.skipped == 1
    assert coalescer.filter_rows([changed], now=106.0) == [changed]
    assert coalescer.filter_rows([dict(changed)], now=116.0) == [changed]


def test_market_ws_write_ticks_closes_long_lived_connection(monkeypatch) -> None:
    class FakeConnection:
        def __init__(self) -> None:
            self.total_changes = 0
            self.closed = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def executemany(self, _sql, rows) -> None:
            self.total_changes += len(list(rows))

        def close(self) -> None:
            self.closed = True

    fake = FakeConnection()
    monkeypatch.setattr(swm, "open_db", lambda **_kwargs: fake)
    monkeypatch.setattr(swm, "init_db", lambda: None)
    monkeypatch.setattr(swm, "_should_run_market_tick_cleanup", lambda _now: False)

    written = _write_ticks(
        [
            {
                "run_date": "2026-04-29",
                "event_date": "2026-04-29",
                "slug": "slug-a",
                "outcome": "20°C",
                "side": "YES",
                "best_bid": 0.31,
                "best_ask": 0.34,
                "mid": 0.325,
                "spread": 0.03,
                "bid_depth_topN": 4.0,
                "ask_depth_topN": 5.0,
                "source": "clob_ws",
                "source_detail": "book",
                "created_at": "2026-04-29T12:00:00+00:00",
            }
        ]
    )

    assert written == 1
    assert fake.closed is True


def test_market_ws_parse_args_exposes_coalesce_tolerance_alias(monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["market_ws_listener.py", "--coalesce-same-quote-tol", "0.001", "--once"],
    )

    args = parse_args()

    assert args.coalesce_same_quote_tol == 0.001
    assert args.coalesce_same_quote_tolerance == 0.001

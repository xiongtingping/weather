from __future__ import annotations

import sqlite3

import copy_follow_production
from copy_follow_production import CopyFollowProductionEngine
from copy_follow_strategy import CopyFollowCandidate


def _candidate() -> CopyFollowCandidate:
    buckets = ("28°C", "29°C", "30°C", "31°C")
    source = {bucket: float(10 - index) for index, bucket in enumerate(buckets)}
    return CopyFollowCandidate(
        source_user="0xabc",
        event_slug="highest-temperature-in-test-on-july-20-2026",
        city_slug="test",
        event_date="2026-07-20",
        buckets=buckets,
        source_notional_by_bucket=source,
        allocation_usdc_by_bucket=source,
        total_source_notional_usdc=sum(source.values()),
        total_allocation_usdc=sum(source.values()),
        main_bucket="28°C",
    )


def test_open_meteo_context_is_display_only_and_failure_is_non_blocking(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    engine = CopyFollowProductionEngine(conn)
    candidate = _candidate()
    registry = {bucket: {"station_id": "ZBAA"} for bucket in candidate.buckets}
    config = {
        "open_meteo": {
            "enabled": True,
            "display_only": True,
            "station_coordinates": {"ZBAA": {"latitude": 40.08, "longitude": 116.58, "timezone": "Asia/Shanghai"}},
        }
    }

    monkeypatch.setattr(
        copy_follow_production,
        "fetch_open_meteo_daily_peak",
        lambda **_kwargs: {
            "source_id": "open_meteo_daily_temperature_2m_max",
            "state": "available",
            "peak_c": 29.2,
            "timezone": "Asia/Shanghai",
            "request_url": "https://api.open-meteo.com/v1/forecast",
        },
    )
    context = engine._forecast_context(candidate, registry, forecast_config=config)
    assert context["open_meteo"]["peak_c"] == 29.2
    assert context["open_meteo"]["display_only"] is True
    assert context["taf"]["source_id"] == "open_meteo_taf_replacement"
    assert context["taf"]["replacement_for"] == "taf_tx_peak"
    assert context["comparison"]["open_meteo"] == "above_main_bucket"

    monkeypatch.setattr(
        copy_follow_production,
        "fetch_open_meteo_daily_peak",
        lambda **_kwargs: {"source_id": "open_meteo_daily_temperature_2m_max", "state": "unavailable", "reason": "request_failed:TimeoutError", "peak_c": None, "rows": []},
    )
    unavailable = engine._forecast_context(candidate, registry, forecast_config=config)
    assert unavailable["open_meteo"]["state"] == "unavailable"
    assert unavailable["open_meteo_peak_c"] is None
    conn.close()


def test_open_meteo_model_parameter_is_forwarded(monkeypatch) -> None:
    captured = []

    def fake_fetch(**kwargs):
        captured.append(dict(kwargs))
        return {"source_id": "open_meteo_ecmwf_ifs025", "state": "available", "peak_c": 22.0, "rows": []}

    monkeypatch.setattr(copy_follow_production, "fetch_open_meteo_daily_peak", fake_fetch)
    conn = sqlite3.connect(":memory:")
    engine = CopyFollowProductionEngine(conn)
    config = {
        "open_meteo": {
            "enabled": True,
            "display_only": True,
            "ecmwf_model": "ecmwf_ifs025",
            "station_coordinates": {"ZBAA": {"latitude": 40.08, "longitude": 116.58, "timezone": "Asia/Shanghai"}},
        }
    }
    context = engine._forecast_context(_candidate(), {bucket: {"station_id": "ZBAA"} for bucket in _candidate().buckets}, forecast_config=config)
    assert [item["models"] for item in captured] == [None, "ecmwf_ifs025"]
    assert context["open_meteo"]["peak_c"] == 22.0
    assert context["ecmwf"]["source_id"] == "open_meteo_ecmwf_ifs025"
    conn.close()

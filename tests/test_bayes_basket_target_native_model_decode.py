from __future__ import annotations

import json
import os

from scripts.decode_bayes_basket_target_native_model_arrivals import build as build_decode_evidence
from bayes_basket_target.native_model_decode import (
    decode_native_temperature_points,
    materialize_native_daily_curves,
    parse_grib_get_temperature_output,
)
from bayes_basket_target.station_registry import station_coordinates


SHA_A = "a" * 64
SHA_B = "b" * 64


def test_parse_ec_codes_temperature_row_converts_kelvin_and_binds_time() -> None:
    rows = parse_grib_get_temperature_output("20260723 600 20260723 1800 12 12 12 instant K 302.702  \n")
    assert rows == [{
        "grib_run_initialization_utc": "2026-07-23T06:00:00Z",
        "grib_valid_time_utc": "2026-07-23T18:00:00Z",
        "step_hours": 12,
        "temperature_2m_c": 29.552,
        "native_units": "K",
    }]


def _point(valid: str, temp: float) -> dict[str, object]:
    return {
        "station_id": "KJFK",
        "event_date": "2026-07-23",
        "provider_id": "icon_global_official",
        "model_run_initialization_utc": "2026-07-23T00:00:00Z",
        "valid_time_utc": valid,
        "temperature_2m_c": temp,
        "raw_message_sha256": SHA_A,
        "receipt_sha256": SHA_B,
    }


def test_single_native_point_is_not_a_daily_curve() -> None:
    result = materialize_native_daily_curves(
        [_point("2026-07-23T16:00:00Z", 30.0)],
        target_timezone="America/New_York",
    )
    assert result["curve_count"] == 1
    assert result["complete_curve_count"] == 0
    assert result["curves"][0]["state"] == "BLOCKED_INCOMPLETE_CURVE"
    assert "native_daily_curve_point_count_insufficient" in result["curves"][0]["blockers"]


def test_full_local_day_curve_yields_a_calibration_row_but_no_authority() -> None:
    points = [
        _point("2026-07-23T04:00:00Z", 24.0),
        _point("2026-07-23T10:00:00Z", 28.0),
        _point("2026-07-23T16:00:00Z", 31.0),
        _point("2026-07-23T22:00:00Z", 29.0),
        _point("2026-07-24T03:00:00Z", 27.0),
    ]
    result = materialize_native_daily_curves(
        points,
        target_timezone="America/New_York",
    )
    assert result["complete_curve_count"] == 1
    assert result["eligible_model_rows"][0]["forecast_daily_max_c"] == 31.0
    assert result["promotion_allowed"] is False
    assert result["production_authority"] is False


def test_daily_curve_accepts_different_component_publication_times() -> None:
    points = []
    for index, (valid_time, temperature) in enumerate(
        (
            ("2026-07-23T04:00:00Z", 24.0),
            ("2026-07-23T10:00:00Z", 28.0),
            ("2026-07-23T16:00:00Z", 31.0),
            ("2026-07-23T22:00:00Z", 29.0),
            ("2026-07-24T03:00:00Z", 27.0),
        )
    ):
        row = _point(valid_time, temperature)
        row["published_at_utc"] = f"2026-07-23T0{5 + index}:00:00Z"
        row["received_at_utc"] = f"2026-07-23T0{5 + index}:05:00Z"
        points.append(row)

    result = materialize_native_daily_curves(
        points,
        target_timezone="America/New_York",
    )

    assert result["complete_curve_count"] == 1
    eligible = result["eligible_model_rows"][0]
    assert eligible["published_at_utc"] == "2026-07-23T09:00:00Z"
    assert eligible["received_at_utc"] == "2026-07-23T09:05:00Z"
    assert eligible["component_published_at_utc"] == [
        "2026-07-23T05:00:00Z",
        "2026-07-23T06:00:00Z",
        "2026-07-23T07:00:00Z",
        "2026-07-23T08:00:00Z",
        "2026-07-23T09:00:00Z",
    ]


def test_station_coordinate_registry_is_official_and_station_specific() -> None:
    cyyz = station_coordinates("CYYZ")
    eddm = station_coordinates("EDDM")
    assert (cyyz["latitude"], cyyz["longitude"]) == (43.679, -79.629)
    assert (eddm["latitude"], eddm["longitude"]) == (48.348, 11.813)
    assert cyyz["source_schema"] == "awc_stationinfo_v1"
    assert cyyz["registry_sha256"] == eddm["registry_sha256"]


def test_unknown_station_has_no_coordinate_fallback() -> None:
    import pytest

    with pytest.raises(Exception, match="native_station_coordinates_missing"):
        station_coordinates("UNKNOWN")


def test_native_decode_binds_cyyz_coordinates_in_grib_get_command(tmp_path) -> None:
    raw_path = tmp_path / "payload.grib2"
    raw_path.write_bytes(b"bounded-test-payload")
    args_path = tmp_path / "args.txt"
    decoder = tmp_path / "grib_get"
    decoder.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" > '{args_path}'\n"
        "printf '20260723 0000 20260724 0400 28 300.0\\n'\n",
        encoding="utf-8",
    )
    decoder.chmod(0o755)
    result = decode_native_temperature_points(
        raw_path,
        provider_id="gfs_0p25_official",
        station_id="CYYZ",
        grib_get_binary=str(decoder),
    )
    assert result["coordinate_lineage"]["source_schema"] == "awc_stationinfo_v1"
    assert result["latitude"] == 43.679
    assert result["longitude"] == -79.629
    command_args = args_path.read_text(encoding="utf-8")
    assert "43.679000,-79.629000,1" in command_args
    assert "30.576000,103.950000,1" not in command_args


def test_native_decode_allows_p0_pre_event_gate_then_checks_raw_lineage(
    tmp_path,
    monkeypatch,
) -> None:
    capture_path = tmp_path / "future-capture.json"
    capture_path.write_text(json.dumps({
        "status": "CAPTURED",
        "transport_receipt_path": str(tmp_path / "must-not-be-read.json"),
        "arrival": {
            "arrival_id": "arrival-future",
            "provider_id": "icon_global_official",
            "model_id": "ICON",
            "station_id": "KJFK",
            "target_local_date": "2026-07-24",
            "target_timezone": "America/New_York",
            "run_initialization_utc": "2026-07-23T00:00:00Z",
            "valid_time_utc": "2026-07-24T16:00:00Z",
            "published_at_utc": "2026-07-23T05:30:00Z",
            "received_at_utc": "2026-07-23T05:34:12Z",
            "decision_at_utc": "2026-07-23T18:00:00Z",
            "publication_time_source": "provider_http_last_modified",
            "source_url": "https://opendata.dwd.de/weather/model.grib2.bz2",
            "raw_payload_sha256s": ["a" * 64],
            "receipt_sha256": "b" * 64,
            "capture_mode": "prospective",
            "historical_arrival_time_observed": True,
            "aggregator_or_republisher": False,
            "open_meteo": False,
        },
    }), encoding="utf-8")

    monkeypatch.setitem(
        build_decode_evidence.__globals__,
        "load_market_registry_snapshot",
        lambda _path: {
            "snapshot_sha256": "c" * 64,
            "rows": [{
                "station_id": "KJFK",
                "event_date": "2026-07-24",
                "settlement_timezone": "America/New_York",
            }],
        },
    )
    result = build_decode_evidence(
        capture_paths=[capture_path],
        as_of_utc="2026-07-24T03:59:59Z",
        market_registry_snapshot_path=tmp_path / "registry.json",
    )

    assert result["decoded_point_count"] == 0
    artifact = result["input_artifacts"][0]
    assert artifact["wrapper_capture_sha256"] == artifact[
        "wrapper_current_sha256"
    ]
    assert artifact["arrival_content_sha256"] == artifact[
        "arrival_identity_sha256"
    ]
    assert artifact["arrival_raw_payload_sha256s"] == ["a" * 64]
    assert artifact["arrival_receipt_sha256"] == "b" * 64
    assert artifact["allowed_wrapper_lifecycle_fields"] == [
        "formal_consumed_at_utc"
    ]
    assert result["blocked_captures"][0]["receipt"]["reason_codes"] == [
        "ResourceBoundError:target_input_stat_failed"
    ]

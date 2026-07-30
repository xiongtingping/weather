from __future__ import annotations

from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from scripts import capture_bayes_basket_target_gfs_same_run as gfs_same_run
from scripts.capture_bayes_basket_target_gfs_same_run import (
    build_gfs_native_object_url,
    build_gfs_urls,
    forecast_hours_for_target_settlement_day,
    parse_gfs_2m_temperature_range,
)


def test_rkpk_settlement_day_maps_to_shared_ecmwf_gfs_grid() -> None:
    assert forecast_hours_for_target_settlement_day(
        target_local_date="2026-07-27",
        run_initialization=datetime(2026, 7, 25, 18, tzinfo=timezone.utc),
        target_timezone="Asia/Seoul",
    ) == (21, 27, 33, 39, 42)


def test_same_utc_date_00z_run_preserves_existing_summer_grid() -> None:
    assert forecast_hours_for_target_settlement_day(
        target_local_date="2026-07-26",
        run_initialization=datetime(2026, 7, 26, 0, tzinfo=timezone.utc),
        target_timezone="America/New_York",
    ) == (6, 12, 18, 24, 27)


def test_winter_et_offset_is_dst_aware() -> None:
    assert forecast_hours_for_target_settlement_day(
        target_local_date="2026-01-15",
        run_initialization=datetime(2026, 1, 14, 0, tzinfo=timezone.utc),
        target_timezone="America/New_York",
    ) == (30, 36, 42, 48, 51)


def test_rejects_run_newer_than_first_target_sample() -> None:
    with pytest.raises(ValueError, match="model_curve_run_newer_than_target_sample"):
        forecast_hours_for_target_settlement_day(
            target_local_date="2026-07-26",
            run_initialization=datetime(2026, 7, 26, 12, tzinfo=timezone.utc),
            target_timezone="America/New_York",
        )


def test_rejects_run_beyond_gfs_supported_horizon() -> None:
    with pytest.raises(ValueError, match="model_curve_forecast_hour_exceeds_supported_horizon"):
        forecast_hours_for_target_settlement_day(
            target_local_date="2026-07-26",
            run_initialization=datetime(2026, 7, 1, 0, tzinfo=timezone.utc),
            target_timezone="America/New_York",
        )


def test_gfs_urls_use_dynamic_forecast_hour_without_full_file_download() -> None:
    filtered, publication = build_gfs_urls(
        run_initialization=datetime(2026, 7, 25, 0, tzinfo=timezone.utc),
        forecast_hour=30,
        latitude=43.6777,
        longitude=-79.6248,
    )
    assert "filter_gfs_0p25_1hr.pl" in filtered
    assert "gfs.t00z.pgrb2.0p25.f030" in filtered
    assert "lev_2_m_above_ground=on" in filtered
    assert publication.endswith("/gfs.t00z.pgrb2.0p25.f030")


def test_gfs_native_object_url_preserves_real_cycle_and_forecast_hour() -> None:
    assert build_gfs_native_object_url(
        run_initialization=datetime(2026, 7, 25, 18, tzinfo=timezone.utc),
        forecast_hour=36,
    ).endswith("/gfs.20260725/18/atmos/gfs.t18z.pgrb2.0p25.f036")


def test_parse_gfs_2m_temperature_range_uses_next_record_boundary() -> None:
    payload = (
        b"580:410000000:d=2026072518:VIS:surface:36 hour fcst:\n"
        b"581:411921714:d=2026072518:TMP:2 m above ground:36 hour fcst:\n"
        b"582:412711999:d=2026072518:POT:2 m above ground:36 hour fcst:\n"
    )
    assert parse_gfs_2m_temperature_range(payload) == (411921714, 412711998)


@pytest.mark.parametrize(
    "payload, reason",
    [
        (b"1:0:d=x:VIS:surface:\n", "not_unique"),
        (
            b"1:100:d=x:TMP:2 m above ground:\n2:100:d=x:POT:2 m above ground:\n",
            "byte_range_invalid",
        ),
        (
            b"1:100:d=x:TMP:2 m above ground:\n2:200:d=x:POT:2 m above ground:\n"
            b"3:300:d=x:TMP:2 m above ground:\n4:400:d=x:POT:2 m above ground:\n",
            "not_unique",
        ),
    ],
)
def test_parse_gfs_2m_temperature_range_fails_closed(payload, reason) -> None:
    with pytest.raises(ValueError, match=reason):
        parse_gfs_2m_temperature_range(payload)


def test_capture_binds_arrival_decision_clock_to_response_completion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    captured_decision_clocks: list[object] = []

    def fake_capture(**kwargs):
        captured_decision_clocks.append(kwargs["decision_at_utc"])
        return {
            "status": "CAPTURED",
            "arrival": {
                "provider_id": gfs_same_run.PROVIDER,
                "station_id": "KJFK",
                "target_local_date": "2026-07-26",
                "run_initialization_utc": "2026-07-25T00:00:00Z",
                "valid_time_utc": kwargs["valid_time_utc"],
            },
            "boundary": {
                "writes_production_db": False,
                "calls_order_gateway": False,
            },
        }

    monkeypatch.setattr(
        gfs_same_run,
        "validate_prospective_capture_window",
        lambda **kwargs: {"state": "open"},
    )
    monkeypatch.setattr(
        gfs_same_run,
        "load_target_capture_config",
        lambda path: {
            "resource_budget": {
                "max_process_rss_bytes": 512 * 1024 * 1024,
                "max_raw_bytes": 1024 * 1024 * 1024,
                "min_free_bytes": 512 * 1024 * 1024,
                "max_response_bytes": 64 * 1024 * 1024,
                "max_capture_bytes": 256 * 1024 * 1024,
            }
        },
    )
    monkeypatch.setattr(gfs_same_run, "assert_process_memory_budget", lambda **kwargs: None)
    monkeypatch.setattr(gfs_same_run, "assert_target_raw_retention_budget", lambda *args, **kwargs: None)
    monkeypatch.setattr(gfs_same_run, "load_bounded_json", lambda path: {
        "target_timezone": "America/New_York",
        "station_ids": ["KJFK"],
        "market_registry_snapshot_sha256": "a" * 64,
    })
    monkeypatch.setattr(
        gfs_same_run,
        "coordinate_registry_manifest",
        lambda: {"station_ids": ["KJFK"]},
    )
    monkeypatch.setattr(
        gfs_same_run,
        "station_coordinates",
        lambda station_id: {"latitude": 40.64, "longitude": -73.78},
    )
    monkeypatch.setattr(gfs_same_run, "validate_capture_request", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        gfs_same_run.importlib,
        "import_module",
        lambda name: SimpleNamespace(capture=fake_capture),
    )
    monkeypatch.setattr(
        gfs_same_run,
        "resolve_gfs_2m_temperature_range",
        lambda **kwargs: ("https://noaa-gfs-bdp-pds.s3.amazonaws.com/object", 100, 199),
    )

    manifest = gfs_same_run.capture_run(
        target_local_date="2026-07-26",
        run_initialization_utc="2026-07-25T00:00:00Z",
        decision_at_utc="2026-07-26T04:00:01Z",
        contract_path=tmp_path / "contract.json",
        capture_config_path=tmp_path / "capture.json",
        raw_root=tmp_path / "raw",
        output_dir=tmp_path / "output",
    )

    assert manifest["captured_or_reused_count"] == 5
    assert captured_decision_clocks == [None] * 5
    assert manifest["boundary"]["calls_order_gateway"] is False
    assert manifest["boundary"]["places_orders"] is False
    assert manifest["boundary"]["authorizes_order"] is False


def test_existing_capture_written_by_transport_is_reusable(tmp_path) -> None:
    raw_root = tmp_path / "model_arrivals"
    raw_path = raw_root / "gfs_0p25_official" / "raw" / (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        ".f030"
    )
    raw_path.parent.mkdir(parents=True)
    raw_path.write_bytes(b"abc")
    receipt_path = (
        raw_root / "gfs_0p25_official" / "receipts" / "transport.json"
    )
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text(
        json.dumps({
            "raw_receipts": [{
                "raw_object_ref": str(raw_path),
                "raw_sha256": (
                    "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb"
                    "410ff61f20015ad"
                ),
            }],
        }),
        encoding="utf-8",
    )
    path = tmp_path / "capture.json"
    path.write_text(
        """{
          "status": "CAPTURED",
          "transport_receipt_path": "%s",
          "arrival": {
            "provider_id": "gfs_0p25_official",
            "station_id": "WSSS",
            "target_local_date": "2026-07-30",
            "target_timezone": "Asia/Singapore",
            "run_initialization_utc": "2026-07-28T18:00:00Z",
            "valid_time_utc": "2026-07-30T00:00:00Z",
            "source_url": "https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.20260728/18/atmos/gfs.t18z.pgrb2.0p25.f030"
          },
          "boundary": {
            "writes_production_db": false,
            "calls_order_gateway": false,
            "places_orders": false,
            "request_byte_range": {"start": 100, "end": 199}
          }
        }""" % str(receipt_path),
        encoding="utf-8",
    )

    assert gfs_same_run._existing_matches(
        path,
        station_id="WSSS",
        target_date="2026-07-30",
        target_timezone="Asia/Singapore",
        run="2026-07-28T18:00:00Z",
        valid="2026-07-30T00:00:00Z",
        raw_root=raw_root,
    )

    raw_path.unlink()
    assert not gfs_same_run._existing_matches(
        path,
        station_id="WSSS",
        target_date="2026-07-30",
        target_timezone="Asia/Singapore",
        run="2026-07-28T18:00:00Z",
        valid="2026-07-30T00:00:00Z",
        raw_root=raw_root,
    )


def test_capture_stops_after_five_identical_blockers_without_failed_fragments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    calls: list[str] = []

    def fake_capture(**kwargs):
        calls.append(kwargs["valid_time_utc"])
        return {
            "status": "BLOCKED",
            "receipt": {"reason_codes": ["SSL: UNEXPECTED_EOF_WHILE_READING"]},
        }

    monkeypatch.setattr(
        gfs_same_run,
        "validate_prospective_capture_window",
        lambda **kwargs: {"state": "pre_event"},
    )
    monkeypatch.setattr(
        gfs_same_run,
        "load_target_capture_config",
        lambda path: {
            "resource_budget": {
                "max_process_rss_bytes": 512 * 1024 * 1024,
                "max_raw_bytes": 1024 * 1024 * 1024,
                "min_free_bytes": 512 * 1024 * 1024,
                "max_response_bytes": 64 * 1024 * 1024,
                "max_capture_bytes": 256 * 1024 * 1024,
            }
        },
    )
    monkeypatch.setattr(gfs_same_run, "assert_process_memory_budget", lambda **kwargs: None)
    monkeypatch.setattr(gfs_same_run, "assert_target_raw_retention_budget", lambda *args, **kwargs: None)
    monkeypatch.setattr(gfs_same_run, "load_bounded_json", lambda path: {
        "target_timezone": "America/New_York",
        "station_ids": ["KJFK", "KLAX"],
        "market_registry_snapshot_sha256": "a" * 64,
    })
    monkeypatch.setattr(
        gfs_same_run,
        "coordinate_registry_manifest",
        lambda: {"station_ids": ["KJFK", "KLAX"]},
    )
    monkeypatch.setattr(
        gfs_same_run,
        "station_coordinates",
        lambda station_id: {"latitude": 40.64, "longitude": -73.78},
    )
    monkeypatch.setattr(gfs_same_run, "validate_capture_request", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        gfs_same_run.importlib,
        "import_module",
        lambda name: SimpleNamespace(capture=fake_capture),
    )
    monkeypatch.setattr(
        gfs_same_run,
        "resolve_gfs_2m_temperature_range",
        lambda **kwargs: ("https://noaa-gfs-bdp-pds.s3.amazonaws.com/object", 100, 199),
    )

    output_dir = tmp_path / "output"
    manifest = gfs_same_run.capture_run(
        target_local_date="2026-07-27",
        run_initialization_utc="2026-07-25T18:00:00Z",
        decision_at_utc="2026-07-26T12:00:00Z",
        contract_path=tmp_path / "contract.json",
        capture_config_path=tmp_path / "capture.json",
        raw_root=tmp_path / "raw",
        output_dir=output_dir,
    )

    assert len(calls) == gfs_same_run.MAX_CONSECUTIVE_IDENTICAL_BLOCKERS
    assert manifest["attempted_count"] == 5
    assert manifest["blocked_count"] == 5
    assert manifest["early_stopped"] is True
    assert manifest["early_stop"]["reason"] == "consecutive_identical_transport_blockers"
    assert all(item["failed_fragment_retained"] is False for item in manifest["blockers"])
    assert sorted(path.name for path in output_dir.iterdir()) == ["manifest.json"]


def test_success_resets_identical_blocker_counter_and_allows_wraparound(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    outcomes = iter(
        [
            "BLOCKED",
            "BLOCKED",
            "BLOCKED",
            "BLOCKED",
            "CAPTURED",
            "BLOCKED",
            "BLOCKED",
            "BLOCKED",
            "BLOCKED",
            "CAPTURED",
        ]
    )

    def fake_capture(**kwargs):
        status = next(outcomes)
        if status == "BLOCKED":
            return {"status": status, "receipt": {"reason_codes": ["same_transport_error"]}}
        return {
            "status": status,
            "arrival": {
                "provider_id": gfs_same_run.PROVIDER,
                "station_id": kwargs["station_id"],
                "target_local_date": kwargs["target_local_date"],
                "run_initialization_utc": kwargs["run_initialization_utc"],
                "valid_time_utc": kwargs["valid_time_utc"],
            },
            "boundary": {"writes_production_db": False, "calls_order_gateway": False},
        }

    monkeypatch.setattr(gfs_same_run, "validate_prospective_capture_window", lambda **kwargs: {"state": "pre_event"})
    monkeypatch.setattr(
        gfs_same_run,
        "load_target_capture_config",
        lambda path: {
            "resource_budget": {
                "max_process_rss_bytes": 512 * 1024 * 1024,
                "max_raw_bytes": 1024 * 1024 * 1024,
                "min_free_bytes": 512 * 1024 * 1024,
                "max_response_bytes": 64 * 1024 * 1024,
                "max_capture_bytes": 256 * 1024 * 1024,
            }
        },
    )
    monkeypatch.setattr(gfs_same_run, "assert_process_memory_budget", lambda **kwargs: None)
    monkeypatch.setattr(gfs_same_run, "assert_target_raw_retention_budget", lambda *args, **kwargs: None)
    monkeypatch.setattr(gfs_same_run, "load_bounded_json", lambda path: {
        "target_timezone": "America/New_York",
        "station_ids": ["KJFK", "KLAX"],
        "market_registry_snapshot_sha256": "a" * 64,
    })
    monkeypatch.setattr(gfs_same_run, "coordinate_registry_manifest", lambda: {"station_ids": ["KJFK", "KLAX"]})
    monkeypatch.setattr(gfs_same_run, "station_coordinates", lambda station_id: {"latitude": 40.64, "longitude": -73.78})
    monkeypatch.setattr(gfs_same_run, "validate_capture_request", lambda *args, **kwargs: None)
    monkeypatch.setattr(gfs_same_run.importlib, "import_module", lambda name: SimpleNamespace(capture=fake_capture))
    monkeypatch.setattr(
        gfs_same_run,
        "resolve_gfs_2m_temperature_range",
        lambda **kwargs: ("https://noaa-gfs-bdp-pds.s3.amazonaws.com/object", 100, 199),
    )

    manifest = gfs_same_run.capture_run(
        target_local_date="2026-07-27",
        run_initialization_utc="2026-07-25T18:00:00Z",
        decision_at_utc="2026-07-26T12:00:00Z",
        contract_path=tmp_path / "contract.json",
        capture_config_path=tmp_path / "capture.json",
        raw_root=tmp_path / "raw",
        output_dir=tmp_path / "output",
    )

    assert manifest["attempted_count"] == 10
    assert manifest["captured_or_reused_count"] == 2
    assert manifest["blocked_count"] == 8
    assert manifest["early_stopped"] is False

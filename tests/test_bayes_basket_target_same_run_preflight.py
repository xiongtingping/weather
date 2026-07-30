from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import rebuild_bayes_basket_target_same_run_from_retained_raw as preflight  # noqa: E402
from bayes_basket_target.contracts import ContractError  # noqa: E402


def _receipt(
    root: Path,
    provider: str,
    raw_path: Path,
    *,
    valid: str,
    station: str = "ZUUU",
    raw_url: str | None = None,
) -> None:
    raw_sha = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    receipt_dir = root / provider / "receipts"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "bayes_basket_target_native_model_transport_receipt_v1",
        "provider_id": provider,
        "station_id": station,
        "target_local_date": "2026-07-25",
        "run_initialization_utc": "2026-07-24T00:00:00Z",
        "valid_time_utc": valid,
        "capture_mode": "prospective",
        "historical_arrival_time_observed": True,
        "aggregator_or_republisher": False,
        "open_meteo": False,
        "raw_receipts": [{
            "raw_request_url": raw_url or (
                "https://data.ecmwf.int/forecasts/20260724/00z/ifs/0p25/oper/test.grib2"
                if provider == "ecmwf_ifs025_official"
                else "https://opendata.dwd.de/weather/nwp/icon/grib/00/t_2m/test.grib2.bz2"
            ),
            "raw_object_ref": str(raw_path),
            "raw_sha256": raw_sha,
            "provider_published_at_utc": "2026-07-24T07:00:00Z",
            "response_completed_at": "2026-07-24T12:00:00Z",
        }],
    }
    (receipt_dir / f"{valid.replace(':', '')}.json").write_text(json.dumps(payload), encoding="utf-8")


def _registry_snapshot(path: Path, *, event_date: str = "2026-07-25") -> Path:
    row = {
        "slug": "highest-temperature-in-test",
        "event_date": event_date,
        "outcome": "30°C",
        "city_slug": "test",
        "station_id": "ZUUU",
        "settlement_timezone": "America/New_York",
        "settlement_unit": "C",
        "source_lane": "amsc",
        "source_eligible": 1,
        "source_block_reason": "",
        "resolution_url": "https://example.invalid/rules",
        "rules_sha256": "a" * 64,
        "yes_token": "yes",
        "no_token": "no",
        "market_active": 1,
        "market_accepting_orders": 1,
        "registry_json": "{}",
        "refreshed_at": "2026-07-24T00:00:00Z",
    }
    payload = {
        "schema": "bayes_basket_target_market_registry_snapshot_v1",
        "source_schema": "reported_trajectory_market_registry_v1",
        "source_scope": "shared_live_registry_export_only",
        "source_database_path": "/isolated/test.db",
        "exported_at_utc": "2026-07-24T00:00:00Z",
        "source_row_count": 1,
        "excluded_not_started_row_count": 0,
        "include_not_started_for_p0": True,
        "as_of_utc": "2026-07-24T00:00:00Z",
        "row_count": 1,
        "rows": [row],
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    payload["snapshot_sha256"] = hashlib.sha256(canonical).hexdigest()
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_receipt_records_deduplicate_same_raw_and_verify_sha(tmp_path: Path) -> None:
    root = tmp_path / "arrivals"
    raw = root / "ecmwf_ifs025_official" / "raw" / "payload.grib2"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"retained-official-raw")
    _receipt(root, "ecmwf_ifs025_official", raw, valid="2026-07-25T06:00:00Z")
    receipt_dir = root / "ecmwf_ifs025_official" / "receipts"
    first_receipt = next(receipt_dir.glob("*.json"))
    (receipt_dir / "duplicate.json").write_text(first_receipt.read_text(), encoding="utf-8")

    records, blockers, inventory = preflight._receipt_records(
        provider_id="ecmwf_ifs025_official",
        provider_root=root / "ecmwf_ifs025_official",
        target_local_date="2026-07-25",
        target_timezone="America/New_York",
        run_initialization_utc="2026-07-24T00:00:00Z",
        stations={"ZUUU", "CYYZ"},
    )

    assert blockers == []
    assert len(records) == 1
    assert inventory["matching_receipts"] == 2
    assert inventory["unique_raw_records"] == 1
    assert inventory["raw_records_sha256_verified"] == 1


def test_ecmwf_google_mirror_is_official_and_accepted(tmp_path: Path) -> None:
    root = tmp_path / "arrivals"
    raw = root / "ecmwf_ifs025_official" / "raw" / "payload.grib2"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"retained-official-raw")
    _receipt(
        root,
        "ecmwf_ifs025_official",
        raw,
        valid="2026-07-25T06:00:00Z",
        raw_url="https://storage.googleapis.com/ecmwf-open-data/test.grib2",
    )

    records, blockers, inventory = preflight._receipt_records(
        provider_id="ecmwf_ifs025_official",
        provider_root=root / "ecmwf_ifs025_official",
        target_local_date="2026-07-25",
        target_timezone="America/New_York",
        run_initialization_utc="2026-07-24T00:00:00Z",
        stations={"ZUUU"},
    )

    assert blockers == []
    assert len(records) == 1
    assert inventory["raw_records_sha256_verified"] == 1


def test_global_models_fan_out_only_from_zuuu_and_gfs_pds_is_global() -> None:
    stations = ["CYYZ", "ZUUU"]
    global_record = {"provider_id": "icon_global_official", "station_id": "ZUUU"}
    assert preflight._stations_for_record(global_record, stations) == stations

    wrong_global = {"provider_id": "icon_global_official", "station_id": "CYYZ"}
    assert preflight._stations_for_record(wrong_global, stations) == ["CYYZ"]

    filtered = {"provider_id": "gfs_0p25_official", "station_id": "ZUUU"}
    assert preflight._stations_for_record(filtered, stations) == ["ZUUU"]

    pds_global = {
        "provider_id": "gfs_0p25_official",
        "station_id": "ZUUU",
        "source_url": "https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.20260724/00/atmos/gfs.t00z.pgrb2.0p25.f006",
    }
    assert preflight._stations_for_record(pds_global, stations) == stations

    pds_wrong_station = {**pds_global, "station_id": "CYYZ"}
    assert preflight._stations_for_record(pds_wrong_station, stations) == ["CYYZ"]


def test_gfs_pds_precedes_station_filtered_nomads_for_same_key() -> None:
    common = {
        "provider_id": "gfs_0p25_official",
        "station_id": "CYYZ",
        "valid_time_utc": "2026-07-25T06:00:00Z",
        "run_initialization_utc": "2026-07-24T00:00:00Z",
    }
    nomads = {
        **common,
        "raw_message_sha256": "a" * 64,
        "source_url": "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25_1hr.pl?file=x",
    }
    pds = {
        **common,
        "raw_message_sha256": "b" * 64,
        "source_url": "https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.x",
    }

    selected, discarded = preflight._apply_gfs_source_precedence([nomads, pds])

    assert discarded == 1
    assert selected == [pds]


def test_gfs_nomads_remains_fallback_when_pds_key_is_absent() -> None:
    nomads = {
        "provider_id": "gfs_0p25_official",
        "station_id": "CYYZ",
        "valid_time_utc": "2026-07-25T06:00:00Z",
        "run_initialization_utc": "2026-07-24T00:00:00Z",
        "raw_message_sha256": "a" * 64,
        "source_url": "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25_1hr.pl?file=x",
    }

    selected, discarded = preflight._apply_gfs_source_precedence([nomads])

    assert discarded == 0
    assert selected == [nomads]


def test_non_official_host_is_blocked(tmp_path: Path) -> None:
    root = tmp_path / "arrivals"
    raw = root / "icon_global_official" / "raw" / "payload.bz2"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"retained-official-raw")
    _receipt(root, "icon_global_official", raw, valid="2026-07-25T04:00:00Z")
    receipt = next((root / "icon_global_official" / "receipts").glob("*.json"))
    value = json.loads(receipt.read_text())
    value["raw_receipts"][0]["raw_request_url"] = "https://example.invalid/icon.grib2.bz2"
    receipt.write_text(json.dumps(value))

    records, blockers, inventory = preflight._receipt_records(
        provider_id="icon_global_official",
        provider_root=root / "icon_global_official",
        target_local_date="2026-07-25",
        target_timezone="America/New_York",
        run_initialization_utc="2026-07-24T00:00:00Z",
        stations={"ZUUU"},
    )

    assert records == []
    assert inventory["unique_raw_records"] == 0
    assert any("same_run_source_host_not_official" in item["reason"] for item in blockers)


def test_same_run_preflight_rejects_future_day_before_decoder_work(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="future_event_day_not_allowed"):
        preflight.rebuild(
            target_local_date="2026-07-25",
            run_initialization_utc="2026-07-24T00:00:00Z",
            station_ids=["ZUUU"],
            market_registry_snapshot=_registry_snapshot(
                tmp_path / "registry.json"
            ),
            source_root=tmp_path / "arrivals",
            as_of_utc="2026-07-24T15:00:00Z",
            grib_get_binary="/usr/bin/true",
            cdo_binary="/usr/bin/true",
        )

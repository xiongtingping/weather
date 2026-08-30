from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.contracts import ContractError  # noqa: E402
from scripts import rebuild_bayes_basket_target_icon_from_retained_raw as rebuild_module  # noqa: E402


def _write_receipt(root: Path, *, raw_path: Path, raw_sha256: str, valid_time: str = "2026-07-24T12:00:00Z") -> Path:
    receipt_dir = root / "receipts"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt = {
        "schema": "bayes_basket_target_native_model_transport_receipt_v1",
        "provider_id": "icon_global_official",
        "station_id": "ZUUU",
        "target_local_date": "2026-07-24",
        "run_at_utc": "2026-07-23T00:00:00Z",
        "valid_time_utc": valid_time,
        "capture_mode": "prospective",
        "raw_receipts": [{
            "raw_request_url": "https://opendata.dwd.de/weather/nwp/icon/grib/20260723/00/t_2m/test.bz2",
            "raw_object_ref": str(raw_path),
            "raw_sha256": raw_sha256,
            "response_completed_at": "2026-07-23T01:00:00Z",
            "response_headers": {"last-modified": "Thu, 23 Jul 2026 01:00:00 GMT"},
        }],
    }
    receipt_path = receipt_dir / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    return receipt_path


def test_collect_messages_requires_retained_body_and_exact_sha(tmp_path: Path) -> None:
    source_root = tmp_path / "icon"
    raw_path = source_root / "raw" / "payload.bz2"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_bytes(b"official-icon-payload")
    digest = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    _write_receipt(source_root, raw_path=raw_path, raw_sha256=digest)

    messages, inventory = rebuild_module._collect_messages(
        source_roots=[source_root], event_dates={"2026-07-24"}
        , target_timezone="America/New_York"
    )

    assert len(messages) == 1
    assert messages[0]["raw_message_sha256"] == digest
    assert inventory["missing_or_mismatched_raw_count"] == 0
    assert inventory["invalid_receipt_count"] == 0

    raw_path.write_bytes(b"tampered-payload")
    messages, inventory = rebuild_module._collect_messages(
        source_roots=[source_root], event_dates={"2026-07-24"}
        , target_timezone="America/New_York"
    )
    assert messages == []
    assert inventory["missing_or_mismatched_raw_count"] == 1
    assert inventory["missing_or_mismatched_raw_sample"][0]["reason"] == "raw_body_missing_or_sha256_mismatch"


def test_collect_messages_reports_missing_body_without_fallback(tmp_path: Path) -> None:
    source_root = tmp_path / "icon"
    missing_path = source_root / "raw" / "missing.bz2"
    digest = hashlib.sha256(b"body-that-is-not-retained").hexdigest()
    _write_receipt(source_root, raw_path=missing_path, raw_sha256=digest)

    messages, inventory = rebuild_module._collect_messages(
        source_roots=[source_root],
        event_dates={"2026-07-24"},
        target_timezone="America/New_York",
    )

    assert messages == []
    assert inventory["missing_or_mismatched_raw_count"] == 1
    blocker = inventory["missing_or_mismatched_raw_sample"][0]
    assert blocker["raw_object_ref"] == str(missing_path.resolve())
    assert blocker["source_url"].startswith("https://opendata.dwd.de/")


def test_collect_messages_recovers_exact_sha_from_another_retained_root(tmp_path: Path) -> None:
    source_root = tmp_path / "icon-primary"
    alias_root = tmp_path / "icon-secondary"
    body = b"official-icon-payload-retained-once"
    raw_alias = alias_root / "raw" / (hashlib.sha256(body).hexdigest() + ".grib2.bz2")
    raw_alias.parent.mkdir(parents=True)
    raw_alias.write_bytes(body)
    missing_ref = source_root / "raw" / raw_alias.name
    digest = hashlib.sha256(body).hexdigest()
    _write_receipt(source_root, raw_path=missing_ref, raw_sha256=digest)

    messages, inventory = rebuild_module._collect_messages(
        source_roots=[source_root, alias_root], event_dates={"2026-07-24"}
        , target_timezone="America/New_York"
    )

    assert len(messages) == 1
    assert messages[0]["raw_object_ref"] == str(raw_alias.resolve())
    assert inventory["missing_or_mismatched_raw_count"] == 0
    assert inventory["raw_alias_recovered_count"] == 1


def test_collect_messages_reads_canonical_run_initialization_field(tmp_path: Path) -> None:
    source_root = tmp_path / "icon"
    raw_path = source_root / "raw" / "payload.bz2"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_bytes(b"official-icon-payload")
    digest = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    receipt_path = _write_receipt(source_root, raw_path=raw_path, raw_sha256=digest)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["run_initialization_utc"] = receipt.pop("run_at_utc")
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    messages, inventory = rebuild_module._collect_messages(
        source_roots=[source_root], event_dates={"2026-07-24"}
        , target_timezone="America/New_York"
    )

    assert len(messages) == 1
    assert messages[0]["run_initialization_utc"] == "2026-07-23T00:00:00Z"
    assert inventory["invalid_receipt_count"] == 0


def test_rebuild_preserves_45_station_lineage_and_has_no_database_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "icon"
    raw_path = source_root / "raw" / "payload.bz2"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_bytes(b"official-icon-payload")
    digest = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    _write_receipt(source_root, raw_path=raw_path, raw_sha256=digest)

    stations = [f"S{index:02d}" for index in range(45)]
    seen: list[str] = []

    def fake_decode(
        message,
        *,
        stations,
        grib_get,
        grib_get_data,
        cdo,
        target_timezone,
    ):
        assert target_timezone == "America/New_York"
        seen.extend(stations)
        return [
            {
                "station_id": station,
                "event_date": message["event_date"],
                "target_timezone": "America/New_York",
                "provider_id": "icon_global_official",
                "model_id": "ICON",
                "model_run_initialization_utc": message["run_initialization_utc"],
                "valid_time_utc": message["valid_time_utc"],
                "temperature_2m_c": 20.0,
                "raw_message_sha256": message["raw_message_sha256"],
                "receipt_sha256": message["receipt_sha256"],
            }
            for station in stations
        ], []

    monkeypatch.setattr(rebuild_module, "_decode_message", fake_decode)
    monkeypatch.setattr(
        rebuild_module,
        "materialize_native_daily_curves",
        lambda points, target_timezone: {
            "schema": "bayes_basket_target_native_model_daily_curve_v1",
            "curve_count": len(points),
            "complete_curve_count": 0,
            "eligible_model_rows": [],
            "curves": [],
            "all_complete": False,
        },
    )

    output = tmp_path / "evidence.json"
    fake_binary = shutil.which("true")
    assert fake_binary
    result = rebuild_module.rebuild(
        event_dates=["2026-07-24"],
        station_ids=stations,
        source_roots=[source_root],
            output=output,
            target_timezone="America/New_York",
        as_of_utc="2026-07-25T04:00:00Z",
        grib_get=fake_binary,
        grib_get_data=fake_binary,
        cdo=fake_binary,
    )

    assert seen == stations
    assert result["model_point_count"] == 45
    assert result["checks"]["production_calibration_eligible"] is False
    assert result["checks"]["writes_shared_live_database"] is False
    assert result["checks"]["writes_target_database"] is False
    assert result["checks"]["calls_order_gateway"] is False
    assert result["boundary"]["protected_production_line_touched"] is False
    assert output.is_file()


def test_rebuild_rejects_future_et_day_before_decoder_work(tmp_path: Path) -> None:
    fake_binary = shutil.which("true")
    assert fake_binary
    with pytest.raises(ContractError, match="future_event_day_not_allowed"):
        rebuild_module.rebuild(
            event_dates=["2026-07-25"],
            station_ids=["ZUUU"],
            source_roots=[tmp_path / "icon"],
                output=tmp_path / "future.json",
                target_timezone="America/New_York",
            as_of_utc="2026-07-24T15:00:00Z",
            grib_get=fake_binary,
            grib_get_data=fake_binary,
            cdo=fake_binary,
        )


def test_rebuild_rejects_more_than_the_fleet_before_work(tmp_path: Path) -> None:
    fake_binary = shutil.which("true")
    assert fake_binary
    with pytest.raises(ContractError, match="icon_retained_raw_station_ids_invalid"):
        rebuild_module.rebuild(
            event_dates=["2026-07-24"],
            station_ids=[
                f"S{index:02d}"
                for index in range(expected_station_count() + 1)
            ],
            source_roots=[tmp_path / "icon"],
                output=tmp_path / "evidence.json",
                target_timezone="America/New_York",
            grib_get=fake_binary,
            grib_get_data=fake_binary,
            cdo=fake_binary,
        )

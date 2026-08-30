from __future__ import annotations

import bz2
import hashlib
import json
from pathlib import Path

import pytest

from bayes_basket_target.icon_eps_daily_curve import COHORT_STATE
from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.station_registry import coordinate_registry_manifest
import scripts.decode_bayes_basket_target_icon_eps_station_members as icon_eps_members
from scripts.decode_bayes_basket_target_icon_eps_station_members import (
    EXPECTED_MEMBERS,
    decode_station_members,
)


RUN = "2026-08-01T00:00:00.000Z"
VALID = "2026-08-01T00:00:00.000Z"


class FakeOfficialRemapAdapter:
    decoder_id = "test_dwd_official_remap_v1"
    remap_contract = "DWD_ICON_GLOBAL2WORLD_025_EASY"

    def __init__(self, *, members: tuple[int, ...] = EXPECTED_MEMBERS, bad_scope: bool = False) -> None:
        self.members = members
        self.bad_scope = bad_scope

    def iter_member_station_values(self, raw_path: Path, *, stations):
        assert raw_path.suffix == ".bz2"
        for member in self.members:
            values = {
                station: 10.0 + member / 100.0 + index / 1000.0
                for index, station in enumerate(sorted(stations))
            }
            if self.bad_scope:
                values.pop(sorted(stations)[0])
            yield {
                "member_id": member,
                "number_of_forecasts_in_ensemble": 40,
                "product_definition_template": "4.1",
                "run_initialization_utc": RUN,
                "valid_time_utc": VALID,
                "station_values_c": values,
            }


def _capture(tmp_path: Path, *, raw_bytes: bytes = b"small official raw") -> Path:
    raw = tmp_path / "icon-eps.grib2.bz2"
    raw.write_bytes(bz2.compress(raw_bytes))
    raw_sha = hashlib.sha256(raw.read_bytes()).hexdigest()
    identity = {
        "schema": "bayes_basket_target_icon_global_eps_forward_capture_v1",
        "provider_id": "icon_global_eps_official",
        "product_id": "DWD_ICON_GLOBAL_EPS",
        "independence_family_id": "DWD_ICON_EPS",
        "state": "OFFICIAL_RAW_CAPTURED_MEMBER_DECODE_PENDING",
        "run_initialization_utc": RUN,
        "forecast_hour": 0,
        "valid_time_utc": VALID,
        "source_url": "https://opendata.dwd.de/weather/nwp/icon-eps/grib/00/t_2m/icon-eps_global_icosahedral_single-level_2026080100_000_t_2m.grib2.bz2",
        "provider_published_at_utc": "2026-08-01T02:37:21.000Z",
        "response_completed_at_utc": "2026-08-01T06:52:10.525Z",
        "response_bytes": raw.stat().st_size,
        "raw_sha256": raw_sha,
        "raw_object_ref": str(raw.resolve()),
        "plan_sha256": "a" * 64,
        "response_headers": {"content-length": str(raw.stat().st_size), "last-modified": "Fri, 01 Aug 2026 02:37:21 GMT"},
        "member_decode_state": "PENDING_FAIL_CLOSED",
        "native_arrival_index_eligible": False,
        "formal_p0_eligible": False,
        "authority": {
            "source_role": "official_native",
            "transport_provider_id": "DWD",
            "member_decode_required": True,
        },
        "boundary": {"single_lead_only": True, "writes_target_database": False},
    }
    receipt = {**identity, "content_sha256": content_sha256(identity)}
    path = tmp_path / "capture.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    return path


def test_decodes_one_40_member_45_station_candidate_cohort(tmp_path: Path) -> None:
    receipt = decode_station_members(
        _capture(tmp_path), adapter=FakeOfficialRemapAdapter()
    )

    assert receipt["state"] == COHORT_STATE
    assert receipt["member_count"] == 40
    assert receipt["station_count"] == expected_station_count()
    assert receipt["member_record_count"] == expected_station_count() * 40
    assert receipt["raw_object_count"] == 1
    assert receipt["single_raw_bytes"] == receipt["cumulative_raw_bytes"]
    assert receipt["resource_guard"]["raw_member_fanout_forbidden"] is True
    assert receipt["formal_p0_eligible"] is False
    assert receipt["native_arrival_index_eligible"] is False
    assert receipt["hard_no_eligible"] is False
    assert receipt["station_coordinate_lineage"]["station_count"] == expected_station_count()
    assert set(row["station_id"] for row in receipt["member_station_records"]) == set(
        coordinate_registry_manifest()["station_ids"]
    )
    assert {row["member_id"] for row in receipt["member_station_records"]} == set(EXPECTED_MEMBERS)
    assert receipt["content_sha256"] == content_sha256(
        {key: value for key, value in receipt.items() if key != "content_sha256"}
    )


def test_member_or_station_scope_defect_fails_closed(tmp_path: Path) -> None:
    capture = _capture(tmp_path)
    with pytest.raises(ContractError, match="member_set_not_1_through_40"):
        decode_station_members(
            capture, adapter=FakeOfficialRemapAdapter(members=tuple(range(1, 40)))
        )
    with pytest.raises(ContractError, match="adapter_station_scope_invalid"):
        decode_station_members(capture, adapter=FakeOfficialRemapAdapter(bad_scope=True))


def test_capture_hash_and_single_raw_guard_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = _capture(tmp_path)
    raw = tmp_path / "icon-eps.grib2.bz2"
    raw_bytes = raw.read_bytes()
    raw.write_bytes(raw_bytes[:-1] + b"X")
    with pytest.raises(ContractError, match="capture_raw_sha_mismatch"):
        decode_station_members(capture, adapter=FakeOfficialRemapAdapter())

    guarded = tmp_path / "guarded"
    guarded.mkdir()
    oversize_capture = _capture(guarded)
    # Keep the regression tiny while exercising the same hard gate that is
    # configured as 64 MiB in production.
    monkeypatch.setattr(icon_eps_members, "MAX_SINGLE_RAW_BYTES", 1)
    with pytest.raises(ContractError, match="single_raw_bound_exceeded"):
        decode_station_members(oversize_capture, adapter=FakeOfficialRemapAdapter())


def test_adapter_dependency_failure_is_fail_closed(tmp_path: Path) -> None:
    class UnavailableAdapter:
        decoder_id = "unavailable"
        remap_contract = "DWD_ICON_GLOBAL2WORLD_025_EASY"

        def iter_member_station_values(self, raw_path: Path, *, stations):
            raise ContractError("icon_eps_station_eccodes_dependencies_missing")
            yield  # pragma: no cover

    with pytest.raises(ContractError, match="eccodes_dependencies_missing"):
        decode_station_members(_capture(tmp_path), adapter=UnavailableAdapter())


def test_default_production_adapter_requires_verified_grid_receipt(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="grid_receipt_required"):
        decode_station_members(_capture(tmp_path))


def test_grid_uuid_and_cell_count_must_match_native_raw() -> None:
    icon_eps_members._require_matching_grid_signature(
        grid_uuid="ae487d14fe2e11e4af85e50a2a56a360",
        grid_cell_count=737_280,
        raw_grid_uuid="ae487d14fe2e11e4af85e50a2a56a360",
        raw_cell_count=737_280,
    )
    with pytest.raises(ContractError, match="grid_signature_mismatch"):
        icon_eps_members._require_matching_grid_signature(
            grid_uuid="9b0b03ca18c411e49318776f158edd08",
            grid_cell_count=327_680,
            raw_grid_uuid="ae487d14fe2e11e4af85e50a2a56a360",
            raw_cell_count=737_280,
        )


def test_expected_official_grid_is_bounded_matching_r03b06() -> None:
    assert icon_eps_members.OFFICIAL_GRID_URL.endswith("icon_grid_0036_R03B06_G.nc.bz2")
    assert icon_eps_members.EXPECTED_OFFICIAL_GRID_CELL_COUNT == 737_280
    assert icon_eps_members.MAX_OFFICIAL_GRID_BYTES >= icon_eps_members.EXPECTED_OFFICIAL_GRID_BYTES
    assert icon_eps_members.MAX_CUMULATIVE_RAW_BYTES > icon_eps_members.MAX_OFFICIAL_GRID_BYTES


def _grid_receipt(receipt_path: Path, raw_path: Path) -> None:
    identity = {
        "schema": icon_eps_members.GRID_CAPTURE_SCHEMA,
        "state": "OFFICIAL_DWD_R03B06_GRID_CAPTURED",
        "source_url": icon_eps_members.OFFICIAL_GRID_URL,
        "grid_filename": icon_eps_members.OFFICIAL_GRID_FILENAME,
        "grid_uuid": "ae487d14fe2e11e4af85e50a2a56a360",
        "grid_cell_count": icon_eps_members.EXPECTED_OFFICIAL_GRID_CELL_COUNT,
        "raw_object_ref": str(raw_path),
        "raw_sha256": "0" * 64,
        "response_bytes": 1,
    }
    receipt_path.write_text(
        json.dumps({**identity, "content_sha256": content_sha256(identity)}),
        encoding="utf-8",
    )


def test_cold_archived_grid_body_is_re_downloaded_instead_of_failing_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The static grid is a cache, so retention may discard its body."""

    receipt_path = tmp_path / "grid_receipt.json"
    _grid_receipt(receipt_path, tmp_path / "grid_root" / "raw" / f"{'0' * 64}.nc.bz2")
    downloaded: list[Path] = []

    def _fake_download(*, destination: Path, timeout_sec: float) -> tuple[dict, int]:
        downloaded.append(destination)
        raise ContractError("stopped_after_download_was_reached")

    monkeypatch.setattr(icon_eps_members, "_curl_grid_download", _fake_download)

    with pytest.raises(ContractError, match="stopped_after_download_was_reached"):
        icon_eps_members.capture_official_grid(
            output_root=tmp_path / "grid_root", receipt_path=receipt_path
        )

    assert len(downloaded) == 1


def test_present_grid_body_that_fails_a_receipt_check_still_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_path = tmp_path / "grid_root" / "raw" / f"{'0' * 64}.nc.bz2"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_bytes(b"present-but-wrong")
    _grid_receipt(tmp_path / "grid_receipt.json", raw_path)

    def _never(*args: object, **kwargs: object) -> None:
        raise AssertionError("a present body must not trigger a download")

    monkeypatch.setattr(icon_eps_members, "_curl_grid_download", _never)

    with pytest.raises(ContractError, match="grid_size_mismatch"):
        icon_eps_members.capture_official_grid(
            output_root=tmp_path / "grid_root",
            receipt_path=tmp_path / "grid_receipt.json",
        )

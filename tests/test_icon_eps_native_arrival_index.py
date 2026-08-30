from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.icon_eps_daily_curve import COHORT_STATE
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.icon_eps_native_arrival_index import (
    bind_icon_eps_daily_archive_arrival_index,
    build_icon_eps_native_arrival_index,
    build_icon_eps_native_arrival_index_repair_receipt,
)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _inputs(tmp_path: Path) -> tuple[dict[str, object], dict[str, object], Path]:
    raw_root = tmp_path / "raw"
    raw_root.mkdir()
    raw_bytes = b"official-dwd-icon-eps-raw"
    raw_sha = _sha(raw_bytes)
    raw_path = raw_root / (raw_sha + ".grib2.bz2")
    raw_path.write_bytes(raw_bytes)
    capture_identity = {
        "schema": "bayes_basket_target_icon_global_eps_forward_capture_v1",
        "state": "OFFICIAL_RAW_CAPTURED_MEMBER_DECODE_PENDING",
        "provider_id": "icon_global_eps_official",
        "product_id": "DWD_ICON_GLOBAL_EPS",
        "independence_family_id": "DWD_ICON_EPS",
        "expected_member_count": 40,
        "raw_sha256": raw_sha,
        "raw_object_ref": str(raw_path.resolve()),
        "run_initialization_utc": "2026-08-01T00:00:00Z",
        "valid_time_utc": "2026-08-01T12:00:00Z",
        "provider_published_at_utc": "2026-08-01T02:43:18Z",
        "response_completed_at_utc": "2026-08-01T03:00:00Z",
        "source_url": "https://opendata.dwd.de/weather/nwp/icon-eps/grib/00/t_2m/example.grib2.bz2",
        "formal_p0_eligible": False,
        "native_arrival_index_eligible": False,
    }
    capture = {**capture_identity, "content_sha256": content_sha256(capture_identity)}
    cohort_identity = {
        "schema": "bayes_basket_target_icon_global_eps_station_member_cohort_v1",
        "state": COHORT_STATE,
        "provider_id": "icon_global_eps_official",
        "product_id": "DWD_ICON_GLOBAL_EPS",
        "independence_family_id": "DWD_ICON_EPS",
        "capture_receipt_sha256": capture["content_sha256"],
        "raw_sha256": raw_sha,
        "raw_object_ref": str(raw_path.resolve()),
        "run_initialization_utc": "2026-08-01T00:00:00Z",
        "valid_time_utc": "2026-08-01T12:00:00Z",
        "provider_published_at_utc": "2026-08-01T02:43:18Z",
        "response_completed_at_utc": "2026-08-01T03:00:00Z",
        "member_count": 40,
        "station_count": expected_station_count(),
        "member_record_count": expected_station_count() * 40,
        "lineage": {
            "source_url": capture_identity["source_url"],
        },
        "formal_p0_eligible": False,
        "native_arrival_index_eligible": False,
        "hard_no_eligible": False,
    }
    cohort = {**cohort_identity, "content_sha256": content_sha256(cohort_identity)}
    return cohort, capture, raw_root


def _daily_archive(cohort: dict[str, object]) -> dict[str, object]:
    identity = {
        "schema": "dwd_icon_global_eps_official_daily_members_v1",
        "state": "OFFICIAL_MEMBER_DAILY_CURVES_READY_STRICT_OOS_PENDING",
        "provider_id": "icon_global_eps_official",
        "product_id": "DWD_ICON_GLOBAL_EPS",
        "independence_family_id": "DWD_ICON_EPS",
        "run_init_utc": "2026-08-01T00:00:00Z",
        "native_arrival_index_eligible": False,
        "source_authority": {
            "source_role": "official_native",
            "transport_provider_id": "DWD",
            "native_publication_and_arrival_observed": True,
            "native_arrival_index_eligible": False,
            "arrival_lineage_status": "PROVIDER_PUBLICATION_AND_LOCAL_RECEIPT_OBSERVED_INDEX_PENDING",
        },
        "lead_results": [{
            "cohort_content_sha256": cohort["content_sha256"],
            "raw_sha256": cohort["raw_sha256"],
            "valid_time_utc": cohort["valid_time_utc"],
        }],
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def test_observed_dwd_capture_can_form_immutable_index_without_p0(tmp_path: Path) -> None:
    cohort, capture, raw_root = _inputs(tmp_path)

    index = build_icon_eps_native_arrival_index(
        [cohort], [capture],
        indexed_at_utc="2026-08-01T04:00:00Z",
        raw_root=raw_root,
    )
    bound = bind_icon_eps_daily_archive_arrival_index(_daily_archive(cohort), index)

    assert index["state"] == "IMMUTABLE_OFFICIAL_NATIVE_ARRIVAL_INDEX_READY"
    assert index["entry_count"] == 1
    assert index["native_arrival_index_eligible"] is True
    assert index["formal_p0_eligible"] is False
    assert bound["native_arrival_index_eligible"] is True
    assert bound["source_authority"]["native_arrival_index_evidence_sha256"] == index["content_sha256"]
    assert bound["source_authority"]["arrival_lineage_status"] == "IMMUTABLE_OFFICIAL_NATIVE_ARRIVAL_INDEX_VERIFIED"


def test_index_rejects_mismatched_retained_raw(tmp_path: Path) -> None:
    cohort, capture, raw_root = _inputs(tmp_path)
    raw_path = Path(str(cohort["raw_object_ref"]))
    raw_path.write_bytes(b"tampered")

    with pytest.raises(ContractError, match="raw_sha_mismatch"):
        build_icon_eps_native_arrival_index(
            [cohort], [capture],
            indexed_at_utc="2026-08-01T04:00:00Z",
            raw_root=raw_root,
        )


def test_missing_original_receipt_stays_partial_instead_of_using_a_later_poll(
    tmp_path: Path,
) -> None:
    cohort, capture, raw_root = _inputs(tmp_path)
    cohort["capture_receipt_sha256"] = "a" * 64
    identity = {key: value for key, value in cohort.items() if key != "content_sha256"}
    cohort["content_sha256"] = content_sha256(identity)

    receipt = build_icon_eps_native_arrival_index_repair_receipt(
        [cohort], [capture],
        inspected_at_utc="2026-08-01T04:00:00Z",
        raw_root=raw_root,
    )

    assert receipt["state"] == "PARTIAL_NATIVE_ARRIVAL_INDEX_FAIL_CLOSED"
    assert receipt["verified_object_count"] == 0
    assert receipt["unresolved_receipt_count"] == 1
    assert receipt["unresolved_receipts"][0]["reason"] == "original_capture_receipt_content_missing"
    assert receipt["native_arrival_index_eligible"] is False

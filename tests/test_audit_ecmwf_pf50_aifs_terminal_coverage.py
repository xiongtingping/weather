from __future__ import annotations

from copy import deepcopy

import pytest

from bayes_basket_target.model_system_coverage import (
    canonical_station_ids,
    validate_coverage_row,
)
from scripts.audit_ecmwf_pf50_aifs_terminal_coverage import (
    AIFS_REPUBLISHER_PRODUCT,
    AIFS_SINGLE_PRODUCT,
    PF50_MEMBER_CONTRACT,
    PF50_PRODUCT,
    TerminalCoverageError,
    build_terminal_matrix,
)
from bayes_basket_target.fleet import expected_station_count


AS_OF = "2026-08-04T00:00:00Z"


def _stations() -> list[str]:
    return list(canonical_station_ids())


def _pf50_archive() -> dict[str, object]:
    stations = _stations()
    members = [
        {
            "member_id": f"perturbed_{number:02d}",
            "member_role": "PERTURBED",
        }
        for number in range(1, 51)
    ]
    receipts = [{"message_sha256": "a" * 64, "index_sha256": "b" * 64}]
    captures = [
        {
            "station_id": station,
            "member_count": 50,
            "member_availability_contract": PF50_MEMBER_CONTRACT,
            "members": members,
            "field_receipts": receipts,
            "field_receipt_set_sha256": "c" * 64,
            "native_publication_at_known": False,
            "native_run_init_known": True,
        }
        for station in stations
    ]
    return {
        "schema": "ecmwf_ifs_open_pf50_historical_multistation_members_v1",
        "ensemble_system_id": PF50_PRODUCT,
        "archive_lead_bucket": "d2",
        "station_count": expected_station_count(),
        "member_count": 50,
        "member_availability_contract": PF50_MEMBER_CONTRACT,
        "captures": captures,
        "content_sha256": "d" * 64,
    }


def _pf50_oos_linkage() -> dict[str, object]:
    return {
        "content_sha256": "e" * 64,
        "current_pointer": {
            "state": "BOUND",
            "selected_system_ids_filter": [PF50_PRODUCT, "NOAA_GEFS_05"],
            "station_count": expected_station_count(),
            "station_ids": _stations(),
        },
    }


def _aifs_readiness() -> dict[str, object]:
    stations = _stations()
    return {
        "content_sha256": "f" * 64,
        "state": "BLOCKED_STRICT_OOS",
        "official_native_forward_capture": {
            "capture_state": "OFFICIAL_NATIVE_FORWARD_COMPLETE",
            "station_count": expected_station_count(),
            "station_valid_arrival_count": 179,
            "unique_raw_message_count": 7,
            "raw_message_sha256s": [f"{number:064x}" for number in range(1, 8)],
            "run_initialization_utc": "2026-08-03T18:00:00Z",
        },
        "decode_evidence": {
            "complete_curve_count": expected_station_count(),
            "decoded_station_ids": stations,
            "coverage_scope": "FULL_45_STATION_FORWARD_NOT_CLOSED_DAY_STRICT_OOS",
        },
        "closed_day_strict_oos": {"deterministic_artifacts": {"state": "MISSING"}},
        "provider_binding": {
            "constructible": False,
            "formal_provider_eligible": False,
            "formal_p0_role_eligible": False,
        },
    }


def _aifs_oos() -> dict[str, object]:
    return {
        "content_sha256": "1" * 64,
        "state": "PENDING_STRICT_OOS_SAMPLES",
        "strict_oos_report": None,
        "scope_shortfalls": [{
            "station_id": _stations()[0],
            "lead_bucket": "d0",
            "run_cycle": "18Z",
            "available_case_count": 1,
        }],
    }


def _republisher_oos() -> dict[str, object]:
    return {
        "content_sha256": "2" * 64,
        "source_role": "aggregator_republisher",
        "candidates": [{
            "station_id": _stations()[0],
            "lead_bucket": "d0",
            "state": "PRODUCTION_NO_LIVE_OOS_PASS",
            "oos_event_temporal_scope": "CLOSED_HISTORICAL_AWC_TRUTH",
            "train_n": 20,
            "holdout_n": 8,
        }],
    }


def _row(matrix: dict[str, object], product_id: str, station: str, lead: str) -> dict[str, object]:
    return next(
        row for row in matrix["rows"]  # type: ignore[index]
        if row["product_id"] == product_id  # type: ignore[index]
        and row["station_id"] == station  # type: ignore[index]
        and row["lead_bucket"] == lead  # type: ignore[index]
    )


def test_matrix_is_shared_exact_45_by_three_by_three_and_all_rows_validate() -> None:
    matrix = build_terminal_matrix(
        pf50_archives=[_pf50_archive()],
        pf50_oos_linkage=_pf50_oos_linkage(),
        aifs_readiness=_aifs_readiness(),
        aifs_oos=_aifs_oos(),
        aifs_republisher_oos=_republisher_oos(),
        as_of_utc=AS_OF,
        station_ids=_stations(),
    )
    station = _stations()[0]

    assert matrix["schema"] == "bayes_basket_target_model_system_coverage_matrix_v1"
    assert matrix["station_count"] == expected_station_count()
    assert matrix["lead_buckets"] == ["d0", "d1", "d2"]
    assert matrix["scope_count"] == expected_station_count() * 3 * 3
    assert matrix["cross_selection_count"] == 0
    assert matrix["cross_activation_count"] == 0
    assert matrix["p0_count"] == 0
    assert all(validate_coverage_row(row) == row for row in matrix["rows"])  # type: ignore[index]

    pf50_d2 = _row(matrix, PF50_PRODUCT, station, "d2")
    assert pf50_d2["state"] == "DATA_READY"
    assert pf50_d2["blockers"] == ["LINEAGE_MISMATCH"]
    assert pf50_d2["native_arrival_indexed"] is False
    assert _row(matrix, PF50_PRODUCT, station, "d1")["blockers"] == ["DATA_MISSING"]

    aifs_d0 = _row(matrix, AIFS_SINGLE_PRODUCT, station, "d0")
    assert aifs_d0["state"] == "DATA_READY"
    assert aifs_d0["blockers"] == ["OOS_SAMPLE_SHORT"]
    assert aifs_d0["evidence_role"] == "historical_strict_oos"
    aifs_d1 = _row(matrix, AIFS_SINGLE_PRODUCT, _stations()[1], "d1")
    assert aifs_d1["state"] == "CANDIDATE_ONLY"
    assert aifs_d1["blockers"] == ["DATA_MISSING"]
    assert aifs_d1["evidence_role"] == "observational_candidate_not_oos"

    republisher_d0 = _row(matrix, AIFS_REPUBLISHER_PRODUCT, station, "d0")
    assert republisher_d0["state"] == "CANDIDATE_ONLY"
    assert republisher_d0["blockers"] == ["DATA_MISSING"]
    assert republisher_d0["native_arrival_indexed"] is False
    assert republisher_d0["formal_live_authority"] is False
    assert republisher_d0["train_count"] == 20
    assert republisher_d0["holdout_count"] == 8


def test_matrix_rejects_noncanonical_station_scope() -> None:
    with pytest.raises(TerminalCoverageError, match="exact_canonical_45"):
        build_terminal_matrix(
            pf50_archives=[],
            pf50_oos_linkage={},
            aifs_readiness={},
            aifs_oos={},
            aifs_republisher_oos={},
            as_of_utc=AS_OF,
            station_ids=[_stations()[0]],
        )


def test_aifs_strict_oos_requires_activation_root_before_oos_ready() -> None:
    station = _stations()[0]
    readiness = _aifs_readiness()
    oos = _aifs_oos()
    oos["strict_oos_report"] = {
        "candidates": [{
            "station_id": station,
            "lead_bucket": "d1",
            "run_cycle": "12Z",
            "oos_state": "STRICT_OOS_PASS",
        }],
    }

    blocked = build_terminal_matrix(
        pf50_archives=[_pf50_archive()],
        pf50_oos_linkage=_pf50_oos_linkage(),
        aifs_readiness=readiness,
        aifs_oos=oos,
        aifs_republisher_oos=_republisher_oos(),
        as_of_utc=AS_OF,
    )
    blocked_row = _row(blocked, AIFS_SINGLE_PRODUCT, station, "d1")
    assert blocked_row["state"] == "BLOCKED"
    assert blocked_row["blockers"] == ["ACTIVATION_ROOT_MISSING"]
    assert blocked_row["run_cycle"] == "12Z"

    promoted_readiness = deepcopy(readiness)
    promoted_readiness["closed_day_strict_oos"] = {
        "deterministic_artifacts": {"state": "FORMAL_DETERMINISTIC_PROVIDER_READY"},
    }
    promoted_readiness["provider_binding"] = {
        "constructible": True,
        "formal_provider_eligible": True,
        "formal_p0_role_eligible": False,
    }
    promoted = build_terminal_matrix(
        pf50_archives=[_pf50_archive()],
        pf50_oos_linkage=_pf50_oos_linkage(),
        aifs_readiness=promoted_readiness,
        aifs_oos=oos,
        aifs_republisher_oos=_republisher_oos(),
        as_of_utc=AS_OF,
    )
    promoted_row = _row(promoted, AIFS_SINGLE_PRODUCT, station, "d1")
    assert promoted_row["state"] == "OOS_READY"
    assert promoted_row["blockers"] == []
    assert promoted_row["train_count"] == 20
    assert promoted_row["holdout_count"] == 8
    assert promoted_row["formal_live_authority"] is False
    assert promoted_row["cross_selection_count"] == 0
    assert promoted_row["cross_activation_count"] == 0
    assert promoted_row["p0_count"] == 0


def test_pf50_provenance_completion_still_needs_single_system_oos_samples() -> None:
    archive = _pf50_archive()
    for capture in archive["captures"]:  # type: ignore[index]
        capture["native_publication_at_known"] = True
        capture["native_run_init_known"] = True

    matrix = build_terminal_matrix(
        pf50_archives=[archive],
        pf50_oos_linkage=_pf50_oos_linkage(),
        aifs_readiness=_aifs_readiness(),
        aifs_oos=_aifs_oos(),
        aifs_republisher_oos=_republisher_oos(),
        as_of_utc=AS_OF,
    )
    row = _row(matrix, PF50_PRODUCT, _stations()[0], "d2")
    assert row["state"] == "DATA_READY"
    assert row["blockers"] == ["OOS_SAMPLE_SHORT"]
    assert row["native_arrival_indexed"] is True
    assert row["train_count"] == 0
    assert row["holdout_count"] == 0


def test_aifs_missing_or_duplicate_raw_receipts_blocks_native_forward_scope() -> None:
    station = _stations()[0]
    readiness = _aifs_readiness()
    readiness["official_native_forward_capture"]["raw_message_sha256s"] = ["a" * 64] * 7  # type: ignore[index]

    matrix = build_terminal_matrix(
        pf50_archives=[_pf50_archive()],
        pf50_oos_linkage=_pf50_oos_linkage(),
        aifs_readiness=readiness,
        aifs_oos=_aifs_oos(),
        aifs_republisher_oos=_republisher_oos(),
        as_of_utc=AS_OF,
    )
    row = _row(matrix, AIFS_SINGLE_PRODUCT, station, "d0")
    assert row["state"] == "BLOCKED"
    assert row["blockers"] == ["DATA_MISSING"]
    assert row["native_arrival_indexed"] is False
    assert row["raw_receipt_sha256"] is None

from __future__ import annotations

import json
from pathlib import Path

from bayes_basket_target.icon_eps_daily_curve import (
    PROVIDER_ID as ICON_EPS_OFFICIAL_PROVIDER_ID,
)
from bayes_basket_target.model_arrival import ALLOWED_PROVIDERS
from bayes_basket_target.model_sources import ALLOWED_MODEL_PROVIDERS
import scripts.audit_dwd_icon_global_eps_official_readiness as readiness
from scripts.audit_dwd_icon_global_eps_official_readiness import assess_records, build_audit
from bayes_basket_target.fleet import expected_station_count


def _with_content_sha(identity: dict[str, object]) -> dict[str, object]:
    return {**identity, "content_sha256": readiness._sha(identity)}


def _object(*, product: str = "DWD_ICON_GLOBAL_EPS", **changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "product_id": product,
        "run_initialization_utc": "2026-08-01T00:00:00Z",
        "valid_time_utc": "2026-08-01T12:00:00Z",
        "published_at_utc": "2026-08-01T01:00:00Z",
        "received_at_utc": "2026-08-01T01:01:00Z",
        "raw_sha256": "a" * 64,
        "dwd_official_transport": True,
        "aggregator_republisher": False,
        "decoded_member_ids": list(range(1, 41)),
        "number_of_forecasts_in_ensemble": 40,
        "product_definition_template": "4.1",
    }
    row.update(changes)
    return row


def test_deterministic_global_receipt_never_counts_as_eps() -> None:
    result = assess_records([_object(product="DWD_ICON_GLOBAL")])
    assert result["official_deterministic_global"]["evidence_record_count"] == 1
    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert result["official_eps_products"]["DWD_ICON_GLOBAL_EPS"]["cohort_count"] == 0


def test_open_meteo_40_member_set_never_counts_as_native() -> None:
    result = assess_records([
        _object(aggregator_republisher=True, dwd_official_transport=False)
    ])
    cohort = result["official_eps_products"]["DWD_ICON_GLOBAL_EPS"]["cohorts"][0]
    assert cohort["ready"] is False
    assert "aggregator_republisher_present" in cohort["blockers"]
    assert result["open_meteo_republisher"]["native_arrival_index_eligible"] is False


def test_39_members_fail_closed() -> None:
    result = assess_records([_object(decoded_member_ids=list(range(1, 40)))])
    cohort = result["official_eps_products"]["DWD_ICON_GLOBAL_EPS"]["cohorts"][0]
    assert cohort["ready"] is False
    assert "decoded_member_set_not_1_through_40" in cohort["blockers"]


def test_duplicate_member_fails_closed() -> None:
    ids = list(range(1, 40)) + [39]
    cohort = assess_records([_object(decoded_member_ids=ids)])["official_eps_products"]["DWD_ICON_GLOBAL_EPS"]["cohorts"][0]
    assert "duplicate_member_id" in cohort["blockers"]
    assert "decoded_member_set_not_1_through_40" in cohort["blockers"]


def test_missing_required_timestamp_fails_closed() -> None:
    for field, blocker in (
        ("run_initialization_utc", "run_initialization_missing"),
        ("published_at_utc", "object_publication_timestamp_missing"),
        ("received_at_utc", "object_received_timestamp_missing"),
    ):
        row = _object()
        row[field] = ""
        cohort = assess_records([row])["official_eps_products"]["DWD_ICON_GLOBAL_EPS"]["cohorts"][0]
        assert blocker in cohort["blockers"]


def test_complete_official_40_member_fixture_is_adapter_ready() -> None:
    result = assess_records([_object()])
    assert result["state"] == "READY_FOR_NATIVE_ADAPTER_EXECUTION"
    product = result["official_eps_products"]["DWD_ICON_GLOBAL_EPS"]
    assert product["ready_cohort_count"] == 1
    assert product["cohorts"][0]["unique_member_count"] == 40


def test_icon_eps_config_uses_the_dedicated_official_provider_lane() -> None:
    root = Path(__file__).resolve().parents[1]
    config = json.loads((
        root / "config/bayes_basket_target/station_regional_model_pool_v1.json"
    ).read_text(encoding="utf-8"))
    official = config["official_native_global_products"][
        "DWD_ICON_GLOBAL_EPS"
    ]
    republisher = config["global_ensemble_aggregator_capture_models"][
        "DWD_ICON_GLOBAL_EPS"
    ]

    assert official["provider_id"] == ICON_EPS_OFFICIAL_PROVIDER_ID
    assert official["formal_p0_eligible"] is False
    assert republisher["source_class"] == "aggregator_republisher"
    assert republisher["formal_p0_eligible"] is False
    # EPS has its own 40-member DWD decode/OOS lane.  It must not alias the
    # deterministic ICON provider in generic model-arrival/model-role maps.
    assert ICON_EPS_OFFICIAL_PROVIDER_ID not in ALLOWED_PROVIDERS
    assert ICON_EPS_OFFICIAL_PROVIDER_ID not in ALLOWED_MODEL_PROVIDERS


def test_capture_and_inventory_receipts_coalesce_to_one_raw_object(tmp_path: Path) -> None:
    shared = {
        "product_id": "DWD_ICON_GLOBAL_EPS",
        "run_initialization_utc": "2026-08-01T00:00:00Z",
        "valid_time_utc": "2026-08-01T00:00:00Z",
        "provider_published_at_utc": "2026-08-01T02:37:21Z",
        "response_completed_at_utc": "2026-08-01T06:52:10Z",
        "raw_sha256": "b" * 64,
    }
    capture = {
        **shared,
        "source_url": (
            "https://opendata.dwd.de/weather/nwp/icon-eps/grib/00/t_2m/"
            "icon-eps_global_icosahedral_single-level_2026080100_000_t_2m.grib2.bz2"
        ),
    }
    inventory = {
        **shared,
        "member_inventory": {
            "member_ids": list(range(1, 41)),
            "members": [
                {
                    "perturbation_number": member_id,
                    "number_of_forecasts_in_ensemble": 40,
                    "product_definition_template": "4.1",
                }
                for member_id in range(1, 41)
            ],
        },
    }
    capture_path = tmp_path / "capture.json"
    inventory_path = tmp_path / "inventory.json"
    capture_path.write_text(json.dumps(capture), encoding="utf-8")
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")

    result = build_audit([capture_path, inventory_path])
    cohort = result["official_eps_products"]["DWD_ICON_GLOBAL_EPS"]["cohorts"][0]
    assert cohort["official_raw_object_count"] == 1
    assert cohort["unique_member_count"] == 40
    assert cohort["ready"] is True
    assert result["scan"]["receipt_file_count"] == 2


def test_bounded_file_scanner_classifies_dwd_and_open_meteo_lineage(tmp_path: Path) -> None:
    official = {
        "ensemble_system_id": "DWD_ICON_GLOBAL_EPS",
        "run_initialization_utc": "2026-08-01T00:00:00Z",
        "valid_time_utc": "2026-08-01T12:00:00Z",
        "raw_request_url": "https://opendata.dwd.de/weather/nwp/icon-eps/grib/00/t_2m/member_01.grib2.bz2",
        "provider_published_at_utc": "2026-08-01T01:00:00Z",
        "response_completed_at": "2026-08-01T01:01:00Z",
        "raw_sha256": "a" * 64,
        "decoded_member_ids": list(range(1, 41)),
        "number_of_forecasts_in_ensemble": 40,
        "product_definition_template": "4.1",
    }
    republisher = dict(official)
    republisher.update({
        "source_class": "aggregator_republisher",
        "transport_provider_id": "OPEN_METEO",
        "raw_request_url": "https://ensemble-api.open-meteo.com/v1/ensemble",
    })
    (tmp_path / "official.json").write_text(json.dumps(official), encoding="utf-8")
    (tmp_path / "republisher.json").write_text(json.dumps(republisher), encoding="utf-8")
    result = build_audit([tmp_path])
    assert result["scan"]["receipt_file_count"] == 2
    assert result["open_meteo_republisher"]["evidence_record_count"] == 1
    cohort = result["official_eps_products"]["DWD_ICON_GLOBAL_EPS"]["cohorts"][0]
    assert cohort["ready"] is False
    assert "non_dwd_transport_present" in cohort["blockers"]
    assert result["boundary"]["network_requests"] == 0


def test_station_member_cohort_requires_exact_grid_bound_45_by_40(tmp_path: Path) -> None:
    raw_sha = "b" * 64
    grid = _with_content_sha({
        "schema": "bayes_basket_target_icon_eps_official_grid_capture_v1",
        "source_url": "https://opendata.dwd.de/weather/lib/cdo/icon_grid_0036_R03B06_G.nc.bz2",
        "raw_sha256": "c" * 64,
        "grid_uuid": "ae487d14fe2e11e4af85e50a2a56a360",
        "grid_cell_count": 737_280,
    })
    records = [
        {"station_id": f"S{station:02d}", "member_id": member, "raw_sha256": raw_sha}
        for station in range(expected_station_count())
        for member in range(1, 41)
    ]
    station = _with_content_sha({
        "schema": "bayes_basket_target_icon_global_eps_station_member_cohort_v1",
        "product_id": "DWD_ICON_GLOBAL_EPS",
        "run_initialization_utc": "2026-08-01T00:00:00Z",
        "valid_time_utc": "2026-08-01T00:00:00Z",
        "raw_sha256": raw_sha,
        "station_count": expected_station_count(),
        "member_count": 40,
        "member_record_count": expected_station_count() * 40,
        "member_station_records": records,
        "native_arrival_index_eligible": False,
        "formal_p0_eligible": False,
        "lineage": {"official_grid": {
            "source_grid_sha256": "c" * 64,
            "source_grid_uuid": "ae487d14fe2e11e4af85e50a2a56a360",
            "source_grid_cell_count": 737_280,
            "source_grid_receipt_sha256": grid["content_sha256"],
        }},
    })
    capture = {
        "product_id": "DWD_ICON_GLOBAL_EPS",
        "run_initialization_utc": "2026-08-01T00:00:00Z",
        "valid_time_utc": "2026-08-01T00:00:00Z",
        "provider_published_at_utc": "2026-08-01T01:00:00Z",
        "response_completed_at_utc": "2026-08-01T01:01:00Z",
        "raw_sha256": raw_sha,
        "source_url": "https://opendata.dwd.de/weather/nwp/icon-eps/grib/00/t_2m/test.grib2.bz2",
    }
    inventory = {
        "product_id": "DWD_ICON_GLOBAL_EPS",
        "run_initialization_utc": "2026-08-01T00:00:00Z",
        "valid_time_utc": "2026-08-01T00:00:00Z",
        "raw_sha256": raw_sha,
        "member_inventory": {
            "member_ids": list(range(1, 41)),
            "members": [
                {"number_of_forecasts_in_ensemble": 40, "product_definition_template": "4.1"}
                for _ in range(40)
            ],
        },
    }
    for name, document in {
        "capture.json": capture,
        "inventory.json": inventory,
        "grid.json": grid,
        "station.json": station,
    }.items():
        (tmp_path / name).write_text(json.dumps(document), encoding="utf-8")

    result = build_audit([tmp_path / name for name in ("capture.json", "inventory.json", "grid.json", "station.json")])
    product = result["official_eps_products"]["DWD_ICON_GLOBAL_EPS"]
    cohort = product["cohorts"][0]
    assert result["state"] == "STATION_MEMBER_COHORT_READY_STRICT_OOS_PENDING"
    assert product["station_member_cohort_ready_count"] == 1
    assert cohort["station_member_cohort_ready"] is True
    assert cohort["station_member_cohort"]["actual_member_record_count"] == expected_station_count() * 40
    assert result["boundary"]["production_activation_allowed"] is False


def test_station_member_cohort_grid_mismatch_remains_fail_closed() -> None:
    station = {
        "content_sha256_valid": True,
        "declared_raw_sha256": "a" * 64,
        "record_raw_sha256s": ["a" * 64],
        "declared_station_count": expected_station_count(),
        "actual_station_count": expected_station_count(),
        "declared_member_count": 40,
        "declared_member_record_count": expected_station_count() * 40,
        "actual_member_record_count": expected_station_count() * 40,
        "record_shape_valid": True,
        "station_member_pairs_unique": True,
        "per_station_member_set_complete": True,
        "native_arrival_index_eligible": False,
        "formal_p0_eligible": False,
        "source_grid_receipt_sha256": "d" * 64,
        "source_grid_sha256": "e" * 64,
        "source_grid_uuid": "ae487d14fe2e11e4af85e50a2a56a360",
        "source_grid_cell_count": 737280,
    }
    blockers = readiness._station_member_cohort_blockers(
        station,
        raw_sha256="a" * 64,
        grid_receipts={"d" * 64: {"content_sha256_valid": True, "raw_sha256": "wrong", "grid_uuid": "wrong", "grid_cell_count": 1, "source_url": "https://opendata.dwd.de/a"}},
    )
    assert blockers == ["station_member_official_grid_binding_invalid"]
